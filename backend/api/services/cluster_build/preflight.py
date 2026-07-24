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
from typing import Any, Dict, List, Optional

from ...models import ClusterBuild, ClusterBuildNode
from ..ssh import SshTarget, get_transport
from . import os_adapters
from .os_adapters import OsFacts
from .profiles import ResolvedProfile
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

def vsphere_checks(build: ClusterBuild) -> Dict[int, List[Dict[str, Any]]]:
    """Per-node placement/sizing checks from the vSphere metadata captured when
    nodes were added. Nodes without vSphere metadata (manual entry) get none."""
    results: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    vm_nodes = [n for n in build.nodes if n.vsphere_vm_moid]

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
        tier = [n for n in vm_nodes if n.role == role and n.vsphere_host]
        if len(tier) < 2:
            continue
        by_host: Dict[str, List[ClusterBuildNode]] = defaultdict(list)
        for node in tier:
            by_host[node.vsphere_host].append(node)
        colocated = {host: nodes for host, nodes in by_host.items() if len(nodes) > 1}
        for node in tier:
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
    cp_nodes = [n for n in vm_nodes if n.role == "control_plane" and n.vsphere_datastore]
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
# Node-side probe (one escalated POSIX script; output parsed as KS_KEY=value)
# ---------------------------------------------------------------------------

def _probe_script(build: ClusterBuild, node: ClusterBuildNode, profile: ResolvedProfile) -> str:
    ports = " ".join(str(p) for p in _required_ports(build, node))
    repo_urls = []
    if profile.repo_mode != "offline":
        minor_placeholder = ".".join(build.k8s_version.lstrip("v").split(".")[:2])
        repo_urls.append(profile.k8s_pkg_repo("debian", minor_placeholder))
        if node.role != "loadbalancer":
            for host in profile.registry_hosts():
                repo_urls.append(f"https://{host}/v2/")
    probe_urls = " ".join(shlex.quote(u) for u in repo_urls)
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
    facts: Dict[str, Any] = {"repo_ok": [], "repo_fail": []}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("KS_") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key == "KS_REPO_OK":
            facts["repo_ok"].append(value)
        elif key == "KS_REPO_FAIL":
            facts["repo_fail"].append(value)
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
) -> Dict[int, Dict[str, Any]]:
    """SSH-probe every node in parallel. Returns {node_id: {status, checks}}."""
    from flask import current_app

    transport = get_transport()
    app = current_app._get_current_object()
    raw_facts: Dict[int, Dict[str, Any]] = {}
    results: Dict[int, Dict[str, Any]] = {}

    # Targets + scripts are built on THIS thread (they read the DB); worker
    # threads still get an app context of their own because the real transport's
    # host-key verification reads/writes ssh_host_keys.
    jobs = []
    for node in build.nodes:
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
    for node in build.nodes:
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
) -> Dict[str, Any]:
    """Combine both halves, stamp per-node results, and compute the verdict."""
    overall = "pass"
    per_node = []
    for node in build.nodes:
        checks = list(vsphere_results.get(node.id, []))
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
