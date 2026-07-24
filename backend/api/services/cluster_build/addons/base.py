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
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

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


@dataclass(frozen=True)
class AddonDescriptor:
    id: str
    display_name: str
    description: str
    support_tier: str
    versions: Tuple[str, ...]
    manifest_files: Tuple[str, ...]
    manifest_urls: Tuple[str, ...]
    manifest_sha256: Tuple[str, ...]
    # kubectl argument strings. The executor supplies admin.conf and tracing.
    readiness_commands: Tuple[str, ...]

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
        if not (
            len(self.manifest_files)
            == len(self.manifest_urls)
            == len(self.manifest_sha256)
        ):
            raise AddonRenderError(
                f"{self.display_name} descriptor is invalid: every manifest file "
                "must have one pinned URL and SHA-256 digest."
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
                self.manifest_sha256,
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
                f"'{profile.repo_mode}' forbids an internet fallback."
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
