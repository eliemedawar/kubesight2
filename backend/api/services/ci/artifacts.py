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
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import IO, Any, Dict, List, Optional, Protocol, runtime_checkable

from ...db import db
from ...models_ci import ARTIFACT_TYPES, CiArtifact
from .runners.base import ArtifactRef


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


def list_for_build(build_id: int) -> List[CiArtifact]:
    return (
        CiArtifact.query.filter_by(build_id=build_id)
        .order_by(CiArtifact.id.asc())
        .all()
    )


def latest_for_service(service_id: int) -> Optional[CiArtifact]:
    return (
        CiArtifact.query.filter_by(service_id=service_id)
        .order_by(CiArtifact.created_at.desc(), CiArtifact.id.desc())
        .first()
    )
