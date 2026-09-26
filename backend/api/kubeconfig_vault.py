"""Encrypted-at-rest kubeconfigs for registered (custom) clusters.

A registered cluster's kubeconfig carries cluster-admin grade credentials, so it
is stored Fernet-encrypted (``secret_encryption``) as ``cluster-<id>.yaml.enc``.
kubectl and helm only take a file path, so every subprocess call borrows a
decrypted copy for exactly as long as it runs:

    with materialized_kubeconfig(access.kubeconfig_path) as plain_path:
        subprocess.run(["kubectl", "--kubeconfig", plain_path, ...])

Design / tradeoff
-----------------
Decrypting is cheap (a few KB of Fernet, microseconds) next to a kubectl call
(tens to hundreds of ms), so the cost that matters is not CPU but *how long
plaintext exists and how many copies there are*.  Materializations are
therefore reference-counted and shared: concurrent calls for the same cluster
(the overview fans out 3-5 kubectl calls in parallel) reuse ONE 0600 temp file,
and the file is deleted the moment the last user releases it.  Nothing is kept
around "warm" between calls — an idle KubeSight holds no plaintext kubeconfig
on disk at all.  A long-lived stream (``kubectl logs -f``) holds its reference
until the stream ends.

The cache key includes the encrypted file's mtime/size, so a kubeconfig that is
rewritten while an old copy is in use gets a fresh materialization; the stale
one is still deleted when its last user finishes.

Temp files live in a per-process 0700 directory, preferring ``/dev/shm``
(tmpfs, never written to a disk) when it exists.  Anything left at interpreter
exit is removed by an ``atexit`` hook; a hard crash can leave the per-process
directory behind, which is why it is 0700 and every file in it 0600.

Paths that are not ours — an auto-discovered ``~/.kube/config`` context, the
in-cluster service account (no path at all), or a legacy plaintext file that
has not been migrated yet — pass through untouched.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

ENCRYPTED_SUFFIX = ".enc"
_STORE_NAME = re.compile(r"^cluster-(\d+)\.yaml(?:\.enc)?$")


class KubeconfigDecryptError(RuntimeError):
    """The stored kubeconfig exists but cannot be decrypted (key changed?)."""


def is_encrypted_kubeconfig(path: Optional[str]) -> bool:
    return bool(path) and str(path).endswith(ENCRYPTED_SUFFIX)


def encrypt_kubeconfig_text(plain: str) -> str:
    from .secret_encryption import encrypt_secret

    return encrypt_secret(plain)


def decrypt_kubeconfig_text(cipher: str) -> str:
    from .secret_encryption import decrypt_secret

    cipher = (cipher or "").strip()
    if not cipher:
        return ""
    plain = decrypt_secret(cipher)
    if not plain:
        raise KubeconfigDecryptError(
            "The stored kubeconfig could not be decrypted. The secret-encryption "
            "key has probably changed since it was saved; re-upload the "
            "cluster's kubeconfig."
        )
    return plain


def read_kubeconfig_path(path: str) -> str:
    """Plaintext content of a stored kubeconfig (encrypted or legacy plain)."""
    raw = Path(path).read_text(encoding="utf-8")
    if is_encrypted_kubeconfig(path):
        return decrypt_kubeconfig_text(raw)
    return raw


def kubeconfig_exists(path: Optional[str]) -> bool:
    return bool(path) and Path(path).is_file()


def kubeconfig_identity(path: Optional[str]) -> str:
    """Stable identity for cache/breaker keys.

    For a stored cluster kubeconfig this is the cluster id (``custom-<id>``), so
    the key survives the plaintext → encrypted rename and never embeds a temp
    path.  Anything else (a discovered context) keeps its path.
    """
    if not path:
        return ""
    match = _STORE_NAME.match(Path(str(path)).name)
    if match:
        return f"custom-{int(match.group(1))}"
    return str(path)


# ---------------------------------------------------------------------------
# Reference-counted plaintext materializations
# ---------------------------------------------------------------------------

_lock = threading.Lock()
# (encrypted path, mtime_ns, size) -> [temp path, refcount]
_live: Dict[Tuple[str, int, int], List] = {}
_runtime_dir: Optional[str] = None


def _get_runtime_dir() -> str:
    global _runtime_dir
    if _runtime_dir and os.path.isdir(_runtime_dir):
        return _runtime_dir
    base = None
    configured = os.getenv("KUBESIGHT_KUBECONFIG_RUNTIME_DIR", "").strip()
    if configured:
        base = configured
        os.makedirs(base, exist_ok=True)
    elif os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK):
        base = "/dev/shm"
    _runtime_dir = tempfile.mkdtemp(prefix=f"kubesight-kc-{os.getpid()}-", dir=base)
    try:
        os.chmod(_runtime_dir, 0o700)
    except OSError:
        pass
    return _runtime_dir


def _write_private(content: str) -> str:
    fd, temp_path = tempfile.mkstemp(prefix="kc-", suffix=".yaml", dir=_get_runtime_dir())
    try:
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    except Exception:
        _unlink_quietly(temp_path)
        raise
    return temp_path


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover — Windows lock races etc.
        logger.warning("Could not delete decrypted kubeconfig %s: %s", path, exc)


def acquire_kubeconfig(path: Optional[str]) -> Tuple[Optional[str], Optional[Tuple[str, int, int]]]:
    """Return (usable_path, lease). Pair every call with ``release_kubeconfig``.

    ``lease`` is None when nothing was materialized (pass-through path)."""
    if not is_encrypted_kubeconfig(path):
        return path, None
    source = str(path)
    try:
        stat = os.stat(source)
    except FileNotFoundError:
        # kubectl reports the missing file itself, with the stored path.
        return source, None
    key = (source, stat.st_mtime_ns, stat.st_size)
    with _lock:
        entry = _live.get(key)
        if entry is not None and os.path.isfile(entry[0]):
            entry[1] += 1
            return entry[0], key
        plain = read_kubeconfig_path(source)
        temp_path = _write_private(plain)
        _live[key] = [temp_path, 1]
        return temp_path, key


def release_kubeconfig(lease: Optional[Tuple[str, int, int]]) -> None:
    if lease is None:
        return
    with _lock:
        entry = _live.get(lease)
        if entry is None:
            return
        entry[1] -= 1
        if entry[1] > 0:
            return
        del _live[lease]
        _unlink_quietly(entry[0])


@contextmanager
def materialized_kubeconfig(path: Optional[str]) -> Iterator[Optional[str]]:
    """Yield a path kubectl/helm can read; delete any decrypted copy after."""
    usable, lease = acquire_kubeconfig(path)
    try:
        yield usable
    finally:
        release_kubeconfig(lease)


def live_materializations() -> int:
    """Number of decrypted kubeconfig files currently on disk (for tests/ops)."""
    with _lock:
        return len(_live)


def _cleanup_at_exit() -> None:
    with _lock:
        for temp_path, _count in list(_live.values()):
            _unlink_quietly(temp_path)
        _live.clear()
    if _runtime_dir:
        shutil.rmtree(_runtime_dir, ignore_errors=True)


atexit.register(_cleanup_at_exit)


# ---------------------------------------------------------------------------
# One-time migration of legacy plaintext files
# ---------------------------------------------------------------------------

def encrypt_plaintext_kubeconfigs(storage_dir: Path) -> List[Tuple[int, str, str]]:
    """Encrypt every legacy ``cluster-<id>.yaml`` in ``storage_dir``.

    Writes ``cluster-<id>.yaml.enc`` (0600) first, then removes the plaintext.
    Returns ``[(cluster_db_id, old_path, new_path)]`` so the caller can repoint
    the Cluster rows.  Idempotent: a directory with no plaintext files is a
    no-op; an existing ``.enc`` alongside a plaintext file is overwritten by the
    plaintext (the plaintext is what kubectl was actually using).
    """
    moved: List[Tuple[int, str, str]] = []
    if not storage_dir.is_dir():
        return moved
    for plain_path in sorted(storage_dir.glob("cluster-*.yaml")):
        match = _STORE_NAME.match(plain_path.name)
        if not match or not plain_path.is_file():
            continue
        try:
            content = plain_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue  # another worker process converted it first
        target = plain_path.with_name(plain_path.name + ENCRYPTED_SUFFIX)
        write_encrypted_file(target, content)
        try:
            plain_path.unlink()
        except FileNotFoundError:
            pass
        moved.append((int(match.group(1)), str(plain_path.resolve()), str(target.resolve())))
    return moved


def write_encrypted_file(target: Path, plain: str) -> None:
    """Atomically write ``plain`` encrypted to ``target`` with mode 0600."""
    cipher = encrypt_kubeconfig_text(plain)
    fd, temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(cipher)
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, target)
    except Exception:
        _unlink_quietly(temp)
        raise
