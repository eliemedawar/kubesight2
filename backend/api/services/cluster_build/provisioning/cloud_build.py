"""A Cluster Builder build whose machines KubeSight creates in a public cloud.

The VMware path (service.py, jobs.py) stays the frame: the same jobs, the same
state backend, the same approvals and the same hand-off to the phase machine.
This module answers the questions that frame asks wherever the answer depends
on the cloud rather than vCenter:

  * what the build's spec may hold (``normalize_spec``), and its shape;
  * which machines exist and what they are called (``plan_machines``) — no
    address is reserved: the cloud picks each VM's private address and the
    apply reads it back (``record_created``);
  * how the build's SSH credential gets onto the VMs (``ssh_access``: its
    public key, or its password, through cloud-init / instance metadata);
  * the OpenTofu configuration and the provider's environment;
  * for a **managed** cluster (OKE, EKS, GKE) there are no machines to SSH into:
    after the apply KubeSight signs in to the new cluster's API with the
    account's keys, gives itself a ``kubesight-admin`` service account and
    registers the cluster with that account's token (``onboard_managed``).
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ....db import db
from ....models import ClusterBuild, ClusterBuildNode, CloudAccount, SshConnectionProfile
from ....secret_encryption import decrypt_secret
from . import clouds, state_store, templates
from .clouds import accounts
from .clouds.base import CloudError, ManagedContext, VmContext, check, normalize_cidrs, tags

_ID_RE = re.compile(r"^[A-Za-z0-9_.:/@-]{1,512}$")
_KINDS = ("vms", "managed")
# Changing these once something exists would mean recreating it elsewhere.
_LOCKED_ONCE_CREATED = (
    "provider", "cloudAccountId", "region", "kind", "networkId", "subnetId",
    "zone", "counts", "vmCount", "compartmentId",
)
SERVICE_ACCOUNT = "kubesight-admin"


def is_cloud(build: ClusterBuild) -> bool:
    return clouds.is_cloud(build.machine_source or "")


def is_managed(build: ClusterBuild) -> bool:
    return is_cloud(build) and (build.provisioning_json or {}).get("kind") == "managed"


def module_for(build: ClusterBuild):
    return clouds.adapter(build.machine_source)


def account_row(build: ClusterBuild) -> CloudAccount:
    spec = build.provisioning_json or {}
    account_id = spec.get("cloudAccountId") or build.cloud_account_id
    row = db.session.get(CloudAccount, int(account_id)) if account_id else None
    if row is None:
        raise ValueError("The cloud account for this build no longer exists.")
    return row


def account_for(build: ClusterBuild):
    row = account_row(build)
    return clouds.adapter(row.provider), accounts.view(row)


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------

def _text(raw: Dict[str, Any], key: str, limit: int = 255) -> str:
    return str(raw.get(key) or "").strip()[:limit]


def _object_id(raw: Dict[str, Any], key: str, label: str, *, required: bool = True) -> Optional[str]:
    value = str(raw.get(key) or "").strip()
    if not value:
        if required:
            raise ValueError(f"Choose a {label}.")
        return None
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"The {label} id does not look like a cloud object id.")
    return value


def _cidr(raw: Dict[str, Any], key: str) -> str:
    value = _text(raw, key, 64)
    if not value:
        return ""
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError as exc:
        raise ValueError(f"{key} must be a CIDR.") from exc


def _image(raw: Any) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    image_id = str(raw.get("id") or "").strip()
    if not image_id or len(image_id) > 512:
        raise ValueError("Choose the image the VMs start from.")
    family = str(raw.get("osFamily") or "").strip().lower()
    return {
        "id": image_id,
        "name": _text(raw, "name"),
        "osFamily": family if family in ("debian", "rhel") else "other",
    }


def _machine_types(raw: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for role, value in (raw or {}).items() if isinstance(raw, dict) else []:
        value = str(value or "").strip()
        if role in templates.ROLE_TO_NODE_ROLE and value:
            if len(value) > 64 or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
                raise ValueError(f"{value} is not a machine type name.")
            out[role] = value
    return out


def normalize_spec(build: ClusterBuild, raw: Any) -> Dict[str, Any]:
    """The cloud provisioning spec from a wizard payload, checked for shape.

    Whether the network, subnet and image still exist is asked again when the
    plan is made — never trusted from the browser."""
    raw = raw if isinstance(raw, dict) else {}
    previous = build.provisioning_json or {}
    provider = build.machine_source
    module = clouds.adapter(provider)
    try:
        account_id = int(raw.get("cloudAccountId") or previous.get("cloudAccountId"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Choose the {module.LABEL} account to create the machines with.") from exc
    row = db.session.get(CloudAccount, account_id)
    if row is None:
        raise ValueError("Cloud account not found.")
    if row.provider != provider:
        raise ValueError(f"{row.name} is a {clouds.adapter(row.provider).LABEL} account, not {module.LABEL}.")

    kind = str(raw.get("kind") or previous.get("kind") or "vms").strip()
    if kind not in _KINDS:
        raise ValueError("kind must be 'vms' or 'managed'.")
    if build.vms_only:
        kind = "vms"
    region = _text(raw, "region", 64) or previous.get("region") or row.region
    if not region:
        raise ValueError("Choose a region.")

    spec: Dict[str, Any] = {
        "provider": provider,
        "cloudAccountId": row.id,
        "region": region,
        "kind": kind,
        "networkId": _object_id(raw, "networkId", "network"),
        "networkName": _text(raw, "networkName"),
        "networkCidr": _cidr(raw, "networkCidr"),
        "subnetId": _object_id(raw, "subnetId", "subnet"),
        "subnetName": _text(raw, "subnetName"),
        "subnetCidr": _cidr(raw, "subnetCidr"),
        "machineTypes": _machine_types(raw.get("machineTypes")),
        "allowedCidrs": normalize_cidrs(raw.get("allowedCidrs")),
        # Clouds size machines by type; there is no "keep the template's size".
        "sizeMode": "custom",
        "machines": list(previous.get("machines") or []),
    }
    if not spec["allowedCidrs"]:
        raise ValueError(
            "Name at least one network allowed to reach the machines (SSH and the "
            "Kubernetes API): the one KubeSight connects from."
        )

    if kind == "managed":
        counts = raw.get("counts") if isinstance(raw.get("counts"), dict) else previous.get("counts") or {}
        try:
            nodes = int(counts.get("worker") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("counts.worker must be a whole number.") from exc
        if not 1 <= nodes <= templates.MAX_WORKERS:
            raise ValueError(f"A node pool takes 1 to {templates.MAX_WORKERS} nodes.")
        spec["counts"] = {"loadbalancer": 0, "controlPlane": 0, "worker": nodes}
        spec["vmCount"] = None
        raw_sizes = raw.get("sizes") or previous.get("sizes") or {}
        spec["sizes"] = {"worker": templates.normalize_sizes(
            {"worker": (raw_sizes or {}).get("worker")}, {"worker": nodes},
        )["worker"]} if isinstance(raw_sizes, dict) else {}
        managed = raw.get("managed") if isinstance(raw.get("managed"), dict) else previous.get("managed") or {}
        version = str(managed.get("kubernetesVersion") or "").strip().lstrip("v")
        if not re.fullmatch(r"\d+\.\d+(\.\d+)?([.-][A-Za-z0-9.-]+)?", version):
            raise ValueError(f"Choose the Kubernetes version {module.MANAGED_LABEL} runs.")
        spec["managed"] = {"kubernetesVersion": version}
    else:
        spec["image"] = _image(raw.get("image") or previous.get("image"))
        if build.vms_only:
            spec["counts"] = None
            spec["vmCount"] = templates.normalize_vm_count(raw.get("vmCount") or previous.get("vmCount"))
            raw_sizes = raw.get("sizes") or previous.get("sizes") or {}
            spec["sizes"] = {templates.VM_ROLE: templates.normalize_vm_size(
                raw_sizes.get(templates.VM_ROLE) if isinstance(raw_sizes, dict) else None
            )}
        else:
            counts = templates.normalize_counts(raw.get("counts") or previous.get("counts"))
            spec["counts"] = counts
            spec["vmCount"] = None
            spec["sizes"] = templates.normalize_sizes(raw.get("sizes") or previous.get("sizes"), counts)

    spec.update(clouds.adapter(provider).normalize_placement(raw, kind, previous))
    if provider == "gcp":
        # Every Google resource names its project; it is the account's.
        spec["projectId"] = (row.settings_json or {}).get("projectId")
        if kind == "managed" and len(build.name or "") > 40:
            raise ValueError("A GKE cluster name is at most 40 characters.")

    if state_store.has_resources(build.id) if build.id else False:
        changed = [key for key in _LOCKED_ONCE_CREATED if spec.get(key) != previous.get(key)]
        if (spec.get("image") or {}).get("id") != (previous.get("image") or {}).get("id"):
            changed.append("image")
        if changed:
            raise ValueError(
                "Some of this build's cloud resources already exist, so where they live and "
                "how many there are can no longer change. Only the sizes of machines not "
                "created yet can."
            )
    return spec


def load_balanced(build: ClusterBuild) -> bool:
    """A cloud network load balancer fronts the API (the shape asked for
    load balancers; on a cloud those are never VMs)."""
    counts = (build.provisioning_json or {}).get("counts") or {}
    return bool(counts.get("loadbalancer")) and not build.vms_only and not is_managed(build)


def apply_shape(build: ClusterBuild) -> None:
    """Topology and endpoint fields as the phase machine reads them."""
    counts = (build.provisioning_json or {}).get("counts") or {}
    if is_managed(build) or not counts:
        build.topology_type = "single_cp"
        build.endpoint_mode = "external_lb"
        return
    build.topology_type = "single_cp" if counts.get("controlPlane") == 1 else "stacked_ha"
    build.endpoint_mode = "external_lb" if counts.get("loadbalancer") else "manual_endpoint"


# ---------------------------------------------------------------------------
# Machines
# ---------------------------------------------------------------------------

def _wanted_roles(spec: Dict[str, Any]) -> List[Tuple[str, int]]:
    if spec.get("vmCount"):
        return [(templates.VM_ROLE, int(spec["vmCount"]))]
    counts = spec.get("counts") or {}
    # Load balancers are the cloud's own; no VM is made for them.
    return [(role, int(counts.get(role) or 0)) for role in ("controlPlane", "worker")]


def machine_for(build: ClusterBuild, role: str, name: str, sizes: Optional[Dict[str, Any]] = None,
                catalog: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    spec = build.provisioning_json or {}
    size = (sizes or spec.get("sizes") or {})[role]
    machine_type = module_for(build).resolve_machine_type(
        {"cpu": int(size["cpu"]), "memoryGb": float(size["memoryGb"]), "role": role}, spec, catalog or [],
    )
    return {
        "name": name,
        "role": role,
        "cpu": int(size["cpu"]),
        "memoryGb": int(size["memoryGb"]),
        "memoryMb": int(size["memoryGb"]) * 1024,
        "diskGb": int(size["diskGb"]),
        "machineType": machine_type,
        "ip": None,
    }


def _catalog(build: ClusterBuild) -> List[Dict[str, Any]]:
    spec = build.provisioning_json or {}
    try:
        return accounts.machine_catalog(account_row(build), spec.get("region") or "",
                                        {"compartmentId": spec.get("compartmentId")})
    except ValueError:
        return []


def plan_machines(build: ClusterBuild) -> List[Dict[str, Any]]:
    """Every VM the build should have. Ones OpenTofu already created keep
    everything; the others take the spec's current sizes."""
    spec = dict(build.provisioning_json or {})
    if spec.get("kind") == "managed":
        spec["machines"] = []
        build.provisioning_json = spec
        build.vip_address = None
        build.control_plane_endpoint = ""
        return []
    created = set(state_store.vm_instances(build.id))
    previous = {m["name"]: m for m in spec.get("machines") or []}
    catalog = _catalog(build)
    wanted: List[Dict[str, Any]] = []
    for role, count in _wanted_roles(spec):
        for index in range(1, count + 1):
            name = f"{build.name}-{templates.ROLE_SHORT[role]}-{index}"
            if name in created and name in previous:
                wanted.append(previous[name])
            else:
                wanted.append(machine_for(build, role, name, catalog=catalog))
    spec["machines"] = wanted
    build.provisioning_json = spec
    build.vip_address = None
    if not created:
        # Known once the cloud has made the load balancer or the first control plane.
        build.control_plane_endpoint = ""
    return wanted


# ---------------------------------------------------------------------------
# SSH: how the build's credential gets onto the new VMs
# ---------------------------------------------------------------------------

def public_key_from_private(pem: str, passphrase: Optional[str] = None) -> str:
    from cryptography.hazmat.primitives import serialization

    data = pem.encode("utf-8")
    password = passphrase.encode("utf-8") if passphrase else None
    try:
        key = serialization.load_ssh_private_key(data, password=password)
    except (ValueError, TypeError, AttributeError):
        key = serialization.load_pem_private_key(data, password=password)
    return key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH,
    ).decode("ascii") + " kubesight"


def ssh_access(build: ClusterBuild) -> Dict[str, Any]:
    """{"username", "publicKey", "password"} for the build's SSH route."""
    if not build.connection_profile_id:
        raise ValueError("Choose the SSH route KubeSight uses to reach the new VMs (Sources row).")
    profile = db.session.get(SshConnectionProfile, build.connection_profile_id)
    if profile is None or profile.credential is None:
        raise ValueError("The build's SSH route no longer exists.")
    credential = profile.credential
    secret = decrypt_secret(credential.secret_cipher) if credential.secret_cipher else ""
    if credential.auth_method == "key":
        passphrase = (decrypt_secret(credential.key_passphrase_cipher)
                      if credential.key_passphrase_cipher else None)
        try:
            public_key = public_key_from_private(secret, passphrase)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"The SSH credential {credential.name} holds a key KubeSight cannot read ({exc})."
            ) from exc
        return {"username": credential.username, "publicKey": public_key, "password": None}
    return {"username": credential.username, "publicKey": None, "password": secret or None}


# ---------------------------------------------------------------------------
# Plan inputs
# ---------------------------------------------------------------------------

def plan_checks(build: ClusterBuild) -> List[Dict[str, str]]:
    """What the cloud says about the plan's inputs, before OpenTofu runs."""
    from .jobs import JobFailed

    module, account = account_for(build)
    spec = build.provisioning_json or {}
    out = [check("ok", f"{module.LABEL} account", f"{account.name}, region {spec.get('region')}")]
    if accounts.simulated():
        return out + [check("ok", "Simulated cloud", "no cloud API was called")]
    try:
        out += list(module.plan_checks(account, spec))
    except CloudError as exc:
        raise JobFailed(f"{module.LABEL} could not be read with the account's keys: {exc}") from exc
    failed = [item for item in out if item.get("status") == "fail"]
    if failed:
        raise JobFailed("; ".join(f"{item['label']}: {item['detail']}" for item in failed))
    return out


def machines_in_scope(build: ClusterBuild, job) -> List[Dict[str, Any]]:
    spec = build.provisioning_json or {}
    machines = list(spec.get("machines") or [])
    if job.operation == "grow":
        machines += list((job.config_json or {}).get("newMachines") or [])
    return machines


def render(build: ClusterBuild, job) -> Dict[str, Any]:
    module, account = account_for(build)
    spec = build.provisioning_json or {}
    labels = tags(build.name, build.id)
    if spec.get("kind") == "managed":
        count = int((job.config_json or {}).get("nodeCount") or (spec.get("counts") or {}).get("worker") or 1)
        size = (spec.get("sizes") or {}).get("worker") or {}
        try:
            ssh = ssh_access(build) if build.connection_profile_id else {"username": "", "publicKey": None, "password": None}
        except ValueError:
            ssh = {"username": "", "publicKey": None, "password": None}
        if not spec.get("nodeImageId") and hasattr(module, "default_node_image"):
            # OKE wants a node image per version and shape; nobody is asked for it.
            pick = (module.simulated_default_node_image(spec) if accounts.simulated()
                    else module.default_node_image(account, spec))
            spec = {**spec, "nodeImageId": pick}
        config = module.render_managed(ManagedContext(
            cluster_name=build.name, build_id=build.id, region=spec["region"], spec=spec,
            kubernetes_version=spec["managed"]["kubernetesVersion"],
            node_pool={
                "count": count, "cpu": size.get("cpu"), "memoryGb": size.get("memoryGb"),
                "diskGb": size.get("diskGb"),
                "machineType": module.resolve_machine_type(
                    {"cpu": int(size.get("cpu") or 2), "memoryGb": float(size.get("memoryGb") or 4),
                     "role": "worker"}, spec, _catalog(build),
                ),
            },
            ssh=ssh, allowed_cidrs=list(spec.get("allowedCidrs") or []), labels=labels,
        ))
    else:
        config = module.render_vms(VmContext(
            cluster_name=build.name, build_id=build.id, region=spec["region"], spec=spec,
            nodes=[
                {k: m.get(k) for k in ("name", "role", "cpu", "memoryGb", "diskGb", "machineType")}
                for m in machines_in_scope(build, job)
            ],
            load_balancer=load_balanced(build),
            ssh=ssh_access(build),
            allowed_cidrs=list(spec.get("allowedCidrs") or []),
            labels=labels,
        ))
    # Whatever the adapter wrote: KubeSight's state backend, and exactly the
    # provider version baked into the image.
    terraform = config.setdefault("terraform", {})
    terraform["backend"] = {"http": {}}
    provider = module.TOFU_PROVIDER
    terraform.setdefault("required_providers", {})[provider["name"]] = {
        "source": provider["source"], "version": f"= {provider['version']}",
    }
    return config


def environment(build: ClusterBuild, workdir: str) -> Tuple[Dict[str, str], List[str]]:
    module, account = account_for(build)
    env, hide = module.environment(account, workdir)
    return dict(env), [item for item in hide if item]


# ---------------------------------------------------------------------------
# After the apply
# ---------------------------------------------------------------------------

def record_created(build: ClusterBuild, job) -> List[ClusterBuildNode]:
    """The new VMs' ids and private addresses, read from OpenTofu's state, and
    the API endpoint (the load balancer, or the first control plane)."""
    module = module_for(build)
    spec = dict(build.provisioning_json or {})
    instances = state_store.vm_instances(build.id)
    if job.operation == "grow":
        new_machines = list((job.config_json or {}).get("newMachines") or [])
        spec["machines"] = list(spec.get("machines") or []) + new_machines
        counts = dict(spec.get("counts") or {})
        for machine in new_machines:
            counts[machine["role"]] = int(counts.get(machine["role"], 0)) + 1
        spec["counts"] = counts
        next_position = max((n.position for n in build.nodes), default=-1) + 1
        for offset, machine in enumerate(new_machines):
            db.session.add(ClusterBuildNode(
                build_id=build.id,
                role=templates.ROLE_TO_NODE_ROLE.get(machine["role"], "worker"),
                position=next_position + offset,
                hostname=machine["name"], address="",
                address_source="provisioned", vsphere_vm_name=machine["name"],
                status="pending",
            ))
        db.session.flush()
        db.session.refresh(build)
        names = {m["name"] for m in new_machines}
    else:
        names = {m["name"] for m in spec.get("machines") or []}

    facts: Dict[str, Dict[str, Any]] = {}
    for name, attrs in instances.items():
        facts[name] = module.vm_facts(attrs)
    machines = []
    for machine in spec.get("machines") or []:
        seen = facts.get(machine["name"]) or {}
        machines.append({**machine, "ip": seen.get("address") or machine.get("ip")})
    spec["machines"] = machines
    build.provisioning_json = spec

    touched: List[ClusterBuildNode] = []
    missing: List[str] = []
    by_name = {m["name"]: m for m in machines}
    for node in build.nodes:
        name = node.vsphere_vm_name or node.hostname
        if name not in names:
            continue
        seen = facts.get(name) or {}
        if not seen.get("address"):
            missing.append(name)
            continue
        machine = by_name.get(name) or {}
        node.address = seen["address"]
        node.vsphere_vm_moid = str(seen.get("id") or "")[:64] or node.vsphere_vm_moid
        node.vsphere_host = seen.get("zone") or node.vsphere_host
        node.vsphere_power_state = "POWERED_ON"
        node.vsphere_cpu = machine.get("cpu") or node.vsphere_cpu
        node.vsphere_memory_mb = (machine.get("memoryGb") or 0) * 1024 or node.vsphere_memory_mb
        touched.append(node)
    if missing:
        from .jobs import JobFailed

        db.session.commit()
        raise JobFailed(
            f"OpenTofu created {', '.join(sorted(missing))} but the cloud reported no private "
            "address for it. Make a new plan once the VM shows one in the console."
        )

    if not build.vms_only and not build.result_cluster_id:
        endpoint = None
        if load_balanced(build):
            endpoint = module.endpoint_from_state(state_store.managed_resources(build.id))
            if not endpoint:
                from .jobs import JobFailed

                db.session.commit()
                raise JobFailed("The cloud load balancer has no address in OpenTofu's state yet. Plan again.")
        else:
            primary = next((n for n in build.nodes if n.role == "control_plane"), None)
            endpoint = primary.address if primary is not None else None
        if endpoint:
            build.control_plane_endpoint = f"{endpoint}:6443"
            try:
                ipaddress.ip_address(endpoint)
                build.vip_address = endpoint if load_balanced(build) else None
            except ValueError:
                build.vip_address = None
    db.session.commit()
    return touched


def _bypass_proxy_opener(ca_data: str) -> urllib.request.OpenerDirector:
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=base64.b64decode(ca_data).decode("ascii"))
    # The API is private (VPN / peering): never send it to the HTTP proxy.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context),
    )


def _kube(opener, server: str, token: str, method: str, path: str,
          body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
    request = urllib.request.Request(
        server.rstrip("/") + path, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json"},
    )
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


# How long a managed cluster's API may take to answer after the cloud says it
# is ready (endpoints and the authorizer warm up for a minute or two).
_API_WAIT_S = 10 * 60


def _service_account_token(server: str, ca_data: str, token: str,
                           on_line=lambda line: None) -> str:
    opener = _bypass_proxy_opener(ca_data)
    deadline = time.monotonic() + _API_WAIT_S
    last = ""
    while True:
        try:
            status, _ = _kube(opener, server, token, "GET", "/version")
            if status == 200:
                break
            last = f"HTTP {status}"
        except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
            last = str(getattr(exc, "reason", exc))
        if time.monotonic() > deadline:
            raise CloudError(
                f"The cluster's API at {server} did not answer within {_API_WAIT_S // 60} minutes "
                f"({last}). KubeSight must reach it over the private network (VPN / peering)."
            )
        on_line(f"Waiting for the cluster's API at {server}: {last}")
        time.sleep(10)

    namespace = "kube-system"
    for method, path, body in (
        ("POST", f"/api/v1/namespaces/{namespace}/serviceaccounts",
         {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": SERVICE_ACCOUNT}}),
        ("POST", "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings", {
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
            "metadata": {"name": SERVICE_ACCOUNT},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
            "subjects": [{"kind": "ServiceAccount", "name": SERVICE_ACCOUNT, "namespace": namespace}],
        }),
        ("POST", f"/api/v1/namespaces/{namespace}/secrets", {
            "apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/service-account-token",
            "metadata": {"name": f"{SERVICE_ACCOUNT}-token",
                         "annotations": {"kubernetes.io/service-account.name": SERVICE_ACCOUNT}},
        }),
    ):
        status, answer = _kube(opener, server, token, method, path, body)
        if status not in (200, 201, 409):
            raise CloudError(
                f"The cluster refused {body['kind']} {SERVICE_ACCOUNT} (HTTP {status}): "
                f"{answer.get('message') or answer}"
            )
        on_line(f"{body['kind']} {SERVICE_ACCOUNT}: {'created' if status != 409 else 'already there'}")
    for _ in range(30):
        status, answer = _kube(opener, server, token, "GET",
                               f"/api/v1/namespaces/{namespace}/secrets/{SERVICE_ACCOUNT}-token")
        data = (answer.get("data") or {}) if status == 200 else {}
        if data.get("token"):
            return base64.b64decode(data["token"]).decode("ascii")
        time.sleep(2)
    raise CloudError("Kubernetes never filled in the service account's token.")


def kubeconfig(name: str, server: str, ca_data: str, token: str) -> str:
    return yaml.safe_dump({
        "apiVersion": "v1", "kind": "Config",
        "clusters": [{"name": name, "cluster": {"server": server, "certificate-authority-data": ca_data}}],
        "users": [{"name": SERVICE_ACCOUNT, "user": {"token": token}}],
        "contexts": [{"name": name, "context": {"cluster": name, "user": SERVICE_ACCOUNT}}],
        "current-context": name,
    }, sort_keys=False)


# Test seam: (build, server, ca_data, token, on_line) -> service-account token.
_token_minter = None


def set_token_minter(minter) -> None:
    global _token_minter
    _token_minter = minter


def onboard_managed(build: ClusterBuild, on_line=lambda line: None) -> Optional[str]:
    """Register a managed cluster OpenTofu just made. Returns the public
    cluster id, or None when there was nothing to register (simulation)."""
    from .. import onboard

    module, account = account_for(build)
    resources = state_store.managed_resources(build.id)
    if accounts.simulated() and _token_minter is None:
        on_line("Simulated cloud: the cluster is not registered (there is no API to reach).")
        return None
    access = module.managed_access(account, resources, build.provisioning_json or {})
    server = access["server"]
    on_line(f"Signing in to {server} with the account's keys")
    mint = _token_minter or (lambda b, s, c, t, log: _service_account_token(s, c, t, log))
    sa_token = mint(build, server, access["caData"], access["token"], on_line)
    host = re.sub(r"^https?://", "", server).rstrip("/")
    build.control_plane_endpoint = host if ":" in host else f"{host}:443"
    public_id = onboard.register_cluster(build, kubeconfig(build.name, server, access["caData"], sa_token))
    on_line(f"Registered as cluster {public_id}, signed in as kube-system/{SERVICE_ACCOUNT}.")
    return public_id
