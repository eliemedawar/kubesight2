"""OpenTofu plan / apply / destroy as restart-safe background jobs.

A job is driven by one worker thread at a time (``_active_jobs`` guards the
process, a heartbeat on ``updated_at`` guards the fleet). What each phase does:

  plan     vCenter checks → address probes → main.tf.json → init → plan → show
  apply    init → apply the saved plan (streamed, per-VM progress) → record VMs
  connect  wait until every new VM answers SSH, then hand the machines to the
           Cluster Builder phase machine (preflight, then build or grow)

Recovery (``advance_provision_jobs``, ticked by the scheduler): a job whose
worker died is picked up again. Planning and connecting simply rerun. An
interrupted apply is never "resumed" blind: its lock is released on its behalf,
a fresh plan is made against the state OpenTofu saved as it went, and that plan
is applied on its own only if it does nothing but finish what the interrupted,
already-approved job set out to do. Anything else waits for a person.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import current_app

from ....audit import log_audit
from ....db import db
from ....models import (
    ClusterBuild,
    ClusterBuildNode,
    ClusterProvisionJob,
    SshHostKey,
    User,
    VSphereConnection,
)
from ....secret_encryption import decrypt_secret, encrypt_secret
from ...vsphere_client import VSphereError
from ..scrub import scrub
from . import inventory, ip_pool, state_store, templates, tofu_config, tofu_runner

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("planning", "applying", "connecting")
OPEN_STATUSES = ACTIVE_STATUSES + ("planned", "awaiting_approval")
_STALE_SECONDS = 180
_HEARTBEAT_SECONDS = 30
_LOG_TAIL_CHARS = 20000
_PLAN_TEXT_CHARS = 120000
_PLAN_TIMEOUT_S = 15 * 60
_APPLY_TIMEOUT_S = 90 * 60
_SSH_TIMEOUT_S = 15 * 60
_FLUSH_SECONDS = 2.0

_active_lock = threading.Lock()
_active_jobs: set = set()


class JobFailed(Exception):
    """A job stopped for a reason a person can act on. The message says which."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# Reading jobs
# ---------------------------------------------------------------------------

def get_job(job_id: int, build_id: Optional[int] = None) -> ClusterProvisionJob:
    job = db.session.get(ClusterProvisionJob, job_id)
    if job is None or (build_id is not None and job.build_id != build_id):
        raise LookupError("Provisioning job not found.")
    return job


def jobs_for(build_id: int) -> List[ClusterProvisionJob]:
    return (
        ClusterProvisionJob.query.filter_by(build_id=build_id)
        .order_by(ClusterProvisionJob.id.desc())
        .all()
    )


def open_job(build_id: int) -> Optional[ClusterProvisionJob]:
    return (
        ClusterProvisionJob.query.filter(
            ClusterProvisionJob.build_id == build_id,
            ClusterProvisionJob.status.in_(OPEN_STATUSES),
        )
        .order_by(ClusterProvisionJob.id.desc())
        .first()
    )


def serialize_job(job: ClusterProvisionJob, *, include_plan: bool = False) -> Dict[str, Any]:
    data = {
        "id": job.id,
        "buildId": job.build_id,
        "operation": job.operation,
        "status": job.status,
        "summary": job.plan_summary_json or None,
        "progress": job.progress_json or None,
        "error": job.error,
        "reason": job.reason,
        "requestedBy": job.requested_by,
        "requestedByUserId": job.requested_by_user_id,
        "appliedBy": job.applied_by,
        "approvedBy": job.approved_by,
        "approvedAt": _iso(job.approved_at),
        "decisionNote": job.decision_note,
        "autoApply": bool(job.auto_apply),
        "recoveredFromJobId": job.recovered_from_job_id,
        "createdAt": _iso(job.created_at),
        "startedAt": _iso(job.started_at),
        "finishedAt": _iso(job.finished_at),
        "updatedAt": _iso(job.updated_at),
    }
    if include_plan:
        data["planText"] = job.plan_text or ""
        data["logTail"] = job.log_tail or ""
    return data


# ---------------------------------------------------------------------------
# What the build reads as while a job runs
# ---------------------------------------------------------------------------

_PROVISION_STATUS = {
    "create": {
        "planning": "planning", "planned": "planned", "plan_failed": "plan_failed",
        "applying": "applying", "connecting": "connecting", "apply_failed": "apply_failed",
        "connect_failed": "connect_failed", "succeeded": "ready", "interrupted": "applying",
    },
    "grow": {
        "planning": "grow_planning", "planned": "grow_planned", "plan_failed": "grow_plan_failed",
        "applying": "grow_applying", "connecting": "grow_connecting",
        "apply_failed": "grow_failed", "connect_failed": "grow_failed",
        "succeeded": "ready", "interrupted": "grow_applying",
    },
    "destroy": {
        "planning": "destroy_planning", "awaiting_approval": "destroy_pending",
        "plan_failed": "destroy_plan_failed", "applying": "destroying",
        "apply_failed": "destroy_failed", "succeeded": "destroyed",
        "interrupted": "destroying",
    },
}


def _sync_build_status(build: ClusterBuild, job: ClusterProvisionJob) -> None:
    status = _PROVISION_STATUS.get(job.operation, {}).get(job.status)
    if status is not None:
        build.provision_status = status
        return
    # discarded / rejected: back to whatever the VMs say.
    if state_store.has_resources(build.id):
        build.provision_status = "ready"
    else:
        build.provision_status = None


# ---------------------------------------------------------------------------
# Authentication for the HTTP state backend
# ---------------------------------------------------------------------------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_token(job: ClusterProvisionJob) -> str:
    token = secrets.token_urlsafe(32)
    job.auth_token_hash = _hash(token)
    db.session.commit()
    return token


def authenticate(username: str, token: str, build_id: int) -> Optional[ClusterProvisionJob]:
    """The running job a state-backend request belongs to, or None."""
    match = re.fullmatch(r"job-(\d+)", username or "")
    if not match or not token:
        return None
    job = db.session.get(ClusterProvisionJob, int(match.group(1)))
    if job is None or job.build_id != build_id or not job.auth_token_hash:
        return None
    if job.status not in ACTIVE_STATUSES:
        return None
    if not hmac.compare_digest(_hash(token), job.auth_token_hash):
        return None
    return job


# ---------------------------------------------------------------------------
# Plan summaries
# ---------------------------------------------------------------------------

def _first(items, default=None):
    return items[0] if isinstance(items, list) and items else default


def _vm_detail(attrs: Dict[str, Any]) -> str:
    if not attrs:
        return ""
    parts = []
    if attrs.get("num_cpus"):
        parts.append(f"{attrs['num_cpus']} vCPU")
    if attrs.get("memory"):
        parts.append(f"{int(attrs['memory']) // 1024} GB")
    disk = _first(attrs.get("disk"), {}) or {}
    if disk.get("size"):
        parts.append(f"{disk['size']} GB disk")
    customize = _first((_first(attrs.get("clone"), {}) or {}).get("customize"), {}) or {}
    nic = _first(customize.get("network_interface"), {}) or {}
    address = nic.get("ipv4_address") or attrs.get("default_ip_address")
    if address:
        parts.append(address)
    return " · ".join(parts)


_RULE_TITLES = {
    "control_planes": "Keep control planes on different ESXi hosts",
    "load_balancers": "Keep load balancers on different ESXi hosts",
}


def summarize_plan(document: Dict[str, Any], operation: str) -> Dict[str, Any]:
    resources = []
    counts = {"add": 0, "change": 0, "destroy": 0, "replace": 0}
    for change in document.get("resource_changes") or []:
        actions = (change.get("change") or {}).get("actions") or []
        if actions in (["no-op"], ["read"], []):
            continue
        if actions == ["create"]:
            action = "create"
            counts["add"] += 1
        elif actions == ["delete"]:
            action = "delete"
            counts["destroy"] += 1
        elif sorted(actions) == ["create", "delete"]:
            action = "replace"
            counts["replace"] += 1
            counts["add"] += 1
            counts["destroy"] += 1
        else:
            action = "update"
            counts["change"] += 1
        rtype = change.get("type")
        key = change.get("index")
        body = (change.get("change") or {})
        attrs = body.get("after") or body.get("before") or {}
        if rtype == "vsphere_virtual_machine":
            kind, title, detail = "vm", str(key), _vm_detail(attrs)
        elif rtype == "vsphere_folder":
            kind, title, detail = "folder", f"VM folder {attrs.get('path') or ''}".strip(), ""
        elif rtype == "vsphere_compute_cluster_vm_anti_affinity_rule":
            kind = "rule"
            title = _RULE_TITLES.get(change.get("name"), attrs.get("name") or "Placement rule")
            detail = attrs.get("name") or ""
        else:
            kind, title, detail = "other", change.get("address"), ""
        resources.append({
            "address": change.get("address"),
            "type": rtype,
            "kind": kind,
            "key": key,
            "action": action,
            "title": title,
            "detail": detail,
        })
    blocked = None
    existing_touched = [r for r in resources if r["action"] in ("update", "replace", "delete")]
    if operation == "grow" and existing_touched:
        names = ", ".join(r["title"] for r in existing_touched[:4])
        blocked = (
            "This plan would change machines that are already running "
            f"({names}). Adding workers must only add. Ask an administrator to "
            "check what changed in vCenter before going on."
        )
    elif operation == "create" and any(r["action"] == "delete" for r in resources):
        blocked = "This plan would delete VMs. Creating a cluster must only create."
    elif operation == "destroy" and any(r["action"] in ("create", "replace") for r in resources):
        blocked = "This destroy plan would create resources. Something is wrong; nothing was changed."
    return {**counts, "resources": resources, "blocked": blocked}


# ---------------------------------------------------------------------------
# Output handling
# ---------------------------------------------------------------------------

_VM_LINE = re.compile(
    r'^vsphere_virtual_machine\.node\["(?P<name>[^"]+)"\]: (?P<what>.*)$'
)
_ELAPSED = re.compile(r"\[(?:id=[^,\]]*, )?(?P<t>[0-9hms]+) elapsed\]")
_WITH_VM = re.compile(r'with vsphere_virtual_machine\.node\["(?P<name>[^"]+)"\]')


class _Output:
    """Collects a command's output, scrubbed, and keeps the job row current.

    Runs on the worker thread (the one that owns the job's session), so the
    periodic flush never contends with another session for the row.
    """

    def __init__(self, job: ClusterProvisionJob, secrets_to_hide: List[str], *, track_vms: bool = False):
        self.job = job
        self.hide = [s for s in secrets_to_hide if s]
        # One transcript per job: a later phase appends to what earlier ones said.
        self.base = job.log_tail or ""
        self.lines: List[str] = []
        self.track_vms = track_vms
        self._last_flush = 0.0
        self._last_error: Optional[str] = None
        self.errors: List[str] = []

    def _clean(self, line: str) -> str:
        for secret in self.hide:
            line = line.replace(secret, "[REDACTED]")
        return scrub(line)

    def on_line(self, line: str) -> None:
        line = self._clean(line)
        self.lines.append(line)
        if line.startswith("Error:") or line.startswith("│ Error:"):
            self._last_error = line.lstrip("│ ").strip()
            self.errors.append(self._last_error)
        if self.track_vms:
            self._track(line)
        now = time.monotonic()
        if now - self._last_flush >= _FLUSH_SECONDS:
            self._last_flush = now
            self.flush()

    def _track(self, line: str) -> None:
        progress = dict(self.job.progress_json or {})
        vms = dict(progress.get("vms") or {})
        match = _VM_LINE.match(line)
        changed = False
        if match:
            name, what = match.group("name"), match.group("what")
            entry = dict(vms.get(name) or {})
            elapsed = _ELAPSED.search(what)
            if what.startswith("Creating"):
                entry.update(state="creating")
            elif what.startswith("Still creating"):
                entry.update(state="creating", elapsed=elapsed.group("t") if elapsed else None)
            elif what.startswith("Creation complete"):
                entry.update(state="created", elapsed=what.split("after", 1)[-1].split("[")[0].strip())
            elif what.startswith("Destroying") or what.startswith("Still destroying"):
                entry.update(state="destroying", elapsed=elapsed.group("t") if elapsed else None)
            elif what.startswith("Destruction complete"):
                entry.update(state="destroyed")
            vms[name] = entry
            changed = True
        with_vm = _WITH_VM.search(line)
        if with_vm and self._last_error:
            name = with_vm.group("name")
            entry = dict(vms.get(name) or {})
            entry.update(state="failed", error=self._last_error[:500])
            vms[name] = entry
            changed = True
        if changed:
            progress["vms"] = vms
            self.job.progress_json = progress

    def text(self) -> str:
        return "\n".join(self.lines)

    def flush(self) -> None:
        try:
            joined = (self.base + "\n" if self.base else "") + self.text()
            self.job.log_tail = joined[-_LOG_TAIL_CHARS:]
            db.session.commit()
        except Exception:  # noqa: BLE001 — a failed flush must not kill the job
            db.session.rollback()

    def error_summary(self) -> str:
        if not self.errors:
            tail = [line for line in self.lines if line.strip()][-6:]
            return "\n".join(tail)[:1500]
        # The block following the first error is OpenTofu's own explanation.
        first = self.text().split(self.errors[0], 1)
        detail = first[1] if len(first) > 1 else ""
        detail_lines = [l.strip(" │") for l in detail.splitlines() if l.strip(" │")][:6]
        return "\n".join([self.errors[0], *detail_lines])[:1500]


# ---------------------------------------------------------------------------
# The vCenter account, the working directory, the environment
# ---------------------------------------------------------------------------

def _connection_for(build: ClusterBuild) -> VSphereConnection:
    spec = build.provisioning_json or {}
    connection_id = spec.get("vsphereConnectionId") or build.vsphere_connection_id
    row = db.session.get(VSphereConnection, int(connection_id)) if connection_id else None
    if row is None:
        raise JobFailed("The vCenter connection for this build no longer exists.")
    return row


def _provisioning_config(row: VSphereConnection):
    from ... import vsphere_service

    try:
        return vsphere_service.provisioning_config(row)
    except ValueError as exc:
        raise JobFailed(str(exc)) from exc


def _workdir(job: ClusterProvisionJob) -> str:
    base = os.getenv("KUBESIGHT_TOFU_WORKDIR") or os.path.join(current_app.instance_path, "tofu")
    path = os.path.join(base, f"build-{job.build_id}", f"job-{job.id}")
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def _cleanup(path: str) -> None:
    """Remove a job's working directory, and its build folder once empty."""
    shutil.rmtree(path, ignore_errors=True)
    try:
        os.rmdir(os.path.dirname(path))
    except OSError:
        pass  # another job of this build still works there, or it is gone


def _internal_base_url() -> str:
    return (os.getenv("KUBESIGHT_INTERNAL_URL") or "http://127.0.0.1:5000").rstrip("/")


def _ca_bundle(workdir: str, ca_pem: str) -> Optional[str]:
    if not ca_pem:
        return None
    parts = [ca_pem.strip()]
    for system in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
        if os.path.exists(system):
            with open(system, encoding="utf-8", errors="ignore") as handle:
                parts.append(handle.read())
            break
    path = os.path.join(workdir, "ca-bundle.pem")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(parts) + "\n")
    return path


def _environment(job: ClusterProvisionJob, token: str, cfg, workdir: str) -> Dict[str, str]:
    from urllib.parse import urlsplit

    base = f"{_internal_base_url()}/api/internal/tofu-state/{job.build_id}"
    parts = urlsplit(cfg.root)
    server = parts.hostname or ""
    if parts.port and parts.port != 443:
        server = f"{server}:{parts.port}"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": workdir,
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "CHECKPOINT_DISABLE": "1",
        "TF_HTTP_ADDRESS": base,
        "TF_HTTP_LOCK_ADDRESS": f"{base}/lock",
        "TF_HTTP_UNLOCK_ADDRESS": f"{base}/unlock",
        "TF_HTTP_LOCK_METHOD": "POST",
        "TF_HTTP_UNLOCK_METHOD": "POST",
        "TF_HTTP_USERNAME": f"job-{job.id}",
        "TF_HTTP_PASSWORD": token,
        "TF_HTTP_RETRY_MAX": "5",
        "VSPHERE_SERVER": server,
        "VSPHERE_USER": cfg.username,
        "VSPHERE_PASSWORD": cfg.password,
        "VSPHERE_ALLOW_UNVERIFIED_SSL": "true" if cfg.skip_tls_verify else "false",
        "KUBESIGHT_BUILD_ID": str(job.build_id),
        "KUBESIGHT_JOB_ID": str(job.id),
    }
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    env["NO_PROXY"] = ",".join(filter(None, ["127.0.0.1", "localhost", no_proxy]))
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    for key in ("KUBESIGHT_SIMULATE_FAIL_VM", "SYSTEMROOT", "TEMP", "TMP"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if tofu_runner.provider_mirror_present():
        cli_path = os.path.join(workdir, "kubesight.tofurc")
        with open(cli_path, "w", encoding="utf-8") as handle:
            handle.write(tofu_config.cli_config(tofu_runner.provider_mirror_path()))
        env["TF_CLI_CONFIG_FILE"] = cli_path
    bundle = _ca_bundle(workdir, cfg.ca_pem)
    if bundle:
        env["SSL_CERT_FILE"] = bundle
    return env


def _run_tofu(engine, workdir, args, env, output: _Output, timeout_s: int) -> int:
    output.on_line(f"$ tofu {' '.join(args)}")
    try:
        code = engine.run(workdir, args, env, output.on_line, timeout_s)
    except (tofu_runner.TofuUnavailable, tofu_runner.TofuTimeout) as exc:
        output.on_line(str(exc))
        output.flush()
        raise JobFailed(str(exc)) from exc
    output.flush()
    return code


def _capture(engine, workdir, args, env, timeout_s: int) -> Tuple[int, str]:
    lines: List[str] = []
    try:
        code = engine.run(workdir, args, env, lines.append, timeout_s)
    except (tofu_runner.TofuUnavailable, tofu_runner.TofuTimeout) as exc:
        raise JobFailed(str(exc)) from exc
    return code, "\n".join(lines)


# ---------------------------------------------------------------------------
# What the configuration describes
# ---------------------------------------------------------------------------

def _range_dict(range_row) -> Dict[str, Any]:
    return ip_pool.serialize_range(range_row)


def _render_config(build: ClusterBuild, job: ClusterProvisionJob, cfg) -> Dict[str, Any]:
    spec = build.provisioning_json or {}
    machines = list(spec.get("machines") or [])
    if job.operation == "grow":
        machines += list((job.config_json or {}).get("newMachines") or [])
    range_row = ip_pool.get_range(int(spec["networkRangeId"]))
    nodes = [
        {
            "name": m["name"], "role": m["role"], "ip": m["ip"],
            "cpu": m["cpu"], "memoryGb": m["memoryGb"], "diskGb": m["diskGb"],
        }
        for m in machines
    ]
    config = tofu_config.render(
        cluster_name=build.name,
        build_id=build.id,
        spec=spec,
        nodes=nodes,
        network_range=_range_dict(range_row),
        allow_unverified_ssl=cfg.skip_tls_verify,
    )
    # Per-machine datastore: machines already created keep theirs, so a re-plan
    # after "pick another datastore" only moves the ones that do not exist yet.
    for_each = (config["resource"].get("vsphere_virtual_machine") or {}).get("node", {}).get("for_each")
    if for_each:
        for machine in machines:
            entry = for_each.get(machine["name"])
            if entry is not None:
                entry["datastore_id"] = machine.get("datastoreId") or spec["datastoreId"]
        config["resource"]["vsphere_virtual_machine"]["node"]["datastore_id"] = "${each.value.datastore_id}"
    return config


def _write_config(workdir: str, config: Dict[str, Any]) -> None:
    with open(os.path.join(workdir, "main.tf.json"), "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def _check(status: str, label: str, detail: str) -> Dict[str, str]:
    return {"status": status, "label": label, "detail": detail}


def _vcenter_checks(build: ClusterBuild, job: ClusterProvisionJob, connection, cfg) -> List[Dict[str, str]]:
    """What vCenter says about the plan's inputs, before OpenTofu runs at all."""
    spec = build.provisioning_json or {}
    checks: List[Dict[str, str]] = []
    try:
        placement = inventory.placement(cfg, cache_key=f"prov:{connection.id}", force_refresh=True)
    except VSphereError as exc:
        raise JobFailed(f"vCenter could not be read with the provisioning account: {exc}") from exc
    datacenter = inventory.find_datacenter(placement, spec.get("datacenterId"))
    if datacenter is None:
        raise JobFailed(f"Datacenter {spec.get('datacenterName')} is no longer in vCenter.")
    cluster = next((c for c in datacenter["clusters"] if c["id"] == spec.get("clusterId")), None)
    if cluster is None:
        raise JobFailed(f"Cluster {spec.get('clusterName')} is no longer in vCenter.")
    datastore = next((d for d in datacenter["datastores"] if d["id"] == spec.get("datastoreId")), None)
    if datastore is None:
        raise JobFailed(f"Datastore {spec.get('datastoreName')} is no longer in vCenter.")
    if not any(n["id"] == spec.get("networkId") for n in datacenter["networks"]):
        raise JobFailed(f"Network {spec.get('networkName')} is no longer in vCenter.")

    if job.operation == "destroy":
        checks.append(_check("ok", "vCenter", f"{datacenter['name']} reachable with the provisioning account"))
        return checks

    template = next(
        (t for t in datacenter["templates"] if t.get("uuid") == (spec.get("template") or {}).get("uuid")),
        None,
    )
    if template is None:
        raise JobFailed(
            f"VM template {(spec.get('template') or {}).get('name')} is no longer in vCenter. "
            "Pick a template again."
        )
    verdict = template.get("compatibility") or inventory.assess_template(template)
    if verdict["status"] == "bad":
        bad = next(c for c in verdict["checks"] if c["status"] == "bad")
        raise JobFailed(f"{template['name']} cannot be used: {bad['label']} — {bad['detail']}.")
    checks.append(_check(
        "warn" if verdict["status"] == "warn" else "ok", "VM template",
        f"{template['name']} — {'usable with warnings' if verdict['status'] == 'warn' else 'compatible'}",
    ))

    machines = _machines_in_scope(build, job)
    smallest = (template.get("disks") or [{}])[0].get("sizeGb") or 0
    too_small = [m["name"] for m in machines if int(m["diskGb"]) < int(smallest)]
    if too_small:
        checks.append(_check(
            "warn", "Disk size",
            f"{', '.join(too_small[:3])} asked for less than the template's {smallest} GB; "
            f"they get {smallest} GB (a clone cannot shrink a disk).",
        ))

    existing = set(state_store.vm_instances(build.id))
    taken = set(datacenter.get("vmNames") or []) - existing
    clashes = [m["name"] for m in machines if m["name"] in taken]
    if clashes:
        raise JobFailed(
            f"vCenter already has VMs called {', '.join(clashes[:5])}. Rename the "
            "cluster, or remove those VMs first."
        )
    checks.append(_check("ok", "VM names", f"none of the {len(machines)} names exist in vCenter yet"))

    new_disk = sum(max(int(m["diskGb"]), int(smallest)) for m in machines if m["name"] not in existing)
    if new_disk > (datastore.get("freeGb") or 0):
        checks.append(_check(
            "warn", "Datastore room",
            f"{datastore['name']} has {datastore['freeGb']} GB free; thin disks for these VMs "
            f"can grow to {new_disk} GB.",
        ))
    else:
        checks.append(_check("ok", "Datastore room", f"{datastore['name']} has {datastore['freeGb']} GB free"))

    counts = (spec.get("counts") or {})
    if (counts.get("controlPlane", 0) > 1 or counts.get("loadbalancer", 0) > 1):
        if cluster["hostCount"] < 2:
            checks.append(_check("warn", "Keep-apart rules",
                                 f"{cluster['name']} has one ESXi host; the HA tiers share it."))
        elif not cluster["drsEnabled"]:
            checks.append(_check("warn", "Keep-apart rules",
                                 f"DRS is off on {cluster['name']}; the rules are created but "
                                 "vCenter will not move VMs to honour them."))
        else:
            checks.append(_check("ok", "Keep-apart rules",
                                 f"{cluster['hostCount']} ESXi hosts with DRS on"))

    try:
        privileges = inventory.check_privileges(cfg, {
            "datacenter": datacenter["id"],
            "folder": spec.get("folderParentId") or None,
            "template": template["id"],
            "pool": spec.get("resourcePoolId"),
            "datastore": spec.get("datastoreId"),
            "network": spec.get("networkId"),
            "cluster": spec.get("clusterId"),
        })
    except Exception as exc:  # noqa: BLE001 — some vCenters refuse the query itself
        checks.append(_check("warn", "Account privileges", f"could not be checked: {scrub(str(exc))[:200]}"))
    else:
        missing = [p["privilege"] for p in privileges if not p["granted"]]
        if missing:
            raise JobFailed(
                "The provisioning account is missing vCenter privileges: "
                f"{', '.join(missing[:8])}{' …' if len(missing) > 8 else ''}."
            )
        checks.append(_check("ok", "Account privileges", f"all {len(privileges)} granted"))
    return checks


def _machines_in_scope(build: ClusterBuild, job: ClusterProvisionJob) -> List[Dict[str, Any]]:
    spec = build.provisioning_json or {}
    if job.operation == "grow":
        return list((job.config_json or {}).get("newMachines") or [])
    return list(spec.get("machines") or [])


def _probe_addresses(build: ClusterBuild, job: ClusterProvisionJob) -> List[Dict[str, str]]:
    """Make sure nothing on the network already answers on a reserved address.

    Only addresses whose VM does not exist yet are probed — a VM this build
    already created answers, and should.
    """
    spec = build.provisioning_json or {}
    existing = set(state_store.vm_instances(build.id))
    machines = [m for m in _machines_in_scope(build, job) if m["name"] not in existing]
    wanted: Dict[str, str] = {m["name"]: m["ip"] for m in machines}
    if job.operation == "create" and (spec.get("counts") or {}).get("loadbalancer"):
        vip_row = next((r for r in ip_pool.reservations_for(build.id) if r.node_name == "vip"), None)
        if vip_row is not None and not build.result_cluster_id:
            wanted["vip"] = vip_row.address
    if not wanted:
        return [_check("ok", "Addresses", "every machine already exists")]
    answering = ip_pool.probe_in_use(wanted.values())
    if not answering:
        return [_check("ok", "Addresses free", f"{len(wanted)} reserved, none answered on the network")]

    range_row = ip_pool.get_range(int(spec["networkRangeId"]))
    keys = [key for key, address in wanted.items() if address in answering]
    replaced = ip_pool.reserve(
        range_row, build.id,
        [{"key": key, "purpose": "vip" if key == "vip" else "node"} for key in keys],
        skip=answering,
    )
    # Second look: the replacements must be quiet too.
    still = ip_pool.probe_in_use(replaced.values())
    if still:
        raise JobFailed(
            f"Addresses in {range_row.network_name} keep answering on the network "
            f"({', '.join(sorted(answering | still)[:6])}). Check the range in Sources."
        )
    _apply_new_addresses(build, job, replaced)
    return [_check(
        "warn", "Addresses",
        f"{', '.join(sorted(answering))} answered on the network and were skipped; "
        f"used {', '.join(replaced[k] for k in keys)} instead",
    )]


def _apply_new_addresses(build: ClusterBuild, job: ClusterProvisionJob, addresses: Dict[str, str]) -> None:
    spec = dict(build.provisioning_json or {})
    if job.operation == "grow":
        config = dict(job.config_json or {})
        config["newMachines"] = [
            {**m, "ip": addresses.get(m["name"], m["ip"])} for m in config.get("newMachines") or []
        ]
        job.config_json = config
    else:
        spec["machines"] = [
            {**m, "ip": addresses.get(m["name"], m["ip"])} for m in spec.get("machines") or []
        ]
        build.provisioning_json = spec
        for node in build.nodes:
            if node.vsphere_vm_name in addresses:
                node.address = addresses[node.vsphere_vm_name]
        if "vip" in addresses:
            build.vip_address = addresses["vip"]
            build.control_plane_endpoint = f"{addresses['vip']}:6443"
        elif build.endpoint_mode == "manual_endpoint":
            primary = next((n for n in build.nodes if n.role == "control_plane"), None)
            if primary is not None:
                build.control_plane_endpoint = f"{primary.address}:6443"
    db.session.commit()


def _do_plan(job: ClusterProvisionJob, engine) -> None:
    build = db.session.get(ClusterBuild, job.build_id)
    connection = _connection_for(build)
    cfg = _provisioning_config(connection)
    progress = dict(job.progress_json or {})
    progress["phase"] = "checking"
    job.progress_json = progress
    db.session.commit()

    checks = _vcenter_checks(build, job, connection, cfg)
    if job.operation != "destroy":
        checks += _probe_addresses(build, job)

    config = _render_config(build, job, cfg)
    job.config_json = {**(job.config_json or {}), "tofu": config}
    progress["phase"] = "planning"
    job.progress_json = progress
    db.session.commit()

    workdir = _workdir(job)
    try:
        token = _issue_token(job)
        env = _environment(job, token, cfg, workdir)
        _write_config(workdir, config)
        output = _Output(job, [cfg.password, token])
        if _run_tofu(engine, workdir, ["init", "-input=false", "-no-color"], env, output, _PLAN_TIMEOUT_S):
            raise JobFailed("OpenTofu could not initialise.\n" + output.error_summary())
        plan_args = ["plan", "-input=false", "-no-color", "-lock-timeout=60s", "-out=job.tfplan"]
        if job.operation == "destroy":
            plan_args.insert(1, "-destroy")
        if _run_tofu(engine, workdir, plan_args, env, output, _PLAN_TIMEOUT_S):
            raise JobFailed("OpenTofu could not make a plan.\n" + output.error_summary())

        code, shown = _capture(engine, workdir, ["show", "-json", "job.tfplan"], env, _PLAN_TIMEOUT_S)
        if code:
            raise JobFailed("OpenTofu could not read back its own plan.")
        try:
            document = json.loads(shown.strip().splitlines()[-1] if shown.strip() else "{}")
        except ValueError as exc:
            raise JobFailed("OpenTofu's plan could not be read.") from exc
        _, text = _capture(engine, workdir, ["show", "-no-color", "job.tfplan"], env, _PLAN_TIMEOUT_S)
        with open(os.path.join(workdir, "job.tfplan"), "rb") as handle:
            plan_bytes = handle.read()
    finally:
        _cleanup(workdir)

    summary = summarize_plan(document, job.operation)
    summary["checks"] = checks
    job.plan_summary_json = summary
    hide = [cfg.password]
    plain = text
    for secret in hide:
        plain = plain.replace(secret, "[REDACTED]")
    job.plan_text = scrub(plain)[:_PLAN_TEXT_CHARS]
    job.plan_cipher = encrypt_secret(base64.b64encode(plan_bytes).decode("ascii"))
    job.status = "awaiting_approval" if job.operation == "destroy" else "planned"
    progress["phase"] = "planned"
    job.progress_json = progress
    _sync_build_status(build, job)
    db.session.commit()


def _recovery_may_apply(job: ClusterProvisionJob) -> bool:
    """A recovery plan applies itself only when it finishes the approved job."""
    if not job.auto_apply or not job.recovered_from_job_id:
        return False
    summary = job.plan_summary_json or {}
    if summary.get("blocked"):
        return False
    original = db.session.get(ClusterProvisionJob, job.recovered_from_job_id)
    if original is None:
        return False
    approved = {
        r["address"] for r in ((original.plan_summary_json or {}).get("resources") or [])
    }
    for item in summary.get("resources") or []:
        if job.operation == "destroy":
            if item["action"] != "delete":
                return False
        else:
            if item["action"] not in ("create", "replace") or item["address"] not in approved:
                return False
    return True


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _do_apply(job: ClusterProvisionJob, engine) -> bool:
    """Returns True when OpenTofu applied the plan."""
    build = db.session.get(ClusterBuild, job.build_id)
    connection = _connection_for(build)
    cfg = _provisioning_config(connection)
    config = (job.config_json or {}).get("tofu")
    if not config or not job.plan_cipher:
        raise JobFailed("This job has no saved plan to apply. Make a new plan.")
    progress = dict(job.progress_json or {})
    progress["phase"] = "applying"
    progress["applyStartedAt"] = _iso(_utcnow())
    vm_names = tofu_config.planned_vm_names(config)
    planned = {
        r["key"]: r["action"]
        for r in (job.plan_summary_json or {}).get("resources") or []
        if r.get("kind") == "vm"
    }
    progress["vms"] = {
        name: {"state": ("waiting" if planned.get(name) in ("create", "replace", "delete") else "exists")}
        for name in (vm_names if job.operation != "destroy" else list(planned))
    }
    job.progress_json = progress
    db.session.commit()

    workdir = _workdir(job)
    output = _Output(job, [cfg.password], track_vms=True)
    try:
        token = _issue_token(job)
        output.hide.append(token)
        env = _environment(job, token, cfg, workdir)
        _write_config(workdir, config)
        with open(os.path.join(workdir, "job.tfplan"), "wb") as handle:
            handle.write(base64.b64decode(decrypt_secret(job.plan_cipher)))
        if _run_tofu(engine, workdir, ["init", "-input=false", "-no-color"], env, output, _PLAN_TIMEOUT_S):
            raise JobFailed("OpenTofu could not initialise.\n" + output.error_summary())
        code = _run_tofu(
            engine, workdir,
            ["apply", "-input=false", "-no-color", "-lock-timeout=60s", "job.tfplan"],
            env, output, _APPLY_TIMEOUT_S,
        )
    finally:
        _cleanup(workdir)
    db.session.expire_all()
    job = db.session.get(ClusterProvisionJob, job.id)
    if code:
        summary = output.error_summary()
        if "Saved plan is stale" in summary:
            summary = (
                "The plan is out of date: the VMs' state changed after it was made. "
                "Make a new plan.\n" + summary
            )
        raise JobFailed(summary or "OpenTofu apply failed.")
    return True


def _record_created_vms(build: ClusterBuild, job: ClusterProvisionJob) -> List[ClusterBuildNode]:
    """Fill the build's nodes in from what OpenTofu recorded; returns the new ones."""
    from ... import vsphere_service

    instances = state_store.vm_instances(build.id)
    spec = dict(build.provisioning_json or {})
    if job.operation == "grow":
        new_machines = list((job.config_json or {}).get("newMachines") or [])
        spec["machines"] = list(spec.get("machines") or []) + new_machines
        build.provisioning_json = spec
        next_position = max((n.position for n in build.nodes), default=-1) + 1
        for offset, machine in enumerate(new_machines):
            db.session.add(ClusterBuildNode(
                build_id=build.id, role="worker", position=next_position + offset,
                hostname=machine["name"], address=machine["ip"],
                address_source="provisioned", vsphere_vm_name=machine["name"],
                status="pending",
            ))
        db.session.commit()
        db.session.refresh(build)
        names = {m["name"] for m in new_machines}
    else:
        names = {m["name"] for m in spec.get("machines") or []}

    hosts: Dict[str, Dict[str, Any]] = {}
    try:
        inventory_items = vsphere_service.get_inventory(_connection_for(build).id, force_refresh=True)
        hosts = {item.get("moid"): item for item in inventory_items}
    except Exception:  # noqa: BLE001 — placement names are a nicety; preflight re-reads
        hosts = {}
    machines = {m["name"]: m for m in spec.get("machines") or []}
    touched: List[ClusterBuildNode] = []
    for node in build.nodes:
        name = node.vsphere_vm_name or node.hostname
        if name not in names:
            continue
        attrs = instances.get(name) or {}
        machine = machines.get(name) or {}
        node.vsphere_vm_moid = attrs.get("moid") or node.vsphere_vm_moid
        seen = hosts.get(node.vsphere_vm_moid) or {}
        node.vsphere_host = seen.get("esxiHost") or attrs.get("host_system_id") or node.vsphere_host
        node.vsphere_datastore = seen.get("datastore") or spec.get("datastoreName")
        node.vsphere_power_state = "POWERED_ON"
        node.vsphere_tools_status = "RUNNING"
        node.vsphere_cpu = machine.get("cpu") or node.vsphere_cpu
        node.vsphere_memory_mb = (machine.get("memoryGb") or 0) * 1024 or node.vsphere_memory_mb
        touched.append(node)
    ip_pool.mark_in_use(build.id, names | {"vip"})
    db.session.commit()
    return touched


# ---------------------------------------------------------------------------
# Connect, then hand over to the Cluster Builder
# ---------------------------------------------------------------------------

def _default_ssh_waiter(targets: List[Tuple[str, Any]], timeout_s: int,
                        on_ready: Callable[[str, bool, str], None]) -> None:
    from ...ssh import get_transport

    transport = get_transport()
    deadline = time.monotonic() + timeout_s

    def _wait(item: Tuple[str, Any]) -> Tuple[str, bool, str]:
        name, target = item
        last = ""
        while time.monotonic() < deadline:
            try:
                transport.run(target, "true", timeout_s=30)
                return name, True, ""
            except Exception as exc:  # noqa: BLE001 — retried until the deadline
                last = str(exc)
                time.sleep(10)
        return name, False, last

    with ThreadPoolExecutor(max_workers=min(len(targets), 8) or 1) as pool:
        for name, ok, detail in pool.map(_wait, targets):
            on_ready(name, ok, detail)


def _simulated_ssh_waiter(targets, timeout_s, on_ready) -> None:
    for name, _target in targets:
        time.sleep(float(os.getenv("KUBESIGHT_PROVISIONING_SIMULATE_DELAY", "4")) / 4)
        on_ready(name, True, "")


_ssh_waiter: Optional[Callable] = None


def set_ssh_waiter(waiter) -> None:
    """Test seam."""
    global _ssh_waiter
    _ssh_waiter = waiter


def _do_connect(job: ClusterProvisionJob) -> None:
    from .. import executor

    build = db.session.get(ClusterBuild, job.build_id)
    if job.operation == "grow":
        names = {m["name"] for m in (job.config_json or {}).get("newMachines") or []}
    else:
        names = {m["name"] for m in (build.provisioning_json or {}).get("machines") or []}
    nodes = [n for n in build.nodes if (n.vsphere_vm_name or n.hostname) in names]

    # These machines did not exist an hour ago. Any host key KubeSight trusted
    # on first use for one of their addresses belonged to an earlier machine,
    # and would make the first SSH connection fail as a key mismatch.
    addresses = [n.address for n in nodes]
    if addresses:
        SshHostKey.query.filter(
            SshHostKey.host.in_(addresses), SshHostKey.source == "tofu"
        ).delete(synchronize_session=False)
        db.session.commit()

    progress = dict(job.progress_json or {})
    progress["phase"] = "connecting"
    vms = dict(progress.get("vms") or {})
    for node in nodes:
        vms[node.vsphere_vm_name or node.hostname] = {
            **(vms.get(node.vsphere_vm_name or node.hostname) or {}), "state": "connecting",
        }
    progress["vms"] = vms
    job.progress_json = progress
    db.session.commit()

    try:
        targets = [(n.vsphere_vm_name or n.hostname, executor._target_for(build, n)) for n in nodes]
    except Exception as exc:  # noqa: BLE001 — usually: no SSH route on the build
        raise JobFailed(f"KubeSight cannot reach the new VMs: {scrub(str(exc))}") from exc

    results: Dict[str, Tuple[bool, str]] = {}
    waiter = _ssh_waiter or (_simulated_ssh_waiter if inventory.simulation_enabled() else _default_ssh_waiter)
    waiter(targets, _SSH_TIMEOUT_S, lambda name, ok, detail: results.__setitem__(name, (ok, detail)))

    vms = dict((job.progress_json or {}).get("vms") or {})
    failed = []
    for name, (ok, detail) in results.items():
        entry = dict(vms.get(name) or {})
        if ok:
            entry.update(state="ready")
        else:
            entry.update(state="unreachable", error=scrub(detail)[:300])
            failed.append(name)
        vms[name] = entry
    job.progress_json = {**(job.progress_json or {}), "vms": vms}
    db.session.commit()
    if failed:
        raise JobFailed(
            f"{', '.join(failed)} did not answer SSH within {_SSH_TIMEOUT_S // 60} minutes. "
            "The VMs exist; check that the template accepts the build's SSH credential "
            "and that guest customization set the address. Retry when fixed."
        )


def _handoff(job: ClusterProvisionJob) -> None:
    """Machines are up: run the Cluster Builder on them, as a person would."""
    from .. import service as build_service

    build = db.session.get(ClusterBuild, job.build_id)
    user = db.session.get(User, job.applied_by_user_id) if job.applied_by_user_id else None
    actor = job.applied_by or ""

    if job.operation == "grow":
        build.status = "completed"
        db.session.commit()
        try:
            result = build_service.preflight_growth(build.id)
        except Exception as exc:  # noqa: BLE001 — the panel shows the machines; a person retries
            _note(job, f"Preflight of the new workers could not run: {scrub(str(exc))}")
            return
        if result.get("status") == "pass":
            try:
                build_service.grow_build(build.id, actor=actor)
            except Exception as exc:  # noqa: BLE001
                _note(job, f"Joining the new workers did not start: {scrub(str(exc))}")
        else:
            _note(job, "Preflight of the new workers needs a look before they join "
                       f"({result.get('status')}). Open Add workers to review it.")
        return

    build.status = "draft"
    db.session.commit()
    try:
        result = build_service.run_preflight(build.id, user=user)
    except Exception as exc:  # noqa: BLE001
        _note(job, f"Preflight could not run: {scrub(str(exc))}")
        return
    if result.get("status") == "pass":
        try:
            build_service.start_build(build.id, actor=actor, user=user)
        except Exception as exc:  # noqa: BLE001
            _note(job, f"The Kubernetes build did not start: {scrub(str(exc))}")
    else:
        _note(job, "Preflight found something to look at before Kubernetes is installed "
                   f"({result.get('status')}). Review it on this page, then start the build.")


def _note(job: ClusterProvisionJob, message: str) -> None:
    db.session.rollback()
    job = db.session.get(ClusterProvisionJob, job.id)
    progress = dict(job.progress_json or {})
    progress["handoffNote"] = message[:1000]
    job.progress_json = progress
    db.session.commit()


# ---------------------------------------------------------------------------
# Destroy
# ---------------------------------------------------------------------------

def _retire_cluster(public_id: str, job: ClusterProvisionJob) -> None:
    from ....cluster_store import delete_kubeconfig_file, get_active_cluster_by_public_id
    from ....k8s_provider import invalidate_cluster_list_cache

    cluster = get_active_cluster_by_public_id(public_id)
    if cluster is None:
        return
    try:
        delete_kubeconfig_file(cluster.id)
    except Exception:  # noqa: BLE001 — the row is retired either way
        logger.warning("Could not delete the kubeconfig of %s", public_id, exc_info=True)
    cluster.kubeconfig_path = None
    cluster.is_active = False
    cluster.last_connection_status = None
    cluster.last_connection_error = None
    cluster.updated_at = _utcnow()
    db.session.commit()
    invalidate_cluster_list_cache()


def _finish_destroy(job: ClusterProvisionJob) -> None:
    build = db.session.get(ClusterBuild, job.build_id)
    if state_store.has_resources(build.id):
        raise JobFailed("OpenTofu finished, but its state still lists resources. Make a new destroy plan.")
    addresses = [n.address for n in build.nodes]
    for node in build.nodes:
        node.status = "removed"
    if build.result_cluster_id:
        _retire_cluster(build.result_cluster_id, job)
    ip_pool.release(build.id)
    if addresses:
        SshHostKey.query.filter(
            SshHostKey.host.in_(addresses), SshHostKey.source == "tofu"
        ).delete(synchronize_session=False)
    build.status = "destroyed"
    build.error = None
    build.finished_at = build.finished_at or _utcnow()
    db.session.commit()
    log_audit(
        "cluster_build_destroyed",
        actor_user_id=job.approved_by_user_id,
        target_type="cluster_build",
        target_id=str(build.id),
        details={
            "name": build.name, "clusterId": build.result_cluster_id,
            "requestedBy": job.requested_by, "approvedBy": job.approved_by,
            "jobId": job.id,
        },
    )


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

def _heartbeat_loop(app, job_id: int, stop: threading.Event) -> None:
    while not stop.wait(_HEARTBEAT_SECONDS):
        try:
            with app.app_context():
                try:
                    ClusterProvisionJob.query.filter(
                        ClusterProvisionJob.id == job_id,
                        ClusterProvisionJob.status.in_(ACTIVE_STATUSES),
                    ).update({"updated_at": _utcnow()}, synchronize_session=False)
                    db.session.commit()
                finally:
                    db.session.remove()
        except Exception:  # noqa: BLE001 — retried next beat
            logger.debug("Provision job %s heartbeat failed", job_id, exc_info=True)


def _fail(job_id: int, status: str, message: str) -> None:
    db.session.rollback()
    job = db.session.get(ClusterProvisionJob, job_id)
    if job is None:
        return
    build = db.session.get(ClusterBuild, job.build_id)
    job.status = status
    job.error = scrub(message)[:4000]
    job.finished_at = _utcnow()
    progress = dict(job.progress_json or {})
    progress["phase"] = status
    job.progress_json = progress
    _sync_build_status(build, job)
    if job.operation == "create" and status in ("apply_failed", "connect_failed"):
        build.status = "provision_failed"
        build.error = job.error
    elif job.operation in ("grow", "destroy") and status in ("apply_failed", "connect_failed"):
        build.status = job.prior_build_status or build.status
    db.session.commit()
    state_store.release_job_lock(job_id)


def _run(app, job_id: int, phase: str) -> None:
    with app.app_context():
        stop = None
        try:
            job = db.session.get(ClusterProvisionJob, job_id)
            if job is None:
                return
            if not app.config.get("TESTING"):
                stop = threading.Event()
                threading.Thread(
                    target=_heartbeat_loop, args=(app, job_id, stop),
                    name=f"provision-job-{job_id}-heartbeat", daemon=True,
                ).start()
            engine = tofu_runner.engine()
            job.started_at = job.started_at or _utcnow()
            db.session.commit()

            if phase == "plan":
                try:
                    _do_plan(job, engine)
                except JobFailed as exc:
                    _fail(job_id, "plan_failed", str(exc))
                    return
                job = db.session.get(ClusterProvisionJob, job_id)
                if not _recovery_may_apply(job):
                    if job.recovered_from_job_id:
                        _note(job, "KubeSight restarted during the previous apply. This plan "
                                   "goes further than finishing it, so it waits for you.")
                    return
                phase = "apply"
                job.status = "applying"
                build = db.session.get(ClusterBuild, job.build_id)
                _sync_build_status(build, job)
                db.session.commit()

            if phase == "apply":
                try:
                    _do_apply(job, engine)
                except JobFailed as exc:
                    _fail(job_id, "apply_failed", str(exc))
                    return
                job = db.session.get(ClusterProvisionJob, job_id)
                build = db.session.get(ClusterBuild, job.build_id)
                if job.operation == "destroy":
                    try:
                        _finish_destroy(job)
                    except JobFailed as exc:
                        _fail(job_id, "apply_failed", str(exc))
                        return
                    job.status = "succeeded"
                    job.finished_at = _utcnow()
                    _sync_build_status(build, job)
                    db.session.commit()
                    return
                _record_created_vms(build, job)
                job.status = "connecting"
                _sync_build_status(build, job)
                db.session.commit()
                phase = "connect"

            if phase == "connect":
                try:
                    _do_connect(job)
                except JobFailed as exc:
                    _fail(job_id, "connect_failed", str(exc))
                    return
                job = db.session.get(ClusterProvisionJob, job_id)
                build = db.session.get(ClusterBuild, job.build_id)
                job.status = "succeeded"
                job.finished_at = _utcnow()
                progress = dict(job.progress_json or {})
                progress["phase"] = "handoff"
                job.progress_json = progress
                _sync_build_status(build, job)
                build.error = None
                db.session.commit()
                _handoff(job)
        except Exception as exc:  # noqa: BLE001 — never lose a job to a crash
            logger.exception("Provision job %s crashed", job_id)
            try:
                status = {"plan": "plan_failed", "apply": "apply_failed"}.get(phase, "connect_failed")
                _fail(job_id, status, f"Internal error: {exc}")
            except Exception:  # noqa: BLE001
                logger.exception("Could not record the failure of provision job %s", job_id)
        finally:
            if stop is not None:
                stop.set()
            with _active_lock:
                _active_jobs.discard(job_id)
            db.session.remove()


def start_worker(job_id: int, phase: str) -> None:
    """Drive a job from ``phase`` on. Synchronous under TESTING."""
    app = current_app._get_current_object()
    with _active_lock:
        if job_id in _active_jobs:
            return
        _active_jobs.add(job_id)
    if app.config.get("TESTING"):
        _run(app, job_id, phase)
        return
    threading.Thread(
        target=_run, args=(app, job_id, phase),
        name=f"provision-job-{job_id}", daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def advance_provision_jobs() -> None:
    """Scheduler tick: take over jobs whose worker died with the process."""
    cutoff = _utcnow() - timedelta(seconds=_STALE_SECONDS)
    stale = ClusterProvisionJob.query.filter(
        ClusterProvisionJob.status.in_(ACTIVE_STATUSES)
    ).all()
    for job in stale:
        with _active_lock:
            if job.id in _active_jobs:
                continue
        updated = _as_utc(job.updated_at)
        if updated is not None and updated > cutoff:
            continue
        released = state_store.release_job_lock(job.id)
        logger.warning(
            "Recovering provision job %s (%s, %s)%s", job.id, job.operation, job.status,
            " and releasing its state lock" if released else "",
        )
        if job.status == "planning":
            job.updated_at = _utcnow()
            db.session.commit()
            start_worker(job.id, "plan")
        elif job.status == "connecting":
            job.updated_at = _utcnow()
            db.session.commit()
            start_worker(job.id, "connect")
        else:  # applying
            job.status = "interrupted"
            job.finished_at = _utcnow()
            job.error = (
                "KubeSight restarted while OpenTofu was applying this plan. OpenTofu "
                "saved its state as it went; a new plan was made to finish the job."
            )
            recovery = ClusterProvisionJob(
                build_id=job.build_id,
                operation=job.operation,
                status="planning",
                config_json={k: v for k, v in (job.config_json or {}).items() if k != "tofu"},
                prior_build_status=job.prior_build_status,
                requested_by=job.requested_by,
                requested_by_user_id=job.requested_by_user_id,
                applied_by=job.applied_by,
                applied_by_user_id=job.applied_by_user_id,
                approved_by=job.approved_by,
                approved_by_user_id=job.approved_by_user_id,
                approved_at=job.approved_at,
                reason=job.reason,
                auto_apply=True,
                recovered_from_job_id=job.id,
            )
            db.session.add(recovery)
            db.session.commit()
            start_worker(recovery.id, "plan")
