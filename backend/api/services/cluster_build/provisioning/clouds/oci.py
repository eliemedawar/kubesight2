"""Oracle Cloud Infrastructure: self-managed VMs and OKE clusters.

See ``base`` for the contract. What is particular to OCI:

  * **Requests are signed by hand** (``sign``): OCI's HTTP Signature scheme
    (draft-cavage, rsa-sha256, keyId ``<tenancy>/<user>/<fingerprint>``) over
    ``date (request-target) host``, plus ``content-length content-type
    x-content-sha256`` when there is a body — the same bytes the oci SDK's
    ``Signer`` produces, without shipping the SDK.
  * **Flexible shapes only.** A size (vCPU, GB) becomes a ``*.Flex`` shape
    with OCPUs and memory; one x86 OCPU is two vCPUs, and OCI wants at least
    1 GB and at most 64 GB of memory per OCPU.
  * **The provider signs in from its OCI_* environment variables**, the key
    in a 0600 file in the job's working directory, so the key never appears
    in main.tf.json or a process's arguments.
  * **user_data is sensitive.** Each VM's cloud-init (which may carry the SSH
    password) is base64'd in Python and kept in a ``sensitive`` variable, so
    plans print "(sensitive value)" instead of it.
  * **No hostname_label.** It fails on a subnet without a DNS label and must
    start with a letter; cloud-init sets the hostname instead.
  * **OKE nodes boot from ``nodeImageId``**, which the wizard normally leaves
    empty: the core calls ``default_node_image`` (the newest x86 Oracle Linux
    OKE image built for the chosen version) right before rendering. There are
    no data sources, so the id is fixed in the plan.
  * **The OKE token** is what ``oci ce cluster generate-token`` makes: a GET
    to ``containerengine.<region>.oraclecloud.com/cluster_request/<id>``
    signed, with its authorization and date moved into the query string, the
    whole URL base64url-encoded.
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import json
import math
import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from . import base, oci_wi
from .base import Account, CloudError, ManagedContext, VmContext, check

PROVIDER = "oci"
LABEL = "Oracle Cloud"
MANAGED_LABEL = "Oracle OKE"
TOFU_PROVIDER = {"name": "oci", "source": "oracle/oci", "version": "9.11.0"}
VM_RESOURCE_TYPE = "oci_core_instance"
NLB_TYPE = "oci_network_load_balancer_network_load_balancer"
CLUSTER_TYPE = "oci_containerengine_cluster"
NODE_POOL_TYPE = "oci_containerengine_node_pool"

DEFAULT_SHAPE = "VM.Standard.E5.Flex"
# x86 flexible shapes: one OCPU = two vCPUs; memory 1–64 GB per OCPU.
FLEX_SHAPES: List[Dict[str, Any]] = [
    oci_wi.SHAPE_ENTRY,
    {"id": "VM.Standard.E5.Flex", "maxOcpus": 94, "maxMemoryGb": 1049, "note": "AMD EPYC (E5), 1–94 OCPU"},
    {"id": "VM.Standard.E4.Flex", "maxOcpus": 64, "maxMemoryGb": 1024, "note": "AMD EPYC (E4), 1–64 OCPU"},
    {"id": "VM.Standard3.Flex", "maxOcpus": 32, "maxMemoryGb": 512, "note": "Intel Xeon, 1–32 OCPU"},
]
_FLEX_BY_ID = {item["id"]: item for item in FLEX_SHAPES}
_MAX_GB_PER_OCPU = 64
_IMAGES = (
    ("Oracle Linux", "9", "rhel"),
    ("Oracle Linux", "8", "rhel"),
    ("Canonical Ubuntu", "24.04", "debian"),
    ("Canonical Ubuntu", "22.04", "debian"),
)
_PAGE_LIMIT = 20
API_PORT = base.API_PORT

FIELDS = [
    base.field("tenancyOcid", "Tenancy OCID", placeholder="ocid1.tenancy.oc1..aaaa…",
               help="Profile → Tenancy in the OCI console."),
    base.field("userOcid", "User OCID", placeholder="ocid1.user.oc1..aaaa…",
               help="The user the API key belongs to. Its groups' policies decide what KubeSight may create."),
    base.field("fingerprint", "API key fingerprint", placeholder="12:34:56:…",
               help="Shown next to the key under the user's API keys."),
    base.field("privateKey", "API private key (PEM)", secret=True, kind="textarea",
               placeholder="-----BEGIN PRIVATE KEY-----"),
    base.field("privateKeyPassphrase", "Key passphrase", secret=True, required=False,
               help="Only if the private key is encrypted."),
    base.field("compartmentOcid", "Default compartment OCID", required=False,
               placeholder="ocid1.compartment.oc1..aaaa…",
               help="Where networks are listed and resources created unless a build picks another. Empty: the tenancy root."),
]

def fields() -> List[Dict[str, Any]]:
    """The account form: workload identity's on an install that uses it (K1, K5)."""
    return oci_wi.FIELDS if oci_wi.enabled() else FIELDS


_OCID_BODY = r"\.[a-z0-9]+\.[a-z0-9.\-]*[a-z0-9]$"


def _ocid_re(*types: str) -> "re.Pattern[str]":
    return re.compile(r"^ocid1\.(?:" + "|".join(types) + ")" + _OCID_BODY)


_TENANCY_RE = _ocid_re("tenancy")
_USER_RE = _ocid_re("user")
_COMPARTMENT_RE = _ocid_re("compartment", "tenancy")
_SUBNET_RE = _ocid_re("subnet")
_IMAGE_RE = _ocid_re("image")
_FINGERPRINT_RE = re.compile(r"^([0-9a-f]{2}:){15}[0-9a-f]{2}$")
_AD_RE = re.compile(r"^[A-Za-z0-9]{1,16}:[A-Za-z0-9-]{3,64}$")
_REGION_RE = re.compile(r"^[a-z]{2,}-[a-z]+-\d+$")


# ---------------------------------------------------------------------------
# The account
# ---------------------------------------------------------------------------

def _load_key(pem: str, passphrase: Optional[str]):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    try:
        key = serialization.load_pem_private_key(
            pem.strip().encode("utf-8"), password=passphrase.encode("utf-8") if passphrase else None,
        )
    except TypeError as exc:
        raise ValueError(
            "The private key is encrypted: give its passphrase." if not passphrase
            else "The passphrase was given but the private key is not encrypted."
        ) from exc
    except ValueError as exc:
        raise ValueError("The private key could not be read (wrong passphrase, or not a PEM private key).") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("OCI API keys are RSA keys; this one is not.")
    return key


def key_fingerprint(private_key) -> str:
    """What the console shows next to an API key: MD5 of the public key's DER."""
    from cryptography.hazmat.primitives import serialization

    der = private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.md5(der).hexdigest()  # noqa: S324 — OCI's fingerprint format, not security
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def normalize_account(settings: Dict[str, Any], secrets: Dict[str, Any],
                      previous_settings: Optional[Dict[str, Any]] = None,
                      previous_secrets: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    if oci_wi.enabled():
        return oci_wi.normalize_account(settings, secrets, previous_settings)
    if (settings or {}).get("authMode") == oci_wi.MODE or (previous_settings or {}).get("authMode") == oci_wi.MODE:
        raise ValueError("OKE workload identity is not switched on for this KubeSight.")
    settings = settings if isinstance(settings, dict) else {}
    secrets = secrets if isinstance(secrets, dict) else {}
    previous_settings = previous_settings or {}
    previous_secrets = previous_secrets or {}

    def setting(key: str) -> str:
        value = settings.get(key)
        if value is None:
            value = previous_settings.get(key)
        return str(value or "").strip()

    tenancy = setting("tenancyOcid")
    if not _TENANCY_RE.fullmatch(tenancy):
        raise ValueError("Tenancy OCID must look like ocid1.tenancy.oc1..aaaa….")
    user = setting("userOcid")
    if not _USER_RE.fullmatch(user):
        raise ValueError("User OCID must look like ocid1.user.oc1..aaaa….")
    fingerprint = setting("fingerprint").lower()
    if not _FINGERPRINT_RE.fullmatch(fingerprint):
        raise ValueError("The fingerprint is 16 pairs of hex digits separated by colons (12:34:…).")
    compartment = setting("compartmentOcid") or tenancy
    if not _COMPARTMENT_RE.fullmatch(compartment):
        raise ValueError("Default compartment must be a compartment (or the tenancy) OCID.")

    private_key = base.keep_or_require(secrets, previous_secrets, "privateKey", "The API private key")
    passphrase = base.keep_or_require(
        secrets, previous_secrets, "privateKeyPassphrase", "The key passphrase", required=False,
    )
    key = _load_key(private_key, passphrase)
    actual = key_fingerprint(key)
    if actual != fingerprint:
        raise ValueError(
            f"The fingerprint {fingerprint} does not belong to this private key (its fingerprint is {actual}). "
            "Check that the key and the fingerprint are from the same API key."
        )
    out_secrets = {"privateKey": private_key}
    if passphrase:
        out_secrets["privateKeyPassphrase"] = passphrase
    return (
        {"tenancyOcid": tenancy, "userOcid": user, "fingerprint": fingerprint, "compartmentOcid": compartment},
        out_secrets,
    )


def _shorten(ocid: str) -> str:
    return f"{ocid[:10]}…{ocid[-8:]}" if len(ocid) > 24 else ocid


def identity_label(account: Account) -> str:
    if oci_wi.is_account(account):
        return oci_wi.identity_label(account)
    return f"user {_shorten(str(account.settings.get('userOcid') or ''))}"


def _compartment(account: Account, scope: Optional[Dict[str, Any]] = None) -> str:
    return str(
        (scope or {}).get("compartmentId") or account.settings.get("compartmentOcid")
        or account.settings.get("tenancyOcid") or ""
    )


# ---------------------------------------------------------------------------
# Signing and REST
# ---------------------------------------------------------------------------

_GENERIC_HEADERS = ("date", "(request-target)", "host")
_BODY_HEADERS = ("content-length", "content-type", "x-content-sha256")


def _key_id(account: Account) -> str:
    s = account.settings
    return f"{s['tenancyOcid']}/{s['userOcid']}/{s['fingerprint']}"


def sign(account: Account, method: str, url: str, *, body: Optional[bytes] = None,
         headers: Optional[Dict[str, str]] = None, date: Optional[str] = None,
         private_key=None) -> Dict[str, str]:
    """The request's headers with OCI's ``authorization`` added (and the
    ``date``, ``host`` and, for a body, content headers it signs)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    out = dict(headers or {})
    out.setdefault("date", date or email.utils.formatdate(usegmt=True))
    out.setdefault("host", parts.netloc)
    verb = method.lower()
    signed = list(_GENERIC_HEADERS)
    if verb in ("post", "put", "patch"):
        data = body or b""
        out.setdefault("content-type", "application/json")
        out.setdefault("content-length", str(len(data)))
        out["x-content-sha256"] = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
        signed += list(_BODY_HEADERS)
    lines = []
    for name in signed:
        if name == "(request-target)":
            lines.append(f"(request-target): {verb} {path}")
        else:
            lines.append(f"{name}: {out[name]}")
    key = private_key or _load_key(account.secrets["privateKey"], account.secrets.get("privateKeyPassphrase"))
    signature = base64.b64encode(
        key.sign("\n".join(lines).encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    ).decode("ascii")
    out["authorization"] = (
        f'Signature algorithm="rsa-sha256",headers="{" ".join(signed)}",'
        f'keyId="{_key_id(account)}",signature="{signature}",version="1"'
    )
    return out


def _error(status: int, raw: bytes, what: str) -> CloudError:
    payload = base.json_body(raw)
    code = message = ""
    if isinstance(payload, dict):
        code = str(payload.get("code") or "")
        message = str(payload.get("message") or "")
    if status == 401:
        hint = (" Check the tenancy and user OCIDs, the fingerprint and that the API key is still "
                "on the user (and the clock of this server is right).")
    elif status == 404 and code in ("NotAuthorizedOrNotFound", ""):
        hint = " It does not exist, or the user's policies do not let it be seen."
    else:
        hint = ""
    text = f"Oracle Cloud refused {what} ({status}{' ' + code if code else ''})"
    if message:
        text += f": {message[:400]}"
    return CloudError(text + "." + hint if not text.endswith(".") else text + hint)


def _endpoint(service: str, region: str) -> str:
    if not _REGION_RE.fullmatch(region or ""):
        raise CloudError(f"{region or 'No region'} is not an OCI region name (e.g. me-dubai-1).")
    return {
        "identity": f"https://identity.{region}.oci.oraclecloud.com/20160918",
        "core": f"https://iaas.{region}.oraclecloud.com/20160918",
        "containerengine": f"https://containerengine.{region}.oci.oraclecloud.com/20180222",
    }[service]


def _call(account: Account, method: str, url: str, *, body: Any = None,
          what: str = "the request", accept: str = "application/json") -> Tuple[bytes, Dict[str, str]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if oci_wi.is_account(account):
        headers = oci_wi.signed_headers(method, url, data, {"accept": accept, "content-type": "application/json"}
                                        if data is not None else {"accept": accept})
    elif oci_wi.enabled():
        # K5: an account saved before the switch still holds a key; never use it.
        raise CloudError("API keys for Oracle Cloud are switched off on this KubeSight. "
                         "Use an OKE workload identity account.")
    else:
        headers = sign(account, method, url, body=data, headers={"accept": accept})
    status, response_headers, raw = base.http(method, url, headers=headers, body=data, ca_pem=account.ca_pem)
    if status >= 300:
        raise _error(status, raw, what)
    return raw, response_headers


def _get_json(account: Account, url: str, *, what: str) -> Any:
    raw, _ = _call(account, "GET", url, what=what)
    return base.json_body(raw)


def _list(account: Account, url: str, params: Dict[str, Any], *, what: str) -> List[Dict[str, Any]]:
    """Every page of an OCI list call (``opc-next-page``)."""
    items: List[Dict[str, Any]] = []
    query = {k: v for k, v in params.items() if v not in (None, "")}
    for _ in range(_PAGE_LIMIT):
        raw, headers = _call(account, "GET", f"{url}?{urllib.parse.urlencode(query)}", what=what)
        page = base.json_body(raw)
        if isinstance(page, dict):
            page = page.get("items") or []
        items.extend(item for item in page or [] if isinstance(item, dict))
        token = headers.get("opc-next-page")
        if not token:
            break
        query["page"] = token
    return items


def test(account: Account) -> str:
    if oci_wi.is_account(account):
        return oci_wi.test(account, _list, _get_json)
    user_id = account.settings["userOcid"]
    user = _get_json(
        account, f"{_endpoint('identity', account.region)}/users/{urllib.parse.quote(user_id, safe='')}",
        what="reading the API key's user",
    ) or {}
    return f"{user.get('name') or user_id} ({identity_label(account)})"


def regions(account: Account) -> List[Dict[str, str]]:
    if oci_wi.is_account(account):
        return [{"id": account.region, "name": account.region}]
    tenancy = account.settings["tenancyOcid"]
    items = _get_json(
        account,
        f"{_endpoint('identity', account.region)}/tenancies/{urllib.parse.quote(tenancy, safe='')}/regionSubscriptions",
        what="listing the tenancy's regions",
    ) or []
    out = []
    for item in items:
        if not isinstance(item, dict) or item.get("status") not in (None, "READY"):
            continue
        name = item.get("regionName")
        if name:
            out.append({"id": name, "name": f"{name}{' (home)' if item.get('isHomeRegion') else ''}"})
    return sorted(out, key=lambda r: r["id"])


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def _version_key(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version or "")[:3])


def _compartments(account: Account, region: str) -> List[Dict[str, str]]:
    tenancy = account.settings["tenancyOcid"]
    identity = _endpoint("identity", region)
    rows = _list(account, f"{identity}/compartments", {
        "compartmentId": tenancy, "compartmentIdInSubtree": "true",
        "lifecycleState": "ACTIVE", "accessLevel": "ACCESSIBLE", "limit": 1000,
    }, what="listing compartments")
    names = {row["id"]: row.get("name") or row["id"] for row in rows if row.get("id")}
    parents = {row["id"]: row.get("compartmentId") for row in rows if row.get("id")}

    def path(cid: str) -> str:
        parts, seen = [], set()
        while cid in names and cid not in seen:
            seen.add(cid)
            parts.append(names[cid])
            cid = parents.get(cid) or ""
        return "/".join(reversed(parts))

    out = [{"id": tenancy, "name": "(root)", "path": "(root)"}]
    out += sorted(({"id": cid, "name": names[cid], "path": path(cid)} for cid in names), key=lambda c: c["path"])
    return out


def _images(account: Account, region: str) -> List[Dict[str, Any]]:
    core = _endpoint("core", region)
    out = []
    for os_name, os_version, family in _IMAGES:
        rows = _list(account, f"{core}/images", {
            "compartmentId": account.settings["tenancyOcid"], "operatingSystem": os_name,
            "operatingSystemVersion": os_version, "shape": DEFAULT_SHAPE,
            "sortBy": "TIMECREATED", "sortOrder": "DESC", "lifecycleState": "AVAILABLE", "limit": 25,
        }, what=f"listing {os_name} {os_version} images")
        for row in rows:
            name = str(row.get("displayName") or "")
            if re.search(r"GPU|aarch64|Minimal", name, re.IGNORECASE):
                continue
            out.append({
                "id": row["id"], "name": name, "osFamily": family,
                "description": f"{os_name} {os_version}", "createdAt": row.get("timeCreated"),
            })
            break
    return out


def _machine_types() -> List[Dict[str, Any]]:
    return [
        {"id": shape["id"], "cpu": shape["maxOcpus"] * 2, "memoryGb": shape["maxMemoryGb"],
         "flexible": True, "note": f"{shape['note']}, 1–{_MAX_GB_PER_OCPU} GB per OCPU"}
        for shape in FLEX_SHAPES
    ]


def inventory(account: Account, region: str, scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if oci_wi.is_account(account):
        return oci_wi.inventory(account, region, _get_json, _list)
    out = base.empty_inventory(region)
    out["nodeImages"] = []
    out["warnings"] = []
    compartment = _compartment(account, scope)
    identity = _endpoint("identity", region)
    core = _endpoint("core", region)

    out["compartments"] = _compartments(account, region)
    ads = _list(account, f"{identity}/availabilityDomains",
                {"compartmentId": account.settings["tenancyOcid"]}, what="listing availability domains")
    out["zones"] = [{"id": ad["name"], "name": ad["name"]} for ad in ads if ad.get("name")]

    vcns = _list(account, f"{core}/vcns", {"compartmentId": compartment, "limit": 100}, what="listing VCNs")
    subnets = _list(account, f"{core}/subnets", {"compartmentId": compartment, "limit": 100}, what="listing subnets")
    by_vcn: Dict[str, List[Dict[str, Any]]] = {}
    for subnet in subnets:
        if subnet.get("lifecycleState") not in (None, "AVAILABLE"):
            continue
        by_vcn.setdefault(subnet.get("vcnId"), []).append({
            "id": subnet["id"], "name": subnet.get("displayName") or subnet["id"],
            "cidr": subnet.get("cidrBlock"), "zone": subnet.get("availabilityDomain") or None,
        })
    for vcn in vcns:
        if vcn.get("lifecycleState") not in (None, "AVAILABLE"):
            continue
        out["networks"].append({
            "id": vcn["id"], "name": vcn.get("displayName") or vcn["id"],
            "cidr": vcn.get("cidrBlock") or (vcn.get("cidrBlocks") or [None])[0],
            "subnets": sorted(by_vcn.get(vcn["id"], []), key=lambda s: s["name"]),
        })
    out["networks"].sort(key=lambda n: n["name"])
    out["images"] = _images(account, region)
    out["machineTypes"] = _machine_types()

    # OKE: a user allowed only to make VMs still gets the rest of the inventory.
    engine = _endpoint("containerengine", region)
    try:
        options = _get_json(account, f"{engine}/clusterOptions/all?{urllib.parse.urlencode({'compartmentId': compartment})}",
                            what="reading OKE cluster options") or {}
        out["kubernetesVersions"] = sorted(options.get("kubernetesVersions") or [], key=_version_key, reverse=True)
        pool = _get_json(account, f"{engine}/nodePoolOptions/all?{urllib.parse.urlencode({'compartmentId': compartment})}",
                         what="reading OKE node pool options") or {}
        out["nodeImages"] = _oke_images(pool.get("sources") or [])[:40]
    except CloudError as exc:
        out["warnings"].append(f"OKE options could not be read, so managed clusters cannot be chosen here: {exc}")
    return out


def simulated_regions() -> List[Dict[str, str]]:
    return [
        {"id": "eu-frankfurt-1", "name": "eu-frankfurt-1"},
        {"id": "me-dubai-1", "name": "me-dubai-1 (home)"},
    ]


_SIM_TENANCY = "ocid1.tenancy.oc1..aaaaaaaasimulatedtenancy"


def simulated_inventory(region: str, scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = base.empty_inventory(region)
    key = region.split("-")[1].upper() if "-" in region else "REGION"
    region_tag = region.upper()
    out["compartments"] = [
        {"id": _SIM_TENANCY, "name": "(root)", "path": "(root)"},
        {"id": "ocid1.compartment.oc1..aaaaaaaasimkubesight", "name": "kubesight", "path": "kubesight"},
        {"id": "ocid1.compartment.oc1..aaaaaaaasimkubesightdev", "name": "dev", "path": "kubesight/dev"},
    ]
    out["zones"] = [{"id": f"kIdk:{region_tag}-AD-{n}", "name": f"kIdk:{region_tag}-AD-{n}"} for n in (1, 2, 3)]
    out["networks"] = [{
        "id": f"ocid1.vcn.oc1.{region}.aaaaaaaasimvcn", "name": f"{key.lower()}-platform-vcn", "cidr": "10.0.0.0/16",
        "subnets": [
            {"id": f"ocid1.subnet.oc1.{region}.aaaaaaaasimnodes", "name": "k8s-nodes (private)",
             "cidr": "10.0.1.0/24", "zone": None},
            {"id": f"ocid1.subnet.oc1.{region}.aaaaaaaasimad1", "name": "k8s-ad1 (private)",
             "cidr": "10.0.2.0/24", "zone": f"kIdk:{region_tag}-AD-1"},
        ],
    }]
    out["images"] = [
        {"id": f"ocid1.image.oc1.{region}.aaaaaaaasimol9", "name": "Oracle-Linux-9.6-2026.09.30-0",
         "osFamily": "rhel", "description": "Oracle Linux 9", "createdAt": "2026-10-02T10:00:00Z"},
        {"id": f"ocid1.image.oc1.{region}.aaaaaaaasimol8", "name": "Oracle-Linux-8.10-2026.09.30-0",
         "osFamily": "rhel", "description": "Oracle Linux 8", "createdAt": "2026-10-02T10:00:00Z"},
        {"id": f"ocid1.image.oc1.{region}.aaaaaaaasimu2404", "name": "Canonical-Ubuntu-24.04-2026.09.15-0",
         "osFamily": "debian", "description": "Canonical Ubuntu 24.04", "createdAt": "2026-09-20T10:00:00Z"},
        {"id": f"ocid1.image.oc1.{region}.aaaaaaaasimu2204", "name": "Canonical-Ubuntu-22.04-2026.09.15-0",
         "osFamily": "debian", "description": "Canonical Ubuntu 22.04", "createdAt": "2026-09-20T10:00:00Z"},
    ]
    out["machineTypes"] = _machine_types()
    out["kubernetesVersions"] = ["v1.34.1", "v1.33.1", "v1.32.1"]
    out["nodeImages"] = [
        {"id": f"ocid1.image.oc1.{region}.aaaaaaaasimoke{v.replace('.', '')}",
         "name": f"Oracle-Linux-8.10-2026.09.30-0-OKE-{v}-900", "kubernetesVersion": f"v{v}"}
        for v in ("1.34.1", "1.33.1", "1.32.1")
    ]
    out["warnings"] = []
    return out


# ---------------------------------------------------------------------------
# Placement and sizes
# ---------------------------------------------------------------------------

def _flex(shape: str, catalog: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    if shape in _FLEX_BY_ID:
        return _FLEX_BY_ID[shape]
    for item in catalog or []:
        if item.get("id") == shape and item.get("flexible"):
            return {"id": shape, "maxOcpus": max(1, int(item.get("cpu") or 2) // 2),
                    "maxMemoryGb": int(item.get("memoryGb") or 1024), "note": item.get("note") or ""}
    raise ValueError(
        f"{shape or 'That shape'} is not a flexible shape KubeSight sizes "
        f"({', '.join(s['id'] for s in FLEX_SHAPES)})."
    )


def normalize_placement(raw: Dict[str, Any], kind: str, previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    previous = previous or {}

    def pick(key: str) -> str:
        value = raw.get(key)
        if value in (None, ""):
            value = previous.get(key)
        return str(value or "").strip()

    compartment = pick("compartmentId")
    if not _COMPARTMENT_RE.fullmatch(compartment):
        raise ValueError("Choose the compartment the resources go into.")
    zone = str(raw.get("zone") or raw.get("subnetZone") or previous.get("zone") or "").strip()
    if not zone:
        raise ValueError("Choose the availability domain.")
    if not _AD_RE.fullmatch(zone):
        raise ValueError(f"{zone} is not an availability domain name (like kIdk:ME-DUBAI-1-AD-1).")
    shape = pick("shape") or DEFAULT_SHAPE
    _flex(shape)
    out: Dict[str, Any] = {"compartmentId": compartment, "zone": zone, "shape": shape}
    pod_subnet = pick("podSubnetId")
    if pod_subnet:
        if not _SUBNET_RE.fullmatch(pod_subnet):
            raise ValueError("The pod subnet must be a subnet OCID.")
        out["podSubnetId"] = pod_subnet
    if kind == "managed":
        # Usually empty: the core asks ``default_node_image`` for the newest
        # OKE image of the chosen version just before rendering.
        node_image = pick("nodeImageId")
        if node_image:
            if not _IMAGE_RE.fullmatch(node_image):
                raise ValueError("The OKE node image must be an image OCID.")
            out["nodeImageId"] = node_image
    return out


_OKE_IMAGE_RE = re.compile(r"^Oracle-Linux-(\d+)\.(\d+)-(\d{4}\.\d{2}\.\d{2})-(\d+)-OKE-(\d+\.\d+\.\d+)")


def _oke_image_key(name: str) -> Tuple:
    """Newest Oracle Linux release first, then the newest build of it."""
    match = _OKE_IMAGE_RE.match(name or "")
    if not match:
        return (0, 0, "", 0)
    return (int(match.group(1)), int(match.group(2)), match.group(3), int(match.group(4)))


def _oke_images(sources: List[Dict[str, Any]], version: str = "") -> List[Dict[str, Any]]:
    """x86, non-GPU OKE images (for ``version`` when given), best first."""
    wanted = version.lstrip("v")
    out = []
    for source in sources or []:
        name = str(source.get("sourceName") or "")
        match = _OKE_IMAGE_RE.match(name)
        if not source.get("imageId") or not match or re.search(r"GPU|aarch64", name, re.IGNORECASE):
            continue
        if wanted and match.group(5) != wanted:
            continue
        out.append({"id": source["imageId"], "name": name, "kubernetesVersion": f"v{match.group(5)}"})
    out.sort(key=lambda i: (_version_key(i["kubernetesVersion"]), _oke_image_key(i["name"])), reverse=True)
    return out


def default_node_image(account: Account, spec: Dict[str, Any]) -> str:
    """The newest x86 Oracle Linux OKE image built for the build's Kubernetes
    version (flexible x86 shapes run every non-GPU, non-Arm image)."""
    version = _oke_version((spec.get("managed") or {}).get("kubernetesVersion") or "")
    if not version:
        raise CloudError("Choose the Kubernetes version before KubeSight picks the OKE node image.")
    engine = _endpoint("containerengine", spec["region"])
    compartment = spec.get("compartmentId") or _compartment(account)
    options = _get_json(
        account, f"{engine}/nodePoolOptions/all?{urllib.parse.urlencode({'compartmentId': compartment})}",
        what="reading OKE node pool options",
    ) or {}
    images = _oke_images(options.get("sources") or [], version)
    if not images:
        shape = ((spec.get("machineTypes") or {}).get("worker") or spec.get("shape") or DEFAULT_SHAPE)
        raise CloudError(
            f"OKE in {spec['region']} has no Oracle Linux node image for Kubernetes {version} on {shape}."
        )
    return images[0]["id"]


def simulated_default_node_image(spec: Dict[str, Any]) -> str:
    version = _oke_version((spec.get("managed") or {}).get("kubernetesVersion") or "") or "v1.33.1"
    return f"ocid1.image.oc1.{spec.get('region') or 'me-dubai-1'}.aaaaaaaasimoke{version.lstrip('v').replace('.', '')}"


def resolve_machine_type(size: Dict[str, Any], spec: Dict[str, Any],
                         catalog: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    role = size.get("role")
    override = str(((spec or {}).get("machineTypes") or {}).get(role) or "").strip()
    shape_id = override or (spec or {}).get("shape") or DEFAULT_SHAPE
    shape = _flex(shape_id, catalog)
    cpu = max(1, int(size.get("cpu") or 1))
    memory = max(1, int(math.ceil(float(size.get("memoryGb") or 1))))
    ocpus = max(1, math.ceil(cpu / 2), math.ceil(memory / _MAX_GB_PER_OCPU))
    memory = max(memory, ocpus)
    if ocpus > shape["maxOcpus"] or memory > shape["maxMemoryGb"]:
        raise ValueError(
            f"{shape_id} goes up to {shape['maxOcpus']} OCPU and {shape['maxMemoryGb']} GB; "
            f"{cpu} vCPU / {memory} GB does not fit."
        )
    return {"type": shape_id, "ocpus": ocpus, "cpu": ocpus * 2, "memoryGb": memory,
            "note": f"{ocpus} OCPU"}


def plan_checks(account: Account, spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """Read what the plan points at, fresh. Anything gone raises CloudError
    (the plan stops, as it does when a vCenter object disappears)."""
    if oci_wi.is_account(account):
        return oci_wi.plan_checks(account, spec, _get_json)
    region = spec["region"]
    core = _endpoint("core", region)
    quote = lambda value: urllib.parse.quote(str(value), safe="")  # noqa: E731
    checks: List[Dict[str, str]] = [check("ok", "Oracle Cloud account", test(account))]

    vcn = _get_json(account, f"{core}/vcns/{quote(spec['networkId'])}", what=f"reading VCN {spec.get('networkName') or ''}".strip())
    if (vcn or {}).get("lifecycleState") not in (None, "AVAILABLE"):
        raise CloudError(f"VCN {spec.get('networkName')} is {vcn.get('lifecycleState')}.")
    subnet = _get_json(account, f"{core}/subnets/{quote(spec['subnetId'])}", what=f"reading subnet {spec.get('subnetName') or ''}".strip()) or {}
    if subnet.get("vcnId") and subnet["vcnId"] != spec["networkId"]:
        raise CloudError(f"Subnet {spec.get('subnetName')} is not in VCN {spec.get('networkName')}.")
    if subnet.get("availabilityDomain") and spec.get("zone") and subnet["availabilityDomain"] != spec["zone"]:
        raise CloudError(
            f"Subnet {spec.get('subnetName')} lives in {subnet['availabilityDomain']}, "
            f"not in {spec['zone']}. Pick that availability domain or a regional subnet."
        )
    if not subnet.get("prohibitPublicIpOnVnic", True):
        checks.append(check("info", "Subnet", f"{spec.get('subnetName')} allows public IPs; KubeSight asks for none."))
    else:
        checks.append(check("ok", "Network", f"{spec.get('networkName')} / {spec.get('subnetName')} ({subnet.get('cidrBlock')})"))

    images = []
    if spec.get("kind") == "managed":
        if spec.get("nodeImageId"):
            images.append(("Node image", spec["nodeImageId"]))
        else:
            checks.append(check("info", "Node image", "the newest Oracle Linux OKE image for this version"))
        engine = _endpoint("containerengine", region)
        options = _get_json(
            account,
            f"{engine}/clusterOptions/all?{urllib.parse.urlencode({'compartmentId': spec.get('compartmentId') or _compartment(account)})}",
            what="reading OKE cluster options",
        ) or {}
        version = _oke_version((spec.get("managed") or {}).get("kubernetesVersion") or "")
        offered = options.get("kubernetesVersions") or []
        if version not in offered:
            raise CloudError(
                f"OKE in {region} does not offer Kubernetes {version or '(none chosen)'}"
                + (f"; it offers {', '.join(sorted(offered, key=_version_key, reverse=True)[:5])}." if offered else ".")
            )
        checks.append(check("ok", "Kubernetes version", f"OKE offers {version}"))
    else:
        images.append(("Image", (spec.get("image") or {}).get("id")))
    for label, image_id in images:
        if not image_id:
            raise CloudError(f"{label}: none chosen.")
        image = _get_json(account, f"{core}/images/{quote(image_id)}", what=f"reading the {label.lower()}") or {}
        if image.get("lifecycleState") not in (None, "AVAILABLE"):
            raise CloudError(f"{label} {image.get('displayName') or image_id} is {image.get('lifecycleState')}.")
        checks.append(check("ok", label, image.get("displayName") or image_id))
    return checks


# ---------------------------------------------------------------------------
# main.tf.json
# ---------------------------------------------------------------------------

def _terraform(region: str) -> Dict[str, Any]:
    return {
        "terraform": {
            "required_providers": {
                "oci": {"source": TOFU_PROVIDER["source"], "version": f"= {TOFU_PROVIDER['version']}"}
            },
            "backend": {"http": {}},
        },
        # Keys come from the OCI_* environment (see ``environment``), never this file.
        "provider": {"oci": {"region": region}},
    }


def _nsg_ref() -> str:
    return "${oci_core_network_security_group.cluster.id}"


def _tcp_rule(source: str, port: int, description: str) -> Dict[str, Any]:
    return {
        "network_security_group_id": _nsg_ref(),
        "direction": "INGRESS",
        "protocol": "6",
        "source_type": "CIDR_BLOCK",
        "source": source,
        "stateless": False,
        "description": description,
        "tcp_options": [{"destination_port_range": [{"min": port, "max": port}]}],
    }


def _firewall(cluster_name: str, spec: Dict[str, Any], allowed: List[str], labels: Dict[str, str],
              *, api_health: bool) -> Dict[str, Any]:
    """One network security group per cluster: members talk freely, the
    allowed networks reach SSH and the API, everything may go out."""
    admin = {}
    for cidr in allowed:
        admin[f"{cidr} tcp/{base.SSH_PORT}"] = _tcp_rule(cidr, base.SSH_PORT, f"SSH from {cidr}")
        admin[f"{cidr} tcp/{API_PORT}"] = _tcp_rule(cidr, API_PORT, f"Kubernetes API from {cidr}")
    rules: Dict[str, Any] = {
        "intra": {
            "network_security_group_id": _nsg_ref(), "direction": "INGRESS", "protocol": "all",
            "source_type": "NETWORK_SECURITY_GROUP", "source": _nsg_ref(), "stateless": False,
            "description": "Cluster members and the API load balancer talk to each other",
        },
        "egress": {
            "network_security_group_id": _nsg_ref(), "direction": "EGRESS", "protocol": "all",
            "destination_type": "CIDR_BLOCK", "destination": "0.0.0.0/0", "stateless": False,
            "description": "Outbound: images, packages, OCI services",
        },
    }
    if api_health and spec.get("subnetCidr"):
        rules["api_health"] = _tcp_rule(spec["subnetCidr"], API_PORT, "Load balancer health checks")
    if spec.get("networkCidr"):
        rules["path_mtu"] = {
            "network_security_group_id": _nsg_ref(), "direction": "INGRESS", "protocol": "1",
            "source_type": "CIDR_BLOCK", "source": spec["networkCidr"], "stateless": False,
            "description": "Path MTU discovery",
            "icmp_options": [{"type": 3, "code": 4}],
        }
    resources: Dict[str, Any] = {
        "oci_core_network_security_group": {"cluster": {
            "compartment_id": spec["compartmentId"],
            "vcn_id": spec["networkId"],
            "display_name": f"kubesight-{cluster_name}",
            "freeform_tags": labels,
        }},
        "oci_core_network_security_group_security_rule": rules,
    }
    if admin:
        rules_admin = {"for_each": {key: {"source": r["source"], "port": r["tcp_options"][0]["destination_port_range"][0]["min"],
                                          "description": r["description"]} for key, r in admin.items()},
                       "network_security_group_id": _nsg_ref(), "direction": "INGRESS", "protocol": "6",
                       "source_type": "CIDR_BLOCK", "source": "${each.value.source}", "stateless": False,
                       "description": "${each.value.description}",
                       "tcp_options": [{"destination_port_range": [{"min": "${each.value.port}", "max": "${each.value.port}"}]}]}
        rules["admin"] = rules_admin
    return resources


def _user_data(ssh: Dict[str, Any], hostname: str) -> str:
    # base.cloud_init escapes for main.tf.json; base64 needs the plain text.
    text = base.cloud_init(ssh, hostname=hostname).replace("$${", "${").replace("%%{", "%{")
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def render_vms(ctx: VmContext) -> Dict[str, Any]:
    spec = ctx.spec
    labels = dict(ctx.labels or base.tags(ctx.cluster_name, ctx.build_id))
    image_id = (spec.get("image") or {})["id"]
    for_each = {}
    user_data = {}
    for node in ctx.nodes:
        machine = node["machineType"]
        for_each[node["name"]] = {
            "role": node["role"],
            "cpu": int(machine["cpu"]),
            "memory_mb": int(machine["memoryGb"]) * 1024,
            "disk_gb": max(50, int(node["diskGb"])),  # OCI's smallest boot volume is 50 GB
            "shape": machine["type"],
            "ocpus": int(machine["ocpus"]),
            "memory_gb": int(machine["memoryGb"]),
            # Not used by OCI; lets the simulator hand out believable addresses.
            "subnet_cidr": spec.get("subnetCidr") or "",
        }
        user_data[node["name"]] = _user_data(ctx.ssh, node["name"])

    metadata = {"user_data": "${var.node_user_data[each.key]}"}
    if ctx.ssh.get("publicKey"):
        metadata["ssh_authorized_keys"] = ctx.ssh["publicKey"]
    vm = {
        "for_each": for_each,
        "availability_domain": spec["zone"],
        "compartment_id": spec["compartmentId"],
        "display_name": "${each.key}",
        "shape": "${each.value.shape}",
        "shape_config": [{"ocpus": "${each.value.ocpus}", "memory_in_gbs": "${each.value.memory_gb}"}],
        "source_details": [{
            "source_type": "image",
            "source_id": image_id,
            "boot_volume_size_in_gbs": "${each.value.disk_gb}",
        }],
        "create_vnic_details": [{
            "subnet_id": spec["subnetId"],
            "assign_public_ip": "false",
            "nsg_ids": [_nsg_ref()],
            # Pod traffic leaves a node with pod addresses (CNI).
            "skip_source_dest_check": True,
            "freeform_tags": labels,
        }],
        "metadata": metadata,
        "freeform_tags": labels,
        "lifecycle": [{
            # KubeSight only creates and deletes these VMs: a newer image,
            # changed cloud-init or a hand-edited VNIC never plans a rebuild.
            "ignore_changes": ["create_vnic_details", "metadata", "source_details"],
        }],
    }

    config = _terraform(ctx.region)
    resources = _firewall(ctx.cluster_name, spec, ctx.allowed_cidrs, labels, api_health=ctx.load_balancer)
    if for_each:
        config["variable"] = {"node_user_data": {
            "type": "map(string)", "sensitive": True, "default": user_data,
            "description": "Each VM's cloud-init, base64 (may carry the SSH password).",
        }}
        resources[VM_RESOURCE_TYPE] = {"node": vm}

    control_planes = [node["name"] for node in ctx.nodes if node["role"] == "controlPlane"]
    if ctx.load_balancer:
        nlb = "${" + NLB_TYPE + ".api.id}"
        resources[NLB_TYPE] = {"api": {
            "compartment_id": spec["compartmentId"],
            "display_name": f"kubesight-{ctx.cluster_name}-api",
            "subnet_id": spec["subnetId"],
            "is_private": True,
            "is_preserve_source_destination": False,
            "network_security_group_ids": [_nsg_ref()],
            "freeform_tags": labels,
        }}
        resources["oci_network_load_balancer_backend_set"] = {"api": {
            "name": "kube-apiserver",
            "network_load_balancer_id": nlb,
            "policy": "FIVE_TUPLE",
            # The balancer's own address as source: a control plane calling the
            # API through it (kubeadm join, kubelet) is answered, not dropped.
            "is_preserve_source": False,
            "health_checker": [{
                "protocol": "TCP", "port": API_PORT,
                "interval_in_millis": 10000, "timeout_in_millis": 3000, "retries": 3,
            }],
        }}
        if control_planes:
            resources["oci_network_load_balancer_backend"] = {"api": {
                "for_each": {name: name for name in control_planes},
                "network_load_balancer_id": nlb,
                "backend_set_name": "${oci_network_load_balancer_backend_set.api.name}",
                "ip_address": "${" + VM_RESOURCE_TYPE + ".node[each.key].private_ip}",
                "port": API_PORT,
            }}
        resources["oci_network_load_balancer_listener"] = {"api": {
            "name": "kube-apiserver",
            "network_load_balancer_id": nlb,
            "default_backend_set_name": "${oci_network_load_balancer_backend_set.api.name}",
            "port": API_PORT,
            "protocol": "TCP",
        }}
    config["resource"] = resources
    return config


def _oke_version(version: str) -> str:
    version = str(version or "").strip()
    return version if not version or version.startswith("v") else f"v{version}"


def render_managed(ctx: ManagedContext) -> Dict[str, Any]:
    spec = ctx.spec
    labels = dict(ctx.labels or base.tags(ctx.cluster_name, ctx.build_id))
    version = _oke_version(ctx.kubernetes_version)
    machine = ctx.node_pool["machineType"]
    pod_subnet = spec.get("podSubnetId")
    cni = "OCI_VCN_IP_NATIVE" if pod_subnet else "FLANNEL_OVERLAY"

    resources = _firewall(ctx.cluster_name, spec, ctx.allowed_cidrs, labels, api_health=False)
    resources[CLUSTER_TYPE] = {"cluster": {
        "compartment_id": spec["compartmentId"],
        "name": ctx.cluster_name,
        "kubernetes_version": version,
        "vcn_id": spec["networkId"],
        "type": "BASIC_CLUSTER",
        "endpoint_config": [{
            "subnet_id": spec["subnetId"],
            "is_public_ip_enabled": False,
            "nsg_ids": [_nsg_ref()],
        }],
        "options": [{"service_lb_subnet_ids": [spec["subnetId"]]}],
        "cluster_pod_network_options": [{"cni_type": cni}],
        "freeform_tags": labels,
    }}
    pod_network = {"cni_type": cni}
    if pod_subnet:
        pod_network.update(pod_subnet_ids=[pod_subnet], pod_nsg_ids=[_nsg_ref()])
    pool: Dict[str, Any] = {
        "cluster_id": "${" + CLUSTER_TYPE + ".cluster.id}",
        "compartment_id": spec["compartmentId"],
        "name": f"{ctx.cluster_name}-workers",
        "kubernetes_version": version,
        "node_shape": machine["type"],
        "node_shape_config": [{"ocpus": int(machine["ocpus"]), "memory_in_gbs": int(machine["memoryGb"])}],
        "node_config_details": [{
            "size": int(ctx.node_pool["count"]),
            "placement_configs": [{"availability_domain": spec["zone"], "subnet_id": spec["subnetId"]}],
            "nsg_ids": [_nsg_ref()],
            "node_pool_pod_network_option_details": [pod_network],
            "freeform_tags": labels,
        }],
        "node_source_details": [{
            "source_type": "IMAGE",
            "image_id": spec["nodeImageId"],
            "boot_volume_size_in_gbs": str(max(50, int(ctx.node_pool.get("diskGb") or 50))),
        }],
        "freeform_tags": labels,
    }
    if ctx.ssh.get("publicKey"):
        pool["ssh_public_key"] = ctx.ssh["publicKey"]
    resources[NODE_POOL_TYPE] = {"workers": pool}

    config = _terraform(ctx.region)
    config["resource"] = resources
    return config


# ---------------------------------------------------------------------------
# OpenTofu's environment
# ---------------------------------------------------------------------------

def _ca_bundle(workdir: str, ca_pem: str) -> Optional[str]:
    if not (ca_pem or "").strip():
        return None
    parts = [ca_pem.strip()]
    for system in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
        if os.path.exists(system):
            with open(system, encoding="utf-8", errors="ignore") as handle:
                parts.append(handle.read())
            break
    path = os.path.join(workdir, "oci-ca-bundle.pem")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(parts) + "\n")
    return path


def _private(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def environment(account: Account, workdir: str) -> Tuple[Dict[str, str], List[str]]:
    """The provider's API-key auth through its OCI_* variables, the key in a
    0600 file under the working directory (removed with it).

    Not ~/.oci/config: the OCI SDK finds "home" with ``user.Current()``, not
    $HOME, so a config file in the working directory is never read (seen with
    oracle/oci 9.11.0)."""
    folder = os.path.join(workdir, ".oci")
    os.makedirs(folder, exist_ok=True)
    key_path = os.path.join(folder, "key.pem")
    _private(key_path, account.secrets["privateKey"].strip() + "\n")
    s = account.settings
    env = {
        "OCI_TENANCY_OCID": s["tenancyOcid"],
        "OCI_USER_OCID": s["userOcid"],
        "OCI_FINGERPRINT": s["fingerprint"],
        "OCI_PRIVATE_KEY_PATH": key_path,
        "OCI_REGION": account.region,
        **base.env_proxy(),
    }
    passphrase = account.secrets.get("privateKeyPassphrase")
    if passphrase:
        env["OCI_PRIVATE_KEY_PASSWORD"] = passphrase
    bundle = _ca_bundle(workdir, account.ca_pem)
    if bundle:
        env["SSL_CERT_FILE"] = bundle
    hide = [account.secrets["privateKey"].strip()]
    if passphrase:
        hide.append(passphrase)
    return env, hide


# ---------------------------------------------------------------------------
# Reading the result
# ---------------------------------------------------------------------------

def vm_facts(attrs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": attrs.get("id"),
        "address": attrs.get("private_ip"),
        "zone": attrs.get("availability_domain"),
        "machineType": attrs.get("shape"),
    }


def _find(resources: List[Dict[str, Any]], rtype: str, name: str) -> Optional[Dict[str, Any]]:
    for item in resources or []:
        if item.get("type") == rtype and item.get("name") == name:
            return item.get("attributes") or {}
    return None


def endpoint_from_state(resources: List[Dict[str, Any]]) -> Optional[str]:
    attrs = _find(resources, NLB_TYPE, "api")
    if not attrs:
        return None
    addresses = attrs.get("ip_addresses") or []
    private = [a for a in addresses if isinstance(a, dict) and not a.get("is_public") and a.get("ip_address")]
    chosen = private or [a for a in addresses if isinstance(a, dict) and a.get("ip_address")]
    return chosen[0]["ip_address"] if chosen else None


def cluster_token(account: Account, region: str, cluster_id: str, *, date: Optional[str] = None) -> str:
    """What ``oci ce cluster generate-token`` prints as the token."""
    if oci_wi.is_account(account):
        return oci_wi.cluster_token(region, cluster_id)
    url = f"https://containerengine.{region}.oraclecloud.com/cluster_request/{cluster_id}"
    headers = sign(account, "GET", url, date=date)
    query = urllib.parse.urlencode({"authorization": headers["authorization"], "date": headers["date"]})
    return base64.urlsafe_b64encode(f"{url}?{query}".encode("utf-8")).decode("utf-8")


def managed_access(account: Account, resources: List[Dict[str, Any]], spec: Dict[str, Any]) -> Dict[str, str]:
    import yaml

    attrs = _find(resources, CLUSTER_TYPE, "cluster")
    if not attrs or not attrs.get("id"):
        raise CloudError("OpenTofu's state has no OKE cluster for this build.")
    endpoints = (attrs.get("endpoints") or [{}])[0] or {}
    private = endpoints.get("private_endpoint")
    if not private:
        raise CloudError("The OKE cluster has no private API endpoint yet.")
    region = spec["region"]
    cluster_id = attrs["id"]
    engine = _endpoint("containerengine", region)
    raw, _ = _call(
        account, "POST", f"{engine}/clusters/{urllib.parse.quote(cluster_id, safe='')}/kubeconfig/content",
        body={"tokenVersion": "2.0.0", "endpoint": "PRIVATE_ENDPOINT"},
        what="reading the OKE cluster's kubeconfig", accept="application/x-yaml, text/plain, */*",
    )
    try:
        document = yaml.safe_load(raw.decode("utf-8")) or {}
        ca = document["clusters"][0]["cluster"]["certificate-authority-data"]
    except (ValueError, KeyError, IndexError, TypeError, yaml.YAMLError) as exc:
        raise CloudError("OKE's kubeconfig for the cluster could not be read.") from exc
    server = private if private.startswith("https://") else f"https://{private}"
    return {"server": server, "caData": ca, "token": cluster_token(account, region, cluster_id)}


def describe_resource(rtype: str, rname: str, key: Any, attrs: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    attrs = attrs or {}
    if rtype == VM_RESOURCE_TYPE:
        parts = []
        config = (attrs.get("shape_config") or [{}])[0] or {}
        if config.get("ocpus"):
            parts.append(f"{config['ocpus']:g} OCPU" if isinstance(config["ocpus"], (int, float)) else f"{config['ocpus']} OCPU")
        if config.get("memory_in_gbs"):
            parts.append(f"{config['memory_in_gbs']} GB")
        source = (attrs.get("source_details") or [{}])[0] or {}
        if source.get("boot_volume_size_in_gbs"):
            parts.append(f"{source['boot_volume_size_in_gbs']} GB disk")
        if attrs.get("shape"):
            parts.append(str(attrs["shape"]))
        if attrs.get("private_ip"):
            parts.append(attrs["private_ip"])
        return "vm", str(key), " · ".join(parts)
    if rtype == "oci_core_network_security_group":
        return "firewall", f"Network security group {attrs.get('display_name') or ''}".strip(), ""
    if rtype == "oci_core_network_security_group_security_rule":
        return "firewall", "Firewall rule", attrs.get("description") or str(key or rname)
    if rtype == NLB_TYPE:
        return "lb", "API load balancer", f"internal, TCP {API_PORT} to the control planes"
    if rtype == "oci_network_load_balancer_backend_set":
        return "lb", "Load balancer backend set", f"health check TCP {API_PORT}"
    if rtype == "oci_network_load_balancer_backend":
        return "lb", f"Load balancer target {key}", f"port {API_PORT}"
    if rtype == "oci_network_load_balancer_listener":
        return "lb", "Load balancer listener", f"TCP {API_PORT}"
    if rtype == CLUSTER_TYPE:
        return "cluster", f"OKE cluster {attrs.get('name') or ''}".strip(), attrs.get("kubernetes_version") or ""
    if rtype == NODE_POOL_TYPE:
        size = ((attrs.get("node_config_details") or [{}])[0] or {}).get("size")
        detail = f"{size} × {attrs.get('node_shape')}" if size else str(attrs.get("node_shape") or "")
        return "nodepool", f"Node pool {attrs.get('name') or ''}".strip(), detail
    return None


def simulate_attributes(rtype: str, rname: str, key: Any, values: Dict[str, Any], seq: int) -> Dict[str, Any]:
    values = values or {}
    fake = f"ocid1.{rtype.replace('oci_', '').replace('_', '')}.oc1..aaaaaaaasim{seq:04d}"
    if rtype == VM_RESOURCE_TYPE:
        return {
            "id": fake, "display_name": key, "state": "RUNNING",
            "private_ip": base.simulated_address(values.get("subnet_cidr") or "10.0.1.0/24", seq),
            "availability_domain": values.get("availability_domain") or "kIdk:SIMULATED-AD-1",
            "shape": values.get("shape") or DEFAULT_SHAPE,
            "shape_config": [{"ocpus": values.get("ocpus"), "memory_in_gbs": values.get("memory_gb")}],
        }
    if rtype == NLB_TYPE:
        return {
            "id": fake, "display_name": values.get("display_name"), "state": "ACTIVE",
            "ip_addresses": [{"ip_address": base.simulated_address("10.0.1.0/24", 200 + seq),
                              "ip_version": "IPV4", "is_public": False, "reserved_ip": []}],
        }
    if rtype == CLUSTER_TYPE:
        return {
            "id": fake, "name": values.get("name"), "state": "ACTIVE",
            "kubernetes_version": values.get("kubernetes_version"),
            "endpoints": [{"private_endpoint": f"{base.simulated_address('10.0.1.0/24', 240 + seq)}:{API_PORT}",
                           "kubernetes": "", "public_endpoint": "", "vcn_hostname_endpoint": "", "ipv6endpoint": ""}],
        }
    if rtype == NODE_POOL_TYPE:
        return {"id": fake, "name": values.get("name"), "node_shape": values.get("node_shape"), "nodes": []}
    return {"id": fake}
