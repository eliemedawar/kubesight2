"""Oracle Cloud by OKE workload identity: no OCI user, no API key.

An install turns this on with ``KUBESIGHT_OCI_AUTH_MODES=workload_identity``
(the chart's ``oke.workloadIdentity.enabled``). Then, for Oracle Cloud:

  * an account has no tenancy user, fingerprint or key: KubeSight's own calls
    are signed by Oracle's SDK signer for OKE workload identity, as the
    backend's own service account (``kubesight-backend``), which OCI's policy
    lets read and make cluster tokens only (K1);
  * an account with API-key fields is refused, and so is any call made with an
    older API-key account (K5);
  * VMs are refused: only OKE clusters (K18);
  * every cluster goes to the one locked compartment (K12), in the platform's
    VCN and its three subnets, taken by OCID (K7), private (K8), ENHANCED (K6),
    on E6 nodes only (K9), flannel with OKE's default ranges (K10), behind two
    NSGs with no SSH and no 0.0.0.0/0 (K11), with the Nexus hosts line in
    cloud-init (K16) and every defined tag left alone (K17).

OpenTofu does not run in the backend for these builds: the provisioner Jobs
do (``provisioning.remote``), as ``kubesight-provisioner``.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import threading
import urllib.parse
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

from . import base
from .base import Account, CloudError, ManagedContext, check

MODE = "workload_identity"
SHAPE = "VM.Standard.E6.Flex"
SHAPE_ENTRY = {"id": SHAPE, "maxOcpus": 126, "maxMemoryGb": 1764, "note": "AMD EPYC (E6), the only shape allowed here"}
CLUSTER_TYPE = "ENHANCED_CLUSTER"
CNI = "FLANNEL_OVERLAY"
PODS_CIDR = "10.244.0.0/16"
SERVICES_CIDR = "10.96.0.0/16"
MAX_EXPIRY_DAYS = 180
# What every LoadBalancer Service on a created cluster must carry (K21).
LB_ANNOTATIONS = {
    "service.beta.kubernetes.io/oci-load-balancer-internal": "true",
    "service.beta.kubernetes.io/oci-load-balancer-shape": "flexible",
    "service.beta.kubernetes.io/oci-load-balancer-shape-flex-min": "10",
    "service.beta.kubernetes.io/oci-load-balancer-shape-flex-max": "100",
    "service.beta.kubernetes.io/oci-load-balancer-security-list-management-mode": "None",
}

_OCID_BODY = r"\.[a-z0-9]+\.[a-z0-9.\-]*[a-z0-9]$"
_TENANCY_RE = re.compile(r"^ocid1\.tenancy" + _OCID_BODY)
_COMPARTMENT_RE = re.compile(r"^ocid1\.compartment" + _OCID_BODY)
_VCN_RE = re.compile(r"^ocid1\.vcn" + _OCID_BODY)
_SUBNET_RE = re.compile(r"^ocid1\.subnet" + _OCID_BODY)
_AD_RE = re.compile(r"^[A-Za-z0-9]{1,16}:[A-Za-z0-9-]{3,64}$")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,}$")
_API_KEY_KEYS = ("userOcid", "fingerprint", "privateKey", "privateKeyPassphrase")

FIELDS = [
    base.field("tenancyOcid", "Tenancy OCID", placeholder="ocid1.tenancy.oc1..aaaa…",
               help="No user and no key: KubeSight signs in by OKE workload identity, as its own "
                    "service account on the cluster it runs in."),
    base.field("compartmentOcid", "Compartment OCID", placeholder="ocid1.compartment.oc1..aaaa…",
               help="Every cluster goes here, and nowhere else."),
    base.field("vcnOcid", "VCN OCID", placeholder="ocid1.vcn.oc1.<region>.aaaa…",
               help="The platform's VCN. KubeSight never creates or changes networks."),
    base.field("endpointSubnetOcid", "API endpoint subnet OCID", placeholder="ocid1.subnet.oc1.<region>.aaaa…"),
    base.field("workerSubnetOcid", "Worker subnet OCID", placeholder="ocid1.subnet.oc1.<region>.aaaa…"),
    base.field("serviceLbSubnetOcid", "Load balancer subnet OCID", placeholder="ocid1.subnet.oc1.<region>.aaaa…"),
    base.field("sourceCidr", "KubeSight's network (CIDR)", placeholder="10.43.11.64/26",
               help="The addresses KubeSight's pods leave from: allowed to the clusters' API on 6443."),
    base.field("availabilityDomain", "Availability domain", placeholder="wchx:ME-DUBAI-1-AD-1"),
    base.field("registryHost", "Image registry host", placeholder="registry.areeba.com",
               help="Nodes pull every image from it (Oracle's own excepted)."),
    base.field("registryAddress", "Image registry address", placeholder="10.43.17.16",
               help="Not in the VCN's DNS: written to each node's /etc/hosts, and the only 443 the nodes reach."),
]


# ---------------------------------------------------------------------------
# Install switches
# ---------------------------------------------------------------------------

def enabled() -> bool:
    """This install signs in to Oracle Cloud by workload identity only."""
    modes = os.getenv("KUBESIGHT_OCI_AUTH_MODES", "").replace(" ", "").lower().split(",")
    return MODE in modes


def compartment_lock() -> str:
    return os.getenv("KUBESIGHT_OCI_COMPARTMENT_LOCK", "").strip()


def vms_allowed() -> bool:
    return os.getenv("KUBESIGHT_OCI_VMS", "true").strip().lower() not in ("0", "false", "no", "off")


def is_account(account: Account) -> bool:
    return (account.settings or {}).get("authMode") == MODE


# ---------------------------------------------------------------------------
# The account
# ---------------------------------------------------------------------------

def normalize_account(settings: Dict[str, Any], secrets: Dict[str, Any],
                      previous_settings: Optional[Dict[str, Any]] = None):
    settings = settings if isinstance(settings, dict) else {}
    secrets = secrets if isinstance(secrets, dict) else {}
    previous_settings = previous_settings or {}
    offered = [key for key in _API_KEY_KEYS
               if str(settings.get(key) or "").strip() or str(secrets.get(key) or "").strip()]
    if offered:
        raise ValueError(
            "This KubeSight signs in to Oracle Cloud by OKE workload identity only: an API key, "
            f"user or fingerprint is refused ({', '.join(offered)})."
        )

    def setting(key: str) -> str:
        value = settings.get(key)
        if value is None:
            value = previous_settings.get(key)
        return str(value or "").strip()

    out: Dict[str, Any] = {"authMode": MODE}
    for key, pattern, label in (
        ("tenancyOcid", _TENANCY_RE, "Tenancy OCID"),
        ("compartmentOcid", _COMPARTMENT_RE, "Compartment OCID"),
        ("vcnOcid", _VCN_RE, "VCN OCID"),
        ("endpointSubnetOcid", _SUBNET_RE, "API endpoint subnet OCID"),
        ("workerSubnetOcid", _SUBNET_RE, "Worker subnet OCID"),
        ("serviceLbSubnetOcid", _SUBNET_RE, "Load balancer subnet OCID"),
    ):
        value = setting(key)
        if not pattern.fullmatch(value):
            raise ValueError(f"{label} is required and must be an OCID of that kind.")
        out[key] = value
    lock = compartment_lock()
    if lock and out["compartmentOcid"] != lock:
        raise ValueError(f"Clusters may only go into the compartment {lock} on this install.")
    subnets = {out["endpointSubnetOcid"], out["workerSubnetOcid"], out["serviceLbSubnetOcid"]}
    if len(subnets) != 3:
        raise ValueError("The endpoint, worker and load balancer subnets must be three different subnets.")
    try:
        out["sourceCidr"] = str(ipaddress.ip_network(setting("sourceCidr"), strict=False))
    except ValueError as exc:
        raise ValueError("KubeSight's network must be a CIDR, e.g. 10.43.11.64/26.") from exc
    if out["sourceCidr"] in ("0.0.0.0/0", "::/0"):
        raise ValueError("KubeSight's network cannot be the whole internet.")
    zone = setting("availabilityDomain")
    if not _AD_RE.fullmatch(zone):
        raise ValueError("Availability domain must look like wchx:ME-DUBAI-1-AD-1.")
    out["availabilityDomain"] = zone
    host = setting("registryHost").lower()
    if not _HOST_RE.fullmatch(host):
        raise ValueError("Image registry host must be a host name, e.g. registry.areeba.com.")
    out["registryHost"] = host
    try:
        out["registryAddress"] = str(ipaddress.ip_address(setting("registryAddress")))
    except ValueError as exc:
        raise ValueError("Image registry address must be an IP address, e.g. 10.43.17.16.") from exc
    return out, {}


def identity_label(account: Account) -> str:
    return "OKE workload identity (kubesight-backend)"


# ---------------------------------------------------------------------------
# Signing: Oracle's SDK signer for OKE workload identity
# ---------------------------------------------------------------------------

_signer_lock = threading.Lock()
_signer = None
# Test seam: a callable taking a requests.PreparedRequest and signing it.
_signer_override: Optional[Callable[[Any], Any]] = None


def set_signer(signer: Optional[Callable[[Any], Any]]) -> None:
    global _signer_override, _signer
    _signer_override = signer
    _signer = None


def signer():
    """One signer per process: it exchanges the pod's service-account token at
    $KUBERNETES_SERVICE_HOST:12250 and refreshes it itself."""
    global _signer
    if _signer_override is not None:
        return _signer_override
    with _signer_lock:
        if _signer is None:
            if not os.getenv("OCI_RESOURCE_PRINCIPAL_VERSION"):
                raise CloudError("Workload identity is not set up in this pod "
                                 "(OCI_RESOURCE_PRINCIPAL_VERSION is missing).")
            try:
                from oci.auth.signers import get_oke_workload_identity_resource_principal_signer
            except ImportError as exc:  # pragma: no cover — the image installs it
                raise CloudError("The OCI SDK (oci>=2.111.0) is not installed in this image.") from exc
            try:
                _signer = get_oke_workload_identity_resource_principal_signer()
            except Exception as exc:  # noqa: BLE001 — the SDK raises many kinds
                raise CloudError(f"OKE workload identity could not sign in: {exc}") from exc
        return _signer


def signed_headers(method: str, url: str, body: Optional[bytes],
                   headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    try:
        import requests
    except ImportError:  # the OCI SDK ships its own copy
        from oci._vendor import requests

    prepared = requests.Request(method.upper(), url, data=body, headers=dict(headers or {})).prepare()
    try:
        signer()(prepared)
    except CloudError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CloudError(f"OKE workload identity could not sign the request: {exc}") from exc
    return {str(k).lower(): str(v) for k, v in prepared.headers.items()}


# ---------------------------------------------------------------------------
# Reading (the backend's read-only rights)
# ---------------------------------------------------------------------------

def _quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def read_network(account: Account, region: str, get_json) -> Dict[str, Any]:
    """The VCN and the three subnets, read by OCID (the backend may read only
    those four, tagged by the platform)."""
    from .oci import _endpoint

    core = _endpoint("core", region)
    s = account.settings
    vcn = get_json(account, f"{core}/vcns/{_quote(s['vcnOcid'])}", what="reading the VCN") or {}
    subnets = {}
    for role, key in (("endpoint", "endpointSubnetOcid"), ("workers", "workerSubnetOcid"),
                      ("loadBalancers", "serviceLbSubnetOcid")):
        subnet = get_json(account, f"{core}/subnets/{_quote(s[key])}", what=f"reading the {role} subnet") or {}
        if subnet.get("vcnId") and subnet["vcnId"] != s["vcnOcid"]:
            raise CloudError(f"The {role} subnet is not in the VCN {vcn.get('displayName') or s['vcnOcid']}.")
        if subnet.get("prohibitPublicIpOnVnic") is False:
            raise CloudError(f"The {role} subnet allows public IPs; this install refuses that.")
        subnets[role] = {"id": s[key], "name": subnet.get("displayName") or s[key],
                         "cidr": subnet.get("cidrBlock")}
    return {
        "vcn": {"id": s["vcnOcid"], "name": vcn.get("displayName") or s["vcnOcid"],
                "cidr": vcn.get("cidrBlock") or (vcn.get("cidrBlocks") or [None])[0]},
        "subnets": subnets,
    }


def simulated_network(account: Account) -> Dict[str, Any]:
    s = account.settings
    return {
        "vcn": {"id": s["vcnOcid"], "name": "simulated-uat-vcn", "cidr": "10.43.20.0/24"},
        "subnets": {
            "endpoint": {"id": s["endpointSubnetOcid"], "name": "simulated-master", "cidr": "10.43.20.128/26"},
            "workers": {"id": s["workerSubnetOcid"], "name": "simulated-workers", "cidr": "10.43.20.192/26"},
            "loadBalancers": {"id": s["serviceLbSubnetOcid"], "name": "simulated-lb", "cidr": "10.43.20.0/25"},
        },
    }


def test(account: Account, list_items, get_json) -> str:
    from .oci import _endpoint

    tenancy = account.settings["tenancyOcid"]
    rows = list_items(account, f"{_endpoint('identity', account.region)}/compartments", {
        "compartmentId": tenancy, "compartmentIdInSubtree": "true", "accessLevel": "ACCESSIBLE",
        "lifecycleState": "ACTIVE", "limit": 1000,
    }, what="listing compartments")
    target = account.settings["compartmentOcid"]
    names = {row.get("id"): row.get("name") for row in rows}
    if target not in names:
        raise CloudError("The compartment is not among those workload identity can see.")
    return f"{identity_label(account)}: {len(rows)} compartments visible, clusters go to {names[target]}"


def inventory(account: Account, region: str, get_json, list_items) -> Dict[str, Any]:
    from .oci import _endpoint, _oke_images, _version_key

    out = base.empty_inventory(region)
    out["warnings"] = []
    s = account.settings
    compartment = s["compartmentOcid"]
    identity = _endpoint("identity", region)
    comp = get_json(account, f"{identity}/compartments/{_quote(compartment)}", what="reading the compartment") or {}
    out["compartments"] = [{"id": compartment, "name": comp.get("name") or compartment,
                            "path": comp.get("name") or compartment}]
    out["zones"] = [{"id": s["availabilityDomain"], "name": s["availabilityDomain"]}]
    network = read_network(account, region, get_json)
    out["networks"] = [network_entry(network)]
    out["images"] = []
    out["machineTypes"] = machine_types()
    engine = _endpoint("containerengine", region)
    query = urllib.parse.urlencode({"compartmentId": compartment})
    options = get_json(account, f"{engine}/clusterOptions/all?{query}", what="reading OKE cluster options") or {}
    out["kubernetesVersions"] = sorted(options.get("kubernetesVersions") or [], key=_version_key, reverse=True)
    pool = get_json(account, f"{engine}/nodePoolOptions/all?{query}", what="reading OKE node pool options") or {}
    out["nodeImages"] = _oke_images(pool.get("sources") or [])[:150]
    out["workloadIdentity"] = True
    return out


def network_entry(network: Dict[str, Any]) -> Dict[str, Any]:
    subnets = network["subnets"]
    return {
        "id": network["vcn"]["id"], "name": network["vcn"]["name"], "cidr": network["vcn"]["cidr"],
        "subnets": [
            {"id": subnets["workers"]["id"], "name": f"{subnets['workers']['name']} (nodes)",
             "cidr": subnets["workers"]["cidr"], "zone": None},
        ],
        "fixed": {role: dict(item) for role, item in subnets.items()},
    }


def simulated_inventory(account: Account, region: str) -> Dict[str, Any]:
    out = base.empty_inventory(region)
    out["warnings"] = []
    s = account.settings
    out["compartments"] = [{"id": s["compartmentOcid"], "name": "K8s-UAT", "path": "DevOps/Kubernetes/K8s-UAT"}]
    out["zones"] = [{"id": s["availabilityDomain"], "name": s["availabilityDomain"]}]
    out["networks"] = [network_entry(simulated_network(account))]
    out["images"] = []
    out["machineTypes"] = machine_types()
    out["kubernetesVersions"] = ["v1.37.0", "v1.36.4", "v1.35.2", "v1.34.0"]
    out["nodeImages"] = [
        {"id": f"ocid1.image.oc1.{region}.aaaaaaaasim{os_tag}oke{v.replace('.', '')}",
         "name": f"Oracle-Linux-{os_name}-2026.10.16-0-OKE-{v}-2067", "kubernetesVersion": f"v{v}"}
        for v in ("1.37.0", "1.36.4", "1.35.2")
        for os_tag, os_name in (("ol9", "9.8"), ("ol8", "8.10"))
    ]
    out["workloadIdentity"] = True
    return out


def machine_types() -> List[Dict[str, Any]]:
    return [{"id": SHAPE, "cpu": SHAPE_ENTRY["maxOcpus"] * 2, "memoryGb": SHAPE_ENTRY["maxMemoryGb"],
             "flexible": True, "note": SHAPE_ENTRY["note"]}]


# ---------------------------------------------------------------------------
# The build's spec
# ---------------------------------------------------------------------------

def normalize_spec(account: Account, spec: Dict[str, Any], raw: Dict[str, Any],
                   previous: Dict[str, Any], *, vms_only: bool) -> Dict[str, Any]:
    """Fixes a workload-identity build to the platform's landing zone: the
    account's compartment, VCN and subnets, E6 only, managed only. What the
    browser sent for those is ignored."""
    s = account.settings
    if vms_only or spec.get("kind") != "managed":
        raise ValueError("On this install Oracle Cloud makes OKE clusters only, never VMs.")
    if raw.get("podSubnetId") or previous.get("podSubnetId"):
        raise ValueError("Pods use the flannel overlay here: no pod subnet.")
    for key in ("compartmentId",):
        asked = str(raw.get(key) or "").strip()
        if asked and asked != s["compartmentOcid"]:
            raise ValueError("Clusters may only go into the account's compartment.")
    lock = compartment_lock()
    if lock and s["compartmentOcid"] != lock:
        raise ValueError(f"Clusters may only go into the compartment {lock} on this install.")
    shapes = {str(v) for v in (spec.get("machineTypes") or {}).values() if v}
    if str(raw.get("shape") or "").strip():
        shapes.add(str(raw.get("shape")).strip())
    if shapes - {SHAPE}:
        raise ValueError(f"Only {SHAPE} nodes are allowed here.")
    spec.update({
        "authMode": MODE,
        "compartmentId": s["compartmentOcid"],
        "networkId": s["vcnOcid"],
        "subnetId": s["workerSubnetOcid"],
        "endpointSubnetId": s["endpointSubnetOcid"],
        "serviceLbSubnetId": s["serviceLbSubnetOcid"],
        "zone": s["availabilityDomain"],
        "shape": SHAPE,
        "machineTypes": {"worker": SHAPE} if spec.get("machineTypes") else {},
        "allowedCidrs": [s["sourceCidr"]],
    })
    spec.pop("podSubnetId", None)
    owner = str(raw.get("ownerEmail") or previous.get("ownerEmail") or "").strip()
    if not _EMAIL_RE.fullmatch(owner):
        raise ValueError("Give the cluster's owner: a work email.")
    expiry_raw = str(raw.get("expiresOn") or previous.get("expiresOn") or "").strip()
    try:
        expiry = date.fromisoformat(expiry_raw)
    except ValueError as exc:
        raise ValueError("Give the cluster's expiry date (YYYY-MM-DD).") from exc
    if expiry <= date.today():
        raise ValueError("The expiry date must be in the future.")
    if expiry > date.today() + timedelta(days=MAX_EXPIRY_DAYS):
        raise ValueError(f"The expiry date may be at most {MAX_EXPIRY_DAYS} days away.")
    spec["ownerEmail"] = owner
    spec["expiresOn"] = expiry.isoformat()
    size = (spec.get("sizes") or {}).get("worker") or {}
    pools = normalize_node_pools(
        raw["nodePools"] if "nodePools" in raw else previous.get("nodePools"),
        fallback={"count": (spec.get("counts") or {}).get("worker") or 1, "cpu": size.get("cpu"),
                  "memoryGb": size.get("memoryGb"), "diskGb": size.get("diskGb")},
    )
    spec["nodePools"] = pools
    # The one-pool fields other code reads: the total, and the first pool's size.
    spec["counts"] = {**(spec.get("counts") or {}), "worker": pool_totals(pools)["nodes"]}
    first = pools[0]
    spec["sizes"] = {"worker": {"cpu": first["ocpus"] * 2, "memoryGb": first["memoryGb"], "diskGb": first["diskGb"]}}
    return spec


# ---------------------------------------------------------------------------
# Node pools: size, count, labels, taints, fault domains
# ---------------------------------------------------------------------------

MAX_POOLS = 8
MAX_POOL_NODES = 50
POOL_LABEL = "kubesight.areeba.com/pool"
FAULT_DOMAINS = ["FAULT-DOMAIN-1", "FAULT-DOMAIN-2", "FAULT-DOMAIN-3"]
TAINT_EFFECTS = ("NoSchedule", "PreferNoSchedule", "NoExecute")
_POOL_NAME_RE = re.compile(r"^[a-z]([a-z0-9-]{0,28}[a-z0-9])?$")
_LABEL_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?$")
_LABEL_PREFIX_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
_IMAGE_RE = re.compile(r"^ocid1\.image\.oc1\.[a-z0-9-]*\.[a-z0-9]{8,}$")
_LABEL_VALUE_RE = re.compile(r"^([A-Za-z0-9]([A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?)?$")


def _label_key(key: str, what: str) -> str:
    key = str(key or "").strip()
    prefix, _, name = key.rpartition("/")
    if not _LABEL_NAME_RE.fullmatch(name) or (prefix and (len(prefix) > 253 or not _LABEL_PREFIX_RE.fullmatch(prefix))):
        raise ValueError(f"{what} '{key}' is not a valid Kubernetes key (e.g. team or example.com/tier).")
    if prefix and (prefix.endswith("kubernetes.io") or prefix.endswith("k8s.io")):
        # The kubelet may not set these itself (NodeRestriction), so OKE would fail the node.
        raise ValueError(f"{what} '{key}': keys under kubernetes.io or k8s.io are Kubernetes' own.")
    if key == POOL_LABEL:
        raise ValueError(f"{POOL_LABEL} is set by KubeSight to the pool's name.")
    return key


def _label_value(value: Any, what: str) -> str:
    value = str(value if value is not None else "").strip()
    if not _LABEL_VALUE_RE.fullmatch(value):
        raise ValueError(f"{what} value '{value}' is not a valid Kubernetes label value (63 characters at most).")
    return value


def _whole(value: Any, what: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} must be a whole number.") from exc
    if not low <= number <= high:
        raise ValueError(f"{what} must be between {low} and {high}.")
    return number


def normalize_node_pools(raw: Any, *, fallback: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The cluster's node pools, checked. ``fallback`` (count, cpu, memoryGb,
    diskGb) makes the single ``workers`` pool of a build that names none."""
    if not raw:
        fb = fallback or {}
        raw = [{"name": "workers", "count": fb.get("count") or 1,
                "ocpus": max(1, int(fb.get("cpu") or 2) // 2), "memoryGb": fb.get("memoryGb") or 16,
                "diskGb": fb.get("diskGb") or 50}]
    if not isinstance(raw, list):
        raise ValueError("nodePools must be a list.")
    if not 1 <= len(raw) <= MAX_POOLS:
        raise ValueError(f"A cluster has 1 to {MAX_POOLS} node pools.")
    pools: List[Dict[str, Any]] = []
    seen = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError("Each node pool is an object.")
        name = str(item.get("name") or "").strip().lower()
        if not _POOL_NAME_RE.fullmatch(name):
            raise ValueError(f"Node pool {index}: the name is 1-30 lowercase letters, digits and dashes.")
        if name in seen:
            raise ValueError(f"Two node pools are called '{name}'.")
        seen.add(name)
        what = f"Node pool '{name}'"
        ocpus = _whole(item.get("ocpus"), f"{what}: OCPUs", 1, 64)
        memory = _whole(item.get("memoryGb"), f"{what}: memory (GB)", ocpus, min(1024, ocpus * 64))
        labels_in = item.get("labels") or {}
        if isinstance(labels_in, list):
            labels_in = {str(row.get("key") or ""): row.get("value") for row in labels_in if isinstance(row, dict)}
        if not isinstance(labels_in, dict) or len(labels_in) > 20:
            raise ValueError(f"{what}: at most 20 labels.")
        pool_labels = {_label_key(k, f"{what}: label"): _label_value(v, f"{what}: label") for k, v in labels_in.items()}
        taints_in = item.get("taints") or []
        if not isinstance(taints_in, list) or len(taints_in) > 10:
            raise ValueError(f"{what}: at most 10 taints.")
        taints = []
        for taint in taints_in:
            if not isinstance(taint, dict):
                raise ValueError(f"{what}: each taint is an object.")
            effect = str(taint.get("effect") or "NoSchedule")
            if effect not in TAINT_EFFECTS:
                raise ValueError(f"{what}: a taint's effect is one of {', '.join(TAINT_EFFECTS)}.")
            taints.append({"key": _label_key(taint.get("key"), f"{what}: taint"),
                           "value": _label_value(taint.get("value"), f"{what}: taint"), "effect": effect})
        image = str(item.get("imageId") or "").strip()
        if image and not _IMAGE_RE.fullmatch(image):
            raise ValueError(f"{what}: the image must be an OKE node image OCID (ocid1.image...).")
        spread = str(item.get("faultDomains") or "any")
        if spread not in ("any", "spread"):
            raise ValueError(f"{what}: faultDomains is 'any' or 'spread'.")
        pools.append({
            "name": name,
            "count": _whole(item.get("count"), f"{what}: nodes", 1, MAX_POOL_NODES),
            "ocpus": ocpus,
            "memoryGb": memory,
            "diskGb": _whole(item.get("diskGb") or 50, f"{what}: boot disk (GB)", 50, 2048),
            "labels": pool_labels,
            "taints": taints,
            "faultDomains": spread,
            # Empty: the newest Oracle Linux OKE image for the cluster's version.
            "imageId": image,
        })
    return pools


def pool_totals(pools: List[Dict[str, Any]]) -> Dict[str, int]:
    return {"nodes": sum(p["count"] for p in pools), "ocpus": sum(p["count"] * p["ocpus"] for p in pools)}


def _pool_key(name: str) -> str:
    return name.replace("-", "_")


def labels(base_labels: Dict[str, str], spec: Dict[str, Any]) -> Dict[str, str]:
    out = dict(base_labels)
    out["owner"] = spec.get("ownerEmail") or ""
    out["expiry"] = spec.get("expiresOn") or ""
    return out


# ---------------------------------------------------------------------------
# main.tf.json
# ---------------------------------------------------------------------------

def _rule(nsg: str, direction: str, protocol: str, cidr: str, *, ports=None, icmp=None,
          service: bool = False, description: str) -> Dict[str, Any]:
    rule: Dict[str, Any] = {
        "network_security_group_id": "${oci_core_network_security_group." + nsg + ".id}",
        "direction": direction,
        "protocol": protocol,
        "stateless": False,
        "description": description,
    }
    kind = "SERVICE_CIDR_BLOCK" if service else "CIDR_BLOCK"
    if direction == "INGRESS":
        rule.update(source=cidr, source_type=kind)
    else:
        rule.update(destination=cidr, destination_type=kind)
    if ports:
        rule["tcp_options"] = [{"destination_port_range": [{"min": ports[0], "max": ports[1]}]}]
    if icmp is not None:
        rule["icmp_options"] = [{"type": icmp, **({"code": 4} if icmp == 3 else {})}]
    return rule


def oracle_services(region: str) -> str:
    """Oracle's name for every service of the region behind the service gateway."""
    key = {"me-dubai-1": "dxb", "me-jeddah-1": "jed", "me-abudhabi-1": "auh",
           "eu-frankfurt-1": "fra", "uk-london-1": "lhr", "us-ashburn-1": "iad"}.get(region)
    if not key:
        raise ValueError(f"KubeSight does not know Oracle's service network name for {region}.")
    return f"all-{key}-services-in-oracle-services-network"


def nsg_rules(spec: Dict[str, Any], network: Dict[str, Any], account_settings: Dict[str, Any],
              region: str) -> Dict[str, Dict[str, Any]]:
    """K11: Oracle's private-endpoint example, no SSH, no 0.0.0.0/0."""
    subnets = network["subnets"]
    vcn = network["vcn"]["cidr"]
    endpoint, workers, lbs = subnets["endpoint"]["cidr"], subnets["workers"]["cidr"], subnets["loadBalancers"]["cidr"]
    for label, cidr in (("VCN", vcn), ("endpoint subnet", endpoint), ("worker subnet", workers),
                        ("load balancer subnet", lbs)):
        if not cidr:
            raise ValueError(f"The {label}'s CIDR could not be read.")
    source = account_settings["sourceCidr"]
    nexus = f"{account_settings['registryAddress']}/32"
    services = oracle_services(region)
    rules = [
        ("endpoint_kubesight_api", _rule("endpoint", "INGRESS", "6", source, ports=(6443, 6443), description="KubeSight to the API")),
        ("endpoint_workers_api", _rule("endpoint", "INGRESS", "6", workers, ports=(6443, 6443), description="Workers to the API")),
        ("endpoint_workers_12250", _rule("endpoint", "INGRESS", "6", workers, ports=(12250, 12250), description="Workers to the control plane")),
        ("endpoint_icmp3_in", _rule("endpoint", "INGRESS", "1", workers, icmp=3, description="Path discovery from the workers")),
        ("endpoint_icmp4_in", _rule("endpoint", "INGRESS", "1", workers, icmp=4, description="Path discovery from the workers")),
        ("endpoint_services", _rule("endpoint", "EGRESS", "6", services, service=True, description="OKE to Oracle's services")),
        ("endpoint_to_workers", _rule("endpoint", "EGRESS", "6", workers, description="Control plane to the workers")),
        ("endpoint_icmp3_out", _rule("endpoint", "EGRESS", "1", workers, icmp=3, description="Path discovery to the workers")),
        ("endpoint_icmp4_out", _rule("endpoint", "EGRESS", "1", workers, icmp=4, description="Path discovery to the workers")),
        ("workers_from_workers", _rule("workers", "INGRESS", "all", workers, description="Pod to pod, node to node")),
        ("workers_from_endpoint", _rule("workers", "INGRESS", "6", endpoint, description="Control plane to the workers")),
        ("workers_icmp3_in", _rule("workers", "INGRESS", "1", vcn, icmp=3, description="Path discovery")),
        ("workers_icmp4_in", _rule("workers", "INGRESS", "1", vcn, icmp=4, description="Path discovery")),
        ("workers_lb_nodeports", _rule("workers", "INGRESS", "6", lbs, ports=(30000, 32767), description="Load balancers to node ports")),
        ("workers_lb_health", _rule("workers", "INGRESS", "6", lbs, ports=(10256, 10256), description="Load balancer health checks")),
        ("workers_to_api", _rule("workers", "EGRESS", "6", endpoint, ports=(6443, 6443), description="Workers to the API")),
        ("workers_to_12250", _rule("workers", "EGRESS", "6", endpoint, ports=(12250, 12250), description="Workers to the control plane")),
        ("workers_services", _rule("workers", "EGRESS", "6", services, service=True, description="Workers to Oracle's services")),
        ("workers_to_workers", _rule("workers", "EGRESS", "all", workers, description="Pod to pod, node to node")),
        ("workers_icmp3_out", _rule("workers", "EGRESS", "1", vcn, icmp=3, description="Path discovery")),
        ("workers_icmp4_out", _rule("workers", "EGRESS", "1", vcn, icmp=4, description="Path discovery")),
        ("workers_nexus", _rule("workers", "EGRESS", "6", nexus, ports=(443, 443), description="Images from the registry")),
    ]
    out = {name: rule for name, rule in rules}
    for rule in out.values():
        for key in ("source", "destination"):
            if rule.get(key) in ("0.0.0.0/0", "::/0"):
                raise ValueError("A rule open to the whole internet is refused.")
        if (rule.get("tcp_options") or [{}])[0].get("destination_port_range", [{}])[0].get("min") == base.SSH_PORT:
            raise ValueError("SSH is never opened.")
    return out


def cloud_init(registry_host: str, registry_address: str, taints: Optional[List[Dict[str, str]]] = None) -> str:
    """Oracle's OKE bootstrap first (with the pool's taints for the kubelet),
    then the registry's name (K16)."""
    if not _HOST_RE.fullmatch(registry_host):
        raise ValueError("Bad registry host.")
    ipaddress.ip_address(registry_address)
    bootstrap = "bash /var/run/oke-init.sh"
    if taints:
        # Keys and values are checked label syntax ([A-Za-z0-9._/-]), safe in quotes.
        spec = ",".join(f"{t['key']}={t['value']}:{t['effect']}" if t.get("value") else f"{t['key']}:{t['effect']}"
                        for t in taints)
        bootstrap += f' --kubelet-extra-args "--register-with-taints={spec}"'
    return (
        "#!/bin/bash\n"
        'curl --fail -H "Authorization: Bearer Oracle" -L0 '
        "http://169.254.169.254/opc/v2/instance/metadata/oke_init_script | base64 --decode >/var/run/oke-init.sh\n"
        f"{bootstrap}\n"
        f"echo '{registry_address}      {registry_host}' >> /etc/hosts\n"
    )


def render_managed(ctx: ManagedContext, network: Dict[str, Any], account_settings: Dict[str, Any]) -> Dict[str, Any]:
    from .oci import CLUSTER_TYPE as CLUSTER_RESOURCE, NODE_POOL_TYPE, TOFU_PROVIDER, _oke_version

    spec = ctx.spec
    tags = labels(ctx.labels or base.tags(ctx.cluster_name, ctx.build_id), spec)
    version = _oke_version(ctx.kubernetes_version)
    machine = ctx.node_pool["machineType"]
    if machine["type"] != SHAPE:
        raise ValueError(f"Only {SHAPE} nodes are allowed here.")
    subnets = network["subnets"]
    nsg = {
        name: {
            "compartment_id": spec["compartmentId"],
            "vcn_id": spec["networkId"],
            "display_name": f"kubesight-{ctx.cluster_name}-{name}",
            "freeform_tags": tags,
            "lifecycle": [{"ignore_changes": ["defined_tags"]}],
        }
        for name in ("endpoint", "workers")
    }
    resources: Dict[str, Any] = {
        "oci_core_network_security_group": nsg,
        "oci_core_network_security_group_security_rule": nsg_rules(spec, network, account_settings, ctx.region),
    }
    resources[CLUSTER_RESOURCE] = {"cluster": {
        "compartment_id": spec["compartmentId"],
        "name": ctx.cluster_name,
        "kubernetes_version": version,
        "vcn_id": spec["networkId"],
        "type": CLUSTER_TYPE,
        "endpoint_config": [{
            "subnet_id": subnets["endpoint"]["id"],
            "is_public_ip_enabled": False,
            "nsg_ids": ["${oci_core_network_security_group.endpoint.id}"],
        }],
        "cluster_pod_network_options": [{"cni_type": CNI}],
        "options": [{
            "service_lb_subnet_ids": [subnets["loadBalancers"]["id"]],
            "kubernetes_network_config": [{"pods_cidr": PODS_CIDR, "services_cidr": SERVICES_CIDR}],
        }],
        "freeform_tags": tags,
        "lifecycle": [{"ignore_changes": [
            "defined_tags",
            "options[0].persistent_volume_config[0].defined_tags",
            "options[0].service_lb_config[0].defined_tags",
        ]}],
    }}
    pools = ctx.node_pool.get("pools") or normalize_node_pools(spec.get("nodePools"), fallback={
        "count": ctx.node_pool["count"], "cpu": int(machine["ocpus"]) * 2,
        "memoryGb": int(machine["memoryGb"]), "diskGb": ctx.node_pool.get("diskGb"),
    })
    resources[NODE_POOL_TYPE] = {}
    for pool in pools:
        placement: Dict[str, Any] = {"availability_domain": spec["zone"], "subnet_id": subnets["workers"]["id"]}
        if pool["faultDomains"] == "spread":
            placement["fault_domains"] = list(FAULT_DOMAINS)
        user_data = base64.b64encode(cloud_init(
            account_settings["registryHost"], account_settings["registryAddress"], pool["taints"],
        ).encode("utf-8")).decode("ascii")
        node_labels = {POOL_LABEL: pool["name"], **pool["labels"]}
        # The first pool named "workers" keeps the address a one-pool build had.
        resources[NODE_POOL_TYPE][_pool_key(pool["name"])] = {
            "cluster_id": "${" + CLUSTER_RESOURCE + ".cluster.id}",
            "compartment_id": spec["compartmentId"],
            "name": f"{ctx.cluster_name}-{pool['name']}",
            "kubernetes_version": version,
            "node_shape": SHAPE,
            "node_shape_config": [{"ocpus": pool["ocpus"], "memory_in_gbs": pool["memoryGb"]}],
            "initial_node_labels": [{"key": k, "value": v} for k, v in node_labels.items()],
            "node_config_details": [{
                "size": pool["count"],
                "placement_configs": [placement],
                "nsg_ids": ["${oci_core_network_security_group.workers.id}"],
                "node_pool_pod_network_option_details": [{"cni_type": CNI}],
                "freeform_tags": {**tags, "pool": pool["name"]},
            }],
            "node_source_details": [{
                "source_type": "IMAGE",
                "image_id": pool.get("imageId") or spec["nodeImageId"],
                "boot_volume_size_in_gbs": str(pool["diskGb"]),
            }],
            "node_metadata": {"user_data": user_data},
            "freeform_tags": tags,
            "lifecycle": [{"ignore_changes": ["defined_tags", "node_config_details[0].defined_tags"]}],
        }
    return {
        "terraform": {
            "required_providers": {
                "oci": {"source": TOFU_PROVIDER["source"], "version": f"= {TOFU_PROVIDER['version']}"}
            },
            "backend": {"http": {}},
        },
        # K2: the provider signs in by workload identity; no key, no user.
        "provider": {"oci": {"auth": "OKEWorkloadIdentity", "region": ctx.region}},
        "resource": resources,
    }


def plan_checks(account: Account, spec: Dict[str, Any], get_json) -> List[Dict[str, str]]:
    from .oci import _endpoint, _oke_images, _oke_version, _version_key

    checks = [check("ok", "Signed in", identity_label(account))]
    network = read_network(account, spec["region"], get_json)
    checks.append(check("ok", "Network", (
        f"{network['vcn']['name']}: endpoint {network['subnets']['endpoint']['cidr']}, "
        f"workers {network['subnets']['workers']['cidr']}, load balancers "
        f"{network['subnets']['loadBalancers']['cidr']}"
    )))
    engine = _endpoint("containerengine", spec["region"])
    query = urllib.parse.urlencode({"compartmentId": spec["compartmentId"]})
    options = get_json(account, f"{engine}/clusterOptions/all?{query}", what="reading OKE cluster options") or {}
    version = _oke_version((spec.get("managed") or {}).get("kubernetesVersion") or "")
    offered = options.get("kubernetesVersions") or []
    if version not in offered:
        raise CloudError(
            f"OKE does not offer Kubernetes {version or '(none chosen)'}"
            + (f"; it offers {', '.join(sorted(offered, key=_version_key, reverse=True)[:5])}." if offered else ".")
        )
    checks.append(check("ok", "Kubernetes version", f"OKE offers {version}"))
    pool_options = get_json(account, f"{engine}/nodePoolOptions/all?{query}", what="reading OKE node pool options") or {}
    checks.extend(image_checks(spec, _oke_images(pool_options.get("sources") or [], version), version))
    checks.append(check("ok", "Cluster", f"{CLUSTER_TYPE}, private endpoint, {CNI}, {SHAPE} nodes"))
    checks.append(check("ok", "Owner", f"{spec.get('ownerEmail')}, expires {spec.get('expiresOn')}"))
    return checks


def image_checks(spec: Dict[str, Any], images: List[Dict[str, Any]], version: str) -> List[Dict[str, str]]:
    """Each pool's node image: the one chosen must be an OKE image built for
    ``version``; none chosen means the newest one (said by name)."""
    from .oci import _oke_version  # noqa: F401 — version is already OKE's form

    by_id = {image["id"]: image for image in images}
    newest = images[0] if images else None
    out = []
    for pool in normalize_node_pools(spec.get("nodePools"), fallback={"count": 1}):
        chosen = pool.get("imageId") or ""
        if chosen and chosen not in by_id:
            raise CloudError(
                f"Node pool '{pool['name']}': the image {chosen} is not an OKE node image for Kubernetes "
                f"{version} here. Choose one built for {version}."
            )
        image = by_id.get(chosen) or newest
        if image is None:
            raise CloudError(f"OKE offers no Oracle Linux node image for Kubernetes {version} here.")
        out.append(check("ok", f"Node image · {pool['name']}",
                         f"{image['name']}{'' if chosen else ' (newest for ' + version + ')'}"))
    return out


# ---------------------------------------------------------------------------
# Tokens for a created cluster's API (K20)
# ---------------------------------------------------------------------------

def cluster_token(region: str, cluster_id: str) -> str:
    """What ``oci ce cluster generate-token`` prints, signed by workload
    identity: a presigned GET whose signature and date ride in the query."""
    url = f"https://containerengine.{region}.oraclecloud.com/cluster_request/{cluster_id}"
    headers = signed_headers("GET", url, None)
    query = urllib.parse.urlencode({"authorization": headers["authorization"], "date": headers["date"]})
    return base64.urlsafe_b64encode(f"{url}?{query}".encode("utf-8")).decode("utf-8")
