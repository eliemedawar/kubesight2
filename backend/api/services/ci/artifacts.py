"""Artifact records and the storage port.

Two distinct roles, deliberately separate:

* :class:`ArtifactStore` — *puts bytes somewhere*. Local disk in Phase 1; an
  object store or a Nexus raw repository later. Used for jars, apks, reports.
* :class:`ArtifactPublisher` — *publishes to a registry and reports back what
  landed*. Container images only; implemented when BuildKit ships. An image is
  never streamed through KubeSight — the builder pushes it directly and we
  record the coordinates.

Everything else in CI records artifacts through :func:`record_artifact`, which
is the only place ``ci_artifacts`` rows are created.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import IO, Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from ...db import db
from ...models_ci import ARTIFACT_TYPES, CiArtifact
from .runners.base import ArtifactRef

logger = logging.getLogger(__name__)


@dataclass
class ArtifactUpload:
    """A local file on its way into the store."""

    service_id: int
    build_id: int
    name: str
    local_path: str


@dataclass
class StoredArtifact:
    """Where the store put it."""

    backend: str
    storage_ref: str
    size_bytes: int
    checksum_sha256: str


@dataclass
class PublishContext:
    """Inputs for publishing a container image (Phase 4)."""

    service_id: int
    build_id: int
    registry_connection_id: Optional[int]
    repository: str
    tag: str
    context_dir: str
    dockerfile_path: str
    build_args: Dict[str, str] = field(default_factory=dict)


@dataclass
class PublishedArtifact:
    """What actually landed in the registry."""

    uri: str
    digest: str
    size_bytes: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ArtifactStore(Protocol):
    backend: str

    def put(self, upload: ArtifactUpload) -> StoredArtifact: ...
    def open(self, artifact: CiArtifact) -> IO[bytes]: ...
    def url(self, artifact: CiArtifact) -> Optional[str]: ...
    def delete(self, artifact: CiArtifact) -> None: ...


@runtime_checkable
class ArtifactPublisher(Protocol):
    """Registry-side publication. Implemented by the BuildKit publisher."""

    def publish(self, context: PublishContext) -> PublishedArtifact: ...


# ---------------------------------------------------------------------------
# Local filesystem store
# ---------------------------------------------------------------------------

def artifact_root() -> str:
    """Root of the local artifact store, mirroring ``MOBILE_ARTIFACT_DIR``."""
    configured = os.getenv("CI_ARTIFACT_DIR", "").strip()
    if configured:
        return os.path.abspath(configured)
    base = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    return os.path.join(base, "data", "ci_artifacts")


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").rsplit("/", 1)[-1]).strip("._")
    return cleaned or "artifact.bin"


class LocalArtifactStore:
    """Files under ``CI_ARTIFACT_DIR/<serviceId>/<buildId>/<name>``."""

    backend = "local"

    def put(self, upload: ArtifactUpload) -> StoredArtifact:
        rel_dir = os.path.join(str(upload.service_id), str(upload.build_id))
        abs_dir = os.path.join(artifact_root(), rel_dir)
        os.makedirs(abs_dir, exist_ok=True)
        filename = safe_filename(upload.name)
        rel_path = os.path.join(rel_dir, filename)
        abs_path = os.path.join(artifact_root(), rel_path)
        if os.path.abspath(upload.local_path) != os.path.abspath(abs_path):
            shutil.copyfile(upload.local_path, abs_path)
        digest = hashlib.sha256()
        with open(abs_path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return StoredArtifact(
            backend=self.backend,
            storage_ref=rel_path.replace("\\", "/"),
            size_bytes=os.path.getsize(abs_path),
            checksum_sha256=digest.hexdigest(),
        )

    def _absolute(self, artifact: CiArtifact) -> str:
        """Resolve a stored ref, refusing anything that escapes the root."""
        root = os.path.abspath(artifact_root())
        candidate = os.path.abspath(os.path.join(root, artifact.storage_ref or ""))
        if os.path.commonpath([root, candidate]) != root:
            raise ValueError("Artifact path is outside the artifact store.")
        return candidate

    def open(self, artifact: CiArtifact) -> IO[bytes]:
        return open(self._absolute(artifact), "rb")

    def url(self, artifact: CiArtifact) -> Optional[str]:
        return None  # Served by the download endpoint, not a direct URL.

    def delete(self, artifact: CiArtifact) -> None:
        try:
            os.remove(self._absolute(artifact))
        except (OSError, ValueError):
            pass


_STORES: Dict[str, ArtifactStore] = {"local": LocalArtifactStore()}


def get_store(backend: str = "local") -> ArtifactStore:
    store = _STORES.get((backend or "local").strip())
    if store is None:
        raise ValueError(f"No artifact store is configured for '{backend}'.")
    return store


def register_store(store: ArtifactStore) -> None:
    _STORES[store.backend] = store


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def record_artifact(
    *,
    service_id: int,
    build_id: Optional[int],
    build_stage_id: Optional[int],
    ref: ArtifactRef,
    commit_sha: Optional[str] = None,
    branch: Optional[str] = None,
    version: Optional[str] = None,
    registry_connection_id: Optional[int] = None,
    commit: bool = False,
) -> CiArtifact:
    """Create one ``ci_artifacts`` row from a runner's :class:`ArtifactRef`.

    A ref carrying ``local_path`` is ingested into the local store first; a ref
    carrying ``uri`` is already published (a pushed image) and is recorded as-is.
    """
    artifact_type = ref.artifact_type if ref.artifact_type in ARTIFACT_TYPES else "binary"
    row = CiArtifact(
        service_id=service_id,
        build_id=build_id,
        build_stage_id=build_stage_id,
        artifact_type=artifact_type,
        name=ref.name[:255],
        version=version,
        uri=ref.uri,
        digest=ref.digest,
        size_bytes=ref.size_bytes,
        commit_sha=commit_sha,
        branch=branch,
        registry_connection_id=registry_connection_id,
        artifact_metadata=dict(ref.metadata or {}),
    )
    if ref.local_path:
        stored = get_store("local").put(
            ArtifactUpload(
                service_id=service_id,
                build_id=build_id or 0,
                name=ref.name,
                local_path=ref.local_path,
            )
        )
        row.storage_backend = stored.backend
        row.storage_ref = stored.storage_ref
        row.size_bytes = stored.size_bytes
        row.checksum_sha256 = stored.checksum_sha256
    elif ref.uri and str(ref.uri).startswith("mock://"):
        row.storage_backend = "local"
    else:
        row.storage_backend = "registry" if artifact_type == "container-image" else "local"

    db.session.add(row)
    if commit:
        db.session.commit()
    return row


def list_for_service(service_id: int, limit: int = 100) -> List[CiArtifact]:
    return (
        CiArtifact.query.filter_by(service_id=service_id)
        .order_by(CiArtifact.created_at.desc(), CiArtifact.id.desc())
        .limit(max(1, min(int(limit), 500)))
        .all()
    )


def list_for_build(build_id: int, limit: int = 200) -> List[CiArtifact]:
    """One page of a build's artifacts, oldest first.

    Bounded for the same reason list_for_service is: a stage that collects
    `dist/**` or `target/**/*.jar` declares one artifact per matched file, so a
    single build can own thousands of rows. Serialising all of them lands the
    whole set in one response and one unbroken list in the drawer. Pair with
    count_for_build when the caller needs to say how many were left out.
    """
    return (
        CiArtifact.query.filter_by(build_id=build_id)
        .order_by(CiArtifact.id.asc())
        .limit(max(1, min(int(limit), 1000)))
        .all()
    )


def count_for_build(build_id: int) -> int:
    return CiArtifact.query.filter_by(build_id=build_id).count()


def latest_for_service(service_id: int) -> Optional[CiArtifact]:
    return (
        CiArtifact.query.filter_by(service_id=service_id)
        .order_by(CiArtifact.created_at.desc(), CiArtifact.id.desc())
        .first()
    )


# ---------------------------------------------------------------------------
# Retention
#
# Artifacts are the one part of CI that grows without bound: every build writes
# files and nothing ever removed them. So they expire — by default a day after
# they were written, swept once a day.
#
# Two things are deliberately never swept:
#
# * The newest build's artifacts, whatever their age. A service that builds
#   once a week would otherwise spend most of its time with nothing to
#   download. CI_ARTIFACT_KEEP_LAST=0 turns that off for anyone who wants the
#   disk back more than the last good output.
# * Container images. Their row is metadata pointing at the registry: deleting
#   it frees no disk and loses the record of what was built. Only artifacts
#   this store actually holds bytes for are candidates.
# ---------------------------------------------------------------------------

DEFAULT_RETENTION_DAYS = 1
DEFAULT_KEEP_LAST_BUILDS = 1
DEFAULT_PURGE_INTERVAL_HOURS = 24.0
# Written in the artifact root itself: the timestamp belongs with the data it
# describes, survives a restart, and needs no schema of its own. Losing it (a
# fresh container, no volume) costs one extra sweep, which is harmless.
PURGE_MARKER = ".last-purge"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def retention_days() -> int:
    """Days an artifact is kept. 0 means keep forever."""
    return _env_int("CI_ARTIFACT_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)


def keep_last_builds() -> int:
    """How many recent builds per service are exempt from expiry."""
    return _env_int("CI_ARTIFACT_KEEP_LAST", DEFAULT_KEEP_LAST_BUILDS)


def purge_interval_hours() -> float:
    raw = os.getenv("CI_ARTIFACT_PURGE_INTERVAL_HOURS", "").strip()
    if not raw:
        return DEFAULT_PURGE_INTERVAL_HOURS
    try:
        return max(0.25, float(raw))
    except ValueError:
        return DEFAULT_PURGE_INTERVAL_HOURS


def autoclean_enabled() -> bool:
    return os.getenv("CI_ARTIFACT_AUTOCLEAN", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    # Rows come back naive from SQLite and aware from PostgreSQL; the age
    # comparison happens in Python for exactly that reason.
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _marker_path() -> str:
    return os.path.join(artifact_root(), PURGE_MARKER)


def last_purge_at() -> Optional[datetime]:
    try:
        stamp = os.path.getmtime(_marker_path())
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc)


def _mark_purged() -> None:
    try:
        os.makedirs(artifact_root(), exist_ok=True)
        with open(_marker_path(), "w", encoding="utf-8") as handle:
            handle.write(datetime.now(timezone.utc).isoformat())
    except OSError as exc:  # A sweep that ran is still a sweep that ran.
        logger.warning("Could not record the artifact purge time: %s", exc)


def usage(service_id: Optional[int] = None) -> Dict[str, int]:
    """What the local store is holding — the number the cleanup is about."""
    query = CiArtifact.query.filter(
        CiArtifact.storage_backend == "local", CiArtifact.storage_ref.isnot(None)
    )
    if service_id is not None:
        query = query.filter(CiArtifact.service_id == int(service_id))
    rows = query.with_entities(CiArtifact.size_bytes).all()
    return {"count": len(rows), "bytes": sum(int(row[0] or 0) for row in rows)}


def policy(service_id: Optional[int] = None) -> Dict[str, Any]:
    """The rules in force plus what they currently apply to, for the UI."""
    days = retention_days()
    last = last_purge_at()
    return {
        "retentionDays": days,
        "keepLastBuilds": keep_last_builds(),
        "autoclean": autoclean_enabled() and days > 0,
        "intervalHours": purge_interval_hours(),
        "lastPurgeAt": last.isoformat() if last else None,
        "root": artifact_root(),
        "usage": usage(service_id),
        # Container images are counted separately so nobody expects cleaning to
        # reclaim them: the bytes are in the registry, not here.
        "registryOnly": CiArtifact.query.filter(
            CiArtifact.storage_backend == "registry",
            *([CiArtifact.service_id == int(service_id)] if service_id is not None else []),
        ).count(),
    }


def _protected_build_ids(service_ids: List[int], keep_last: int) -> set:
    """The most recent ``keep_last`` builds per service that produced files."""
    if keep_last <= 0 or not service_ids:
        return set()
    protected = set()
    for service_id in service_ids:
        rows = (
            CiArtifact.query.filter(
                CiArtifact.service_id == service_id,
                CiArtifact.storage_backend == "local",
                CiArtifact.storage_ref.isnot(None),
                CiArtifact.build_id.isnot(None),
            )
            .with_entities(CiArtifact.build_id)
            .distinct()
            .order_by(CiArtifact.build_id.desc())
            .limit(keep_last)
            .all()
        )
        protected.update(int(row[0]) for row in rows)
    return protected


def delete_artifact(row: CiArtifact, *, commit: bool = True) -> int:
    """Remove one artifact's bytes and its record. Returns bytes freed."""
    freed = int(row.size_bytes or 0)
    if row.storage_ref and (row.storage_backend or "local") == "local":
        try:
            get_store("local").delete(row)
        except ValueError:  # A ref that escapes the root: never follow it.
            logger.warning("Refusing to delete artifact %s outside the store", row.id)
            freed = 0
    else:
        # Nothing of ours to remove — the bytes live in a registry.
        freed = 0
    db.session.delete(row)
    if commit:
        db.session.commit()
    return freed


def purge(
    *,
    service_id: Optional[int] = None,
    older_than_days: Optional[int] = None,
    keep_last: Optional[int] = None,
) -> Dict[str, Any]:
    """Delete stored artifacts, oldest first.

    ``older_than_days=0`` means "everything in scope" — that is the explicit
    clean, not the expiry. ``keep_last`` still applies unless it is passed as 0,
    so a routine sweep cannot leave a service with nothing to download.
    """
    days = retention_days() if older_than_days is None else max(0, int(older_than_days))
    keep = keep_last_builds() if keep_last is None else max(0, int(keep_last))

    query = CiArtifact.query.filter(
        CiArtifact.storage_backend == "local", CiArtifact.storage_ref.isnot(None)
    )
    if service_id is not None:
        query = query.filter(CiArtifact.service_id == int(service_id))
    candidates = query.order_by(CiArtifact.id.asc()).all()

    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days > 0 else None
    service_ids = sorted({int(row.service_id) for row in candidates})
    protected = _protected_build_ids(service_ids, keep)

    deleted = 0
    freed = 0
    kept_recent = 0
    for row in candidates:
        if cutoff is not None:
            created = _aware(row.created_at)
            if created is None or created >= cutoff:
                continue
        if row.build_id is not None and int(row.build_id) in protected:
            kept_recent += 1
            continue
        freed += delete_artifact(row, commit=False)
        deleted += 1

    if deleted:
        db.session.commit()
    # One walk, whether or not this sweep removed anything: directories left
    # behind by an earlier single delete are exactly what makes a cleaned store
    # look uncleaned.
    _prune_empty_dirs()
    return {
        "deleted": deleted,
        "freedBytes": freed,
        "keptRecent": kept_recent,
        "retentionDays": days,
        "keepLastBuilds": keep,
        "usage": usage(service_id),
    }


def _prune_empty_dirs() -> None:
    """Remove the ``<serviceId>/<buildId>`` directories nothing is left in.

    Cosmetic but worth it: without this the store fills with empty directories
    that make it look like the cleanup did nothing.
    """
    root = artifact_root()
    if not os.path.isdir(root):
        return
    for current, directories, files in os.walk(root, topdown=False):
        if current == root or files or directories:
            continue
        try:
            os.rmdir(current)
        except OSError:
            pass


def purge_due() -> bool:
    """Whether the automatic sweep should run now."""
    if not autoclean_enabled() or retention_days() <= 0:
        return False
    last = last_purge_at()
    if last is None:
        return True
    elapsed_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    return elapsed_hours >= purge_interval_hours()


def run_due_purge() -> bool:
    """Scheduler hook: expire artifacts once per interval. True if it ran.

    The marker is written whether or not anything was deleted — a sweep that
    found nothing has still done its job, and re-running it every tick would
    walk the whole table for no reason.
    """
    if not purge_due():
        return False
    result = purge()
    _mark_purged()
    if result["deleted"]:
        logger.info(
            "Artifact retention: removed %d artifacts older than %d day(s), freeing %d bytes",
            result["deleted"],
            result["retentionDays"],
            result["freedBytes"],
        )
    return True

