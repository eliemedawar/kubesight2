"""Signature detection for mobile binaries — is this artifact still signed?

SafeCore (and any post-build shielding step) strips code signatures: the binary
that comes back is byte-different from the one the build signed, so its
signature is gone. Publishing that always fails at the store, or ships something
no device will install. KubeSight checks the state at ingest so an unsigned
build can never reach a publish, and re-checks it on whatever the re-signing job
hands back so a job that quietly produced a dud cannot go unnoticed.

Detection needs no keys and no tooling — it only asks whether the signing
artifacts are *present* in the archive, which is a pure read of the zip
container. Nothing here validates a signature cryptographically; that is the
signing job's ``jarsigner -verify`` / ``codesign --verify`` step, which runs
where the platform tooling lives.
"""

from __future__ import annotations

import logging
import re
import struct
import zipfile
from typing import List, Optional

logger = logging.getLogger(__name__)

SIGNED = "signed"
UNSIGNED = "unsigned"
UNKNOWN = "unknown"

# JAR/v1 signature: META-INF/<NAME>.{RSA,DSA,EC} holding the PKCS#7 block.
_JAR_SIG_RE = re.compile(r"^META-INF/[^/]+\.(RSA|DSA|EC)$", re.IGNORECASE)
# The signed .app bundle sits directly under Payload/ in an IPA.
_IOS_SIG_RE = re.compile(r"^Payload/[^/]+\.app/_CodeSignature/CodeResources$")

# APK Signature Scheme v2/v3 stores its block immediately before the ZIP central
# directory, terminated by this magic. An APK signed with v2+ only carries no
# META-INF/*.RSA at all, so checking the JAR entries alone would call a
# perfectly good APK unsigned.
_APK_SIG_BLOCK_MAGIC = b"APK Sig Block 42"
_EOCD_MAGIC = b"PK\x05\x06"
# 22-byte end-of-central-directory record + up to 65535 bytes of zip comment.
_EOCD_MAX = 65557


def _entry_names(path: str) -> Optional[List[str]]:
    """Archive member names, or None when the file is not a readable zip."""
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.namelist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return None


def _has_jar_signature(names: List[str]) -> bool:
    return any(_JAR_SIG_RE.match(n) for n in names)


def _has_ios_signature(names: List[str]) -> bool:
    return any(_IOS_SIG_RE.match(n) for n in names)


def _has_apk_signing_block(path: str) -> bool:
    """True when an APK Signing Block (v2/v3) precedes the central directory.

    Parsed by hand because ``zipfile`` has no notion of it — the block lives in
    the gap between the last entry and the central directory, which the zip
    format itself ignores.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            if size < 22:
                return False
            tail_len = min(size, _EOCD_MAX)
            fh.seek(size - tail_len)
            tail = fh.read(tail_len)

            idx = tail.rfind(_EOCD_MAGIC)
            if idx < 0 or idx + 20 > len(tail):
                return False
            # Central-directory offset lives 16 bytes into the EOCD record.
            (cd_offset,) = struct.unpack_from("<I", tail, idx + 16)
            # 0xFFFFFFFF means the real offset is in the Zip64 record; APKs
            # never reach 4 GB, so treat it as undetectable rather than guess.
            if cd_offset == 0xFFFFFFFF:
                return False
            magic_at = cd_offset - len(_APK_SIG_BLOCK_MAGIC)
            if magic_at < 0 or cd_offset > size:
                return False
            fh.seek(magic_at)
            return fh.read(len(_APK_SIG_BLOCK_MAGIC)) == _APK_SIG_BLOCK_MAGIC
    except OSError:
        return False


def detect(path: str, artifact_type: str = "") -> str:
    """Return SIGNED / UNSIGNED / UNKNOWN for a stored binary.

    ``artifact_type`` is the build's ``apk`` | ``aab`` | ``ipa``. An unreadable
    file, a non-zip, or an unrecognised type yields UNKNOWN — which never blocks
    a publish, because guessing "unsigned" on something we simply failed to
    parse would strand a legitimate build.
    """
    kind = str(artifact_type or "").strip().lower()
    if kind not in ("apk", "aab", "ipa"):
        return UNKNOWN

    names = _entry_names(path)
    if names is None:
        return UNKNOWN

    if kind == "ipa":
        return SIGNED if _has_ios_signature(names) else UNSIGNED
    if kind == "aab":
        # App bundles are JAR-signed only — there is no APK Signing Block in a
        # .aab, so the META-INF check is the whole story.
        return SIGNED if _has_jar_signature(names) else UNSIGNED
    # apk: v1 (JAR) or v2/v3 (signing block) both count as signed.
    if _has_jar_signature(names):
        return SIGNED
    return SIGNED if _has_apk_signing_block(path) else UNSIGNED


def detect_safe(path: str, artifact_type: str = "") -> str:
    """``detect`` that never raises — ingest must not fail over a probe."""
    try:
        return detect(path, artifact_type)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Signature detection failed: path=%s", path)
        return UNKNOWN
