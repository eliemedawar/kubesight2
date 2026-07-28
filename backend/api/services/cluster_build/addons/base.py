"""Version-pinned cluster add-on descriptors.

Add-ons follow the same safety model as CNI plugins: users select from a closed
catalog, manifests are integrity-pinned, image references can be rewritten to
an internal registry proxy, and the executor waits for readiness.
"""

from __future__ import annotations

import hashlib
import ssl
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..cni.base import extract_images, rewrite_manifest_images
from ..profiles import ResolvedProfile

_FETCH_TIMEOUT_SECONDS = 30
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MANIFEST_CACHE: dict[tuple[str, str], bytes] = {}
_MANIFEST_CACHE_LOCK = threading.Lock()


class AddonRenderError(RuntimeError):
    pass


def _data_dir() -> Path:
    # backend/api/data/addons — sibling of backend/api/services.
    return Path(__file__).resolve().parents[3] / "data" / "addons"


class AddonConfigError(ValueError):
    """A rejected per-add-on configuration payload."""


@dataclass(frozen=True)
class AddonDescriptor:
    id: str
    display_name: str
    description: str
    support_tier: str
    versions: Tuple[str, ...]
    manifest_files: Tuple[str, ...]
    manifest_urls: Tuple[str, ...]
    # Digests keyed by add-on version, each a tuple parallel to
    # ``manifest_files``. Keyed by version because two versions of the same
    # add-on ship different bytes for the same filename — a single flat tuple
    # would pin one version's digests onto every version and fail the integrity
    # check for all but one of them.
    manifest_sha256: Dict[str, Tuple[str, ...]]
    # kubectl argument strings. The executor supplies admin.conf and tracing.
    readiness_commands: Tuple[str, ...]
    # Declarative description of ``config`` for the wizard. Add-ons that need
    # no configuration leave this empty and reject any config sent to them.
    config_fields: Tuple[Dict[str, Any], ...] = ()
    # Kubernetes minors each add-on *version* is validated against. Per version
    # for the same reason as the CNI descriptors: support windows move.
    k8s_minors_by_version: Dict[str, Tuple[str, ...]] = field(default_factory=dict)

    def bundled_path(self, version: str, filename: str) -> Path:
        return _data_dir() / self.id / version / filename

    def digests_for(self, version: str) -> Tuple[str, ...]:
        return self.manifest_sha256.get(version, ())

    def supported_k8s_minors(self, version: str) -> Tuple[str, ...]:
        return self.k8s_minors_by_version.get(version, ())

    def supports_k8s_minor(self, version: str, minor: str) -> bool:
        return minor in self.supported_k8s_minors(version)

    def versions_for_k8s_minor(self, minor: str) -> List[str]:
        """This add-on's versions validated on ``minor``, newest first."""
        return [
            version for version in self.versions
            if self.supports_k8s_minor(version, minor)
        ]

    def default_version_for_k8s_minor(self, minor: str) -> str:
        candidates = self.versions_for_k8s_minor(minor)
        return candidates[0] if candidates else ""

    def normalize_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and canonicalize this add-on's ``config`` object.

        The default add-on takes no configuration, so anything supplied is a
        client mistake worth surfacing rather than dropping.
        """
        if config:
            raise AddonConfigError(
                f"{self.display_name} does not take any configuration."
            )
        return {}

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
            raise AddonRenderError(
                f"{self.display_name} {version}: manifest '{filename}' "
                f"fetch failed from {url} — {exc}"
            ) from exc
        if len(content) > _MAX_MANIFEST_BYTES:
            raise AddonRenderError(
                f"{self.display_name} {version}: manifest '{filename}' "
                f"exceeds {_MAX_MANIFEST_BYTES} bytes."
            )
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != expected_sha256:
            raise AddonRenderError(
                f"{self.display_name} {version}: manifest '{filename}' "
                "failed its pinned SHA-256 integrity check."
            )
        with _MANIFEST_CACHE_LOCK:
            _MANIFEST_CACHE[cache_key] = content
        return content

    def load_manifests(self, version: str, profile: ResolvedProfile) -> List[str]:
        if version not in self.versions:
            raise AddonRenderError(
                f"{self.display_name} {version} is not in the tested version set "
                f"({', '.join(self.versions)})."
            )
        digests = self.digests_for(version)
        if not (
            len(self.manifest_files)
            == len(self.manifest_urls)
            == len(digests)
        ):
            raise AddonRenderError(
                f"{self.display_name} {version} descriptor is invalid: every "
                "manifest file must have one pinned URL and one SHA-256 digest "
                "recorded for this version."
            )

        sources = [
            (
                filename,
                url_template,
                digest,
                self.bundled_path(version, filename),
            )
            for filename, url_template, digest in zip(
                self.manifest_files,
                self.manifest_urls,
                digests,
            )
        ]
        missing = [
            filename for filename, _, _, path in sources if not path.is_file()
        ]
        if missing and profile.repo_mode == "offline":
            raise AddonRenderError(
                f"{self.display_name} {version}: bundled manifest(s) "
                f"{', '.join(missing)} not found under "
                f"{_data_dir() / self.id / version} and repo mode "
                f"'{profile.repo_mode}' forbids an internet fallback. Run "
                "`python tools/fetch_cluster_build_bundles.py` on the "
                "KubeSight host to populate it."
            )

        manifests: List[str] = []
        for filename, url_template, expected_sha256, path in sources:
            if path.is_file():
                content = path.read_bytes()
            else:
                url = url_template.format(version=version)
                content = self._download(
                    filename=filename,
                    url=url,
                    expected_sha256=expected_sha256,
                    profile=profile,
                    version=version,
                )

            actual_sha256 = hashlib.sha256(content).hexdigest()
            if actual_sha256 != expected_sha256:
                raise AddonRenderError(
                    f"{self.display_name} {version}: manifest '{filename}' "
                    "failed its pinned SHA-256 integrity check."
                )
            try:
                manifests.append(content.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise AddonRenderError(
                    f"{self.display_name} {version}: manifest '{filename}' "
                    "is not valid UTF-8."
                ) from exc
        return manifests

    def render(self, version: str, profile: ResolvedProfile) -> List[str]:
        return [
            rewrite_manifest_images(manifest, profile.addon_image_registry)
            for manifest in self.load_manifests(version, profile)
        ]

    def required_images(self, version: str, profile: ResolvedProfile) -> List[str]:
        images: List[str] = []
        for manifest in self.render(version, profile):
            for image in extract_images(manifest):
                if image not in images:
                    images.append(image)
        return images
