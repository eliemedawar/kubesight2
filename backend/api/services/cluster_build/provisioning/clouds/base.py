"""What every public-cloud adapter (aws, gcp, oci) shares, and the contract each
one implements.

KubeSight creates machines on Oracle Cloud, AWS and Google Cloud the same way
it creates them in vCenter: OpenTofu, run by ``provisioning.jobs`` against
KubeSight's own HTTP state backend, with the same plan → apply → connect →
hand-off and the same approvals. Only four things differ per cloud, and each
adapter module answers exactly those:

  1. **The account** — which keys sign in, how they are checked, how OpenTofu's
     provider gets them (environment / files in the job's working directory,
     never main.tf.json).
  2. **The inventory** — regions, networks + subnets, zones, images, machine
     types and (for managed clusters) Kubernetes versions, read live with the
     account's keys. Small hand-written REST clients (signing in ``aws.py`` /
     ``gcp.py`` / ``oci.py``) instead of three SDKs.
  3. **The configuration** — main.tf.json for self-managed VMs (``render_vms``)
     or a managed cluster (``render_managed``).
  4. **Reading the result** — VM ids and private addresses, the API load
     balancer's address, and short-lived access to a managed cluster's API.

Decisions every adapter keeps (the user's, 2026-11-03):

  * **Private addresses only.** No public IP is ever asked for. KubeSight
    reaches the machines over the customer's VPN / peering / interconnect or
    through the bastion of the build's SSH route.
  * **The cloud picks each VM's private address** from the subnet; KubeSight
    reads it back from OpenTofu's state after the apply. (vCenter has no IPAM,
    which is why VMware builds reserve addresses from ranges; clouds do.)
  * **The API endpoint of an HA (or "load balancer") shape is the cloud's own
    internal network load balancer** on TCP 6443 in front of the control
    planes — a keepalived VIP cannot float on a cloud network (no gratuitous
    ARP). So on a cloud, ``counts.loadbalancer > 0`` means "put a cloud load
    balancer in front", and no load-balancer VMs are created.
  * **Stored keys per account**, encrypted at rest (``secret_encryption``).

Adapter contract — every name below must exist in aws.py, gcp.py and oci.py:

  PROVIDER: str                         "aws" | "gcp" | "oci"
  LABEL: str                            "Amazon Web Services" …
  MANAGED_LABEL: str                    "Amazon EKS" | "Google GKE" | "Oracle OKE"
  TOFU_PROVIDER: dict                   {"name": "aws", "source": "hashicorp/aws", "version": "6.71.0"}
  VM_RESOURCE_TYPE: str                 "aws_instance" | "google_compute_instance" | "oci_core_instance"
  FIELDS: list[dict]                    account form fields, see ``field()``

  normalize_account(settings, secrets, previous_settings, previous_secrets)
      -> (settings, secrets)            validate; a blank secret keeps the previous one; ValueError
  identity_label(account) -> str        who the keys are, without any secret
  test(account) -> str                  sign in for real; returns the identity; CloudError
  regions(account) -> [{"id", "name"}]
  inventory(account, region, scope) -> dict   see ``empty_inventory()``; CloudError
  simulated_regions() -> [...] / simulated_inventory(region, scope) -> dict
  normalize_placement(raw, kind, previous) -> dict
      the provider-only spec fields (zone, subnetIds, compartmentId, diskType …), ValueError
  resolve_machine_type(size, spec, catalog=None) -> dict
      {"type", "cpu", "memoryGb", "note"?, …provider fields (OCI: "ocpus")}
  plan_checks(account, spec) -> [check]  fresh read: region/network/subnet/image still exist
  render_vms(ctx) -> dict               main.tf.json, see ``VmContext`` below
  render_managed(ctx) -> dict           main.tf.json, see ``ManagedContext`` below
  environment(account, workdir) -> (env: dict, hide: list[str])
  vm_facts(attrs) -> {"id", "address", "zone", "machineType"}   from one VM's state attributes
  endpoint_from_state(resources) -> str | None                  the API LB's host (no port)
  managed_access(account, resources, spec) -> {"server", "caData", "token"}
  describe_resource(rtype, rname, key, attrs) -> (kind, title, detail) | None
  simulate_attributes(rtype, rname, key, values, seq) -> dict

Resource names every adapter uses, because the core reads them back:
  VMs              ``<VM_RESOURCE_TYPE>.node`` with ``for_each`` keyed by VM name
                   (each.value carries at least role, cpu, memory_mb, disk_gb)
  API LB           its main resource named ``api``
  managed cluster  ``<type>.cluster``; its node pool ``<type>.workers``
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Dict, List, Optional, Tuple


class CloudError(RuntimeError):
    """The cloud refused, or could not be reached. The message is safe to show."""


@dataclass
class Account:
    """One cloud account, decrypted for the length of a request or a job."""

    provider: str
    name: str
    region: str
    settings: Dict[str, Any]
    secrets: Dict[str, str]
    ca_pem: str = ""
    id: Optional[int] = None


# ---------------------------------------------------------------------------
# Form fields an adapter publishes (the Sources form is drawn from these)
# ---------------------------------------------------------------------------

def field(key: str, label: str, *, secret: bool = False, required: bool = True,
          kind: str = "text", help: str = "", placeholder: str = "") -> Dict[str, Any]:
    """``kind``: text | textarea (multi-line, e.g. a PEM or a JSON key file)."""
    return {
        "key": key, "label": label, "secret": secret, "required": required,
        "kind": kind, "help": help, "placeholder": placeholder,
    }


def keep_or_require(raw: Dict[str, Any], previous: Dict[str, Any], key: str, label: str,
                    *, required: bool = True) -> Optional[str]:
    """A secret from the form: blank keeps the stored one (editing an account
    never shows its secrets), and a new account must give it."""
    value = raw.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip() if "\n" not in value else value.strip() + "\n"
    if previous.get(key):
        return previous[key]
    if required:
        raise ValueError(f"{label} is required.")
    return None


# ---------------------------------------------------------------------------
# HTTP (the adapters' REST calls)
# ---------------------------------------------------------------------------

# (method, url, headers, body, timeout_s, ca_pem) -> (status, headers, body)
Transport = Callable[[str, str, Dict[str, str], Optional[bytes], float, str], Tuple[int, Dict[str, str], bytes]]
_transport: Optional[Transport] = None


def set_transport(transport: Optional[Transport]) -> None:
    """Test seam: answer every cloud HTTP call from a function."""
    global _transport
    _transport = transport


def _ssl_context(ca_pem: str) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if ca_pem and ca_pem.strip():
        # Corporate TLS inspection: the account may carry the proxy's CA.
        context.load_verify_locations(cadata=ca_pem)
    return context


def _urllib_transport(method, url, headers, body, timeout_s, ca_pem):
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s, context=_ssl_context(ca_pem)) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read() or b""


def http(method: str, url: str, *, headers: Optional[Dict[str, str]] = None,
         body: Optional[bytes] = None, timeout_s: float = 30.0, ca_pem: str = "") -> Tuple[int, Dict[str, str], bytes]:
    """One HTTPS call. Network failures become CloudError; HTTP errors are
    returned as they are, for the adapter to read the cloud's own message."""
    send = _transport or _urllib_transport
    try:
        return send(method, url, dict(headers or {}), body, timeout_s, ca_pem or "")
    except CloudError:
        raise
    except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
        reason = getattr(exc, "reason", None) or exc
        host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
        raise CloudError(f"Could not reach {host}: {reason}") from exc


def json_body(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8") or "null")
    except (ValueError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Machines: sizes, SSH access, the firewall every cluster gets
# ---------------------------------------------------------------------------

# The ports the Kubernetes API load balancer serves and KubeSight itself uses.
API_PORT = 6443
SSH_PORT = 22


def smallest_fitting(catalog: List[Dict[str, Any]], cpu: int, memory_gb: float,
                     *, prefer: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """The cheapest-looking machine type with at least ``cpu`` and
    ``memory_gb``: fewest CPUs, then least memory, then the ``prefer`` order of
    families (a type's family is everything before its first '.' or '-')."""
    order = {name: index for index, name in enumerate(prefer or [])}

    def family(type_id: str) -> str:
        return re.split(r"[.\-]", type_id, maxsplit=1)[0]

    fitting = [
        item for item in catalog
        if int(item.get("cpu") or 0) >= cpu and float(item.get("memoryGb") or 0) >= memory_gb
        and (not order or family(item["id"]) in order)
    ]
    if not fitting:
        return None
    fitting.sort(key=lambda item: (
        int(item["cpu"]), float(item["memoryGb"]), order.get(family(item["id"]), len(order)), item["id"],
    ))
    return fitting[0]


def tf_escape(text: str) -> str:
    """A literal string inside main.tf.json: OpenTofu would otherwise read
    ``${`` and ``%{`` as template syntax."""
    return text.replace("${", "$${").replace("%{", "%%{")


def cloud_init(ssh: Dict[str, Any], *, hostname: str = "${each.key}") -> str:
    """#cloud-config that lets the build's SSH credential in, with password-less
    sudo (the Cluster Builder runs ``sudo -n``), and sets the hostname.

    ``ssh``: {"username", "publicKey" (OpenSSH line) or None, "password" or None}.
    The text is already escaped for main.tf.json except ``hostname``, which is
    usually the ``${each.key}`` interpolation and must stay one.
    """
    marker = "KUBESIGHT-HOSTNAME-7f3a"
    username = ssh["username"]
    lines = ["#cloud-config", f"hostname: {marker}", "preserve_hostname: false"]
    key = ssh.get("publicKey")
    password = ssh.get("password")
    if username == "root":
        lines += ["disable_root: false"]
        user = {"name": "root"}
    else:
        user = {"name": username, "sudo": "ALL=(ALL) NOPASSWD:ALL", "shell": "/bin/bash",
                "lock_passwd": not bool(password)}
    if key:
        user["ssh_authorized_keys"] = [key]
    lines.append("users:")
    lines.append("  - default")
    lines.append("  - " + json.dumps(user, sort_keys=True))
    if password:
        lines.append("ssh_pwauth: true")
        lines.append("chpasswd:")
        lines.append("  expire: false")
        lines.append("  users:")
        lines.append("    - " + json.dumps({"name": username, "password": password, "type": "text"}))
    # Escape what came from people (a password may hold "${"), then put the
    # interpolation back unescaped.
    return tf_escape("\n".join(lines) + "\n").replace(marker, hostname)


def tags(cluster_name: str, build_id: int) -> Dict[str, str]:
    """Labels on everything KubeSight creates, so it can be found in the console."""
    return {"managed-by": "kubesight", "kubesight-cluster": cluster_name, "kubesight-build": str(build_id)}


def normalize_cidrs(raw: Any, *, label: str = "Allowed networks") -> List[str]:
    if isinstance(raw, str):
        raw = [part for part in re.split(r"[\s,]+", raw) if part]
    out: List[str] = []
    for item in raw or []:
        try:
            network = ipaddress.ip_network(str(item).strip(), strict=False)
        except ValueError as exc:
            raise ValueError(f"{label}: {item} is not a CIDR.") from exc
        if network.version != 4:
            raise ValueError(f"{label}: only IPv4 networks for now ({item}).")
        if network.prefixlen == 0:
            raise ValueError(f"{label}: 0.0.0.0/0 would open SSH and the API to everyone.")
        if str(network) not in out:
            out.append(str(network))
    return out


def simulated_address(cidr: str, seq: int) -> str:
    """A believable private address for the simulator: the subnet's 10th + seq."""
    try:
        network = ipaddress.ip_network(cidr or "10.0.1.0/24", strict=False)
    except ValueError:
        network = ipaddress.ip_network("10.0.1.0/24")
    return str(network.network_address + 10 + (seq % max(1, network.num_addresses - 12)))


def empty_inventory(region: str) -> Dict[str, Any]:
    """The shape ``inventory()`` returns (every key present, lists may be empty)."""
    return {
        "region": region,
        # [{"id", "name"}] — AWS availability zones, Google zones, OCI availability domains
        "zones": [],
        # [{"id", "name", "cidr", "subnets": [{"id", "name", "cidr", "zone" (None = regional)}]}]
        "networks": [],
        # [{"id", "name", "osFamily": "debian"|"rhel"|"other", "description", "createdAt"}]
        "images": [],
        # [{"id", "cpu", "memoryGb", "flexible": bool, "note"}]
        "machineTypes": [],
        # ["1.33", "1.32", ...] newest first (managed clusters)
        "kubernetesVersions": [],
        # OCI only: [{"id", "name", "path"}]
        "compartments": [],
    }


@dataclass
class VmContext:
    """What ``render_vms`` gets. Plain data; nothing secret except ``ssh.password``."""

    cluster_name: str
    build_id: int
    region: str
    spec: Dict[str, Any]
    # [{"name", "role" (controlPlane|worker|vm), "cpu", "memoryGb", "diskGb",
    #   "machineType": resolve_machine_type(...)}]
    nodes: List[Dict[str, Any]]
    # An internal TCP 6443 load balancer in front of the controlPlane nodes.
    load_balancer: bool
    # {"username", "publicKey", "password"}
    ssh: Dict[str, Any]
    allowed_cidrs: List[str]
    labels: Dict[str, str] = dc_field(default_factory=dict)


@dataclass
class ManagedContext:
    """What ``render_managed`` gets."""

    cluster_name: str
    build_id: int
    region: str
    spec: Dict[str, Any]
    kubernetes_version: str
    # {"count", "cpu", "memoryGb", "diskGb", "machineType": resolve_machine_type(...)}
    node_pool: Dict[str, Any]
    ssh: Dict[str, Any]
    allowed_cidrs: List[str]
    labels: Dict[str, str] = dc_field(default_factory=dict)


def check(status: str, label: str, detail: str) -> Dict[str, str]:
    return {"status": status, "label": label, "detail": detail}


def env_proxy() -> Dict[str, str]:
    """Proxy settings OpenTofu's providers should inherit."""
    return {k: os.environ[k] for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy") if os.environ.get(k)}
