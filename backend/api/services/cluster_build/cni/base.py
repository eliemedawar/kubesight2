"""CNI descriptor base: manifest sourcing + shared transforms."""

from __future__ import annotations

import hashlib
import re
import ssl
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..profiles import ResolvedProfile

_FETCH_TIMEOUT_SECONDS = 30
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MANIFEST_CACHE: dict[tuple[str, str], bytes] = {}
_MANIFEST_CACHE_LOCK = threading.Lock()


class CniRenderError(RuntimeError):
    pass


def _data_dir() -> Path:
    # backend/api/data/cni — sibling of backend/api/services.
    return Path(__file__).resolve().parents[3] / "data" / "cni"


_IMAGE_LINE_RE = re.compile(r'^(\s*(?:-\s*)?image:\s*["\']?)([^"\'\s]+)(["\']?\s*)$')


def _rewrite_image(ref: str, registry: str) -> str:
    """Point an image ref at a mirror registry, preserving the repo path.
    'docker.io/calico/node:v3' → '<mirror>/calico/node:v3';
    'calico/node:v3' → '<mirror>/calico/node:v3'."""
    parts = ref.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
    return registry.rstrip("/") + "/" + "/".join(parts)


def rewrite_manifest_images(manifest: str, registry: str) -> str:
    if not registry:
        return manifest
    lines = []
    for line in manifest.splitlines():
        match = _IMAGE_LINE_RE.match(line)
        if match:
            line = match.group(1) + _rewrite_image(match.group(2), registry) + match.group(3)
        lines.append(line)
    return "\n".join(lines) + ("\n" if manifest.endswith("\n") else "")


def extract_images(manifest: str) -> List[str]:
    """Every image ref in the manifest — feeds offline validation, so the
    required-image list is derived, never hand-maintained."""
    images = []
    for line in manifest.splitlines():
        match = _IMAGE_LINE_RE.match(line)
        if match and match.group(2) not in images:
            images.append(match.group(2))
    return images


@dataclass(frozen=True)
class CniDescriptor:
    id: str
    display_name: str
    support_tier: str                    # production | lab | experimental
    versions: Tuple[str, ...]            # newest first; [0] is the default
    default_pod_cidr: str
    manifest_files: Tuple[str, ...]      # filenames under data/cni/<id>/<ver>/
    manifest_urls: Tuple[str, ...]       # pinned upstream, {version} templated
    manifest_sha256: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    # DaemonSet the readiness gate waits on: (namespace, name).
    readiness_daemonset: Tuple[str, str] = ("kube-system", "")

    def bundled_path(self, version: str, filename: str) -> Path:
        return _data_dir() / self.id / version / filename

    def _download(
        self,
        *,
        filename: str,
        url: str,
        expected_sha256: str,
        profile: ResolvedProfile,
        version: str,
    ) -> bytes:
        cache_key = (url, expected_sha256)
        with _MANIFEST_CACHE_LOCK:
            cached = _MANIFEST_CACHE.get(cache_key)
        if cached is not None:
            return cached

        proxies = {}
        if profile.http_proxy:
            proxies["http"] = profile.http_proxy
        if profile.https_proxy:
            proxies["https"] = profile.https_proxy
        context = ssl.create_default_context()
        if profile.extra_ca_certs_pem:
            context.load_verify_locations(cadata=profile.extra_ca_certs_pem)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(proxies or None),
            urllib.request.HTTPSHandler(context=context),
        )
        try:
            with opener.open(url, timeout=_FETCH_TIMEOUT_SECONDS) as response:
                content = response.read(_MAX_MANIFEST_BYTES + 1)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise CniRenderError(
                f"{self.display_name} {version}: manifest '{filename}' "
                f"fetch failed from {url} — {exc}"
            ) from exc
        if len(content) > _MAX_MANIFEST_BYTES:
            raise CniRenderError(
                f"{self.display_name} {version}: manifest '{filename}' "
                f"exceeds {_MAX_MANIFEST_BYTES} bytes."
            )
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise CniRenderError(
                f"{self.display_name} {version}: manifest '{filename}' "
                "failed its pinned SHA-256 integrity check."
            )
        with _MANIFEST_CACHE_LOCK:
            _MANIFEST_CACHE[cache_key] = content
        return content

    def load_manifests(self, version: str, profile: ResolvedProfile) -> List[str]:
        """Load bundled or integrity-pinned manifests through profile egress."""
        if version not in self.versions:
            raise CniRenderError(
                f"{self.display_name} {version} is not in the tested version set "
                f"({', '.join(self.versions)})."
            )
        if self.manifest_urls and len(self.manifest_files) != len(self.manifest_urls):
            raise CniRenderError(
                f"{self.display_name} descriptor must pair every manifest "
                "file with one pinned URL."
            )
        digests = self.manifest_sha256.get(version, ())
        if self.manifest_urls and len(digests) != len(self.manifest_files):
            raise CniRenderError(
                f"{self.display_name} {version} descriptor must pin one "
                "SHA-256 digest per remote manifest."
            )

        manifests: List[str] = []
        for index, filename in enumerate(self.manifest_files):
            path = self.bundled_path(version, filename)
            if path.is_file():
                content = path.read_bytes()
                if len(content) > _MAX_MANIFEST_BYTES:
                    raise CniRenderError(
                        f"{self.display_name} {version}: manifest '{filename}' "
                        f"exceeds {_MAX_MANIFEST_BYTES} bytes."
                    )
            else:
                if not self.manifest_urls:
                    raise CniRenderError(
                        f"{self.display_name} {version}: bundled-only manifest "
                        f"'{filename}' was not found under "
                        f"{_data_dir() / self.id / version}."
                    )
                if profile.repo_mode == "offline":
                    raise CniRenderError(
                        f"{self.display_name} {version}: bundled manifest "
                        f"'{filename}' not found under "
                        f"{_data_dir() / self.id / version} and offline mode "
                        "forbids fetching it."
                    )
                url = self.manifest_urls[index].format(version=version)
                content = self._download(
                    filename=filename,
                    url=url,
                    expected_sha256=digests[index],
                    profile=profile,
                    version=version,
                )

            expected_sha256 = digests[index] if digests else ""
            if (
                expected_sha256
                and hashlib.sha256(content).hexdigest() != expected_sha256
            ):
                raise CniRenderError(
                    f"{self.display_name} {version}: manifest '{filename}' "
                    "failed its pinned SHA-256 integrity check."
                )
            try:
                manifests.append(content.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise CniRenderError(
                    f"{self.display_name} {version}: manifest '{filename}' "
                    "is not valid UTF-8."
                ) from exc
        return manifests

    # Subclass hooks -------------------------------------------------------

    def apply_pod_cidr(self, manifest: str, pod_cidr: str) -> str:
        return manifest

    def render(
        self, version: str, pod_cidr: str, profile: ResolvedProfile
    ) -> List[str]:
        rendered = []
        for manifest in self.load_manifests(version, profile):
            manifest = self.apply_pod_cidr(manifest, pod_cidr)
            manifest = rewrite_manifest_images(manifest, profile.cni_image_registry)
            rendered.append(manifest)
        return rendered

    def required_images(
        self, version: str, profile: ResolvedProfile
    ) -> List[str]:
        images: List[str] = []
        for manifest in self.render(version, pod_cidr=self.default_pod_cidr, profile=profile):
            for image in extract_images(manifest):
                if image not in images:
                    images.append(image)
        return images
