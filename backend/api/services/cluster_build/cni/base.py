"""CNI descriptor base: manifest sourcing + shared transforms."""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from ..profiles import ResolvedProfile

_FETCH_TIMEOUT_SECONDS = 30


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
    # DaemonSet the readiness gate waits on: (namespace, name).
    readiness_daemonset: Tuple[str, str] = ("kube-system", "")

    def bundled_path(self, version: str, filename: str) -> Path:
        return _data_dir() / self.id / version / filename

    def load_manifests(self, version: str, profile: ResolvedProfile) -> List[str]:
        """Bundled files first; upstream fetch only in internet mode."""
        if version not in self.versions:
            raise CniRenderError(
                f"{self.display_name} {version} is not in the tested version set "
                f"({', '.join(self.versions)})."
            )
        manifests: List[str] = []
        missing: List[str] = []
        for filename in self.manifest_files:
            path = self.bundled_path(version, filename)
            if path.is_file():
                manifests.append(path.read_text(encoding="utf-8"))
            else:
                missing.append(filename)
        if not missing:
            return manifests
        if profile.repo_mode != "internet":
            raise CniRenderError(
                f"{self.display_name} {version}: bundled manifest(s) "
                f"{', '.join(missing)} not found under {_data_dir() / self.id / version} "
                f"and repo mode '{profile.repo_mode}' forbids fetching from the "
                "internet. Bundle the manifests to proceed."
            )
        for url_template in self.manifest_urls:
            url = url_template.format(version=version)
            try:
                with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_SECONDS) as response:
                    manifests.append(response.read().decode("utf-8", errors="replace"))
            except (urllib.error.URLError, OSError) as exc:
                raise CniRenderError(
                    f"{self.display_name} {version}: manifest fetch failed from "
                    f"{url} — {exc}"
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
