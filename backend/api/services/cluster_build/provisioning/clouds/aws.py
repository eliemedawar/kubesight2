"""Amazon Web Services: EC2 machines (self-managed) and EKS (managed).

Deliberate choices, each one a failure avoided:

  * **Signature Version 4 by hand, no boto3.** Inventory and the account
    check are a dozen read calls; a 100 MB SDK for them would also bring its
    own idea of proxies and CA bundles. The signer is checked against AWS's
    published example and against botocore (tests).
  * **Security-group rules as their own resources**
    (``aws_vpc_security_group_*_rule``), never inline: inline rules in JSON
    syntax must spell out every attribute, and the provider recommends the
    separate resources since v5.
  * **API load balancer = internal NLB, targets by IP, client IP not
    preserved.** With instance targets and client-IP preservation a control
    plane that reaches the API through the NLB can be sent to itself and the
    connection is dropped (the NLB "hairpin"); kubeadm does exactly that while
    a second control plane joins.
  * **``source_dest_check = false``** on every VM: a CNI that routes pod
    traffic without encapsulation (Calico BGP mode) needs the instance to
    forward packets not addressed to it.
  * **The image and the cloud-init text are ignored after creation.** A newer
    AMI picked for a later build, or a changed SSH key, must never plan to
    replace a running node.
  * **A managed cluster's endpoint is private only**, and the keys that create
    it become its first admin (EKS access entry), which is how KubeSight gets
    in to make its own service account.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from defusedxml import ElementTree

from . import base
from .base import Account, CloudError

PROVIDER = "aws"
LABEL = "Amazon Web Services"
MANAGED_LABEL = "Amazon EKS"
TOFU_PROVIDER = {"name": "aws", "source": "hashicorp/aws", "version": "6.71.0"}
VM_RESOURCE_TYPE = "aws_instance"

FIELDS = [
    base.field("accessKeyId", "Access key ID", placeholder="AKIA…",
               help="An IAM user's (or role session's) access key. Not secret on its own."),
    base.field("secretAccessKey", "Secret access key", secret=True),
    base.field("sessionToken", "Session token", secret=True, required=False, kind="textarea",
               help="Only for temporary credentials (ASIA… keys). They expire; long-lived keys do not."),
]

EC2_API_VERSION = "2016-11-15"
STS_API_VERSION = "2011-06-15"
_ALGORITHM = "AWS4-HMAC-SHA256"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_ACCESS_KEY_RE = re.compile(r"^[A-Z0-9]{16,128}$")
_SUBNET_RE = re.compile(r"^subnet-[0-9a-f]{8,17}$")
_ZONE_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d[a-z]$")
_TYPE_RE = re.compile(r"^[a-z0-9]+\.[a-z0-9]+$")
_DISK_TYPES = ("gp3", "gp2", "io2")
_DEFAULT_FAMILIES = ["m6i", "t3", "m7i", "c6i", "r6i"]
# EKS releases KubeSight offers when the account cannot be asked
# (DescribeClusterVersions is newer than some IAM policies allow).
_STATIC_EKS_VERSIONS = ["1.34", "1.33", "1.32", "1.31"]

REGION_NAMES = {
    "us-east-1": "US East (N. Virginia)", "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)", "us-west-2": "US West (Oregon)",
    "ca-central-1": "Canada (Central)", "sa-east-1": "South America (São Paulo)",
    "eu-west-1": "Europe (Ireland)", "eu-west-2": "Europe (London)", "eu-west-3": "Europe (Paris)",
    "eu-central-1": "Europe (Frankfurt)", "eu-central-2": "Europe (Zurich)",
    "eu-north-1": "Europe (Stockholm)", "eu-south-1": "Europe (Milan)", "eu-south-2": "Europe (Spain)",
    "me-south-1": "Middle East (Bahrain)", "me-central-1": "Middle East (UAE)",
    "il-central-1": "Israel (Tel Aviv)", "af-south-1": "Africa (Cape Town)",
    "ap-south-1": "Asia Pacific (Mumbai)", "ap-south-2": "Asia Pacific (Hyderabad)",
    "ap-east-1": "Asia Pacific (Hong Kong)", "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)", "ap-northeast-3": "Asia Pacific (Osaka)",
    "ap-southeast-1": "Asia Pacific (Singapore)", "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-southeast-3": "Asia Pacific (Jakarta)", "ap-southeast-4": "Asia Pacific (Melbourne)",
}

# Static, so sizes resolve with no network at all (and in the simulator).
# (family, size, vCPU, GiB)
_TYPE_TABLE: List[Tuple[str, str, int, float]] = [
    ("t3", "small", 2, 2), ("t3", "medium", 2, 4), ("t3", "large", 2, 8),
    ("t3", "xlarge", 4, 16), ("t3", "2xlarge", 8, 32),
] + [
    (family, size, cpu, cpu * 4)
    for family in ("m6i", "m7i")
    for size, cpu in (("large", 2), ("xlarge", 4), ("2xlarge", 8), ("4xlarge", 16),
                      ("8xlarge", 32), ("12xlarge", 48), ("16xlarge", 64))
] + [
    ("c6i", size, cpu, cpu * 2)
    for size, cpu in (("large", 2), ("xlarge", 4), ("2xlarge", 8), ("4xlarge", 16),
                      ("8xlarge", 32), ("16xlarge", 64))
] + [
    ("r6i", size, cpu, cpu * 8)
    for size, cpu in (("large", 2), ("xlarge", 4), ("2xlarge", 8), ("4xlarge", 16),
                      ("8xlarge", 32), ("16xlarge", 64))
]
_FAMILY_NOTES = {
    "t3": "burstable", "m6i": "general purpose", "m7i": "general purpose",
    "c6i": "compute optimised", "r6i": "memory optimised",
}
MACHINE_TYPES: List[Dict[str, Any]] = [
    {"id": f"{family}.{size}", "cpu": cpu, "memoryGb": mem, "flexible": False,
     "note": _FAMILY_NOTES[family]}
    for family, size, cpu, mem in _TYPE_TABLE
]

# (label, owner, name patterns, osFamily). Owners are the publishers' own AWS
# accounts — an AMI with the right name from anyone else is not offered.
_IMAGE_FAMILIES = [
    ("Ubuntu LTS", "099720109477", ["ubuntu/images/hvm-ssd*/ubuntu-*-??.04-amd64-server-*"], "debian"),
    ("Red Hat Enterprise Linux", "309956199498", ["RHEL-*_HVM-*-x86_64-*"], "rhel"),
    ("Rocky Linux", "792107900819", ["Rocky-*-EC2-Base-*x86_64*"], "rhel"),
    ("Amazon Linux 2023", "137112412989", ["al2023-ami-2023.*-x86_64"], "other"),
]
_IMAGES_PER_FAMILY = 4


# ---------------------------------------------------------------------------
# Signature Version 4
# ---------------------------------------------------------------------------

def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    return key


def _quote(text: str) -> str:
    return quote(text, safe="-_.~")


def _canonical_query(query: str) -> str:
    pairs = parse_qsl(query, keep_blank_values=True)
    return "&".join(f"{k}={v}" for k, v in sorted((_quote(k), _quote(v)) for k, v in pairs))


def _canonical_uri(path: str) -> str:
    return quote(path or "/", safe="/~")


def _amz_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sign(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], *,
         access_key: str, secret_key: str, region: str, service: str,
         session_token: Optional[str] = None, amz_date: Optional[str] = None) -> Dict[str, str]:
    """``headers`` plus Host, X-Amz-Date, the session token and Authorization.
    Every header given is signed."""
    parts = urlsplit(url)
    amz_date = amz_date or _amz_now()
    out = dict(headers)
    out["Host"] = parts.netloc
    out["X-Amz-Date"] = amz_date
    if session_token:
        out["X-Amz-Security-Token"] = session_token
    canonical = {k.lower(): " ".join(str(v).strip().split()) for k, v in out.items()}
    signed_headers = ";".join(sorted(canonical))
    payload_hash = hashlib.sha256(body or b"").hexdigest()
    request = "\n".join([
        method.upper(),
        _canonical_uri(parts.path),
        _canonical_query(parts.query),
        "".join(f"{k}:{canonical[k]}\n" for k in sorted(canonical)),
        signed_headers,
        payload_hash,
    ])
    scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    to_sign = "\n".join([_ALGORITHM, amz_date, scope, hashlib.sha256(request.encode("utf-8")).hexdigest()])
    signature = hmac.new(_signing_key(secret_key, amz_date[:8], region, service),
                         to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out["Authorization"] = (
        f"{_ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return out


def presign(method: str, url: str, *, access_key: str, secret_key: str, region: str,
            service: str, expires: int, headers: Optional[Dict[str, str]] = None,
            session_token: Optional[str] = None, amz_date: Optional[str] = None) -> str:
    """A query-string-signed URL; ``headers`` (besides Host) must be sent with it."""
    parts = urlsplit(url)
    amz_date = amz_date or _amz_now()
    scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    canonical = {k.lower(): " ".join(str(v).strip().split()) for k, v in (headers or {}).items()}
    canonical["host"] = parts.netloc
    signed_headers = ";".join(sorted(canonical))
    query = parse_qsl(parts.query, keep_blank_values=True) + [
        ("X-Amz-Algorithm", _ALGORITHM),
        ("X-Amz-Credential", f"{access_key}/{scope}"),
        ("X-Amz-Date", amz_date),
        ("X-Amz-Expires", str(int(expires))),
        ("X-Amz-SignedHeaders", signed_headers),
    ]
    if session_token:
        query.append(("X-Amz-Security-Token", session_token))
    canonical_query = "&".join(f"{k}={v}" for k, v in sorted((_quote(k), _quote(v)) for k, v in query))
    request = "\n".join([
        method.upper(), _canonical_uri(parts.path), canonical_query,
        "".join(f"{k}:{canonical[k]}\n" for k in sorted(canonical)),
        signed_headers, _EMPTY_SHA256,
    ])
    to_sign = "\n".join([_ALGORITHM, amz_date, scope, hashlib.sha256(request.encode("utf-8")).hexdigest()])
    signature = hmac.new(_signing_key(secret_key, amz_date[:8], region, service),
                         to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{parts.scheme}://{parts.netloc}{parts.path or '/'}?{canonical_query}&X-Amz-Signature={signature}"


def eks_token(account: Account, cluster_name: str, *, region: Optional[str] = None,
              amz_date: Optional[str] = None) -> str:
    """The bearer token ``aws eks get-token`` makes: a presigned STS
    GetCallerIdentity URL naming the cluster, valid for a minute."""
    region = region or account.region
    url = presign(
        "GET", f"https://sts.{region}.{_suffix(region)}/?Action=GetCallerIdentity&Version={STS_API_VERSION}",
        access_key=account.settings["accessKeyId"], secret_key=account.secrets["secretAccessKey"],
        session_token=account.secrets.get("sessionToken") or None,
        region=region, service="sts", expires=60,
        headers={"x-k8s-aws-id": cluster_name}, amz_date=amz_date,
    )
    return "k8s-aws-v1." + base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------

def _suffix(region: str) -> str:
    return "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"


def _partition(region: str) -> str:
    if region.startswith("cn-"):
        return "aws-cn"
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    return "aws"


def _strip_ns(root):
    for element in root.iter():
        if isinstance(element.tag, str) and "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    return root


def _error_text(raw: bytes, status: int, service: str) -> str:
    code = message = ""
    try:
        root = _strip_ns(ElementTree.fromstring(raw))
        code = root.findtext(".//Error/Code") or root.findtext(".//Code") or ""
        message = root.findtext(".//Error/Message") or root.findtext(".//Message") or ""
    except Exception:  # noqa: BLE001 — JSON (EKS) or an HTML proxy page
        document = base.json_body(raw)
        if isinstance(document, dict):
            code = str(document.get("__type") or document.get("code") or "").split("#")[-1]
            message = str(document.get("message") or document.get("Message") or "")
    if code in ("AuthFailure", "InvalidClientTokenId", "SignatureDoesNotMatch", "UnrecognizedClientException"):
        message = f"AWS did not accept the keys ({code}): {message}".rstrip(": ")
    elif code in ("UnauthorizedOperation", "AccessDenied", "AccessDeniedException"):
        message = f"The keys may not do this ({code}): {message}".rstrip(": ")
    elif code or message:
        message = f"{code}: {message}".strip(": ")
    else:
        message = f"HTTP {status}"
    return f"AWS {service}: {message[:600]}"


def _creds(account: Account) -> Dict[str, Any]:
    return {
        "access_key": account.settings.get("accessKeyId") or "",
        "secret_key": account.secrets.get("secretAccessKey") or "",
        "session_token": account.secrets.get("sessionToken") or None,
    }


def _query(account: Account, service: str, region: str, params: Dict[str, Any], *,
           version: str = EC2_API_VERSION, timeout_s: float = 30.0):
    """One Query-API call (EC2, STS) → its XML document, namespaces removed."""
    url = f"https://{service}.{region}.{_suffix(region)}/"
    body = urlencode({**params, "Version": version}).encode("utf-8")
    headers = sign("POST", url, {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
                   body, region=region, service=service, **_creds(account))
    status, _, raw = base.http("POST", url, headers=headers, body=body, timeout_s=timeout_s,
                               ca_pem=account.ca_pem)
    if status >= 400:
        raise CloudError(_error_text(raw, status, service.upper()))
    try:
        return _strip_ns(ElementTree.fromstring(raw))
    except Exception as exc:  # noqa: BLE001
        raise CloudError(f"AWS {service.upper()} answered with something that is not XML.") from exc


def _query_pages(account: Account, service: str, region: str, params: Dict[str, Any],
                 *, max_pages: int = 5) -> Iterable[Any]:
    token = None
    for _ in range(max_pages):
        page = dict(params)
        if token:
            page["NextToken"] = token
        root = _query(account, service, region, page)
        yield root
        token = root.findtext("nextToken")
        if not token:
            return


def _rest_json(account: Account, service: str, region: str, path: str) -> Any:
    url = f"https://{service}.{region}.{_suffix(region)}{path}"
    headers = sign("GET", url, {"Accept": "application/json"}, None,
                   region=region, service=service, **_creds(account))
    status, _, raw = base.http("GET", url, headers=headers, ca_pem=account.ca_pem)
    if status >= 400:
        raise CloudError(_error_text(raw, status, service.upper()))
    return base.json_body(raw)


def _filters(filters: List[Tuple[str, List[str]]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for n, (name, values) in enumerate(filters, start=1):
        out[f"Filter.{n}.Name"] = name
        for m, value in enumerate(values, start=1):
            out[f"Filter.{n}.Value.{m}"] = value
    return out


def _name_tag(item) -> Optional[str]:
    for tag in item.findall("tagSet/item"):
        if tag.findtext("key") == "Name":
            return tag.findtext("value") or None
    return None


# ---------------------------------------------------------------------------
# The account
# ---------------------------------------------------------------------------

def normalize_account(settings: Dict[str, Any], secrets: Dict[str, Any],
                      previous_settings: Optional[Dict[str, Any]] = None,
                      previous_secrets: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    settings = settings if isinstance(settings, dict) else {}
    secrets = secrets if isinstance(secrets, dict) else {}
    previous_settings = previous_settings or {}
    previous_secrets = previous_secrets or {}
    key_id = str(settings.get("accessKeyId") or previous_settings.get("accessKeyId") or "").strip()
    if not _ACCESS_KEY_RE.fullmatch(key_id):
        raise ValueError("Access key ID looks wrong: 16 or more capital letters and digits (AKIA… or ASIA…).")
    # A new key id with the old secret can never work: ask for both.
    if key_id != previous_settings.get("accessKeyId"):
        previous_secrets = {}
    out_secrets = {"secretAccessKey": base.keep_or_require(secrets, previous_secrets, "secretAccessKey",
                                                           "Secret access key")}
    token = base.keep_or_require(secrets, previous_secrets, "sessionToken", "Session token", required=False)
    if token:
        out_secrets["sessionToken"] = token.strip()
    if any(c.isspace() for c in out_secrets["secretAccessKey"].strip()):
        raise ValueError("Secret access key cannot contain spaces.")
    out_secrets["secretAccessKey"] = out_secrets["secretAccessKey"].strip()
    return {"accessKeyId": key_id}, out_secrets


def identity_label(account: Account) -> str:
    key_id = str(account.settings.get("accessKeyId") or "")
    return f"access key {key_id[:4]}…{key_id[-4:]}" if len(key_id) > 8 else "access key"


def test(account: Account) -> str:
    root = _query(account, "sts", account.region or "us-east-1", {"Action": "GetCallerIdentity"},
                  version=STS_API_VERSION)
    arn = root.findtext(".//GetCallerIdentityResult/Arn") or root.findtext(".//Arn")
    if not arn:
        raise CloudError("AWS STS answered without saying who these keys are.")
    return arn


def regions(account: Account) -> List[Dict[str, str]]:
    root = _query(account, "ec2", account.region or "us-east-1", {"Action": "DescribeRegions"})
    ids = sorted({item.findtext("regionName") for item in root.findall("regionInfo/item")} - {None})
    return [{"id": rid, "name": REGION_NAMES.get(rid, rid)} for rid in ids]


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def _zones(account: Account, region: str) -> List[Dict[str, str]]:
    root = _query(account, "ec2", region, {
        "Action": "DescribeAvailabilityZones",
        **_filters([("zone-type", ["availability-zone"]), ("state", ["available"])]),
    })
    names = sorted({item.findtext("zoneName") for item in root.findall("availabilityZoneInfo/item")} - {None})
    return [{"id": name, "name": name} for name in names]


def _subnet_item(item) -> Dict[str, Any]:
    subnet_id = item.findtext("subnetId")
    return {
        "id": subnet_id,
        "name": _name_tag(item) or subnet_id,
        "cidr": item.findtext("cidrBlock"),
        "zone": item.findtext("availabilityZone"),
        "vpcId": item.findtext("vpcId"),
    }


def _networks(account: Account, region: str) -> List[Dict[str, Any]]:
    vpcs: Dict[str, Dict[str, Any]] = {}
    for page in _query_pages(account, "ec2", region, {"Action": "DescribeVpcs", "MaxResults": "1000"}):
        for item in page.findall("vpcSet/item"):
            vpc_id = item.findtext("vpcId")
            vpcs[vpc_id] = {"id": vpc_id, "name": _name_tag(item) or vpc_id,
                            "cidr": item.findtext("cidrBlock"), "subnets": []}
    for page in _query_pages(account, "ec2", region, {"Action": "DescribeSubnets", "MaxResults": "1000"}):
        for item in page.findall("subnetSet/item"):
            subnet = _subnet_item(item)
            vpc = vpcs.get(subnet.pop("vpcId"))
            if vpc is not None:
                vpc["subnets"].append(subnet)
    out = sorted(vpcs.values(), key=lambda v: v["name"].lower())
    for vpc in out:
        vpc["subnets"].sort(key=lambda s: (s["zone"] or "", s["name"].lower()))
    return out


_UBUNTU_RE = re.compile(r"ubuntu-[a-z]+-(\d\d)\.04-amd64-server-")


def _image_label(family: str, name: str) -> str:
    match = _UBUNTU_RE.search(name)
    if match:
        return f"Ubuntu {match.group(1)}.04 LTS"
    return family


def _images(account: Account, region: str) -> List[Dict[str, Any]]:
    year = _dt.datetime.now(_dt.timezone.utc).year
    out: List[Dict[str, Any]] = []
    for family, owner, patterns, os_family in _IMAGE_FAMILIES:
        found: List[Dict[str, Any]] = []
        params = {
            "Action": "DescribeImages", "Owner.1": owner,
            **_filters([
                ("name", patterns), ("architecture", ["x86_64"]), ("virtualization-type", ["hvm"]),
                ("root-device-type", ["ebs"]), ("state", ["available"]),
                # Publishers rebuild monthly: two years of images is plenty
                # and keeps a DescribeImages answer small.
                ("creation-date", [f"{year}-*", f"{year - 1}-*"]),
            ]),
        }
        for page in _query_pages(account, "ec2", region, params, max_pages=3):
            for item in page.findall("imagesSet/item"):
                name = item.findtext("name") or ""
                match = _UBUNTU_RE.search(name)
                if os_family == "debian" and (not match or int(match.group(1)) % 2):
                    continue  # interim Ubuntu releases: nine months of support
                found.append({
                    "id": item.findtext("imageId"),
                    "name": name,
                    "osFamily": os_family,
                    "description": item.findtext("description") or _image_label(family, name),
                    "createdAt": item.findtext("creationDate"),
                    "family": _image_label(family, name),
                })
        found.sort(key=lambda image: image["createdAt"] or "", reverse=True)
        # The newest few per release line (Ubuntu 24.04 and 22.04 each).
        per_line: Dict[str, int] = {}
        for image in found:
            if per_line.get(image["family"], 0) >= _IMAGES_PER_FAMILY:
                continue
            per_line[image["family"]] = per_line.get(image["family"], 0) + 1
            out.append(image)
    return out


def _kubernetes_versions(account: Account, region: str) -> List[str]:
    try:
        document = _rest_json(account, "eks", region, "/cluster-versions?maxResults=100")
    except CloudError:
        return list(_STATIC_EKS_VERSIONS)
    versions = [
        str(item.get("clusterVersion")) for item in (document or {}).get("clusterVersions") or []
        if isinstance(item, dict) and item.get("clusterVersion")
        and str(item.get("status") or item.get("versionStatus") or "").upper() not in ("UNSUPPORTED",)
    ]
    versions = sorted(set(versions), key=lambda v: tuple(int(p) for p in v.split(".") if p.isdigit()),
                      reverse=True)
    return versions or list(_STATIC_EKS_VERSIONS)


def inventory(account: Account, region: str, scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = base.empty_inventory(region)
    out["zones"] = _zones(account, region)
    out["networks"] = _networks(account, region)
    out["images"] = _images(account, region)
    out["machineTypes"] = [dict(item) for item in MACHINE_TYPES]
    out["kubernetesVersions"] = _kubernetes_versions(account, region)
    return out


def simulated_regions() -> List[Dict[str, str]]:
    return [{"id": rid, "name": REGION_NAMES[rid]} for rid in ("eu-west-1", "me-central-1")]


def simulated_inventory(region: str, scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = base.empty_inventory(region)
    zones = [f"{region}{letter}" for letter in "abc"]
    out["zones"] = [{"id": z, "name": z} for z in zones]
    out["networks"] = [
        {"id": "vpc-0a1b2c3d4e5f60718", "name": "prod-vpc", "cidr": "10.20.0.0/16", "subnets": [
            {"id": f"subnet-0{n}11aa22bb33cc44d", "name": f"prod-private-{z[-1]}",
             "cidr": f"10.20.{n * 16}.0/20", "zone": z}
            for n, z in enumerate(zones, start=1)
        ]},
        {"id": "vpc-0f9e8d7c6b5a40312", "name": "shared-services", "cidr": "10.30.0.0/16", "subnets": [
            {"id": f"subnet-0{n}99ee88dd77cc66b", "name": f"shared-private-{z[-1]}",
             "cidr": f"10.30.{n}.0/24", "zone": z}
            for n, z in enumerate(zones[:2], start=1)
        ]},
    ]
    out["images"] = [
        {"id": "ami-0a7d80731ae1b2435", "name": "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-20261015",
         "osFamily": "debian", "description": "Canonical, Ubuntu, 24.04, amd64 noble image",
         "createdAt": "2026-10-15T10:21:04.000Z", "family": "Ubuntu 24.04 LTS"},
        {"id": "ami-0c1c30571d2dae5c9", "name": "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-20261012",
         "osFamily": "debian", "description": "Canonical, Ubuntu, 22.04, amd64 jammy image",
         "createdAt": "2026-10-12T08:02:11.000Z", "family": "Ubuntu 22.04 LTS"},
        {"id": "ami-07d4917b6f95f5c2a", "name": "RHEL-9.6.0_HVM-20261001-x86_64-0-Hourly2-GP3",
         "osFamily": "rhel", "description": "Provided by Red Hat, Inc.",
         "createdAt": "2026-10-01T14:40:55.000Z", "family": "Red Hat Enterprise Linux"},
        {"id": "ami-0e3b1e0a5d8f2c771", "name": "Rocky-9-EC2-Base-9.6-20260920.0.x86_64",
         "osFamily": "rhel", "description": "Rocky Linux 9.6 Base",
         "createdAt": "2026-09-20T03:11:40.000Z", "family": "Rocky Linux"},
    ]
    out["machineTypes"] = [dict(item) for item in MACHINE_TYPES]
    out["kubernetesVersions"] = list(_STATIC_EKS_VERSIONS)
    return out


# ---------------------------------------------------------------------------
# The build's placement and sizes
# ---------------------------------------------------------------------------

def normalize_placement(raw: Any, kind: str, previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """AWS-only spec fields. ``raw`` may also carry ``subnetZone`` (the chosen
    subnet's AZ) and ``subnetZones`` ({subnetId: AZ}) from the inventory, so
    the zone rules can be checked here; the plan re-reads them from AWS."""
    raw = raw if isinstance(raw, dict) else {}
    previous = previous or {}
    out: Dict[str, Any] = {}
    disk_type = str(raw.get("diskType") or previous.get("diskType") or "gp3").strip()
    if disk_type not in _DISK_TYPES:
        raise ValueError("diskType must be gp3, gp2 or io2.")
    out["diskType"] = disk_type
    family = str(raw.get("instanceFamily") or "").strip().lower()
    if family:
        if not re.fullmatch(r"[a-z0-9]{2,12}", family):
            raise ValueError("instanceFamily must look like m6i or t3.")
        out["instanceFamily"] = family

    zone = str(raw.get("zone") or "").strip()
    subnet_zone = str(raw.get("subnetZone") or "").strip()
    if zone and not _ZONE_RE.fullmatch(zone):
        raise ValueError(f"{zone} is not an AWS availability zone name.")
    if zone and subnet_zone and zone != subnet_zone:
        raise ValueError(f"The subnet is in {subnet_zone}, not {zone}: an AWS subnet lives in one zone.")
    out["zone"] = zone or subnet_zone or None

    if kind == "managed":
        ids = raw.get("subnetIds") or previous.get("subnetIds") or []
        if isinstance(ids, str):
            ids = [part for part in re.split(r"[\s,]+", ids) if part]
        ids = list(dict.fromkeys(str(item).strip() for item in ids if str(item).strip()))
        if len(ids) < 2:
            raise ValueError("EKS needs at least two subnets, in two different availability zones.")
        bad = [item for item in ids if not _SUBNET_RE.fullmatch(item)]
        if bad:
            raise ValueError(f"Not a subnet id: {', '.join(bad[:3])}.")
        zones = raw.get("subnetZones") if isinstance(raw.get("subnetZones"), dict) else {}
        known = {zones[item] for item in ids if zones.get(item)}
        if len(known) == 1 and len([item for item in ids if zones.get(item)]) == len(ids):
            raise ValueError(f"Every chosen subnet is in {known.pop()}; EKS needs two availability zones.")
        out["subnetIds"] = ids
    return out


def _catalog_or_static(catalog: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    return list(catalog) if catalog else [dict(item) for item in MACHINE_TYPES]


def resolve_machine_type(size: Dict[str, Any], spec: Dict[str, Any],
                         catalog: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """``size``: {"cpu", "memoryGb", "role"}. A per-role choice in
    ``spec["machineTypes"]`` wins; otherwise the smallest type that fits."""
    catalog = _catalog_or_static(catalog)
    cpu, memory = int(size.get("cpu") or 1), float(size.get("memoryGb") or 1)
    wanted = str(((spec or {}).get("machineTypes") or {}).get(size.get("role")) or "").strip()
    if wanted:
        known = next((item for item in catalog if item["id"] == wanted), None)
        if known:
            return {"type": wanted, "cpu": int(known["cpu"]), "memoryGb": known["memoryGb"],
                    "note": known.get("note") or ""}
        if not _TYPE_RE.fullmatch(wanted):
            raise ValueError(f"{wanted} is not an EC2 instance type (like m6i.xlarge).")
        return {"type": wanted, "cpu": cpu, "memoryGb": memory,
                "note": "not in KubeSight's table: the size shown is what was asked for"}
    family = (spec or {}).get("instanceFamily")
    prefer = [family] if family else _DEFAULT_FAMILIES
    found = base.smallest_fitting(catalog, cpu, memory, prefer=prefer)
    if found is None:
        raise ValueError(
            f"No {'/'.join(prefer)} instance type has {cpu} vCPU and {memory:g} GB; "
            "choose one for this role."
        )
    return {"type": found["id"], "cpu": int(found["cpu"]), "memoryGb": found["memoryGb"],
            "note": found.get("note") or ""}


def plan_checks(account: Account, spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """Fresh from AWS. A ``fail`` check means the plan must stop."""
    region = spec.get("region") or account.region
    checks: List[Dict[str, str]] = []
    managed = spec.get("kind") == "managed"
    ids = list(spec.get("subnetIds") or []) if managed else [spec.get("subnetId")]
    ids = [item for item in ids if item]
    root = _query(account, "ec2", region, {"Action": "DescribeSubnets", **_filters([("subnet-id", ids)])})
    found = {s["id"]: s for s in (_subnet_item(item) for item in root.findall("subnetSet/item"))}
    for subnet_id in ids:
        subnet = found.get(subnet_id)
        if subnet is None:
            checks.append(base.check("fail", "Subnet", f"{subnet_id} no longer exists in {region}."))
        elif spec.get("networkId") and subnet["vpcId"] != spec["networkId"]:
            checks.append(base.check("fail", "Subnet", f"{subnet_id} is in {subnet['vpcId']}, not {spec['networkId']}."))
        else:
            checks.append(base.check("ok", "Subnet", f"{subnet['name']} ({subnet['cidr']}) in {subnet['zone']}"))
    if managed:
        zones = {s["zone"] for s in found.values()}
        if len(zones) < 2:
            checks.append(base.check("fail", "Availability zones",
                                     "EKS needs subnets in at least two availability zones."))
        version = str((spec.get("managed") or {}).get("kubernetesVersion") or "")
        offered = _kubernetes_versions(account, region)
        if version and _minor(version) not in offered:
            checks.append(base.check("warn", "Kubernetes version",
                                     f"EKS in {region} offers {', '.join(offered[:4])}; {version} may be refused."))
    else:
        zone = spec.get("zone")
        subnet = found.get(spec.get("subnetId"))
        if zone and subnet and subnet["zone"] != zone:
            checks.append(base.check("warn", "Zone", f"The subnet is in {subnet['zone']}, the build says {zone}."))
        image_id = (spec.get("image") or {}).get("id")
        if image_id:
            root = _query(account, "ec2", region, {"Action": "DescribeImages", **_filters([("image-id", [image_id])])})
            image = root.find("imagesSet/item")
            if image is None:
                checks.append(base.check("fail", "Image", f"{image_id} is not available in {region}."))
            elif (image.findtext("imageState") or "available") != "available":
                checks.append(base.check("fail", "Image", f"{image_id} is {image.findtext('imageState')}."))
            else:
                checks.append(base.check("ok", "Image", image.findtext("name") or image_id))
    return checks


# ---------------------------------------------------------------------------
# main.tf.json
# ---------------------------------------------------------------------------

def _short_name(build_id: int, cluster: str, suffix: str, limit: int) -> str:
    """Names AWS keeps unique per region (load balancers, target groups,
    placement groups): the build id makes them unique, the cluster readable."""
    head = re.sub(r"[^a-z0-9-]", "-", f"ks{build_id}-{cluster}".lower())
    head = head[: limit - len(suffix) - 1].rstrip("-")
    return f"{head}-{suffix}"


def _minor(version: str) -> str:
    parts = str(version).lstrip("v").split(".")
    return ".".join(parts[:2])


def _terraform(region: str, labels: Dict[str, str]) -> Dict[str, Any]:
    return {
        "terraform": {
            "required_providers": {
                "aws": {"source": TOFU_PROVIDER["source"], "version": f"= {TOFU_PROVIDER['version']}"}
            },
            "backend": {"http": {}},
        },
        "provider": {"aws": {"region": region, "default_tags": [{"tags": dict(labels)}]}},
    }


def _sg_rules(cidrs: Dict[str, Tuple[int, str, str]]) -> Dict[str, Any]:
    """{key: (port, cidr, description)} → for_each ingress rules on the cluster group."""
    return {
        "for_each": {
            key: {"port": port, "cidr": cidr, "description": description}
            for key, (port, cidr, description) in cidrs.items()
        },
        "security_group_id": "${aws_security_group.cluster.id}",
        "ip_protocol": "tcp",
        "from_port": "${each.value.port}",
        "to_port": "${each.value.port}",
        "cidr_ipv4": "${each.value.cidr}",
        "description": "${each.value.description}",
    }


def _user_data(ssh: Dict[str, Any]) -> str:
    """cloud-init with the hostname left as OpenTofu's ``${each.key}``."""
    return base.cloud_init(ssh)


def render_vms(ctx: base.VmContext) -> Dict[str, Any]:
    spec = ctx.spec
    labels = ctx.labels or base.tags(ctx.cluster_name, ctx.build_id)
    config = _terraform(ctx.region, labels)
    cps = [node["name"] for node in ctx.nodes if node["role"] == "controlPlane"]
    spread = len(cps) >= 2
    pg_name = _short_name(ctx.build_id, ctx.cluster_name, "cp", 255)

    rules: Dict[str, Tuple[int, str, str]] = {}
    for cidr in ctx.allowed_cidrs:
        rules[f"ssh {cidr}"] = (base.SSH_PORT, cidr, "KubeSight SSH")
        rules[f"api {cidr}"] = (base.API_PORT, cidr, "Kubernetes API")
    if ctx.load_balancer and spec.get("networkCidr"):
        # The NLB reaches the targets from its own addresses in the VPC.
        rules[f"api {spec['networkCidr']}"] = (base.API_PORT, spec["networkCidr"],
                                               "Kubernetes API through the load balancer")

    resources: Dict[str, Any] = {
        "aws_security_group": {"cluster": {
            "name": f"kubesight-{ctx.cluster_name}-b{ctx.build_id}",
            "description": f"KubeSight cluster {ctx.cluster_name} (build {ctx.build_id})",
            "vpc_id": spec["networkId"],
            "tags": {"Name": f"kubesight-{ctx.cluster_name}"},
        }},
        "aws_vpc_security_group_ingress_rule": {
            "cluster_members": {
                "security_group_id": "${aws_security_group.cluster.id}",
                "referenced_security_group_id": "${aws_security_group.cluster.id}",
                "ip_protocol": "-1",
                "description": "Everything between the cluster's machines",
            },
        },
        "aws_vpc_security_group_egress_rule": {"all": {
            "security_group_id": "${aws_security_group.cluster.id}",
            "cidr_ipv4": "0.0.0.0/0",
            "ip_protocol": "-1",
            "description": "Outbound: packages, images, the cloud's APIs",
        }},
    }
    if rules:
        resources["aws_vpc_security_group_ingress_rule"]["admin"] = _sg_rules(rules)

    for_each = {
        node["name"]: {
            "role": node["role"],
            "cpu": int(node["cpu"]),
            "memory_mb": int(float(node["memoryGb"]) * 1024),
            "disk_gb": int(node["diskGb"]),
            "instance_type": node["machineType"]["type"],
            "placement_group": pg_name if spread and node["role"] == "controlPlane" else None,
        }
        for node in ctx.nodes
    }
    if for_each:
        instance: Dict[str, Any] = {
            "for_each": for_each,
            "ami": spec["image"]["id"],
            "instance_type": "${each.value.instance_type}",
            "subnet_id": spec["subnetId"],
            "vpc_security_group_ids": ["${aws_security_group.cluster.id}"],
            "associate_public_ip_address": False,
            "source_dest_check": False,
            "user_data": _user_data(ctx.ssh),
            "user_data_replace_on_change": False,
            "root_block_device": [{
                "volume_size": "${each.value.disk_gb}",
                "volume_type": spec.get("diskType") or "gp3",
                "encrypted": True,
                "delete_on_termination": True,
            }],
            "metadata_options": [{
                "http_endpoint": "enabled",
                "http_tokens": "required",
                # 2: containers on the node may still reach the metadata service.
                "http_put_response_hop_limit": 2,
            }],
            "tags": {"Name": "${each.key}", "kubesight-role": "${each.value.role}"},
            "lifecycle": [{"ignore_changes": ["ami", "user_data", "placement_group"]}],
        }
        if spread:
            instance["placement_group"] = "${each.value.placement_group}"
            instance["depends_on"] = ["aws_placement_group.control_planes"]
        resources["aws_instance"] = {"node": instance}
    if spread:
        resources["aws_placement_group"] = {"control_planes": {
            "name": pg_name, "strategy": "spread",
        }}

    if ctx.load_balancer:
        resources["aws_lb"] = {"api": {
            "name": _short_name(ctx.build_id, ctx.cluster_name, "api", 32),
            "internal": True,
            "load_balancer_type": "network",
            "subnets": [spec["subnetId"]],
            # Clients are held to the same allowed networks as the nodes'.
            "security_groups": ["${aws_security_group.cluster.id}"],
            "enable_cross_zone_load_balancing": True,
        }}
        resources["aws_lb_target_group"] = {"api": {
            "name": _short_name(ctx.build_id, ctx.cluster_name, "6443", 32),
            "port": base.API_PORT,
            "protocol": "TCP",
            "target_type": "ip",
            "preserve_client_ip": "false",
            "vpc_id": spec["networkId"],
            "deregistration_delay": "30",
            "health_check": [{
                "protocol": "TCP", "port": str(base.API_PORT),
                "interval": 10, "healthy_threshold": 2, "unhealthy_threshold": 2,
            }],
        }}
        resources["aws_lb_listener"] = {"api": {
            "load_balancer_arn": "${aws_lb.api.arn}",
            "port": base.API_PORT,
            "protocol": "TCP",
            "default_action": [{"type": "forward", "target_group_arn": "${aws_lb_target_group.api.arn}"}],
        }}
        if cps:
            resources["aws_lb_target_group_attachment"] = {"api": {
                "for_each": {name: name for name in cps},
                "target_group_arn": "${aws_lb_target_group.api.arn}",
                "target_id": "${aws_instance.node[each.key].private_ip}",
                "port": base.API_PORT,
            }}
    config["resource"] = resources
    return config


def _assume(service: str) -> str:
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": service},
            "Action": ["sts:AssumeRole", "sts:TagSession"] if service.startswith("eks") else "sts:AssumeRole",
        }],
    }, sort_keys=True)


def render_managed(ctx: base.ManagedContext) -> Dict[str, Any]:
    spec = ctx.spec
    labels = ctx.labels or base.tags(ctx.cluster_name, ctx.build_id)
    config = _terraform(ctx.region, labels)
    partition = _partition(ctx.region)
    policy = f"arn:{partition}:iam::aws:policy/"
    name = f"kubesight-{ctx.cluster_name}"
    pool = ctx.node_pool
    count = int(pool["count"])

    resources: Dict[str, Any] = {
        "aws_iam_role": {
            "cluster": {"name": f"{name}-eks-cluster"[:64], "assume_role_policy": _assume("eks.amazonaws.com")},
            "nodes": {"name": f"{name}-eks-nodes"[:64], "assume_role_policy": _assume("ec2.amazonaws.com")},
        },
        "aws_iam_role_policy_attachment": {
            "cluster": {
                "for_each": {"AmazonEKSClusterPolicy": f"{policy}AmazonEKSClusterPolicy"},
                "role": "${aws_iam_role.cluster.name}",
                "policy_arn": "${each.value}",
            },
            "nodes": {
                "for_each": {
                    item: f"{policy}{item}" for item in (
                        "AmazonEKSWorkerNodePolicy", "AmazonEKS_CNI_Policy",
                        "AmazonEC2ContainerRegistryReadOnly",
                    )
                },
                "role": "${aws_iam_role.nodes.name}",
                "policy_arn": "${each.value}",
            },
        },
        "aws_security_group": {"cluster": {
            "name": f"{name}-b{ctx.build_id}-api",
            "description": f"KubeSight: who may reach the EKS API of {ctx.cluster_name}",
            "vpc_id": spec["networkId"],
            "tags": {"Name": f"{name}-api"},
        }},
        "aws_vpc_security_group_egress_rule": {"all": {
            "security_group_id": "${aws_security_group.cluster.id}",
            "cidr_ipv4": "0.0.0.0/0",
            "ip_protocol": "-1",
        }},
        "aws_eks_cluster": {"cluster": {
            "name": name[:100],
            "role_arn": "${aws_iam_role.cluster.arn}",
            "version": _minor(ctx.kubernetes_version),
            "vpc_config": [{
                "subnet_ids": list(spec["subnetIds"]),
                "endpoint_private_access": True,
                "endpoint_public_access": False,
                "security_group_ids": ["${aws_security_group.cluster.id}"],
            }],
            "access_config": [{
                "authentication_mode": "API_AND_CONFIG_MAP",
                "bootstrap_cluster_creator_admin_permissions": True,
            }],
            "depends_on": ["aws_iam_role_policy_attachment.cluster"],
        }},
        "aws_eks_node_group": {"workers": {
            "cluster_name": "${aws_eks_cluster.cluster.name}",
            "node_group_name": f"{name}-workers"[:63],
            "node_role_arn": "${aws_iam_role.nodes.arn}",
            "subnet_ids": list(spec["subnetIds"]),
            "instance_types": [pool["machineType"]["type"]],
            "disk_size": int(pool["diskGb"]),
            "ami_type": "AL2023_x86_64_STANDARD",
            "scaling_config": [{"desired_size": count, "min_size": count, "max_size": max(count, 1)}],
            "update_config": [{"max_unavailable": 1}],
            "depends_on": ["aws_iam_role_policy_attachment.nodes"],
        }},
    }
    if ctx.allowed_cidrs:
        resources["aws_vpc_security_group_ingress_rule"] = {
            "admin": _sg_rules({f"api {cidr}": (443, cidr, "Kubernetes API") for cidr in ctx.allowed_cidrs}),
        }
    config["resource"] = resources
    return config


# ---------------------------------------------------------------------------
# Running OpenTofu, and reading what it made
# ---------------------------------------------------------------------------

def environment(account: Account, workdir: str) -> Tuple[Dict[str, str], List[str]]:
    env = {
        "AWS_ACCESS_KEY_ID": account.settings.get("accessKeyId") or "",
        "AWS_SECRET_ACCESS_KEY": account.secrets.get("secretAccessKey") or "",
        "AWS_REGION": account.region or "",
        "AWS_DEFAULT_REGION": account.region or "",
        # No ~/.aws profile, no instance metadata: only these keys.
        "AWS_EC2_METADATA_DISABLED": "true",
        **base.env_proxy(),
    }
    hide = [env["AWS_SECRET_ACCESS_KEY"]]
    token = account.secrets.get("sessionToken")
    if token:
        env["AWS_SESSION_TOKEN"] = token
        hide.append(token)
    if account.ca_pem and account.ca_pem.strip():
        path = os.path.join(workdir, "aws-ca-bundle.pem")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(account.ca_pem.strip() + "\n")
        env["AWS_CA_BUNDLE"] = path
    return env, [item for item in hide if item]


def vm_facts(attrs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": attrs.get("id"),
        "address": attrs.get("private_ip"),
        "zone": attrs.get("availability_zone"),
        "machineType": attrs.get("instance_type"),
    }


def _find(resources: List[Dict[str, Any]], rtype: str, name: str) -> Optional[Dict[str, Any]]:
    for item in resources or []:
        if item.get("type") == rtype and item.get("name") == name:
            return item.get("attributes") or {}
    return None


def endpoint_from_state(resources: List[Dict[str, Any]]) -> Optional[str]:
    attrs = _find(resources, "aws_lb", "api")
    return (attrs or {}).get("dns_name") or None


def managed_access(account: Account, resources: List[Dict[str, Any]], spec: Dict[str, Any]) -> Dict[str, str]:
    attrs = _find(resources, "aws_eks_cluster", "cluster")
    if not attrs or not attrs.get("endpoint"):
        raise CloudError("OpenTofu's state has no EKS cluster endpoint yet.")
    authority = attrs.get("certificate_authority") or [{}]
    ca_data = (authority[0] if isinstance(authority, list) and authority else {}).get("data") or ""
    region = spec.get("region") or account.region
    return {
        "server": attrs["endpoint"],
        "caData": ca_data,
        "token": eks_token(account, attrs.get("name") or attrs.get("id"), region=region),
        "clusterName": attrs.get("name") or "",
    }


_POLICY_TAIL = re.compile(r"[^/]+$")


def describe_resource(rtype: str, rname: str, key: Any, attrs: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    attrs = attrs or {}
    if rtype == "aws_instance":
        detail = " · ".join(str(part) for part in (attrs.get("instance_type"), attrs.get("private_ip")) if part)
        return "vm", str(key), detail
    if rtype == "aws_lb":
        return "lb", "Internal network load balancer for the Kubernetes API", attrs.get("name") or ""
    if rtype == "aws_lb_target_group":
        return "lb", "Load balancer target group (TCP 6443)", attrs.get("name") or ""
    if rtype == "aws_lb_listener":
        return "lb", "Load balancer listener on TCP 6443", ""
    if rtype == "aws_lb_target_group_attachment":
        return "lb", f"Send API traffic to {key}", ""
    if rtype == "aws_security_group":
        return "network", "Security group for the cluster", attrs.get("name") or ""
    if rtype in ("aws_vpc_security_group_ingress_rule", "aws_vpc_security_group_egress_rule"):
        direction = "Inbound" if rtype.endswith("ingress_rule") else "Outbound"
        what = attrs.get("description") or (str(key) if key is not None else rname)
        return "network", f"{direction} rule: {what}", str(key) if key is not None else ""
    if rtype == "aws_placement_group":
        return "network", "Keep control planes on different hardware (spread placement)", attrs.get("name") or ""
    if rtype == "aws_eks_cluster":
        return "cluster", f"EKS cluster {attrs.get('name') or ''}".strip(), f"Kubernetes {attrs.get('version') or ''}".strip()
    if rtype == "aws_eks_node_group":
        scaling = (attrs.get("scaling_config") or [{}])[0] if isinstance(attrs.get("scaling_config"), list) else {}
        types = ", ".join(attrs.get("instance_types") or [])
        detail = " × ".join(str(part) for part in ((scaling or {}).get("desired_size"), types) if part)
        return "nodepool", f"Node group {attrs.get('node_group_name') or ''}".strip(), detail
    if rtype == "aws_iam_role":
        return "iam", f"IAM role {attrs.get('name') or rname}", ""
    if rtype == "aws_iam_role_policy_attachment":
        arn = str(attrs.get("policy_arn") or key or "")
        match = _POLICY_TAIL.search(arn)
        return "iam", f"Attach {match.group(0) if match else arn} to the {rname} role", ""
    return None


def simulate_attributes(rtype: str, rname: str, key: Any, values: Dict[str, Any], seq: int) -> Dict[str, Any]:
    """Believable state attributes for the simulator. ``values`` is the
    for_each value (or the resource body's plain attributes); ``subnetCidr``,
    ``zone`` and ``region`` are read from it when the caller merges them in."""
    values = values or {}
    region = values.get("region") or "eu-west-1"
    digest = hashlib.sha256(f"{rtype}.{rname}.{key}.{seq}".encode()).hexdigest()
    if rtype == "aws_instance":
        return {
            "id": f"i-0{digest[:16]}",
            "private_ip": base.simulated_address(values.get("subnetCidr") or values.get("subnet_cidr") or "", seq),
            "availability_zone": values.get("zone") or f"{region}a",
            "instance_type": values.get("instance_type"),
            "arn": f"arn:aws:ec2:{region}:123456789012:instance/i-0{digest[:16]}",
        }
    if rtype == "aws_lb":
        name = values.get("name") or f"ks-{digest[:8]}-api"
        return {
            "id": f"arn:aws:elasticloadbalancing:{region}:123456789012:loadbalancer/net/{name}/{digest[:16]}",
            "arn": f"arn:aws:elasticloadbalancing:{region}:123456789012:loadbalancer/net/{name}/{digest[:16]}",
            "name": name,
            "dns_name": f"internal-{name}-{digest[:10]}.elb.{region}.amazonaws.com",
        }
    if rtype == "aws_eks_cluster":
        name = values.get("name") or "kubesight-demo"
        pem = "-----BEGIN CERTIFICATE-----\nU0lNVUxBVEVEIEVLUyBDQQ==\n-----END CERTIFICATE-----\n"
        return {
            "id": name, "name": name, "version": values.get("version") or "1.33",
            "endpoint": f"https://{digest[:32].upper()}.gr7.{region}.eks.amazonaws.com",
            "certificate_authority": [{"data": base64.b64encode(pem.encode()).decode()}],
            "arn": f"arn:aws:eks:{region}:123456789012:cluster/{name}",
        }
    return {"id": f"sim-{rtype}-{digest[:12]}"}
