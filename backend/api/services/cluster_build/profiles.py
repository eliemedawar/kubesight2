"""Build profiles: where packages and images come from.

``resolve(row)`` collapses a BuildProfile row into a ``ResolvedProfile`` of
concrete URLs/registries every OS adapter and CNI plugin consumes — nothing
downstream ever hardcodes pkgs.k8s.io or registry.k8s.io.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ...db import db
from ...models import BuildProfile
from ...secret_encryption import decrypt_secret, encrypt_secret

_REPO_MODES = {"internet", "mirror", "offline"}

# Internet-mode defaults (dev/test). {minor} is e.g. "1.32".
DEFAULT_K8S_PKG_REPO_DEB = "https://pkgs.k8s.io/core:/stable:/v{minor}/deb/"
DEFAULT_K8S_PKG_REPO_RPM = "https://pkgs.k8s.io/core:/stable:/v{minor}/rpm/"
DEFAULT_K8S_IMAGE_REGISTRY = "registry.k8s.io"
DEFAULT_CRI_REPO_RPM = "https://download.docker.com/linux/centos/docker-ce.repo"


@dataclass(frozen=True)
class ResolvedProfile:
    repo_mode: str
    k8s_pkg_repo_url: str            # may contain {minor}
    k8s_pkg_gpg_key_url: str
    cri_pkg_repo_url: str
    k8s_image_registry: str
    cni_image_registry: str          # "" ⇒ keep manifest defaults
    addon_image_registry: str
    registry_username: str
    registry_password: str
    http_proxy: str
    https_proxy: str
    no_proxy: str
    extra_ca_certs_pem: str
    offline_bundle_path: str
    offline_bundle_checksum: str

    def k8s_pkg_repo(self, os_family: str, minor: str) -> str:
        template = self.k8s_pkg_repo_url
        if not template:
            template = (
                DEFAULT_K8S_PKG_REPO_DEB if os_family == "debian"
                else DEFAULT_K8S_PKG_REPO_RPM
            )
        return template.replace("{minor}", minor)

    def proxy_env(self) -> str:
        """Shell export lines for proxy settings; empty string when unset."""
        lines = []
        if self.http_proxy:
            lines.append(f'export http_proxy="{self.http_proxy}"')
        if self.https_proxy:
            lines.append(f'export https_proxy="{self.https_proxy}"')
        if self.no_proxy:
            lines.append(f'export no_proxy="{self.no_proxy}"')
        return "\n".join(lines)


def default_profile() -> ResolvedProfile:
    """Internet mode with stock upstream endpoints (used when a build has no
    profile attached — dev/lab convenience)."""
    return ResolvedProfile(
        repo_mode="internet",
        k8s_pkg_repo_url="",
        k8s_pkg_gpg_key_url="",
        cri_pkg_repo_url="",
        k8s_image_registry=DEFAULT_K8S_IMAGE_REGISTRY,
        cni_image_registry="",
        addon_image_registry="",
        registry_username="",
        registry_password="",
        http_proxy="",
        https_proxy="",
        no_proxy="",
        extra_ca_certs_pem="",
        offline_bundle_path="",
        offline_bundle_checksum="",
    )


def resolve(row: Optional[BuildProfile]) -> ResolvedProfile:
    if row is None:
        return default_profile()
    return ResolvedProfile(
        repo_mode=row.repo_mode or "internet",
        k8s_pkg_repo_url=row.k8s_pkg_repo_url or "",
        k8s_pkg_gpg_key_url=row.k8s_pkg_gpg_key_url or "",
        cri_pkg_repo_url=row.cri_pkg_repo_url or "",
        k8s_image_registry=row.k8s_image_registry or DEFAULT_K8S_IMAGE_REGISTRY,
        cni_image_registry=row.cni_image_registry or "",
        addon_image_registry=row.addon_image_registry or "",
        registry_username=row.registry_username or "",
        registry_password=decrypt_secret(row.registry_password_cipher or ""),
        http_proxy=row.http_proxy or "",
        https_proxy=row.https_proxy or "",
        no_proxy=row.no_proxy or "",
        extra_ca_certs_pem=row.extra_ca_certs_pem or "",
        offline_bundle_path=row.offline_bundle_path or "",
        offline_bundle_checksum=row.offline_bundle_checksum or "",
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def serialize(row: BuildProfile) -> Dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "repoMode": row.repo_mode,
        "k8sPkgRepoUrl": row.k8s_pkg_repo_url,
        "k8sPkgGpgKeyUrl": row.k8s_pkg_gpg_key_url,
        "criPkgRepoUrl": row.cri_pkg_repo_url,
        "k8sImageRegistry": row.k8s_image_registry,
        "cniImageRegistry": row.cni_image_registry,
        "addonImageRegistry": row.addon_image_registry,
        "registryUsername": row.registry_username,
        "registryPasswordConfigured": bool(row.registry_password_cipher),
        "httpProxy": row.http_proxy,
        "httpsProxy": row.https_proxy,
        "noProxy": row.no_proxy,
        "extraCaConfigured": bool(row.extra_ca_certs_pem),
        "offlineBundlePath": row.offline_bundle_path,
        "offlineBundleChecksum": row.offline_bundle_checksum,
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


def list_profiles() -> List[Dict[str, Any]]:
    rows = BuildProfile.query.order_by(BuildProfile.name.asc()).all()
    return [serialize(row) for row in rows]


def get_profile(profile_id: int) -> BuildProfile:
    row = db.session.get(BuildProfile, profile_id)
    if row is None:
        raise LookupError("Build profile not found.")
    return row


def _apply_payload(row: BuildProfile, payload: Dict[str, Any]) -> None:
    name = str(payload.get("name", row.name or "")).strip()
    if not name:
        raise ValueError("name is required.")
    repo_mode = str(payload.get("repoMode", row.repo_mode or "internet")).strip()
    if repo_mode not in _REPO_MODES:
        raise ValueError("repoMode must be 'internet', 'mirror', or 'offline'.")
    row.name = name
    row.repo_mode = repo_mode

    text_fields = {
        "k8sPkgRepoUrl": "k8s_pkg_repo_url",
        "k8sPkgGpgKeyUrl": "k8s_pkg_gpg_key_url",
        "criPkgRepoUrl": "cri_pkg_repo_url",
        "k8sImageRegistry": "k8s_image_registry",
        "cniImageRegistry": "cni_image_registry",
        "addonImageRegistry": "addon_image_registry",
        "registryUsername": "registry_username",
        "httpProxy": "http_proxy",
        "httpsProxy": "https_proxy",
        "noProxy": "no_proxy",
        "extraCaCertsPem": "extra_ca_certs_pem",
        "offlineBundlePath": "offline_bundle_path",
        "offlineBundleChecksum": "offline_bundle_checksum",
    }
    for key, attr in text_fields.items():
        if key in payload:
            setattr(row, attr, str(payload.get(key) or "").strip() or None)

    password = payload.get("registryPassword")
    if password:
        row.registry_password_cipher = encrypt_secret(str(password))

    if repo_mode == "mirror":
        if not (row.k8s_pkg_repo_url and row.k8s_image_registry):
            raise ValueError(
                "mirror mode requires k8sPkgRepoUrl and k8sImageRegistry."
            )
    if repo_mode == "offline" and not row.offline_bundle_path:
        raise ValueError("offline mode requires offlineBundlePath.")


def create_profile(payload: Dict[str, Any]) -> Dict[str, Any]:
    row = BuildProfile()
    _apply_payload(row, payload)
    db.session.add(row)
    db.session.commit()
    return serialize(row)


def update_profile(profile_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = get_profile(profile_id)
    _apply_payload(row, payload)
    db.session.commit()
    return serialize(row)


def delete_profile(profile_id: int) -> None:
    from ...models import ClusterBuild

    row = get_profile(profile_id)
    if ClusterBuild.query.filter_by(build_profile_id=profile_id).count():
        raise ValueError("Profile is referenced by cluster builds; remove those first.")
    db.session.delete(row)
    db.session.commit()
