"""Build profiles: where packages and images come from.

``resolve(row)`` collapses a BuildProfile row into a ``ResolvedProfile`` of
concrete URLs/registries every OS adapter and CNI plugin consumes — nothing
downstream ever hardcodes pkgs.k8s.io or registry.k8s.io.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from ...db import db
from ...models import BuildProfile
from ...secret_encryption import (
    decrypt_secret,
    encrypt_secret,
    secret_encryption_key_configured,
)

_REPO_MODES = {"internet", "mirror", "offline"}

# Internet-mode defaults (dev/test). {minor} is e.g. "1.32".
DEFAULT_K8S_PKG_REPO_DEB = "https://pkgs.k8s.io/core:/stable:/v{minor}/deb/"
DEFAULT_K8S_PKG_REPO_RPM = "https://pkgs.k8s.io/core:/stable:/v{minor}/rpm/"
DEFAULT_K8S_IMAGE_REGISTRY = "registry.k8s.io"
DEFAULT_CRI_REPO_RPM = "https://download.docker.com/linux/centos/docker-ce.repo"

_REGISTRY_HOST_RE = re.compile(
    r"^(?:localhost|[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)"
    r"(?::[0-9]{1,5})?$"
)
_REGISTRY_PATH_SEGMENT_RE = re.compile(
    r"^[a-z0-9]+(?:[._-]+[a-z0-9]+)*$"
)
_CERTIFICATE_BLOCK_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----\s+"
    r"[A-Za-z0-9+/=\r\n]+\s+"
    r"-----END CERTIFICATE-----",
    re.MULTILINE,
)


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
    registry_auth_host: str
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
        for lower, upper, value in (
            ("http_proxy", "HTTP_PROXY", self.http_proxy),
            ("https_proxy", "HTTPS_PROXY", self.https_proxy),
            ("no_proxy", "NO_PROXY", self.no_proxy),
        ):
            if value:
                quoted = shlex.quote(value)
                lines.append(f"export {lower}={quoted}")
                lines.append(f"export {upper}={quoted}")
        return "\n".join(lines)

    def registry_hosts(self) -> List[str]:
        """Distinct registry authorities used by Kubernetes, CNI, and add-ons."""
        hosts: List[str] = []
        for prefix in (
            self.k8s_image_registry,
            self.cni_image_registry,
            self.addon_image_registry,
        ):
            host = registry_authority(prefix)
            if host and host not in hosts:
                hosts.append(host)
        return hosts


def registry_authority(prefix: str) -> str:
    """Return ``host[:port]`` from a validated image prefix."""
    return str(prefix or "").strip().split("/", 1)[0]


def _normalize_registry_prefix(value: Any, field: str) -> Optional[str]:
    """Validate a Docker image prefix such as ``nexus:5000/kubernetes``.

    These values are interpolated into kubeadm arguments and YAML image names,
    so schemes, credentials, shell metacharacters, tags, and digests are
    rejected rather than escaped into surprising semantics.
    """
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return None
    if "://" in raw:
        raise ValueError(
            f"{field} must be an image prefix without http:// or https://."
        )
    if any(char.isspace() or ord(char) < 32 for char in raw):
        raise ValueError(f"{field} must not contain whitespace or control characters.")
    if any(char in raw for char in """'\"`$;&|<>?#@\\ """):
        raise ValueError(f"{field} contains an unsupported character.")

    authority, *segments = raw.split("/")
    if not _REGISTRY_HOST_RE.fullmatch(authority):
        raise ValueError(
            f"{field} must start with host[:port], optionally followed by a path."
        )
    if ":" in authority:
        try:
            port = int(authority.rsplit(":", 1)[1])
        except ValueError as exc:
            raise ValueError(f"{field} has an invalid port.") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"{field} port must be 1-65535.")
    if any(not segment or not _REGISTRY_PATH_SEGMENT_RE.fullmatch(segment)
           for segment in segments):
        raise ValueError(
            f"{field} path segments must use lowercase OCI repository names."
        )
    return raw


def _normalize_proxy_url(value: Any, field: str) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if any(char.isspace() or ord(char) < 32 for char in raw):
        raise ValueError(f"{field} must not contain whitespace or control characters.")
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"{field} must be a valid http:// or https:// URL.")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError(
            f"{field} must not contain credentials; use a credential-free "
            "forward proxy URL."
        )
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError(f"{field} must contain only scheme, host, and optional port.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field} has an invalid port.") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{field} port must be 1-65535.")
    return raw


def _normalize_no_proxy(value: Any) -> Optional[str]:
    """Validate NO_PROXY before it is written to shell and systemd environments."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) > 4096:
        raise ValueError("noProxy must be 4096 characters or fewer.")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("noProxy must not contain control characters.")
    return raw


def _proxy_display(value: Any) -> Optional[str]:
    """Return a legacy proxy URL without userinfo.

    New writes reject authenticated proxy URLs because process environment
    variables are not a safe credential channel. This still prevents an older
    row from disclosing its password through the read-only profile API.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    if any(char.isspace() or ord(char) < 32 for char in raw):
        return None
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return None
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host + (f":{port}" if port is not None else "")
        return urlunsplit((parsed.scheme, netloc, "", "", ""))
    except (TypeError, ValueError):
        return None


def _repo_display(value: Any, field: str) -> Optional[str]:
    """Expose only source URLs that pass the current no-secret policy."""
    try:
        return _normalize_repo_url(value, field)
    except ValueError:
        return None


def _registry_display(value: Any, field: str) -> Optional[str]:
    """Expose only normalized image prefixes from legacy profile rows."""
    try:
        return _normalize_registry_prefix(value, field)
    except ValueError:
        return None


def _normalize_repo_url(value: Any, field: str) -> Optional[str]:
    """Validate a package repository URL before it reaches root shell scripts."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if any(char.isspace() or ord(char) < 32 for char in raw):
        raise ValueError(f"{field} must not contain whitespace or control characters.")
    if raw.count("{minor}") > 1 or re.search(r"[{}]", raw.replace("{minor}", "")):
        raise ValueError(f"{field} contains an unsupported template placeholder.")
    parsed = urlsplit(raw.replace("{minor}", "1.32"))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"{field} must be a valid http:// or https:// URL.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field} must not contain embedded credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field} must not contain a URL query or fragment.")
    if any(char in raw for char in "\"'`$;&|<>\x00"):
        raise ValueError(f"{field} contains an unsupported character.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field} has an invalid port.") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{field} port must be 1-65535.")
    return raw


def _normalize_ca_bundle(value: Any) -> Optional[str]:
    """Parse and canonicalize a PEM certificate bundle with no extra text."""
    raw = str(value or "").strip()
    if not raw:
        return None
    blocks = _CERTIFICATE_BLOCK_RE.findall(raw)
    remainder = _CERTIFICATE_BLOCK_RE.sub("", raw)
    if not blocks or remainder.strip():
        raise ValueError(
            "extraCaCertsPem must contain only PEM-encoded certificates."
        )
    certificates = []
    try:
        for block in blocks:
            certificates.append(
                x509.load_pem_x509_certificate(block.encode("ascii"))
            )
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError(
            "extraCaCertsPem contains an invalid PEM certificate."
        ) from exc
    return "".join(
        certificate.public_bytes(Encoding.PEM).decode("ascii")
        for certificate in certificates
    ).strip()


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
        registry_auth_host="",
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
    repo_mode = str(row.repo_mode or "internet").strip()
    if repo_mode not in _REPO_MODES:
        raise ValueError("repoMode must be 'internet', 'mirror', or 'offline'.")
    k8s_registry = _normalize_registry_prefix(
        row.k8s_image_registry, "k8sImageRegistry"
    )
    cni_registry = _normalize_registry_prefix(
        row.cni_image_registry, "cniImageRegistry"
    )
    addon_registry = _normalize_registry_prefix(
        row.addon_image_registry, "addonImageRegistry"
    )
    explicit_hosts = {
        registry_authority(prefix)
        for prefix in (k8s_registry, cni_registry, addon_registry)
        if prefix
    }
    username = row.registry_username or ""
    if row.registry_password_cipher and not secret_encryption_key_configured():
        raise ValueError(
            "Registry credentials require ALERT_ROUTING_SECRET_KEY or "
            "JWT_SECRET_KEY to be configured securely."
        )
    password = decrypt_secret(row.registry_password_cipher or "")
    if bool(username) != bool(password):
        raise ValueError(
            "registryUsername and registryPassword must be configured together."
        )
    if username and len(explicit_hosts) != 1:
        raise ValueError(
            "Registry credentials must be bound to one explicit registry authority."
        )
    k8s_pkg_repo_url = _normalize_repo_url(
        row.k8s_pkg_repo_url, "k8sPkgRepoUrl"
    ) or ""
    offline_bundle_path = str(row.offline_bundle_path or "").strip()
    if repo_mode == "mirror" and not k8s_pkg_repo_url:
        raise ValueError("mirror mode requires k8sPkgRepoUrl.")
    if repo_mode == "offline" and not offline_bundle_path:
        raise ValueError("offline mode requires offlineBundlePath.")
    return ResolvedProfile(
        repo_mode=repo_mode,
        k8s_pkg_repo_url=k8s_pkg_repo_url,
        k8s_pkg_gpg_key_url=_normalize_repo_url(
            row.k8s_pkg_gpg_key_url, "k8sPkgGpgKeyUrl"
        ) or "",
        cri_pkg_repo_url=_normalize_repo_url(
            row.cri_pkg_repo_url, "criPkgRepoUrl"
        ) or "",
        k8s_image_registry=k8s_registry or DEFAULT_K8S_IMAGE_REGISTRY,
        cni_image_registry=cni_registry or "",
        addon_image_registry=addon_registry or "",
        registry_username=username,
        registry_password=password,
        registry_auth_host=next(iter(explicit_hosts), "") if username else "",
        http_proxy=_normalize_proxy_url(row.http_proxy, "httpProxy") or "",
        https_proxy=_normalize_proxy_url(row.https_proxy, "httpsProxy") or "",
        no_proxy=_normalize_no_proxy(row.no_proxy) or "",
        extra_ca_certs_pem=_normalize_ca_bundle(row.extra_ca_certs_pem) or "",
        offline_bundle_path=offline_bundle_path,
        offline_bundle_checksum=row.offline_bundle_checksum or "",
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def serialize(row: BuildProfile) -> Dict[str, Any]:
    k8s_pkg_repo_url = _repo_display(row.k8s_pkg_repo_url, "k8sPkgRepoUrl")
    k8s_pkg_gpg_key_url = _repo_display(
        row.k8s_pkg_gpg_key_url, "k8sPkgGpgKeyUrl"
    )
    cri_pkg_repo_url = _repo_display(row.cri_pkg_repo_url, "criPkgRepoUrl")
    k8s_image_registry = _registry_display(
        row.k8s_image_registry, "k8sImageRegistry"
    )
    cni_image_registry = _registry_display(
        row.cni_image_registry, "cniImageRegistry"
    )
    addon_image_registry = _registry_display(
        row.addon_image_registry, "addonImageRegistry"
    )
    return {
        "id": row.id,
        "name": row.name,
        "repoMode": row.repo_mode,
        "k8sPkgRepoUrl": k8s_pkg_repo_url,
        "k8sPkgGpgKeyUrl": k8s_pkg_gpg_key_url,
        "criPkgRepoUrl": cri_pkg_repo_url,
        "k8sImageRegistry": k8s_image_registry,
        "cniImageRegistry": cni_image_registry,
        "addonImageRegistry": addon_image_registry,
        "registryUsername": row.registry_username,
        "registryPasswordConfigured": bool(row.registry_password_cipher),
        "httpProxy": _proxy_display(row.http_proxy),
        "httpsProxy": _proxy_display(row.https_proxy),
        "noProxy": row.no_proxy,
        "extraCaConfigured": bool(row.extra_ca_certs_pem),
        "imageProxyEnabled": bool(
            k8s_image_registry
            or cni_image_registry
            or addon_image_registry
        ),
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
        "registryUsername": "registry_username",
        "offlineBundlePath": "offline_bundle_path",
        "offlineBundleChecksum": "offline_bundle_checksum",
    }
    for key, attr in text_fields.items():
        if key in payload:
            setattr(row, attr, str(payload.get(key) or "").strip() or None)

    for key, attr in (
        ("k8sPkgRepoUrl", "k8s_pkg_repo_url"),
        ("k8sPkgGpgKeyUrl", "k8s_pkg_gpg_key_url"),
        ("criPkgRepoUrl", "cri_pkg_repo_url"),
    ):
        if key in payload:
            setattr(row, attr, _normalize_repo_url(payload.get(key), key))

    for key, attr in (
        ("k8sImageRegistry", "k8s_image_registry"),
        ("cniImageRegistry", "cni_image_registry"),
        ("addonImageRegistry", "addon_image_registry"),
    ):
        if key in payload:
            setattr(row, attr, _normalize_registry_prefix(payload.get(key), key))

    for key, attr in (("httpProxy", "http_proxy"), ("httpsProxy", "https_proxy")):
        if key in payload:
            setattr(row, attr, _normalize_proxy_url(payload.get(key), key))

    if "noProxy" in payload:
        row.no_proxy = _normalize_no_proxy(payload.get("noProxy"))

    if "extraCaCertsPem" in payload:
        row.extra_ca_certs_pem = _normalize_ca_bundle(
            payload.get("extraCaCertsPem")
        )

    password = payload.get("registryPassword")
    if password:
        if not secret_encryption_key_configured():
            raise ValueError(
                "Registry credentials require ALERT_ROUTING_SECRET_KEY or "
                "JWT_SECRET_KEY to be configured securely."
            )
        row.registry_password_cipher = encrypt_secret(str(password))
    if payload.get("clearRegistryPassword"):
        row.registry_password_cipher = None

    if bool(row.registry_username) != bool(row.registry_password_cipher):
        raise ValueError(
            "registryUsername and registryPassword must be configured together."
        )
    if row.registry_username:
        authorities = {
            registry_authority(prefix)
            for prefix in (
                row.k8s_image_registry,
                row.cni_image_registry,
                row.addon_image_registry,
            )
            if prefix
        }
        if len(authorities) != 1:
            raise ValueError(
                "Authenticated image proxy profiles must use one registry "
                "authority for Kubernetes, CNI, and add-on prefixes."
            )

    if repo_mode == "mirror":
        if not row.k8s_pkg_repo_url:
            raise ValueError("mirror mode requires k8sPkgRepoUrl.")
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
    from ...models import ClusterBuild

    if ClusterBuild.query.filter_by(build_profile_id=profile_id).count():
        raise ValueError(
            "Build profiles are immutable once referenced by a cluster build. "
            "Create a new profile so preflight and retries use reviewed settings."
        )
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
