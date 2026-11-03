"""Google Cloud: Compute Engine VMs for self-managed clusters, GKE for managed.

Implements the adapter contract in ``base``. Deliberate choices, each one a
failure avoided:

  * **No Google SDK.** A service-account JSON key signs a JWT (RS256, PyJWT)
    that ``oauth2.googleapis.com`` trades for an access token (the JWT-bearer
    grant google-auth uses). The token is cached per key until five minutes
    before it expires, and only ever sent to Google API hosts — never to a URL
    taken from a build's spec.
  * **Network objects by self link.** Networks, subnetworks and images are
    identified by their ``selfLink``, which the provider accepts as-is and
    which already names the project they live in (a Shared VPC host project,
    a public image project).
  * **Keys, not passwords.** The Linux images Google publishes create users
    from the ``ssh-keys`` metadata through the guest agent (with password-less
    sudo); most of them have no cloud-init. So a build whose SSH credential is
    a password cannot run on Google Cloud, and says so. ``enable-oslogin`` is
    switched off per VM, or OS Login would ignore the metadata keys.
  * **No public address.** ``network_interface`` never has an
    ``access_config``; KubeSight reaches the VMs privately.
  * **can_ip_forward** on every VM, so a CNI that routes pod traffic without
    encapsulation is not dropped by Google's anti-spoofing.

The API load balancer is an internal passthrough network load balancer
(regional backend service + forwarding rule on TCP 6443) in front of an
unmanaged instance group of the control planes. **Hairpin:** Google's guest
agent adds the forwarding rule's address as a *local* route on every backend
VM, so a control plane that connects to the load balancer's address is
answered by itself, not by another control plane. That is harmless once the
VM's own API server runs, but a control plane *joining* the cluster must not
reach for the endpoint through itself; ``HAIRPIN_LOCAL_ROUTE`` tells the
Cluster Builder's executor to work around it during control-plane joins.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from . import base
from .base import Account, CloudError, ManagedContext, VmContext

PROVIDER = "gcp"
LABEL = "Google Cloud"
MANAGED_LABEL = "Google GKE"
TOFU_PROVIDER = {"name": "google", "source": "hashicorp/google", "version": "8.9.0"}
VM_RESOURCE_TYPE = "google_compute_instance"
VM_ADDRESS = f"{VM_RESOURCE_TYPE}.node"
# A backend VM of an internal passthrough load balancer answers the load
# balancer's address itself (see the module docstring).
HAIRPIN_LOCAL_ROUTE = True

SCOPE = "https://www.googleapis.com/auth/cloud-platform"
COMPUTE = "https://compute.googleapis.com/compute/v1"
CONTAINER = "https://container.googleapis.com/v1"
# Where Google's health checks for internal passthrough load balancers come from.
HEALTH_CHECK_RANGES = ["35.191.0.0/16", "130.211.0.0/22"]
DISK_TYPES = ("pd-balanced", "pd-ssd", "pd-standard")
DEFAULT_MASTER_CIDR = "172.16.0.32/28"
# Token lifetime asked for, and how long before expiry a cached one is renewed.
_TOKEN_SECONDS = 3600
_TOKEN_MARGIN_S = 300

FIELDS = [
    base.field(
        "projectId", "Project ID", required=False,
        help="The project VMs and clusters are created in. Empty: the key's own project.",
        placeholder="my-project-123456",
    ),
    base.field(
        "serviceAccountKey", "Service account key (JSON)", secret=True, kind="textarea",
        help="A JSON key of a service account with Compute Admin, Kubernetes Engine Admin "
             "and Service Account User on the project.",
        placeholder='{"type": "service_account", ...}',
    ),
]

# Machine types offered when sizes are turned into a type: (id, vCPU, GB).
_STATIC_MACHINE_TYPES: List[Tuple[str, int, int]] = (
    [(f"e2-standard-{n}", n, n * 4) for n in (2, 4, 8, 16, 32)]
    + [(f"n2-standard-{n}", n, n * 4) for n in (2, 4, 8, 16, 32, 48, 64, 80)]
    + [(f"n2-highmem-{n}", n, n * 8) for n in (2, 4, 8, 16, 32, 48, 64, 80)]
    + [(f"c3-standard-{n}", n, n * 4) for n in (4, 8, 22, 44, 88)]
)
MACHINE_TYPES: List[Dict[str, Any]] = [
    {"id": type_id, "cpu": cpu, "memoryGb": memory, "flexible": False, "note": ""}
    for type_id, cpu, memory in _STATIC_MACHINE_TYPES
]
_MACHINE_TYPE_RE = re.compile(r"^[a-z0-9]+-[a-z0-9-]+$")
_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_ZONE_RE = re.compile(r"^[a-z]+-[a-z]+[0-9]+-[a-z]$")
# Compute Engine instance / firewall names, GKE cluster names.
_GCE_NAME_RE = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")
_GKE_NAME_MAX = 40

# Public image families offered for new VMs: (project, family, osFamily).
IMAGE_FAMILIES = [
    ("ubuntu-os-cloud", "ubuntu-2404-lts-amd64", "debian"),
    ("ubuntu-os-cloud", "ubuntu-2204-lts", "debian"),
    ("debian-cloud", "debian-12", "debian"),
    ("rhel-cloud", "rhel-9", "rhel"),
    ("rocky-linux-cloud", "rocky-linux-9", "rhel"),
]
STATIC_KUBERNETES_VERSIONS = ["1.34", "1.33", "1.32"]


# ---------------------------------------------------------------------------
# The account
# ---------------------------------------------------------------------------

def _parse_key(text: str) -> Dict[str, Any]:
    try:
        key = json.loads(text)
    except ValueError as exc:
        raise ValueError("The service account key is not valid JSON.") from exc
    if not isinstance(key, dict) or key.get("type") != "service_account":
        raise ValueError('The key must be a service account JSON key ("type": "service_account").')
    for name in ("client_email", "private_key", "token_uri"):
        if not str(key.get(name) or "").strip():
            raise ValueError(f"The service account key has no {name}.")
    if "PRIVATE KEY" not in key["private_key"]:
        raise ValueError("The service account key's private_key is not a PEM key.")
    _check_token_uri(key["token_uri"])
    return key


def _check_token_uri(uri: str) -> None:
    parts = urllib.parse.urlsplit(str(uri))
    host = parts.hostname or ""
    if parts.scheme != "https" or not (host.endswith(".googleapis.com") or host == "accounts.google.com"):
        raise ValueError("The key's token_uri is not a Google address.")


def normalize_account(settings: Dict[str, Any], secrets: Dict[str, Any],
                      previous_settings: Optional[Dict[str, Any]] = None,
                      previous_secrets: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    settings = settings if isinstance(settings, dict) else {}
    secrets = secrets if isinstance(secrets, dict) else {}
    text = base.keep_or_require(secrets, previous_secrets or {}, "serviceAccountKey", "Service account key")
    key = _parse_key(text)
    project = str(settings.get("projectId") or "").strip() or str(key.get("project_id") or "").strip()
    if not project:
        raise ValueError("Project ID is required (the key names no project).")
    if not _PROJECT_RE.fullmatch(project):
        raise ValueError("Project ID must be 6–30 lowercase letters, digits and hyphens, starting with a letter.")
    return (
        {"projectId": project, "clientEmail": key["client_email"]},
        {"serviceAccountKey": text},
    )


def _key(account: Account) -> Dict[str, Any]:
    text = (account.secrets or {}).get("serviceAccountKey") or ""
    try:
        return _parse_key(text)
    except ValueError as exc:
        raise CloudError(str(exc)) from exc


def _project(account: Account) -> str:
    project = (account.settings or {}).get("projectId") or _key(account).get("project_id")
    if not project:
        raise CloudError("The account has no project.")
    return str(project)


def identity_label(account: Account) -> str:
    email = (account.settings or {}).get("clientEmail")
    if not email:
        try:
            email = _key(account)["client_email"]
        except CloudError:
            email = "service account"
    return f"{email} · project {(account.settings or {}).get('projectId') or '?'}"


# ---------------------------------------------------------------------------
# Access tokens (JWT-bearer grant)
# ---------------------------------------------------------------------------

_token_lock = threading.Lock()
_token_cache: Dict[Tuple[str, str], Tuple[str, float]] = {}


def _clear_token_cache() -> None:
    with _token_lock:
        _token_cache.clear()


def jwt_assertion(key: Dict[str, Any], *, now: Optional[int] = None) -> str:
    """The signed assertion google-auth's service-account credentials send."""
    import jwt  # PyJWT

    issued = int(now if now is not None else time.time())
    claims = {
        "iss": key["client_email"],
        "scope": SCOPE,
        "aud": key["token_uri"],
        "iat": issued,
        "exp": issued + _TOKEN_SECONDS,
    }
    headers = {"kid": key["private_key_id"]} if key.get("private_key_id") else None
    try:
        return jwt.encode(claims, key["private_key"], algorithm="RS256", headers=headers)
    except (ValueError, TypeError) as exc:
        raise CloudError("The service account key's private key could not sign a request.") from exc


def _error_text(status: int, body: bytes) -> str:
    data = base.json_body(body)
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str):
            return str(data.get("error_description") or error)
    return f"HTTP {status}"


def access_token(account: Account) -> str:
    key = _key(account)
    cache_key = (key["client_email"], str(key.get("private_key_id") or ""))
    with _token_lock:
        cached = _token_cache.get(cache_key)
        if cached and cached[1] - _TOKEN_MARGIN_S > time.time():
            return cached[0]
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt_assertion(key),
    }).encode("ascii")
    status, _, raw = base.http(
        "POST", key["token_uri"],
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        body=body, ca_pem=account.ca_pem,
    )
    data = base.json_body(raw)
    if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        raise CloudError(f"Google refused the service account key: {_error_text(status, raw)}")
    token = str(data["access_token"])
    expires = time.time() + int(data.get("expires_in") or _TOKEN_SECONDS)
    with _token_lock:
        _token_cache[cache_key] = (token, expires)
    return token


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

_API_PREFIXES = (
    "https://compute.googleapis.com/compute/",
    "https://www.googleapis.com/compute/",
    "https://container.googleapis.com/",
)


def _api_url(link: str) -> str:
    """A self link from a spec, accepted only on a Google API host (the
    request carries the account's token)."""
    link = str(link or "").strip()
    if link.startswith("projects/"):
        return f"{COMPUTE}/{link}"
    if any(link.startswith(prefix) for prefix in _API_PREFIXES):
        return link
    raise CloudError(f"{link or 'An empty link'} is not a Google Compute resource.")


def _get(account: Account, url: str, *, what: str = "") -> Tuple[int, Any]:
    status, _, raw = base.http(
        "GET", url,
        headers={"Authorization": f"Bearer {access_token(account)}", "Accept": "application/json"},
        ca_pem=account.ca_pem,
    )
    if status == 200:
        return status, base.json_body(raw)
    if status == 404:
        return status, None
    raise CloudError(f"Google Cloud refused{' ' + what if what else ''}: {_error_text(status, raw)}")


def _list(account: Account, url: str, *, what: str = "") -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    token = None
    for _ in range(50):
        query = {"maxResults": "500"}
        if token:
            query["pageToken"] = token
        status, data = _get(account, f"{url}?{urllib.parse.urlencode(query)}", what=what)
        if status == 404 or not isinstance(data, dict):
            raise CloudError(f"Google Cloud could not find {what or url}.")
        items.extend(item for item in data.get("items") or [] if isinstance(item, dict))
        token = data.get("nextPageToken")
        if not token:
            break
    return items


def _tail(link: str) -> str:
    return str(link or "").rstrip("/").rsplit("/", 1)[-1]


def test(account: Account) -> str:
    project = _project(account)
    status, data = _get(account, f"{COMPUTE}/projects/{project}", what=f"reading project {project}")
    if status == 404 or not data:
        raise CloudError(f"Project {project} does not exist, or the service account cannot see it.")
    return f"{_key(account)['client_email']} · project {project}"


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def regions(account: Account) -> List[Dict[str, str]]:
    project = _project(account)
    items = _list(account, f"{COMPUTE}/projects/{project}/regions", what="listing regions")
    return sorted(
        ({"id": item["name"], "name": item["name"]} for item in items
         if item.get("name") and item.get("status", "UP") == "UP"),
        key=lambda item: item["id"],
    )


def _minor_versions(versions: List[str]) -> List[str]:
    out: List[str] = []
    for version in versions:
        match = re.match(r"^(\d+)\.(\d+)", str(version))
        if match and f"{match.group(1)}.{match.group(2)}" not in out:
            out.append(f"{match.group(1)}.{match.group(2)}")
    return sorted(out, key=lambda v: tuple(int(p) for p in v.split(".")), reverse=True)


def inventory(account: Account, region: str, scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    project = _project(account)
    out = base.empty_inventory(region)
    zones = _list(account, f"{COMPUTE}/projects/{project}/zones", what="listing zones")
    out["zones"] = sorted(
        ({"id": z["name"], "name": z["name"]} for z in zones
         if _tail(z.get("region", "")) == region and z.get("status") == "UP"),
        key=lambda z: z["id"],
    )
    if not out["zones"]:
        raise CloudError(f"Region {region} has no available zones in project {project}.")

    networks = _list(account, f"{COMPUTE}/projects/{project}/global/networks", what="listing networks")
    subnets = _list(account, f"{COMPUTE}/projects/{project}/regions/{region}/subnetworks",
                    what="listing subnetworks")
    by_network: Dict[str, List[Dict[str, Any]]] = {}
    for subnet in subnets:
        by_network.setdefault(subnet.get("network", ""), []).append({
            "id": subnet.get("selfLink"),
            "name": subnet.get("name"),
            "cidr": subnet.get("ipCidrRange") or "",
            "zone": None,  # subnetworks are regional
        })
    out["networks"] = sorted(
        ({
            "id": net.get("selfLink"),
            "name": net.get("name"),
            "cidr": net.get("IPv4Range") or "",  # legacy networks only
            "subnets": sorted(by_network.get(net.get("selfLink"), []), key=lambda s: s["name"] or ""),
        } for net in networks),
        key=lambda n: n["name"] or "",
    )

    for image_project, family, os_family in IMAGE_FAMILIES:
        try:
            status, image = _get(
                account, f"{COMPUTE}/projects/{image_project}/global/images/family/{family}",
                what=f"reading image family {family}",
            )
        except CloudError:
            continue  # one family Google retired never hides the others
        if status != 200 or not isinstance(image, dict) or not image.get("selfLink"):
            continue
        out["images"].append({
            "id": image["selfLink"],
            "name": image.get("name") or family,
            "osFamily": os_family,
            "description": image.get("description") or family,
            "createdAt": image.get("creationTimestamp"),
        })

    out["machineTypes"] = [dict(item) for item in MACHINE_TYPES]
    try:
        _, config = _get(account, f"{CONTAINER}/projects/{project}/locations/{region}/serverConfig",
                         what="reading GKE versions")
        versions = _minor_versions((config or {}).get("validMasterVersions") or [])
    except CloudError:
        versions = []
    out["kubernetesVersions"] = versions or list(STATIC_KUBERNETES_VERSIONS)
    return out


def simulated_regions() -> List[Dict[str, str]]:
    return [
        {"id": "europe-west1", "name": "europe-west1"},
        {"id": "me-central1", "name": "me-central1"},
    ]


_SIM_PROJECT = "kubesight-demo"
_SIM_SUBNETS = (("apps", "10.10.1.0/24"), ("data", "10.10.2.0/24"))


def simulated_inventory(region: str, scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = base.empty_inventory(region)
    out["zones"] = [{"id": f"{region}-{letter}", "name": f"{region}-{letter}"} for letter in "abc"]
    network = f"https://www.googleapis.com/compute/v1/projects/{_SIM_PROJECT}/global/networks/kubesight-vpc"
    out["networks"] = [{
        "id": network, "name": "kubesight-vpc", "cidr": "",
        "subnets": [
            {"id": f"https://www.googleapis.com/compute/v1/projects/{_SIM_PROJECT}/regions/{region}/subnetworks/{name}",
             "name": name, "cidr": cidr, "zone": None}
            for name, cidr in _SIM_SUBNETS
        ],
    }]
    out["images"] = [
        {"id": f"https://www.googleapis.com/compute/v1/projects/{project}/global/images/{family}-v20261020",
         "name": f"{family}-v20261020", "osFamily": os_family, "description": family,
         "createdAt": "2026-10-20T00:00:00.000-07:00"}
        for project, family, os_family in IMAGE_FAMILIES
    ]
    out["machineTypes"] = [dict(item) for item in MACHINE_TYPES]
    out["kubernetesVersions"] = list(STATIC_KUBERNETES_VERSIONS)
    return out


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------

def normalize_placement(raw: Dict[str, Any], kind: str, previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    previous = previous or {}
    region = str(raw.get("region") or previous.get("region") or "").strip()
    managed = kind == "managed"
    regional = bool(raw.get("regional", previous.get("regional", False))) if managed else False

    zone = str(raw.get("zone") or previous.get("zone") or "").strip()
    if zone:
        if not _ZONE_RE.fullmatch(zone):
            raise ValueError(f"{zone} is not a Google Cloud zone.")
        if region and not zone.startswith(region + "-"):
            raise ValueError(f"Zone {zone} is not in region {region}.")
    elif not regional:
        raise ValueError("Choose the zone the machines go in.")

    disk_type = str(raw.get("diskType") or previous.get("diskType") or "pd-balanced").strip()
    if disk_type not in DISK_TYPES:
        raise ValueError(f"Disk type must be one of {', '.join(DISK_TYPES)}.")

    out: Dict[str, Any] = {"zone": zone or None, "diskType": disk_type}
    if managed:
        master = str(raw.get("masterCidr") or previous.get("masterCidr") or DEFAULT_MASTER_CIDR).strip()
        try:
            network = ipaddress.ip_network(master, strict=True)
        except ValueError as exc:
            raise ValueError(f"Control plane range {master} is not a CIDR (like {DEFAULT_MASTER_CIDR}).") from exc
        if network.version != 4 or network.prefixlen != 28 or not network.is_private:
            raise ValueError("The GKE control plane range must be a private IPv4 /28.")
        out["masterCidr"] = str(network)
        out["regional"] = regional
    return out


def _type_size(type_id: str) -> Optional[Tuple[int, float]]:
    """vCPU and GB of a type from Google's naming (n2-standard-8, e2-custom-4-16384)."""
    for item in MACHINE_TYPES:
        if item["id"] == type_id:
            return int(item["cpu"]), float(item["memoryGb"])
    match = re.fullmatch(r"[a-z0-9]+-custom-(\d+)-(\d+)(-ext)?", type_id)
    if match:
        return int(match.group(1)), int(match.group(2)) / 1024
    match = re.fullmatch(r"[a-z0-9]+-(standard|highmem|highcpu)-(\d+)", type_id)
    if match:
        cpu = int(match.group(2))
        return cpu, cpu * {"standard": 4, "highmem": 8, "highcpu": 1}[match.group(1)]
    return None


def resolve_machine_type(size: Dict[str, Any], spec: Dict[str, Any],
                         catalog: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    cpu = int(size.get("cpu") or 0)
    memory = float(size.get("memoryGb") or 0)
    override = str(((spec or {}).get("machineTypes") or {}).get(size.get("role")) or "").strip()
    if override:
        if not _MACHINE_TYPE_RE.fullmatch(override):
            raise ValueError(f"{override} is not a Google Cloud machine type.")
        known = _type_size(override)
        if known is None:
            return {"type": override, "cpu": cpu, "memoryGb": memory,
                    "note": "Not in KubeSight's list; Google checks it when the plan is applied."}
        if known[0] < cpu or known[1] < memory:
            raise ValueError(
                f"{override} has {known[0]} vCPU and {known[1]:g} GB, less than the "
                f"{cpu} vCPU and {memory:g} GB asked for."
            )
        return {"type": override, "cpu": known[0], "memoryGb": known[1]}
    found = base.smallest_fitting(catalog or MACHINE_TYPES, cpu, memory, prefer=["e2", "n2", "c3"])
    if found is None:
        raise ValueError(f"No Google Cloud machine type has {cpu} vCPU and {memory:g} GB.")
    return {"type": found["id"], "cpu": int(found["cpu"]), "memoryGb": float(found["memoryGb"])}


def plan_checks(account: Account, spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """A fresh read of what the plan points at. Anything gone stops the plan
    (CloudError); what is there comes back as "ok" checks."""
    project = _project(account)
    checks: List[Dict[str, str]] = []
    region = spec.get("region")
    status, data = _get(account, f"{COMPUTE}/projects/{project}/regions/{region}", what=f"reading region {region}")
    if status == 404 or not data:
        raise CloudError(f"Region {region} is not available in project {project}.")
    if spec.get("zone"):
        status, data = _get(account, f"{COMPUTE}/projects/{project}/zones/{spec['zone']}",
                            what=f"reading zone {spec['zone']}")
        if status == 404 or not data:
            raise CloudError(f"Zone {spec['zone']} is not available in project {project}.")
        if data.get("status") != "UP":
            raise CloudError(f"Zone {spec['zone']} is {data.get('status') or 'not up'}.")
    checks.append(base.check("ok", "Region", f"{region}{' · ' + spec['zone'] if spec.get('zone') else ''}"))
    for key, name_key, label in (("networkId", "networkName", "Network"), ("subnetId", "subnetName", "Subnetwork")):
        status, data = _get(account, _api_url(spec.get(key)), what=f"reading {label.lower()}")
        if status == 404 or not data:
            raise CloudError(f"{label} {spec.get(name_key) or _tail(spec.get(key))} no longer exists.")
        detail = data.get("name") or ""
        if key == "subnetId" and data.get("ipCidrRange"):
            detail += f" · {data['ipCidrRange']}"
        checks.append(base.check("ok", label, detail))
    image = (spec.get("image") or {}).get("id")
    if spec.get("kind") != "managed" and image:
        status, data = _get(account, _api_url(image), what="reading the image")
        if status == 404 or not data:
            raise CloudError(f"Image {(spec.get('image') or {}).get('name') or _tail(image)} no longer exists.")
        if (data.get("deprecated") or {}).get("state") in ("DELETED", "OBSOLETE"):
            raise CloudError(f"Image {data.get('name')} is {data['deprecated']['state'].lower()}.")
        checks.append(base.check("ok", "Image", data.get("name") or _tail(image)))
    return checks


# ---------------------------------------------------------------------------
# main.tf.json
# ---------------------------------------------------------------------------

def _label_value(value: str) -> str:
    value = re.sub(r"[^a-z0-9_-]", "-", str(value).lower())[:63]
    return value


def _labels(raw: Dict[str, str]) -> Dict[str, str]:
    """Google's label rules: lowercase letters, digits, - and _; keys start
    with a letter; at most 63 characters."""
    out: Dict[str, str] = {}
    for key, value in (raw or {}).items():
        key = _label_value(key)
        if not key or not key[0].isalpha():
            key = f"k-{key}"[:63]
        out[key] = _label_value(value)
    return out


def network_tag(cluster_name: str) -> str:
    return f"kubesight-{cluster_name}"[:63]


def _resource_name(cluster_name: str, suffix: str) -> str:
    """Firewall rules, groups and load-balancer parts: must start with a letter."""
    return f"ks-{cluster_name}-{suffix}"[:63].rstrip("-")


def _provider(spec: Dict[str, Any], region: str) -> Dict[str, Any]:
    provider: Dict[str, Any] = {"region": region}
    if spec.get("projectId"):
        provider["project"] = spec["projectId"]
    return provider


def _terraform() -> Dict[str, Any]:
    return {
        "required_providers": {
            "google": {"source": TOFU_PROVIDER["source"], "version": f"= {TOFU_PROVIDER['version']}"}
        },
        "backend": {"http": {}},
    }


def _ssh_key_line(ssh: Dict[str, Any]) -> str:
    key = str(ssh.get("publicKey") or "").strip()
    if not key:
        raise ValueError(
            "Google Cloud VMs only take SSH keys (the guest agent ignores passwords). "
            "Give the build an SSH route whose credential is a private key."
        )
    username = str(ssh.get("username") or "").strip()
    if not username or username == "root":
        raise ValueError(
            "Google Cloud's images refuse root logins over SSH: give the build's SSH "
            "credential a normal user name (it gets password-less sudo)."
        )
    return base.tf_escape(f"{username}:{key}")


def _role_label(role: str) -> str:
    return {"controlPlane": "cp", "worker": "wk", "loadbalancer": "lb", "vm": "vm"}.get(role, "vm")


def render_vms(ctx: VmContext) -> Dict[str, Any]:
    spec = ctx.spec
    tag = network_tag(ctx.cluster_name)
    for node in ctx.nodes:
        if not _GCE_NAME_RE.fullmatch(node["name"]):
            raise ValueError(
                f"{node['name']} cannot name a Google Cloud VM: names start with a letter "
                "and have at most 63 lowercase letters, digits and hyphens."
            )
    zone = spec.get("zone")
    if not zone:
        raise ValueError("Choose the zone the machines go in.")
    image = (spec.get("image") or {}).get("id")
    if not image:
        raise ValueError("Choose the image the VMs start from.")

    for_each = {
        node["name"]: {
            "role": node["role"],
            "role_label": _role_label(node["role"]),
            "cpu": int(node["cpu"]),
            "memory_mb": int(float(node["memoryGb"]) * 1024),
            "disk_gb": int(node["diskGb"]),
            "machine_type": node["machineType"]["type"],
        }
        for node in ctx.nodes
    }
    labels = _labels(ctx.labels)
    instance = {
        "for_each": for_each,
        "name": "${each.key}",
        "zone": zone,
        "machine_type": "${each.value.machine_type}",
        "can_ip_forward": True,
        "allow_stopping_for_update": True,
        "deletion_protection": False,
        "tags": [tag],
        "labels": {**labels, "kubesight-role": "${each.value.role_label}"},
        "boot_disk": [{
            "auto_delete": True,
            "initialize_params": [{
                "image": image,
                "size": "${each.value.disk_gb}",
                "type": spec.get("diskType") or "pd-balanced",
            }],
        }],
        # No access_config: no public address, ever.
        "network_interface": [{"subnetwork": spec["subnetId"]}],
        "metadata": {
            "ssh-keys": _ssh_key_line(ctx.ssh),
            "enable-oslogin": "FALSE",
            # Images that do run cloud-init (Ubuntu) take the same user from here.
            "user-data": base.cloud_init(ctx.ssh),
        },
        "lifecycle": [{
            # A newer image in the family, or a key rotated in the metadata,
            # must never plan to rebuild a running VM.
            "ignore_changes": ["boot_disk[0].initialize_params[0].image", "metadata"],
        }],
    }

    sources = list(ctx.allowed_cidrs) or ([spec["subnetCidr"]] if spec.get("subnetCidr") else [])
    firewalls: Dict[str, Any] = {
        "cluster_internal": {
            "name": _resource_name(ctx.cluster_name, "internal"),
            "network": spec["networkId"],
            "description": f"KubeSight cluster {ctx.cluster_name}: every machine reaches every other.",
            "direction": "INGRESS",
            "source_tags": [tag],
            "target_tags": [tag],
            "allow": [{"protocol": "all"}],
        },
    }
    if sources:
        firewalls["admin"] = {
            "name": _resource_name(ctx.cluster_name, "admin"),
            "network": spec["networkId"],
            "description": f"KubeSight cluster {ctx.cluster_name}: SSH and the Kubernetes API from KubeSight's networks.",
            "direction": "INGRESS",
            "source_ranges": sources,
            "target_tags": [tag],
            "allow": [{"protocol": "tcp", "ports": [str(base.SSH_PORT), str(base.API_PORT)]}],
        }
    resources: Dict[str, Any] = {"google_compute_firewall": firewalls}
    if for_each:
        resources[VM_RESOURCE_TYPE] = {"node": instance}

    control_planes = [n["name"] for n in ctx.nodes if n["role"] == "controlPlane"]
    if ctx.load_balancer and control_planes:
        firewalls["health"] = {
            "name": _resource_name(ctx.cluster_name, "lb-health"),
            "network": spec["networkId"],
            "description": f"KubeSight cluster {ctx.cluster_name}: Google's load-balancer health checks.",
            "direction": "INGRESS",
            "source_ranges": list(HEALTH_CHECK_RANGES),
            "target_tags": [tag],
            "allow": [{"protocol": "tcp", "ports": [str(base.API_PORT)]}],
        }
        resources["google_compute_instance_group"] = {"control_planes": {
            "name": _resource_name(ctx.cluster_name, "cp"),
            "description": f"KubeSight cluster {ctx.cluster_name}: control planes behind the API load balancer.",
            "zone": zone,
            "instances": [f'${{{VM_ADDRESS}["{name}"].self_link}}' for name in control_planes],
        }}
        resources["google_compute_region_health_check"] = {"api": {
            "name": _resource_name(ctx.cluster_name, "api"),
            "region": ctx.region,
            "check_interval_sec": 5,
            "timeout_sec": 5,
            "healthy_threshold": 2,
            "unhealthy_threshold": 3,
            "tcp_health_check": [{"port": base.API_PORT}],
        }}
        resources["google_compute_region_backend_service"] = {"api": {
            "name": _resource_name(ctx.cluster_name, "api"),
            "region": ctx.region,
            "protocol": "TCP",
            "load_balancing_scheme": "INTERNAL",
            "health_checks": ["${google_compute_region_health_check.api.id}"],
            "backend": [{
                "group": "${google_compute_instance_group.control_planes.self_link}",
                "balancing_mode": "CONNECTION",
            }],
        }}
        resources["google_compute_forwarding_rule"] = {"api": {
            "name": _resource_name(ctx.cluster_name, "api"),
            "region": ctx.region,
            "load_balancing_scheme": "INTERNAL",
            "backend_service": "${google_compute_region_backend_service.api.id}",
            "ip_protocol": "TCP",
            "ports": [str(base.API_PORT)],
            "network": spec["networkId"],
            "subnetwork": spec["subnetId"],
            "labels": labels,
        }}

    return {
        "terraform": _terraform(),
        "provider": {"google": _provider(spec, ctx.region)},
        "resource": resources,
    }


def render_managed(ctx: ManagedContext) -> Dict[str, Any]:
    spec = ctx.spec
    name = ctx.cluster_name
    if len(name) > _GKE_NAME_MAX or not _GCE_NAME_RE.fullmatch(name):
        raise ValueError(
            f"{name} cannot name a GKE cluster: at most {_GKE_NAME_MAX} lowercase letters, "
            "digits and hyphens, starting with a letter."
        )
    if not ctx.allowed_cidrs:
        raise ValueError(
            "A private GKE cluster answers only the networks it is told about: give the "
            "networks KubeSight connects from (Allowed networks)."
        )
    location = ctx.region if spec.get("regional") else spec.get("zone")
    if not location:
        raise ValueError("Choose the zone the cluster goes in, or make it regional.")
    labels = _labels(ctx.labels)
    pool = ctx.node_pool
    cluster = {
        "name": name,
        "location": location,
        "network": spec["networkId"],
        "subnetwork": spec["subnetId"],
        "remove_default_node_pool": True,
        "initial_node_count": 1,
        "deletion_protection": False,
        "min_master_version": ctx.kubernetes_version,
        "networking_mode": "VPC_NATIVE",
        "ip_allocation_policy": [{}],
        "private_cluster_config": [{
            "enable_private_nodes": True,
            "enable_private_endpoint": True,
            "master_ipv4_cidr_block": spec.get("masterCidr") or DEFAULT_MASTER_CIDR,
        }],
        "master_authorized_networks_config": [{
            "cidr_blocks": [
                {"cidr_block": cidr, "display_name": f"kubesight-{index + 1}"}
                for index, cidr in enumerate(ctx.allowed_cidrs)
            ],
        }],
        "resource_labels": labels,
    }
    node_pool = {
        "name": "workers",
        "cluster": "${google_container_cluster.cluster.id}",
        "location": location,
        "node_count": int(pool["count"]),
        "node_config": [{
            "machine_type": pool["machineType"]["type"],
            "disk_size_gb": int(pool["diskGb"]),
            "disk_type": spec.get("diskType") or "pd-balanced",
            "oauth_scopes": [SCOPE],
            "labels": labels,
            "resource_labels": labels,
            "tags": [network_tag(name)],
        }],
    }
    return {
        "terraform": _terraform(),
        "provider": {"google": _provider(spec, ctx.region)},
        "resource": {
            "google_container_cluster": {"cluster": cluster},
            "google_container_node_pool": {"workers": node_pool},
        },
    }


def _ca_bundle(workdir: str, ca_pem: str) -> Optional[str]:
    """The account's CA plus the system roots (SSL_CERT_FILE replaces them)."""
    if not ca_pem or not ca_pem.strip():
        return None
    parts = [ca_pem.strip()]
    for system in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
        if os.path.exists(system):
            with open(system, encoding="utf-8", errors="ignore") as handle:
                parts.append(handle.read())
            break
    path = os.path.join(workdir, "gcp-ca-bundle.pem")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(parts) + "\n")
    return path


def environment(account: Account, workdir: str) -> Tuple[Dict[str, str], List[str]]:
    key_text = (account.secrets or {}).get("serviceAccountKey") or ""
    key = _key(account)
    env = {
        "GOOGLE_CREDENTIALS": key_text,
        "GOOGLE_PROJECT": _project(account),
        "GOOGLE_REGION": account.region or "",
        **base.env_proxy(),
    }
    bundle = _ca_bundle(workdir, account.ca_pem)
    if bundle:
        env["SSL_CERT_FILE"] = bundle
    hide = [text for text in (key_text.strip(), key.get("private_key", "").strip()) if text]
    return env, hide


# ---------------------------------------------------------------------------
# Reading the result
# ---------------------------------------------------------------------------

def _first(items: Any) -> Dict[str, Any]:
    return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}


def vm_facts(attrs: Dict[str, Any]) -> Dict[str, Any]:
    attrs = attrs or {}
    return {
        "id": str(attrs.get("instance_id") or attrs.get("id") or ""),
        "address": _first(attrs.get("network_interface")).get("network_ip") or "",
        "zone": _tail(attrs.get("zone") or ""),
        "machineType": _tail(attrs.get("machine_type") or ""),
    }


def _find(resources: List[Dict[str, Any]], rtype: str, name: str) -> Optional[Dict[str, Any]]:
    for item in resources or []:
        if item.get("type") == rtype and item.get("name") == name:
            return item.get("attributes") or {}
    return None


def endpoint_from_state(resources: List[Dict[str, Any]]) -> Optional[str]:
    attrs = _find(resources, "google_compute_forwarding_rule", "api")
    return (attrs or {}).get("ip_address") or None


def managed_access(account: Account, resources: List[Dict[str, Any]], spec: Dict[str, Any]) -> Dict[str, str]:
    attrs = _find(resources, "google_container_cluster", "cluster")
    if not attrs:
        raise CloudError("OpenTofu's state has no GKE cluster for this build.")
    host = _first(attrs.get("private_cluster_config")).get("private_endpoint") or attrs.get("endpoint")
    ca = _first(attrs.get("master_auth")).get("cluster_ca_certificate")
    if not host or not ca:
        raise CloudError("GKE has not reported the cluster's endpoint and CA yet.")
    return {"server": f"https://{host}", "caData": ca, "token": access_token(account)}


def describe_resource(rtype: str, rname: str, key: Any, attrs: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    attrs = attrs or {}
    if rtype == VM_RESOURCE_TYPE:
        params = _first(_first(attrs.get("boot_disk")).get("initialize_params"))
        parts = [_tail(attrs.get("machine_type") or "")]
        if params.get("size"):
            parts.append(f"{params['size']} GB disk")
        address = _first(attrs.get("network_interface")).get("network_ip")
        if address:
            parts.append(address)
        return "vm", str(key if key is not None else attrs.get("name") or rname), " · ".join(p for p in parts if p)
    if rtype == "google_compute_firewall":
        allowed = []
        for rule in attrs.get("allow") or []:
            ports = ",".join(rule.get("ports") or [])
            allowed.append(f"{rule.get('protocol')}{' ' + ports if ports else ''}")
        sources = attrs.get("source_ranges") or attrs.get("source_tags") or []
        detail = " · ".join(filter(None, [", ".join(allowed), f"from {', '.join(sources)}" if sources else ""]))
        return "firewall", f"Firewall rule {attrs.get('name') or rname}", detail
    if rtype == "google_compute_instance_group":
        return "loadbalancer", "Control planes behind the API load balancer", attrs.get("name") or ""
    if rtype in ("google_compute_region_health_check", "google_compute_region_backend_service"):
        what = "health check" if rtype.endswith("health_check") else "backend"
        return "loadbalancer", f"API load balancer {what}", attrs.get("name") or ""
    if rtype == "google_compute_forwarding_rule":
        return "loadbalancer", "API load balancer (internal, TCP 6443)", attrs.get("ip_address") or ""
    if rtype == "google_container_cluster":
        version = attrs.get("min_master_version") or ""
        return ("cluster", f"GKE cluster {attrs.get('name') or rname}",
                " · ".join(filter(None, [f"Kubernetes {version}" if version else "", "private endpoint"])))
    if rtype == "google_container_node_pool":
        config = _first(attrs.get("node_config"))
        return ("nodepool", f"Node pool {attrs.get('name') or rname}",
                f"{attrs.get('node_count', '?')} × {config.get('machine_type') or '?'}")
    return None


_SIM_CA = base64.b64encode(
    b"-----BEGIN CERTIFICATE-----\nU0lNVUxBVEVEIEdLRSBDQQ==\n-----END CERTIFICATE-----\n"
).decode("ascii")


def simulate_attributes(rtype: str, rname: str, key: Any, values: Dict[str, Any], seq: int) -> Dict[str, Any]:
    values = values or {}
    if rtype == VM_RESOURCE_TYPE:
        zone = values.get("zone") or "europe-west1-b"
        return {
            "name": key, "instance_id": str(7300000000000000000 + seq),
            "machine_type": values.get("machine_type"), "zone": zone,
            "self_link": f"{COMPUTE}/projects/{_SIM_PROJECT}/zones/{zone}/instances/{key}",
            "network_interface": [{"network_ip": base.simulated_address(_SIM_SUBNETS[0][1], seq)}],
            "boot_disk": [{"initialize_params": [{"size": values.get("disk_gb")}]}],
        }
    if rtype == "google_compute_forwarding_rule":
        return {"name": values.get("name") or rname,
                "ip_address": base.simulated_address(_SIM_SUBNETS[0][1], 240)}
    if rtype == "google_container_cluster":
        return {
            "name": values.get("name") or rname, "endpoint": "172.16.0.34",
            "min_master_version": values.get("min_master_version"),
            "private_cluster_config": [{"private_endpoint": "172.16.0.34"}],
            "master_auth": [{"cluster_ca_certificate": _SIM_CA}],
        }
    if rtype == "google_container_node_pool":
        return {"name": values.get("name") or rname, "node_count": values.get("node_count")}
    return {"name": values.get("name") or rname, "id": f"sim-{rtype}-{seq}"}
