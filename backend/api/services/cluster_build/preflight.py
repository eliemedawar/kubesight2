"""Preflight: vCenter placement checks + per-node SSH readiness checks.

Results are per node, per check: ``{id, label, status: pass|warn|fail, detail,
hint}``. Hard fails block the build; warns are overridable with a recorded
acknowledgement. The vCenter checks run without touching a single node — the
anti-affinity check ("your 3 control planes share one ESXi host") is the whole
reason the vSphere integration exists.
"""

from __future__ import annotations

import base64
import shlex
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ...models import ClusterBuild, ClusterBuildNode
from ..ssh import SshTarget, get_transport
from . import addons as addon_registry
from . import cni as cni_registry
from . import k8s_versions, kubeadm, os_adapters
from .os_adapters import OsFacts
from .profiles import ResolvedProfile, registry_authority
from .scrub import scrub

_ROLE_MINIMUMS = {
    # role: (cpus, memory MiB, disk GiB on /var)
    "control_plane": (2, 2048, 20),
    "worker": (2, 2048, 20),
    "loadbalancer": (1, 512, 5),
}

# Free /var disk, tiered: below fail_floor => fail; below pass_floor => warn
# (enough to build, tight for image churn over time); at/above => pass. A
# control plane/worker wants 20 GiB long-term but can smoke-test on 8.
_DISK_THRESHOLDS = {
    # role: (fail_floor GiB, pass_floor GiB)
    "control_plane": (8, 20),
    "worker": (8, 20),
    "loadbalancer": (5, 5),
}
_CLOCK_SKEW_WARN_S = 30
_CLOCK_SKEW_FAIL_S = 120
_FSYNC_WARN_MS = 10.0
_PREFLIGHT_WORKERS = 8

# Ports that must be free before kubeadm runs, per role.
_ROLE_PORTS = {
    "control_plane": (6443, 2379, 2380, 10250, 10257, 10259),
    "worker": (10250,),
    "loadbalancer": (6443,),
}


def _required_ports(
    build: ClusterBuild,
    node: ClusterBuildNode,
) -> tuple[int, ...]:
    """Local ports that must be unused before the selected components start."""
    ports = list(_ROLE_PORTS.get(node.role, ()))
    metal_lb_selected = any(
        (
            item == "metallb"
            if isinstance(item, str)
            else isinstance(item, dict) and item.get("id") == "metallb"
        )
        for item in (build.addons_json or [])
    )
    if metal_lb_selected and node.role in ("control_plane", "worker"):
        ports.append(7946)
    return tuple(ports)


def _check(check_id: str, label: str, status: str, detail: str = "", hint: str = "") -> Dict[str, Any]:
    return {"id": check_id, "label": label, "status": status, "detail": detail, "hint": hint}


def _worst(checks: List[Dict[str, Any]]) -> str:
    statuses = {c["status"] for c in checks}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "pass"


# ---------------------------------------------------------------------------
# vCenter-side checks (no SSH involved)
# ---------------------------------------------------------------------------

def vsphere_checks(
    build: ClusterBuild,
    nodes: Optional[List[ClusterBuildNode]] = None,
) -> Dict[int, List[Dict[str, Any]]]:
    """Per-node placement/sizing checks from the vSphere metadata captured when
    nodes were added. Nodes without vSphere metadata (manual entry) get none.

    ``nodes`` narrows the checks to a subset — growing a live cluster must not
    re-examine the machines already serving it. Anti-affinity still reads the
    whole tier, because that is the question being asked.
    """
    subject_ids = {n.id for n in nodes} if nodes is not None else None
    results: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    vm_nodes = [
        n for n in build.nodes
        if n.vsphere_vm_moid and (subject_ids is None or n.id in subject_ids)
    ]

    for node in vm_nodes:
        checks = results[node.id]
        if (node.vsphere_power_state or "").upper() != "POWERED_ON":
            checks.append(_check(
                "vs_power", "VM powered on", "fail",
                f"Power state: {node.vsphere_power_state or 'unknown'}.",
                "Power the VM on in vCenter before building.",
            ))
        else:
            checks.append(_check("vs_power", "VM powered on", "pass"))

        tools = (node.vsphere_tools_status or "").upper()
        if tools == "RUNNING":
            checks.append(_check("vs_tools", "VMware Tools", "pass"))
        else:
            status = "warn" if node.address else "fail"
            checks.append(_check(
                "vs_tools", "VMware Tools", status,
                "Tools is not running — vCenter cannot report the guest IP.",
                "Recommended, not required. A manual management IP override "
                "is set." if node.address else
                "Install VMware Tools or enter a manual management IP.",
            ))

        cpus, mem_mib, _ = _ROLE_MINIMUMS.get(node.role, (2, 2048, 20))
        if node.vsphere_cpu is not None and node.vsphere_cpu < cpus:
            checks.append(_check(
                "vs_cpu", "vCPU allocation", "fail",
                f"{node.vsphere_cpu} vCPU < required {cpus} for {node.role}.",
                "Increase the VM's vCPU count in vCenter.",
            ))
        else:
            checks.append(_check("vs_cpu", "vCPU allocation", "pass"))
        if node.vsphere_memory_mb is not None and node.vsphere_memory_mb < mem_mib:
            checks.append(_check(
                "vs_mem", "Memory allocation", "fail",
                f"{node.vsphere_memory_mb} MiB < required {mem_mib} MiB for {node.role}.",
                "Increase the VM's memory in vCenter.",
            ))
        else:
            checks.append(_check("vs_mem", "Memory allocation", "pass"))

    # Anti-affinity: an "HA" tier whose VMs share an ESXi host is not HA.
    for role, check_id, label in (
        ("control_plane", "vs_cp_affinity", "Control-plane host anti-affinity"),
        ("loadbalancer", "vs_lb_affinity", "Load-balancer host anti-affinity"),
    ):
        tier = [
            n for n in build.nodes
            if n.role == role and n.vsphere_vm_moid and n.vsphere_host
        ]
        if len(tier) < 2:
            continue
        by_host: Dict[str, List[ClusterBuildNode]] = defaultdict(list)
        for node in tier:
            by_host[node.vsphere_host].append(node)
        colocated = {host: nodes for host, nodes in by_host.items() if len(nodes) > 1}
        for node in tier:
            if subject_ids is not None and node.id not in subject_ids:
                continue
            if colocated:
                detail = "; ".join(
                    f"{host}: {', '.join(x.vsphere_vm_name or x.hostname for x in nodes)}"
                    for host, nodes in colocated.items()
                )
                results[node.id].append(_check(
                    check_id, label, "fail",
                    f"Multiple {role.replace('_', ' ')} VMs share one ESXi host — {detail}.",
                    "Migrate the VMs to separate hosts (or add a DRS "
                    "anti-affinity rule) so one host failure cannot take the "
                    "tier down.",
                ))
            else:
                results[node.id].append(_check(check_id, label, "pass"))

    # Shared datastore across control planes: warn (etcd co-located on one array).
    cp_nodes = [
        n for n in build.nodes
        if n.role == "control_plane" and n.vsphere_vm_moid and n.vsphere_datastore
    ]
    if len(cp_nodes) > 1:
        datastores = {n.vsphere_datastore for n in cp_nodes}
        for node in cp_nodes:
            if len(datastores) == 1:
                results[node.id].append(_check(
                    "vs_cp_datastore", "Control-plane datastore diversity", "warn",
                    f"All control planes share datastore '{node.vsphere_datastore}'.",
                    "A datastore outage would take down every etcd member; "
                    "consider spreading them.",
                ))
            else:
                results[node.id].append(_check(
                    "vs_cp_datastore", "Control-plane datastore diversity", "pass"
                ))

    return dict(results)


# ---------------------------------------------------------------------------
# Build-scoped checks for the exact selected Kubernetes version
#
# These need no SSH: they answer "can this build's pinned patch version be
# assembled at all from the selected CNI, add-ons and repo mode" before a single
# node is touched. An unsupported combination must fail here, not half-way
# through cluster creation.
# ---------------------------------------------------------------------------

def _kubeadm_config_check(build: ClusterBuild, profile: ResolvedProfile) -> Dict[str, Any]:
    """Render this build's kubeadm config and prove it parses for its version.

    Migrating a minor to a newer kubeadm API is never just an ``apiVersion``
    edit, so the generated document is parsed back and its required fields are
    asserted rather than assumed.
    """
    try:
        expected_api = kubeadm.config_api_version(build.k8s_version)
        rendered = kubeadm.render_init_config(
            k8s_version=build.k8s_version,
            control_plane_endpoint=build.control_plane_endpoint or "127.0.0.1:6443",
            pod_cidr=build.pod_cidr,
            service_cidr=build.service_cidr,
            profile=profile,
            node_name="preflight-render",
            server_tls_bootstrap=any(
                (item or {}).get("id") == "metrics-server"
                for item in (build.addons_json or [])
            ),
        )
        documents = [doc for doc in yaml.safe_load_all(rendered) if doc]
    except (ValueError, yaml.YAMLError) as exc:
        return _check(
            "k8s_kubeadm_config", "kubeadm configuration valid", "fail",
            scrub(str(exc)),
            "The generated kubeadm configuration could not be produced for "
            f"Kubernetes {build.k8s_version}.",
        )

    by_kind = {str(doc.get("kind") or ""): doc for doc in documents}
    missing = [
        kind for kind in ("InitConfiguration", "ClusterConfiguration",
                          "KubeletConfiguration")
        if kind not in by_kind
    ]
    if missing:
        return _check(
            "k8s_kubeadm_config", "kubeadm configuration valid", "fail",
            f"The rendered configuration is missing: {', '.join(missing)}.",
        )

    problems: List[str] = []
    for kind in ("InitConfiguration", "ClusterConfiguration"):
        actual = str(by_kind[kind].get("apiVersion") or "")
        if actual != expected_api:
            problems.append(
                f"{kind} uses {actual or 'no apiVersion'}, expected {expected_api}"
            )
    init_registration = by_kind["InitConfiguration"].get("nodeRegistration") or {}
    if not init_registration.get("criSocket"):
        problems.append("InitConfiguration.nodeRegistration.criSocket is unset")
    cluster = by_kind["ClusterConfiguration"]
    networking = cluster.get("networking") or {}
    for path, value in (
        ("kubernetesVersion", cluster.get("kubernetesVersion")),
        ("controlPlaneEndpoint", cluster.get("controlPlaneEndpoint")),
        ("networking.podSubnet", networking.get("podSubnet")),
        ("networking.serviceSubnet", networking.get("serviceSubnet")),
        ("apiServer.certSANs", (cluster.get("apiServer") or {}).get("certSANs")),
    ):
        if not value:
            problems.append(f"ClusterConfiguration.{path} is unset")
    pinned = str(cluster.get("kubernetesVersion") or "").lstrip("v")
    if pinned and pinned != build.k8s_version:
        problems.append(
            f"ClusterConfiguration.kubernetesVersion is v{pinned}, but this "
            f"build is pinned to {build.k8s_version}"
        )
    if problems:
        return _check(
            "k8s_kubeadm_config", "kubeadm configuration valid", "fail",
            "; ".join(problems) + ".",
            "kubeadm would reject or misread this configuration.",
        )
    return _check(
        "k8s_kubeadm_config", "kubeadm configuration valid", "pass",
        f"{expected_api} rendered and parsed for v{build.k8s_version}.",
    )


def _offline_artifact_check(
    build: ClusterBuild, profile: ResolvedProfile
) -> Optional[Dict[str, Any]]:
    """Offline mode: every manifest this build needs must already be vendored."""
    if profile.repo_mode != "offline":
        return None
    missing: List[str] = []
    descriptor = cni_registry.get(build.cni_plugin)
    if descriptor is not None:
        version = build.cni_version or descriptor.versions[0]
        missing.extend(
            f"{descriptor.display_name} {version}: {filename}"
            for filename in descriptor.manifest_files
            if not descriptor.bundled_path(version, filename).is_file()
        )
    for selection in build.addons_json or []:
        addon = addon_registry.get(str((selection or {}).get("id") or ""))
        if addon is None:
            continue
        version = str(selection.get("version") or addon.versions[0])
        missing.extend(
            f"{addon.display_name} {version}: {filename}"
            for filename in addon.manifest_files
            if not addon.bundled_path(version, filename).is_file()
        )
    if profile.offline_bundle_path and not Path(profile.offline_bundle_path).exists():
        missing.append(f"offline bundle path {profile.offline_bundle_path}")
    if missing:
        return _check(
            "k8s_offline_bundle", "Offline artifacts vendored", "fail",
            "Not present on the KubeSight host: " + "; ".join(missing) + ".",
            "Run `python backend/tools/fetch_cluster_build_bundles.py` on the "
            "KubeSight host, or pick a repo mode that allows an internet "
            "fallback.",
        )
    return _check(
        "k8s_offline_bundle", "Offline artifacts vendored", "pass",
        "Every pinned manifest this build needs is vendored locally.",
    )


def version_checks(
    build: ClusterBuild, profile: ResolvedProfile
) -> List[Dict[str, Any]]:
    """Checks that depend only on the build's pinned Kubernetes version."""
    record = k8s_versions.record_for(build.k8s_version)
    if record is None or not record.enabled:
        reason = (
            "; ".join(record.blockers) if record is not None and record.blockers
            else "no support record is declared for this minor"
        )
        return [_check(
            "k8s_version", "Kubernetes version supported", "fail",
            f"Kubernetes {build.k8s_version} is not supported by the Cluster "
            f"Builder — {reason}.",
            "Supported minors: "
            f"{', '.join(k8s_versions.enabled_minors()) or 'none'}.",
        )]

    checks = [_check(
        "k8s_version", "Kubernetes version supported", "pass",
        f"v{build.k8s_version} (minor {record.minor}; pause image "
        f"{record.pause_image_tag}; {record.kubeadm_config_api}).",
    )]
    checks.append(_kubeadm_config_check(build, profile))

    descriptor = cni_registry.get(build.cni_plugin)
    cni_version = build.cni_version or (
        descriptor.versions[0] if descriptor is not None else ""
    )
    if descriptor is None:
        checks.append(_check(
            "k8s_cni_support", "CNI supports this Kubernetes minor", "fail",
            f"CNI plugin '{build.cni_plugin}' is unavailable.",
        ))
    elif not descriptor.supports_k8s_minor(cni_version, record.minor):
        usable = descriptor.versions_for_k8s_minor(record.minor)
        checks.append(_check(
            "k8s_cni_support", "CNI supports this Kubernetes minor", "fail",
            f"{descriptor.display_name} {cni_version} is not validated on "
            f"Kubernetes {record.minor} (this version covers: "
            f"{', '.join(descriptor.supported_k8s_minors(cni_version)) or 'nothing'}).",
            f"Use {descriptor.display_name} {', '.join(usable)} instead." if usable
            else "No vendored release of this CNI covers that Kubernetes "
                 "version; choose another plugin or Kubernetes version.",
        ))
    else:
        checks.append(_check(
            "k8s_cni_support", "CNI supports this Kubernetes minor", "pass",
            f"{descriptor.display_name} {cni_version} on Kubernetes "
            f"{record.minor}.",
        ))

    unsupported = []
    for selection in build.addons_json or []:
        addon = addon_registry.get(str((selection or {}).get("id") or ""))
        if addon is None:
            unsupported.append(f"'{(selection or {}).get('id')}' is unavailable")
            continue
        addon_version = str(selection.get("version") or addon.versions[0])
        if not addon.supports_k8s_minor(addon_version, record.minor):
            usable = addon.versions_for_k8s_minor(record.minor)
            unsupported.append(
                f"{addon.display_name} {addon_version} is not validated on "
                f"Kubernetes {record.minor}"
                + (f" (use {', '.join(usable)})" if usable else "")
            )
    if build.addons_json:
        checks.append(_check(
            "k8s_addon_support", "Add-ons support this Kubernetes minor",
            "fail" if unsupported else "pass",
            "; ".join(unsupported) + "." if unsupported
            else f"All selected add-ons are validated on Kubernetes {record.minor}.",
            "Deselect the add-on or choose a Kubernetes version it supports."
            if unsupported else "",
        ))

    offline = _offline_artifact_check(build, profile)
    if offline is not None:
        checks.append(offline)
    return checks


# ---------------------------------------------------------------------------
# Node-side probe (one escalated POSIX script; output parsed as KS_KEY=value)
# ---------------------------------------------------------------------------

def image_manifest_url(image_prefix: str, repository: str, tag: str) -> str:
    """Registry v2 manifest URL for ``<prefix>/<repository>:<tag>``.

    ``registry.k8s.io`` → ``https://registry.k8s.io/v2/pause/manifests/3.10``;
    ``nexus:5000/kubernetes`` →
    ``https://nexus:5000/v2/kubernetes/pause/manifests/3.10``.
    """
    prefix = str(image_prefix or "").strip().strip("/")
    authority = registry_authority(prefix)
    path = prefix[len(authority):].strip("/")
    repo_path = f"{path}/{repository}" if path else repository
    return f"https://{authority}/v2/{repo_path}/manifests/{tag}"


def _probe_script(build: ClusterBuild, node: ClusterBuildNode, profile: ResolvedProfile) -> str:
    ports = " ".join(str(p) for p in _required_ports(build, node))
    version = build.k8s_version.lstrip("v")
    minor_placeholder = ".".join(version.split(".")[:2])
    deb_repo = profile.k8s_pkg_repo("debian", minor_placeholder)
    rpm_repo = profile.k8s_pkg_repo("rhel", minor_placeholder)
    repo_urls = []
    image_urls = []
    if profile.repo_mode != "offline":
        repo_urls.append(deb_repo)
        if node.role != "loadbalancer":
            for host in profile.registry_hosts():
                repo_urls.append(f"https://{host}/v2/")
            # The pause tag is the one image kubeadm pins per minor rather than
            # per patch, so a mirror stocked for an older minor fails here
            # instead of at pod-sandbox creation time on every node. An
            # unrecorded minor is reported by ``version_checks`` instead.
            record = k8s_versions.record_for(version)
            if record is not None:
                image_urls.append(image_manifest_url(
                    profile.k8s_image_registry, "pause", record.pause_image_tag
                ))
    probe_urls = " ".join(shlex.quote(u) for u in repo_urls)
    image_probe_urls = " ".join(shlex.quote(u) for u in image_urls)
    # Load balancers never install kubelet/kubeadm/kubectl, and an offline
    # build must not carry an outbound URL into the script at all.
    probe_packages = (
        node.role != "loadbalancer" and profile.repo_mode != "offline"
    )
    pkg_probe = "1" if probe_packages else "0"
    deb_packages_url = shlex.quote(
        deb_repo.rstrip("/") + "/Packages" if probe_packages else ""
    )
    rpm_repomd_url = shlex.quote(
        rpm_repo.rstrip("/") + "/repodata/repomd.xml" if probe_packages else ""
    )
    proxy_env = profile.proxy_env()
    ca_b64 = base64.b64encode(
        profile.extra_ca_certs_pem.encode("ascii")
    ).decode("ascii")
    fsync_test = "1" if node.role == "control_plane" else "0"
    vip = build.vip_address or ""
    vip_test = "1" if (node.role == "loadbalancer" and vip) else "0"
    return f"""#!/bin/sh
{proxy_env}
# KubeSight node preflight probe — emits KS_KEY=value lines only.
umask 077
KS_CA_FILE=""
KS_FSYNC_FILE=""
ks_cleanup() {{
  [ -z "$KS_CA_FILE" ] || rm -f -- "$KS_CA_FILE"
  [ -z "$KS_FSYNC_FILE" ] || rm -f -- "$KS_FSYNC_FILE"
}}
trap ks_cleanup EXIT HUP INT TERM
[ -f /etc/os-release ] && . /etc/os-release
echo "KS_OS_ID=$ID"
echo "KS_OS_LIKE=$ID_LIKE"
echo "KS_OS_VERSION=$VERSION_ID"
echo "KS_OS_PRETTY=$PRETTY_NAME"
echo "KS_ARCH=$(uname -m)"
echo "KS_KERNEL=$(uname -r)"
echo "KS_HOSTNAME=$(hostname)"
[ -r /sys/class/dmi/id/product_uuid ] && echo "KS_PRODUCT_UUID=$(cat /sys/class/dmi/id/product_uuid)"
echo "KS_CPUS=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN)"
echo "KS_MEM_MIB=$(( $(grep MemTotal /proc/meminfo | tr -s ' ' | cut -d' ' -f2) / 1024 ))"
echo "KS_SWAP_KB=$(grep SwapTotal /proc/meminfo | tr -s ' ' | cut -d' ' -f2)"
echo "KS_DISK_VAR_GB=$(df -P /var 2>/dev/null | tail -1 | tr -s ' ' | cut -d' ' -f4 | awk '{{print int($1/1048576)}}')"
echo "KS_EPOCH=$(date +%s)"
for mod in overlay br_netfilter; do
  if modprobe -n $mod >/dev/null 2>&1 || grep -q "^$mod" /proc/modules; then
    echo "KS_MOD_$mod=ok"
  else
    echo "KS_MOD_$mod=missing"
  fi
done
if command -v ss >/dev/null 2>&1; then
  for port in {ports}; do
    if ss -Hln 2>/dev/null | grep -q ":$port "; then echo "KS_PORT_$port=busy"; else echo "KS_PORT_$port=free"; fi
  done
fi
if command -v curl >/dev/null 2>&1; then
  if [ -n "{ca_b64}" ]; then
    KS_CA_FILE="$(mktemp /run/.kubesight-preflight-ca.XXXXXX)" || exit 1
    for system_ca in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt; do
      if [ -r "$system_ca" ]; then cat "$system_ca" > "$KS_CA_FILE"; break; fi
    done
    echo "{ca_b64}" | base64 -d >> "$KS_CA_FILE"
    chmod 600 "$KS_CA_FILE"
  fi
  ks_curl() {{
    if [ -n "$KS_CA_FILE" ]; then
      curl --cacert "$KS_CA_FILE" "$@"
    else
      curl "$@"
    fi
  }}
  for url in {probe_urls}; do
    if ks_curl -m 8 -sf -o /dev/null "$url" 2>/dev/null || ks_curl -m 8 -s -o /dev/null -w '%{{http_code}}' "$url" 2>/dev/null | grep -qE '^(2|3|401|403)'; then
      echo "KS_REPO_OK=$url"
    else
      echo "KS_REPO_FAIL=$url"
    fi
  done
  # Registry v2 manifest probe. The Accept header matters: without it a
  # registry serving an OCI index answers 404 for an image that is present.
  for url in {image_probe_urls}; do
    KS_IMG_CODE=$(ks_curl -m 8 -s -o /dev/null -w '%{{http_code}}' \
      -H 'Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json' \
      "$url" 2>/dev/null)
    # 401/403: the mirror wants credentials, which the image pre-pull phase
    # proves. Only 404/000 means the tag is genuinely unavailable here.
    if echo "$KS_IMG_CODE" | grep -qE '^(2|3|401|403)'; then
      echo "KS_IMG_OK=$url"
    else
      echo "KS_IMG_FAIL=$url"
    fi
  done
  if [ "{pkg_probe}" = "1" ]; then
    # The exact kubeadm/kubelet/kubectl build must be obtainable, not merely
    # the repository. Debian repos publish a flat Packages index readable
    # before the repo is configured on the node.
    if command -v apt-get >/dev/null 2>&1; then
      if ks_curl -m 15 -sf {deb_packages_url} 2>/dev/null | grep -q '^Version: {version}-'; then
        echo "KS_PKG_EXACT=ok"
      else
        echo "KS_PKG_EXACT=missing"
      fi
    elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
      # RPM metadata is compressed and indexed; confirm this minor's repository
      # publishes metadata and let the pinned dnf install assert the patch.
      if ks_curl -m 15 -sf -o /dev/null {rpm_repomd_url} 2>/dev/null; then
        echo "KS_PKG_EXACT=repo_only"
      else
        echo "KS_PKG_EXACT=missing"
      fi
    fi
  fi
  [ -z "$KS_CA_FILE" ] || rm -f -- "$KS_CA_FILE"
  KS_CA_FILE=""
else
  echo "KS_CURL=missing"
fi
if [ "{fsync_test}" = "1" ] && command -v dd >/dev/null 2>&1; then
  KS_FSYNC_FILE="$(mktemp /var/tmp/.kubesight-fsync.XXXXXX)" || exit 1
  start=$(date +%s%N 2>/dev/null || echo 0)
  dd if=/dev/zero of="$KS_FSYNC_FILE" bs=2048 count=100 oflag=dsync >/dev/null 2>&1
  end=$(date +%s%N 2>/dev/null || echo 0)
  rm -f -- "$KS_FSYNC_FILE"
  KS_FSYNC_FILE=""
  if [ "$start" != "0" ] && [ "$end" != "0" ]; then
    echo "KS_FSYNC_MS_PER_OP=$(( (end - start) / 100000000 )).$(( ((end - start) / 10000000) % 10 ))"
  fi
fi
if [ "{vip_test}" = "1" ]; then
  if ping -c 1 -W 1 {vip} >/dev/null 2>&1; then echo "KS_VIP_STATE=in_use"; else echo "KS_VIP_STATE=free"; fi
fi
if sudo -n true 2>/dev/null || [ "$(id -u)" = "0" ]; then echo "KS_ESCALATION=ok"; fi
exit 0
"""


def _parse_probe(output: str) -> Dict[str, Any]:
    facts: Dict[str, Any] = {
        "repo_ok": [], "repo_fail": [], "image_ok": [], "image_fail": [],
    }
    collected = {
        "KS_REPO_OK": "repo_ok",
        "KS_REPO_FAIL": "repo_fail",
        "KS_IMG_OK": "image_ok",
        "KS_IMG_FAIL": "image_fail",
    }
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("KS_") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        bucket = collected.get(key)
        if bucket is not None:
            facts[bucket].append(value)
        else:
            facts[key] = value
    return facts


def _node_checks(
    build: ClusterBuild,
    node: ClusterBuildNode,
    facts: Dict[str, Any],
    profile: ResolvedProfile,
) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = [
        _check("ssh", "SSH connectivity + escalation", "pass",
               f"Connected as configured user; sudo {'ok' if 'KS_ESCALATION' in facts else 'unverified'}.")
    ]

    os_facts = OsFacts(
        os_id=str(facts.get("KS_OS_ID", "")).strip('"'),
        os_id_like=str(facts.get("KS_OS_LIKE", "")).strip('"'),
        version_id=str(facts.get("KS_OS_VERSION", "")).strip('"'),
        pretty_name=str(facts.get("KS_OS_PRETTY", "")).strip('"'),
        arch=str(facts.get("KS_ARCH", "")),
    )
    adapter = os_adapters.detect(os_facts)
    if adapter is None:
        matrix = ", ".join(
            f"{a['displayName']} {'/'.join(a['validatedVersions'])}"
            for a in os_adapters.supported_matrix()
        )
        checks.append(_check(
            "os", "Operating system supported", "fail",
            f"Unsupported distribution: {os_facts.pretty_name or os_facts.os_id or 'unknown'}.",
            f"Supported matrix: {matrix}. No generic fallback is attempted.",
        ))
        return checks  # nothing else is meaningful on an unsupported OS
    node.os_family = adapter.id
    node.os_version = os_facts.version_id
    node.arch = os_facts.arch
    if adapter.version_validated(os_facts):
        checks.append(_check("os", "Operating system supported", "pass",
                             os_facts.pretty_name))
    else:
        checks.append(_check(
            "os", "Operating system supported", "warn",
            f"{os_facts.pretty_name}: {adapter.display_name} is supported but "
            f"this version is outside the validated set "
            f"({', '.join(adapter.validated_versions)}).",
        ))

    if os_facts.arch not in ("x86_64", "amd64"):
        checks.append(_check(
            "arch", "CPU architecture", "fail",
            f"{os_facts.arch or 'unknown'} — v1 supports x86_64 only.",
        ))
    else:
        checks.append(_check("arch", "CPU architecture", "pass", os_facts.arch))

    cpus_req, mem_req, disk_req = _ROLE_MINIMUMS.get(node.role, (2, 2048, 20))
    try:
        cpus = int(facts.get("KS_CPUS", 0))
    except (TypeError, ValueError):
        cpus = 0
    checks.append(_check(
        "cpu", "CPU count", "pass" if cpus >= cpus_req else "fail",
        f"{cpus} CPUs (minimum {cpus_req} for {node.role}).",
    ))
    try:
        mem = int(facts.get("KS_MEM_MIB", 0))
    except (TypeError, ValueError):
        mem = 0
    checks.append(_check(
        "memory", "Memory", "pass" if mem >= mem_req * 0.95 else "fail",
        f"{mem} MiB (minimum {mem_req} MiB for {node.role}).",
    ))
    try:
        disk = int(facts.get("KS_DISK_VAR_GB", 0))
    except (TypeError, ValueError):
        disk = 0
    disk_fail_floor, disk_pass_floor = _DISK_THRESHOLDS.get(node.role, (8, 20))
    if disk < disk_fail_floor:
        disk_status = "fail"
    elif disk < disk_pass_floor:
        disk_status = "warn"
    else:
        disk_status = "pass"
    checks.append(_check(
        "disk", "Free disk on /var", disk_status,
        f"{disk} GiB free on /var "
        f"(need ≥ {disk_fail_floor} GiB; ≥ {disk_pass_floor} GiB recommended).",
        "Free space or grow the disk — kubeadm, containerd, and pulled images "
        f"land on /var." if disk_status != "pass" else "",
    ))

    try:
        swap_kb = int(facts.get("KS_SWAP_KB", 0))
    except (TypeError, ValueError):
        swap_kb = 0
    if swap_kb > 0:
        checks.append(_check(
            "swap", "Swap", "warn",
            "Swap is enabled — the build disables it (swapoff + fstab).",
        ))
    else:
        checks.append(_check("swap", "Swap", "pass", "Disabled."))

    for module in ("overlay", "br_netfilter"):
        state = facts.get(f"KS_MOD_{module}", "missing")
        checks.append(_check(
            f"mod_{module}", f"Kernel module {module}",
            "pass" if state == "ok" else "fail",
            "" if state == "ok" else f"{module} is unavailable on this kernel.",
        ))

    for port in _required_ports(build, node):
        state = facts.get(f"KS_PORT_{port}")
        if state == "busy":
            checks.append(_check(
                f"port_{port}", f"Port {port} free", "fail",
                f"Something is already listening on :{port}.",
            ))
        elif state == "free":
            checks.append(_check(f"port_{port}", f"Port {port} free", "pass"))

    try:
        node_epoch = int(facts.get("KS_EPOCH", 0))
    except (TypeError, ValueError):
        node_epoch = 0
    if node_epoch:
        skew = abs(int(time.time()) - node_epoch)
        if skew > _CLOCK_SKEW_FAIL_S:
            status = "fail"
        elif skew > _CLOCK_SKEW_WARN_S:
            status = "warn"
        else:
            status = "pass"
        checks.append(_check(
            "clock", "Clock skew", status,
            f"{skew}s vs the KubeSight backend.",
            "TLS and etcd both suffer under clock skew; enable NTP/chrony." if status != "pass" else "",
        ))

    if profile.repo_mode != "offline":
        for url in facts.get("repo_fail", []):
            checks.append(_check(
                "repo", "Repository reachability", "fail",
                f"Unreachable from this node: {url}",
                "Every configured repo/registry must be reachable from every "
                "node before install starts.",
            ))
        if facts.get("repo_ok") and not facts.get("repo_fail"):
            checks.append(_check("repo", "Repository reachability", "pass",
                                 f"{len(facts['repo_ok'])} endpoint(s) reachable "
                                 "with TLS verified where applicable; registry "
                                 "credentials are verified "
                                 "by the image pre-pull phase."))
        if facts.get("KS_CURL") == "missing":
            checks.append(_check(
                "repo", "Repository reachability", "warn",
                "curl is not installed; reachability could not be probed.",
            ))

        for url in facts.get("image_fail", []):
            checks.append(_check(
                "k8s_images", "Kubernetes images available", "fail",
                f"The configured registry does not serve {url}.",
                "kubeadm pins this pause tag for the selected Kubernetes "
                "minor; mirror it before building, or the pod sandbox cannot "
                "start on any node.",
            ))
        if facts.get("image_ok") and not facts.get("image_fail"):
            checks.append(_check(
                "k8s_images", "Kubernetes images available", "pass",
                f"pause:{kubeadm.pause_image_tag(build.k8s_version)} is served "
                "by the configured registry.",
            ))

        package_state = facts.get("KS_PKG_EXACT")
        if package_state == "ok":
            checks.append(_check(
                "k8s_packages", "Exact Kubernetes packages available", "pass",
                f"kubelet/kubeadm/kubectl {build.k8s_version} are published by "
                "the configured repository.",
            ))
        elif package_state == "repo_only":
            checks.append(_check(
                "k8s_packages", "Exact Kubernetes packages available", "warn",
                f"The Kubernetes {'.'.join(build.k8s_version.split('.')[:2])} "
                "RPM repository publishes metadata, but RPM indexes are "
                "compressed so the exact patch is asserted by the pinned "
                "install itself.",
            ))
        elif package_state == "missing":
            checks.append(_check(
                "k8s_packages", "Exact Kubernetes packages available", "fail",
                f"kubelet/kubeadm/kubectl {build.k8s_version} are not published "
                "by the configured package repository.",
                "Pick a version the repository carries, or mirror this patch "
                "release before building.",
            ))

    fsync = facts.get("KS_FSYNC_MS_PER_OP")
    if fsync is not None and node.role == "control_plane":
        try:
            fsync_ms = float(fsync)
        except (TypeError, ValueError):
            fsync_ms = None
        if fsync_ms is not None:
            checks.append(_check(
                "fsync", "etcd disk fsync latency",
                "pass" if fsync_ms <= _FSYNC_WARN_MS else "warn",
                f"~{fsync_ms:.1f} ms/op (etcd wants ≤ {_FSYNC_WARN_MS:.0f} ms).",
                "Slow datastores are the top cause of flaky stacked-etcd "
                "clusters." if fsync_ms > _FSYNC_WARN_MS else "",
            ))

    vip_state = facts.get("KS_VIP_STATE")
    if vip_state == "in_use":
        checks.append(_check(
            "vip", "VIP address free", "fail",
            f"{build.vip_address} already answers ping — it must be unused.",
            "Pick an unallocated address on the control-plane L2 segment.",
        ))
    elif vip_state == "free":
        checks.append(_check("vip", "VIP address free", "pass"))

    return checks


def _uniqueness_checks(nodes_facts: Dict[int, Dict[str, Any]], build: ClusterBuild) -> Dict[int, List[Dict[str, Any]]]:
    extra: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    node_by_id = {n.id: n for n in build.nodes}
    for key, check_id, label in (
        ("KS_HOSTNAME", "hostname_unique", "Unique hostname"),
        ("KS_PRODUCT_UUID", "uuid_unique", "Unique machine UUID"),
    ):
        seen: Dict[str, List[int]] = defaultdict(list)
        for node_id, facts in nodes_facts.items():
            value = str(facts.get(key, "")).strip()
            if value:
                seen[value].append(node_id)
        for value, ids in seen.items():
            status = "fail" if len(ids) > 1 else "pass"
            for node_id in ids:
                detail = ""
                if status == "fail":
                    others = [
                        node_by_id[i].hostname or node_by_id[i].address
                        for i in ids if i != node_id and i in node_by_id
                    ]
                    detail = f"'{value}' is shared with: {', '.join(others)}."
                extra[node_id].append(_check(
                    check_id, label, status, detail,
                    "kubeadm requires unique hostnames and product UUIDs "
                    "(cloned VMs often collide)." if status == "fail" else "",
                ))
    return dict(extra)


def run_node_preflight(
    build: ClusterBuild,
    profile: ResolvedProfile,
    target_builder,  # (node) -> SshTarget
    nodes: Optional[List[ClusterBuildNode]] = None,
) -> Dict[int, Dict[str, Any]]:
    """SSH-probe nodes in parallel. Returns {node_id: {status, checks}}.

    ``nodes`` defaults to every node in the build. Pass a subset when growing a
    live cluster: a running control plane legitimately holds :6443, so probing
    it would report a port clash against the cluster it is already serving.
    """
    subjects = build.nodes if nodes is None else nodes
    from flask import current_app

    transport = get_transport()
    app = current_app._get_current_object()
    raw_facts: Dict[int, Dict[str, Any]] = {}
    results: Dict[int, Dict[str, Any]] = {}

    # Targets + scripts are built on THIS thread (they read the DB); worker
    # threads still get an app context of their own because the real transport's
    # host-key verification reads/writes ssh_host_keys.
    jobs = []
    for node in subjects:
        try:
            jobs.append((node.id, target_builder(node), _probe_script(build, node, profile)))
        except Exception as exc:  # noqa: BLE001 — a broken profile is a check result
            results[node.id] = {
                "status": "fail",
                "checks": [_check(
                    "ssh", "SSH connectivity + escalation", "fail", scrub(str(exc)),
                    "Verify the node's SSH connection profile.",
                )],
            }

    def _probe(job) -> None:
        node_id, target, script = job
        try:
            with app.app_context():
                probe_result = transport.run(target, script, timeout_s=90)
            raw_facts[node_id] = _parse_probe(probe_result.output)
        except Exception as exc:  # noqa: BLE001 — a dead node is a check result
            results[node_id] = {
                "status": "fail",
                "checks": [_check(
                    "ssh", "SSH connectivity + escalation", "fail", scrub(str(exc)),
                    "Verify the address, credential, sudo mode, and that the "
                    "KubeSight backend can reach the node on its SSH port.",
                )],
            }

    with ThreadPoolExecutor(max_workers=_PREFLIGHT_WORKERS) as pool:
        list(pool.map(_probe, jobs))

    cross = _uniqueness_checks(raw_facts, build)
    for node in subjects:
        if node.id in results:  # SSH failure already recorded
            continue
        facts = raw_facts.get(node.id, {})
        checks = _node_checks(build, node, facts, profile)
        checks.extend(cross.get(node.id, []))
        results[node.id] = {"status": _worst(checks), "checks": checks}
    return results


def merge_preflight(
    build: ClusterBuild,
    vsphere_results: Dict[int, List[Dict[str, Any]]],
    node_results: Dict[int, Dict[str, Any]],
    nodes: Optional[List[ClusterBuildNode]] = None,
    build_checks: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Combine both halves, stamp per-node results, and compute the verdict.

    Only the nodes under examination are stamped. Growing a cluster must not
    rewrite a joined machine's status back to 'preflight_passed'.

    ``build_checks`` are whole-build verdicts (the selected Kubernetes version
    and what it implies). They are attached to the first subject node so they
    are visible once in the per-node UI and count toward the overall status —
    an unsupported version has to block the build, not just annotate it.
    """
    overall = "pass"
    per_node = []
    pending_build_checks = list(build_checks or [])
    for node in (build.nodes if nodes is None else nodes):
        checks = list(pending_build_checks)
        pending_build_checks = []
        checks.extend(vsphere_results.get(node.id, []))
        node_result = node_results.get(node.id, {"status": "pass", "checks": []})
        checks.extend(node_result["checks"])
        status = _worst(checks)
        node.preflight_json = {"status": status, "checks": checks}
        node.status = "preflight_passed" if status != "fail" else "preflight_failed"
        per_node.append({
            "nodeId": node.id,
            "hostname": node.hostname,
            "address": node.address,
            "role": node.role,
            "status": status,
            "checks": checks,
        })
        if status == "fail":
            overall = "fail"
        elif status == "warn" and overall == "pass":
            overall = "warn"
    return {"status": overall, "nodes": per_node}
