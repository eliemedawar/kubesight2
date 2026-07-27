"""The restart-safe build phase machine.

Phases (single-CP builds skip loadbalancer/join_cp as configured):

    base_prep → loadbalancer → pull_images → init → cni → join_cp (serial!)
    → join_workers → verify → onboard

Every phase writes ClusterBuildStep rows; completed steps are skipped on
resume, so a backend restart RESUMES a build instead of restarting it
(orphan recovery lives in ``advance_cluster_builds``, ticked by the alert
scheduler like the mobile pipeline).

Threading rules: worker threads only ever run SSH commands against
pre-built targets — every DB read/write happens on the phase-machine thread.
All persisted log text passes through ``scrub`` first, no exceptions.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import re
import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from flask import current_app

from ...db import db
from ...models import BuildProfile, ClusterBuild, ClusterBuildNode, ClusterBuildStep
from ...secret_encryption import decrypt_secret, encrypt_secret
from ..ssh import SshCommandError, SshConnectionError, SshTarget, get_transport
from .. import ssh_profile_service
from . import addons as addon_registry
from . import cni as cni_registry
from . import kubeadm, lb, onboard, os_adapters
from .addons import metallb as metallb_addon
from .cni.base import extract_images, rewrite_manifest_images
from .profiles import resolve as resolve_profile
from .scrub import scrub

logger = logging.getLogger(__name__)

_LOG_TAIL_CHARS = 8000
_STALE_BUILD_MINUTES = 5
_CERT_KEY_TTL_HOURS = 2
_CNI_APPLY_TIMEOUT_S = 600
_CNI_ROLLOUT_TIMEOUT_S = 1200
_NODE_READY_TIMEOUT_S = 1200
_COREDNS_ROLLOUT_TIMEOUT_S = 600
_SMOKE_POD_TIMEOUT_S = 600
_KUBELET_CSR_APPROVER_IMAGE = (
    "docker.io/postfinance/kubelet-csr-approver:v1.2.14@"
    "sha256:c0f6aa1abdc225a32f9a29992fd97f711e78e2df21434f9ce7bc60981f96a5f8"
)

_active_lock = threading.Lock()
_active_builds: set = set()


class _Cancelled(Exception):
    pass


class _PhaseFailed(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _tail(text: str) -> str:
    text = scrub(text or "")
    return text[-_LOG_TAIL_CHARS:]


# ---------------------------------------------------------------------------
# Step bookkeeping
# ---------------------------------------------------------------------------

def _get_step(build: ClusterBuild, phase: str, node: Optional[ClusterBuildNode] = None) -> ClusterBuildStep:
    node_id = node.id if node else None
    step = ClusterBuildStep.query.filter_by(
        build_id=build.id, phase=phase, node_id=node_id
    ).first()
    if step is None:
        step = ClusterBuildStep(build_id=build.id, phase=phase, node_id=node_id)
        db.session.add(step)
        db.session.commit()
    return step


def _step_start(step: ClusterBuildStep) -> None:
    step.status = "running"
    step.started_at = step.started_at or _utcnow()
    step.error = None
    db.session.commit()


def _step_done(step: ClusterBuildStep, log: str = "") -> None:
    step.status = "completed"
    step.finished_at = _utcnow()
    if log:
        step.log_tail = _tail(log)
    db.session.commit()


def _step_fail(step: ClusterBuildStep, error: str, log: str = "") -> None:
    step.status = "failed"
    step.finished_at = _utcnow()
    step.error = scrub(error)[:2000]
    if log:
        step.log_tail = _tail(log)
    db.session.commit()


def _check_cancelled(build: ClusterBuild) -> None:
    db.session.refresh(build)
    if build.status == "cancelled":
        raise _Cancelled()


class _StreamTail:
    """Buffers a step's SSH output and periodically persists the scrubbed tail
    to its ClusterBuildStep row, so the UI can show live progress during long
    phases (apt installs, kubeadm init) instead of an empty log until the end.

    Must be used inside an app context; each worker thread owns exactly one
    step, so per-thread sessions never contend on a row.
    """

    def __init__(self, step_id: int, interval_s: float = 2.5):
        self.step_id = step_id
        self.interval_s = interval_s
        self._parts: List[str] = []
        self._last_flush = 0.0

    def header(self, label: str) -> None:
        self.write(f"\n>>> {label}\n")

    def command(self, target: SshTarget, command: str, input_summary: str = "") -> None:
        self.header(f"COMMAND on {target.describe()}")
        self.write(f"$ {scrub(command)}\n")
        if input_summary:
            self.write(f"stdin: {scrub(input_summary)}\n")

    def result(self, exit_code: int = 0) -> None:
        self.write(f"\n<<< exit {exit_code}\n")

    def write(self, chunk: str) -> None:
        self._parts.append(chunk)
        now = time.monotonic()
        if now - self._last_flush >= self.interval_s:
            self._last_flush = now
            self.flush()

    def text(self) -> str:
        return "".join(self._parts)

    def flush(self) -> None:
        try:
            step = db.session.get(ClusterBuildStep, self.step_id)
            if step is not None:
                step.log_tail = _tail(self.text())  # _tail() scrubs
                db.session.commit()
        except Exception:  # noqa: BLE001 — a failed flush must not kill the phase
            db.session.rollback()


# ---------------------------------------------------------------------------
# Target + script helpers
# ---------------------------------------------------------------------------

def _target_for(build: ClusterBuild, node: ClusterBuildNode) -> SshTarget:
    profile_id = node.connection_profile_id or build.connection_profile_id
    if not profile_id:
        raise _PhaseFailed(
            f"{node.hostname or node.address}: no SSH connection profile configured."
        )
    profile = ssh_profile_service.get_profile(profile_id)
    label = f"{node.hostname or node.vsphere_vm_name or node.address} ({node.address})"
    return ssh_profile_service.build_target(profile, node.address, label=label)


def _adapter_for(node: ClusterBuildNode):
    adapter = os_adapters.by_id(node.os_family or "")
    if adapter is None:
        raise _PhaseFailed(
            f"{node.hostname or node.address}: OS family '{node.os_family}' has "
            "no adapter — run preflight first."
        )
    return adapter


def _script_ctx(build: ClusterBuild, resolved, node: ClusterBuildNode):
    return os_adapters.ScriptContext(
        profile=resolved,
        k8s_version=build.k8s_version,
        facts=os_adapters.OsFacts(
            os_id=node.os_family or "", version_id=node.os_version or "",
            arch=node.arch or "",
        ),
    )


def _run_traced(
    target: SshTarget,
    script: str,
    *,
    timeout_s: int,
    stream: Optional[_StreamTail] = None,
    display_command: Optional[str] = None,
    input_summary: str = "",
):
    if stream is not None:
        stream.command(target, display_command or script, input_summary)
    try:
        result = get_transport().run(
            target,
            script,
            timeout_s=timeout_s,
            on_output=stream.write if stream is not None else None,
        )
    except (SshCommandError, SshConnectionError) as exc:
        if stream is not None:
            output = getattr(exc, "output", "")
            if output and output not in stream.text():
                stream.write(output)
            stream.result(getattr(exc, "exit_code", -1))
            stream.flush()
        raise
    if stream is not None:
        # Some transports/fakes return output without invoking on_output.
        if result.output and result.output not in stream.text():
            stream.write(result.output)
        stream.result(result.exit_code)
        stream.flush()
    return result


def _upload_and_run_traced(
    target: SshTarget,
    file_path: str,
    content: str,
    command: str,
    *,
    timeout_s: int,
    stream: _StreamTail,
) -> str:
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    script = f"""set -e
mkdir -p $(dirname {file_path})
echo {encoded} | base64 -d > {file_path}
{command}
"""
    return _run_traced(
        target,
        script,
        timeout_s=timeout_s,
        stream=stream,
        display_command=command,
        input_summary=f"file {file_path} ({len(content.encode('utf-8'))} bytes; content hidden)",
    ).output


def _kubectl(
    target: SshTarget,
    args: str,
    timeout_s: int = 120,
    stream: Optional[_StreamTail] = None,
) -> str:
    command = f"kubectl --kubeconfig /etc/kubernetes/admin.conf {args}"
    return _run_traced(
        target, command, timeout_s=timeout_s, stream=stream
    ).output


def _wait_until(fn, *, timeout_s: int, interval_s: int = 5, describe: str = "") -> None:
    deadline = time.monotonic() + timeout_s
    last_error = ""
    while time.monotonic() < deadline:
        try:
            if fn():
                return
        except _Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — retried until deadline
            last_error = str(exc)
        time.sleep(interval_s)
    raise _PhaseFailed(
        f"Timed out after {timeout_s}s waiting for {describe or 'condition'}."
        + (f" Last error: {scrub(last_error)[:500]}" if last_error else "")
    )


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def _nodes_by_role(build: ClusterBuild) -> Tuple[List[ClusterBuildNode], List[ClusterBuildNode], List[ClusterBuildNode]]:
    cps = [n for n in build.nodes if n.role == "control_plane"]
    workers = [n for n in build.nodes if n.role == "worker"]
    lbs = [n for n in build.nodes if n.role == "loadbalancer"]
    cps.sort(key=lambda n: (not n.is_primary_cp, n.position))
    return cps, workers, lbs


def _phase_base_prep(build: ClusterBuild, resolved) -> None:
    """Parallel node preparation. Scripts per role:
    CP/worker → CA, kernel prep, containerd, kube packages;
    LB → CA + haproxy/keepalived packages only."""
    jobs = []  # (node, step, [scripts])
    for node in build.nodes:
        step = _get_step(build, "base_prep", node)
        if step.status == "completed":
            continue
        adapter = _adapter_for(node)
        ctx = _script_ctx(build, resolved, node)
        scripts = []  # (stage label, script) — labels become >>> markers in the live log
        ca_script = adapter.script_configure_ca(ctx)
        if ca_script:
            scripts.append(("Trusted CA certificates", ca_script))
        if node.role == "loadbalancer":
            scripts.append(("haproxy + keepalived install", adapter.script_install_haproxy_keepalived(ctx)))
        else:
            scripts.append(("Kernel modules + sysctl + swap off", adapter.script_kernel_prep(ctx)))
            scripts.append(("containerd install + config", adapter.script_install_containerd(ctx)))
            scripts.append(("Kubernetes packages (kubeadm, kubelet, kubectl)", adapter.script_install_kube_packages(ctx)))
        jobs.append((
            node, step, scripts, _target_for(build, node),
            node.id, step.id,
        ))

    if not jobs:
        return
    for node, step, _, _, _, _ in jobs:
        node.status = "preparing"
        _step_start(step)  # commits, persisting the node status too

    results: Dict[int, Tuple[bool, str]] = {}
    app = current_app._get_current_object()

    def _prep(node_id: int, step_id: int, target: SshTarget, scripts) -> None:
        # App context per thread: the real transport's host-key verification
        # reads/writes the ssh_host_keys table, and the stream tail persists
        # partial output so the UI shows live progress.
        try:
            with app.app_context():
                stream = _StreamTail(step_id)
                try:
                    for label, script in scripts:
                        stream.header(label)
                        _run_traced(
                            target,
                            script,
                            timeout_s=900,
                            stream=stream,
                            display_command=f"{label} (generated script hidden)",
                            input_summary="configuration content hidden",
                        )
                    results[node_id] = (True, stream.text())
                except SshCommandError as exc:
                    # Chunks already streamed; make sure the buffer holds the
                    # full output even if the transport didn't stream (fakes).
                    if exc.output and exc.output not in stream.text():
                        stream.write(exc.output)
                    stream.write(f"\n!! {exc}\n")
                    results[node_id] = (False, stream.text())
                finally:
                    stream.flush()
        except Exception as exc:  # noqa: BLE001
            results[node_id] = (False, f"{exc}")

    with ThreadPoolExecutor(max_workers=5) as pool:
        for _, _, scripts, target, node_id, step_id in jobs:
            pool.submit(_prep, node_id, step_id, target, scripts)

    failures = []
    for node, step, _, _, node_id, _ in jobs:
        ok, log = results.get(node_id, (False, "no result"))
        if ok:
            node.status = "ready"
            _step_done(step, log)
        else:
            node.status = "failed"
            node.error = scrub(log)[-2000:]
            _step_fail(step, f"Node preparation failed on {node.hostname or node.address}.", log)
            failures.append(node.hostname or node.address)
    if failures:
        raise _PhaseFailed(f"base_prep failed on: {', '.join(failures)}.")


def _phase_loadbalancer(build: ClusterBuild, resolved) -> None:
    if build.endpoint_mode != "managed_haproxy":
        return
    cps, _, lbs = _nodes_by_role(build)
    if not lbs:
        raise _PhaseFailed("managed_haproxy endpoint mode requires load-balancer nodes.")
    if not build.vip_address:
        raise _PhaseFailed("managed_haproxy endpoint mode requires a VIP address.")

    step = _get_step(build, "loadbalancer")
    if step.status == "completed":
        return
    _step_start(step)
    stream = _StreamTail(step.id)
    try:
        master = next((n for n in lbs if n.is_lb_master), lbs[0])
        auth_pass = decrypt_secret(build.vrrp_auth_pass_cipher or "")
        if not auth_pass:
            auth_pass = lb.generate_vrrp_auth_pass()
            build.vrrp_auth_pass_cipher = encrypt_secret(auth_pass)
            db.session.commit()
        router_id = build.vrrp_router_id or 51

        haproxy_cfg = lb.render_haproxy_cfg(
            [(n.hostname or f"cp{i}", n.address) for i, n in enumerate(cps, 1)]
        )
        haproxy_b64 = base64.b64encode(haproxy_cfg.encode()).decode()

        for node in lbs:
            target = _target_for(build, node)
            iface = build.vip_interface
            if not iface:
                detected = _run_traced(
                    target,
                    lb.detect_interface_script(node.address),
                    timeout_s=30,
                    stream=stream,
                ).output.strip().splitlines()
                iface = detected[-1].strip() if detected else ""
                if not iface:
                    raise _PhaseFailed(
                        f"Could not detect the VIP interface on {node.address}; "
                        "set vipInterface explicitly."
                    )
            keepalived_conf = lb.render_keepalived_conf(
                is_master=(node.id == master.id),
                interface=iface,
                router_id=router_id,
                auth_pass=auth_pass,
                vip=build.vip_address,
                peer_addresses=[n.address for n in lbs if n.id != node.id],
            )
            keepalived_b64 = base64.b64encode(keepalived_conf.encode()).decode()
            _run_traced(
                target,
                lb.lb_apply_script(haproxy_b64, keepalived_b64),
                timeout_s=180,
                stream=stream,
                display_command="install and validate HAProxy/Keepalived configuration",
                input_summary=(
                    f"haproxy.cfg {len(haproxy_cfg.encode())} bytes; "
                    f"keepalived.conf {len(keepalived_conf.encode())} bytes; "
                    "content hidden"
                ),
            )

        # VIP must be bound on the master and answer from a peer BEFORE init
        # bakes the endpoint into certificates.
        master_target = _target_for(build, master)
        _wait_until(
            lambda: "bound" in _run_traced(
                master_target,
                lb.vip_verify_script(build.vip_address),
                timeout_s=30,
                stream=stream,
            ).output,
            timeout_s=60, interval_s=5, describe="VIP to bind on the LB master",
        )
        peer = next((n for n in lbs if n.id != master.id), None) or (cps[0] if cps else None)
        if peer is not None:
            peer_target = _target_for(build, peer)
            _run_traced(
                peer_target,
                lb.vip_ping_script(build.vip_address),
                timeout_s=30,
                stream=stream,
            )
        _step_done(step, stream.text())
    except (_PhaseFailed, SshCommandError, Exception) as exc:  # noqa: BLE001
        extra = getattr(exc, "output", "")
        if extra and extra not in stream.text():
            stream.write(extra)
        _step_fail(step, str(exc), stream.text())
        raise _PhaseFailed(f"loadbalancer phase failed: {exc}") from exc


def _phase_pull_images(build: ClusterBuild, resolved) -> None:
    cps, workers, _ = _nodes_by_role(build)
    nodes = cps + workers

    # A retry resumes after the last failed phase.  Do not resolve CNI/add-on
    # manifests again when every node already completed image pre-pulling:
    # remote manifests may be unavailable after a backend restart even though
    # the cluster and its required images are already healthy.
    if nodes and all(
        _get_step(build, "pull_images", node).status == "completed"
        for node in nodes
    ):
        return

    repo_flag = ""
    from .profiles import DEFAULT_K8S_IMAGE_REGISTRY

    if resolved.k8s_image_registry and resolved.k8s_image_registry != DEFAULT_K8S_IMAGE_REGISTRY:
        repo_flag = f" --image-repository {resolved.k8s_image_registry}"
    descriptor = cni_registry.get(build.cni_plugin)
    try:
        images = (
            descriptor.required_images(
                build.cni_version or descriptor.versions[0], resolved
            )
            if descriptor is not None else []
        )
        for selection in build.addons_json or []:
            addon = addon_registry.get(str(selection.get("id") or ""))
            if addon is None:
                raise addon_registry.AddonRenderError(
                    f"Add-on '{selection.get('id')}' is not available."
                )
            version = str(selection.get("version") or addon.versions[0])
            images.extend(addon.required_images(version, resolved))
            if addon.id == "metrics-server":
                approver_manifest = _metrics_csr_approver_manifest(
                    build, resolved
                )
                images.extend(extract_images(approver_manifest))
        images = list(dict.fromkeys(images))
    except (cni_registry.CniRenderError, addon_registry.AddonRenderError) as exc:
        for node in nodes:
            failed_step = _get_step(build, "pull_images", node)
            if failed_step.status == "completed":
                continue
            _step_start(failed_step)
            _step_fail(failed_step, f"Required image resolution failed: {exc}")
            break
        raise _PhaseFailed(f"Required image resolution failed: {exc}") from exc
    cp_ids = {node.id for node in cps}
    for node in nodes:
        step = _get_step(build, "pull_images", node)
        if step.status == "completed":
            continue
        _step_start(step)
        try:
            stream = _StreamTail(step.id)
            target = _target_for(build, node)
            pull_commands = []
            if resolved.proxy_env():
                proxy_env = resolved.proxy_env()
            else:
                proxy_env = ""
            if node.id in cp_ids:
                pull_commands.append(
                    "kubeadm config images pull "
                    f"--kubernetes-version v{build.k8s_version.lstrip('v')}{repo_flag}"
                )
            pull_commands.extend(
                f"crictl pull {shlex.quote(image)}" for image in images
            )
            commands = ["set -e"]
            if proxy_env:
                commands.append(proxy_env)
            commands.extend(pull_commands)
            _run_traced(
                target,
                "\n".join(commands),
                timeout_s=1800,
                stream=stream,
                display_command="\n".join(
                    ["set -e", *pull_commands, "# proxy environment hidden"]
                ),
            )
            _step_done(step, stream.text())
        except (SshCommandError, SshConnectionError) as exc:
            _step_fail(step, f"Image pull failed on {node.hostname or node.address}.",
                       getattr(exc, "output", ""))
            raise _PhaseFailed(
                f"pull_images failed on {node.hostname or node.address} — "
                "registry unreachable or images missing. Fails early by design."
            ) from exc


def _phase_init(build: ClusterBuild, resolved) -> ClusterBuildNode:
    cps, _, _ = _nodes_by_role(build)
    if not cps:
        raise _PhaseFailed("No control-plane node defined.")
    primary = cps[0]
    step = _get_step(build, "init", primary)
    if step.status == "completed":
        return primary
    _step_start(step)
    is_ha = len(cps) > 1
    config = kubeadm.render_init_config(
        k8s_version=build.k8s_version,
        control_plane_endpoint=build.control_plane_endpoint,
        pod_cidr=build.pod_cidr,
        service_cidr=build.service_cidr,
        profile=resolved,
        node_name=primary.hostname or primary.address,
        server_tls_bootstrap=any(
            item.get("id") == "metrics-server"
            for item in (build.addons_json or [])
        ),
    )
    upload_flag = " --upload-certs" if is_ha else ""
    try:
        stream = _StreamTail(step.id)
        target = _target_for(build, primary)
        output = _upload_and_run_traced(
            target,
            "/etc/kubernetes/kubesight-init.yaml",
            config,
            f"kubeadm init --config /etc/kubernetes/kubesight-init.yaml{upload_flag}",
            timeout_s=1200,
            stream=stream,
        )
    except (SshCommandError, SshConnectionError) as exc:
        # Mark the node failed so retry runs kubeadm reset on it — a half-run
        # init leaves certs/manifests behind and a bare re-run always fails.
        primary.status = "failed"
        primary.error = scrub(str(exc))[:2000]
        _step_fail(step, "kubeadm init failed.", getattr(exc, "output", ""))
        raise _PhaseFailed(f"kubeadm init failed on {primary.hostname or primary.address}.") from exc

    artifacts = kubeadm.parse_init_output(output)
    problem = kubeadm.validate_init_artifacts(artifacts, need_certificate_key=is_ha)
    if problem:
        primary.status = "failed"
        primary.error = problem
        _step_fail(step, problem, output)
        raise _PhaseFailed(problem)

    build.join_command_cipher = encrypt_secret(
        artifacts.worker_join_command(build.control_plane_endpoint)
    )
    if artifacts.certificate_key:
        build.cert_key_cipher = encrypt_secret(artifacts.certificate_key)
        build.cert_key_expires_at = _utcnow() + timedelta(hours=_CERT_KEY_TTL_HOURS)
    primary.status = "joined"
    db.session.commit()
    _step_done(step, stream.text())  # _tail() scrubs the token/cert key
    return primary


def _phase_cni(build: ClusterBuild, resolved, primary: ClusterBuildNode) -> None:
    step = _get_step(build, "cni")
    if step.status == "completed":
        return
    _step_start(step)
    descriptor = cni_registry.get(build.cni_plugin)
    if descriptor is None:
        _step_fail(step, f"CNI plugin '{build.cni_plugin}' is not available.")
        raise _PhaseFailed(f"CNI plugin '{build.cni_plugin}' is not available.")
    try:
        manifests = descriptor.render(
            build.cni_version or descriptor.versions[0], build.pod_cidr, resolved
        )
        target = _target_for(build, primary)
        stream = _StreamTail(step.id)
        for index, manifest in enumerate(manifests):
            _upload_and_run_traced(
                target,
                f"/etc/kubernetes/kubesight-cni-{index}.yaml",
                manifest,
                f"kubectl --kubeconfig /etc/kubernetes/admin.conf apply -f "
                f"/etc/kubernetes/kubesight-cni-{index}.yaml",
                timeout_s=_CNI_APPLY_TIMEOUT_S,
                stream=stream,
            )
        namespace, daemonset = descriptor.readiness_daemonset
        if daemonset:
            _kubectl(
                target,
                f"-n {namespace} rollout status daemonset/{daemonset} "
                f"--timeout={_CNI_ROLLOUT_TIMEOUT_S}s",
                timeout_s=_CNI_ROLLOUT_TIMEOUT_S + 30,
                stream=stream,
            )
        _step_done(step, stream.text())
    except (cni_registry.CniRenderError, SshCommandError, SshConnectionError) as exc:
        extra = getattr(exc, "output", "")
        _step_fail(step, str(exc), extra)
        raise _PhaseFailed(f"CNI installation failed: {exc}") from exc


def _cert_key_expired(build: ClusterBuild) -> bool:
    expires = build.cert_key_expires_at
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= _utcnow()


def _ensure_join_secrets(
    build: ClusterBuild, primary: ClusterBuildNode, *, need_cert_key: bool
) -> Tuple[str, str]:
    """Return (join_base, cert_key), minting fresh ones on the primary when the
    stored secrets are missing or expired.

    Bootstrap tokens live 24h and the --upload-certs key only 2h, so a retry
    hours after a failure would otherwise be doomed even though the cluster
    itself is fine — kubeadm can always mint replacements from the primary.
    """
    join_base = decrypt_secret(build.join_command_cipher or "")
    cert_key = decrypt_secret(build.cert_key_cipher or "") if need_cert_key else ""
    cert_key_stale = need_cert_key and (not cert_key or _cert_key_expired(build))
    if join_base and not cert_key_stale:
        return join_base, cert_key

    target = _target_for(build, primary)
    label = primary.hostname or primary.address
    try:
        if not join_base:
            output = get_transport().run(
                target, "set -e\nkubeadm token create --print-join-command",
                timeout_s=120,
            ).output
            artifacts = kubeadm.parse_init_output(output)
            if not artifacts.token or not artifacts.ca_cert_hash:
                raise _PhaseFailed(
                    "Could not mint a fresh join token on the primary control plane "
                    "(kubeadm token create output was unparsable)."
                )
            join_base = artifacts.worker_join_command(build.control_plane_endpoint)
            build.join_command_cipher = encrypt_secret(join_base)
        if cert_key_stale:
            output = get_transport().run(
                target, "set -e\nkubeadm init phase upload-certs --upload-certs",
                timeout_s=180,
            ).output
            cert_key = kubeadm.parse_certificate_key(output)
            if not cert_key:
                raise _PhaseFailed(
                    "Could not re-upload control-plane certificates on the primary "
                    "(no certificate key in kubeadm output)."
                )
            build.cert_key_cipher = encrypt_secret(cert_key)
            build.cert_key_expires_at = _utcnow() + timedelta(hours=_CERT_KEY_TTL_HOURS)
    except (SshCommandError, SshConnectionError) as exc:
        raise _PhaseFailed(
            f"Could not refresh the join secrets on {label}: {exc}"
        ) from exc
    db.session.commit()
    return join_base, cert_key


def _phase_join_cp(build: ClusterBuild, primary: ClusterBuildNode) -> None:
    """SERIAL control-plane joins with an etcd-health gate between each —
    concurrent member additions are a reliable way to lose quorum."""
    cps, _, _ = _nodes_by_role(build)
    secondaries = [n for n in cps if n.id != primary.id]
    if not secondaries:
        return
    pending = [
        (node, _get_step(build, "join_cp", node)) for node in secondaries
    ]
    pending = [(node, step) for node, step in pending if step.status != "completed"]
    if not pending:
        return
    # Minted only when there is a join left to run — on a resume where every CP
    # already joined, the secrets are gone by design and needed by nobody.
    join_base, cert_key = _ensure_join_secrets(build, primary, need_cert_key=True)
    primary_target = _target_for(build, primary)
    for node, step in pending:
        _check_cancelled(build)
        _step_start(step)
        node_name = shlex.quote(node.hostname or node.address)
        join_command = (
            f"{join_base} --control-plane --certificate-key {cert_key} "
            f"--node-name {node_name}"
        )
        try:
            stream = _StreamTail(step.id)
            result = _run_traced(
                _target_for(build, node),
                f"set -e\n{join_command}",
                timeout_s=900,
                stream=stream,
            )
        except (SshCommandError, SshConnectionError) as exc:
            # Failed status is what makes retry reset this node (and remove its
            # stale etcd member) before rejoining.
            node.status = "failed"
            node.error = scrub(str(exc))[:2000]
            _step_fail(step, f"Control-plane join failed on {node.hostname or node.address}.",
                       getattr(exc, "output", ""))
            raise _PhaseFailed(
                f"join_cp failed on {node.hostname or node.address}. Retry will "
                "reset the node (and remove any stale etcd member) before rejoining."
            ) from exc
        # etcd quorum gate before the next member joins.
        _wait_until(
            lambda: "ok" in _kubectl(primary_target, "get --raw /readyz/etcd", timeout_s=30).lower(),
            timeout_s=180, interval_s=5,
            describe=f"etcd quorum health after joining {node.hostname or node.address}",
        )
        node.status = "joined"
        db.session.commit()
        _step_done(step, stream.text())


def _phase_join_workers(build: ClusterBuild) -> None:
    cps, workers, _ = _nodes_by_role(build)
    pending = []
    for node in workers:
        step = _get_step(build, "join_workers", node)
        if step.status == "completed":
            continue
        pending.append((node, step))
    if not pending:
        return
    if not cps:
        raise _PhaseFailed("Worker join requires a control-plane node.")
    # Minted only when a join is actually outstanding (see _phase_join_cp).
    join_command, _ = _ensure_join_secrets(build, cps[0], need_cert_key=False)

    jobs = []
    for node, step in pending:
        _step_start(step)
        jobs.append(
            (
                node,
                step,
                _target_for(build, node),
                shlex.quote(node.hostname or node.address),
            )
        )

    results: Dict[int, Tuple[bool, str]] = {}
    app = current_app._get_current_object()

    def _join(
        node_id: int,
        step_id: int,
        target: SshTarget,
        node_name: str,
    ) -> None:
        try:
            with app.app_context():
                stream = _StreamTail(step_id)
                try:
                    _run_traced(
                        target,
                        f"set -e\n{join_command} --node-name {node_name}",
                        timeout_s=900,
                        stream=stream,
                    )
                    results[node_id] = (True, stream.text())
                except SshCommandError as exc:
                    if exc.output and exc.output not in stream.text():
                        stream.write(exc.output)
                    stream.write(f"\n!! {exc}\n")
                    results[node_id] = (False, stream.text())
                finally:
                    stream.flush()
        except Exception as exc:  # noqa: BLE001
            results[node_id] = (False, str(exc))

    with ThreadPoolExecutor(max_workers=5) as pool:
        for node, step, target, node_name in jobs:
            pool.submit(_join, node.id, step.id, target, node_name)

    failures = []
    for node, step, _, _ in jobs:
        ok, log = results.get(node.id, (False, "no result"))
        if ok:
            node.status = "joined"
            _step_done(step, log)
        else:
            node.status = "failed"
            node.error = scrub(log)[-2000:]
            _step_fail(step, f"Worker join failed on {node.hostname or node.address}.", log)
            failures.append(node.hostname or node.address)
    db.session.commit()
    if failures:
        raise _PhaseFailed(f"join_workers failed on: {', '.join(failures)}.")


def _phase_verify(build: ClusterBuild, resolved, primary: ClusterBuildNode) -> None:
    step = _get_step(build, "verify")
    if step.status == "completed":
        return
    _step_start(step)
    target = _target_for(build, primary)
    cps, workers, _ = _nodes_by_role(build)
    expected = len(cps) + len(workers)
    stream = _StreamTail(step.id)
    try:
        descriptor = cni_registry.get(build.cni_plugin)
        if descriptor is None:
            raise _PhaseFailed(f"CNI plugin '{build.cni_plugin}' is not available.")
        namespace, daemonset = descriptor.readiness_daemonset
        # The first CNI gate runs before workers join. Gate the DaemonSet again
        # now so a smoke pod cannot race a newly joined worker whose CNI image
        # is still pulling or whose host files are not initialized.
        if daemonset:
            _kubectl(
                target,
                f"-n {namespace} rollout status daemonset/{daemonset} "
                f"--timeout={_CNI_ROLLOUT_TIMEOUT_S}s",
                timeout_s=_CNI_ROLLOUT_TIMEOUT_S + 30,
                stream=stream,
            )
        if build.cni_plugin == "calico":
            _kubectl(
                target,
                "-n kube-system rollout status deployment/calico-kube-controllers "
                f"--timeout={_CNI_ROLLOUT_TIMEOUT_S}s",
                timeout_s=_CNI_ROLLOUT_TIMEOUT_S + 30,
                stream=stream,
            )

        # admin.conf points at controlPlaneEndpoint, so every kubectl below also
        # proves the VIP/LB path end to end.
        def _all_ready() -> bool:
            output = _kubectl(
                target,
                "get nodes --no-headers -o "
                "custom-columns=S:.status.conditions[-1].type,ST:.status.conditions[-1].status",
                timeout_s=60,
                stream=stream,
            )
            rows = [line for line in output.splitlines() if line.strip()]
            ready = [line for line in rows if "Ready" in line and "True" in line]
            return len(rows) >= expected and len(ready) >= expected

        _wait_until(_all_ready, timeout_s=_NODE_READY_TIMEOUT_S, interval_s=10,
                    describe=f"all {expected} nodes to be Ready")
        _kubectl(target, "get nodes -o wide", timeout_s=60, stream=stream)
        _kubectl(
            target,
            "-n kube-system rollout status deployment/coredns "
            f"--timeout={_COREDNS_ROLLOUT_TIMEOUT_S}s",
            timeout_s=_COREDNS_ROLLOUT_TIMEOUT_S + 30,
            stream=stream,
        )
        if len(cps) > 1:
            etcd_health = _kubectl(
                target, "get --raw /readyz/etcd", timeout_s=30, stream=stream
            )
            if "ok" not in etcd_health.lower():
                raise _PhaseFailed(f"etcd readyz reported: {etcd_health.strip()[:200]}")
            stream.write("etcd quorum: ok\n")
        pause_image = (
            f"{resolved.k8s_image_registry}/pause:"
            f"{kubeadm.pause_image_tag(build.k8s_version)}"
        )
        # A previous failed verify attempt may have left the pod behind — a
        # bare `kubectl run` would then fail on AlreadyExists forever.
        _kubectl(
            target, "delete pod kubesight-smoke --ignore-not-found",
            timeout_s=60, stream=stream,
        )
        _kubectl(
            target,
            f"run kubesight-smoke --image={pause_image} --restart=Never "
            "--overrides='{\"spec\":{\"tolerations\":[{\"operator\":\"Exists\"}]}}'",
            timeout_s=60,
            stream=stream,
        )

        def _smoke_finished() -> bool:
            phase = _kubectl(
                target,
                "get pod kubesight-smoke -o jsonpath='{.status.phase}'",
                timeout_s=30,
                stream=stream,
            ).strip()
            return phase in ("Running", "Succeeded")

        _wait_until(
            _smoke_finished,
            timeout_s=_SMOKE_POD_TIMEOUT_S,
            interval_s=5,
            describe="the smoke pod to schedule and run",
        )
        _kubectl(
            target, "get pod kubesight-smoke -o wide",
            timeout_s=30, stream=stream,
        )
        _kubectl(
            target, "delete pod kubesight-smoke --ignore-not-found",
            timeout_s=60, stream=stream,
        )
        _step_done(step, stream.text())
    except (SshCommandError, SshConnectionError, _PhaseFailed) as exc:
        extra = getattr(exc, "output", "")
        if extra and extra not in stream.text():
            stream.write(extra)
        _step_fail(step, str(exc), stream.text())
        raise _PhaseFailed(f"verify failed: {exc}") from exc


def _phase_onboard(build: ClusterBuild, primary: ClusterBuildNode) -> None:
    step = _get_step(build, "onboard")
    if step.status == "completed":
        return
    _step_start(step)
    try:
        stream = _StreamTail(step.id)
        target = _target_for(build, primary)
        # admin.conf carries cluster-admin credentials + CA material: it goes
        # straight to cluster_store and NEVER into a log or step record.
        admin_conf = get_transport().run(
            target, "cat /etc/kubernetes/admin.conf", timeout_s=60
        ).output
        # Make plain `kubectl` work for the account used by the builder and for
        # existing sudo/wheel users. Those accounts already have root-equivalent
        # access to admin.conf; this adds convenience without widening the
        # machine's privilege boundary. Never log or echo admin.conf itself.
        username = target.username
        quoted_user = shlex.quote(username)
        configure = f"""set -e
BUILDER_USER={quoted_user}
for KUBECTL_USER in $(getent passwd | cut -d: -f1); do
  if [ "$KUBECTL_USER" != "$BUILDER_USER" ] && \
     ! id -nG "$KUBECTL_USER" | tr ' ' '\\n' | grep -Eq '^(sudo|wheel)$'; then
    continue
  fi
  KUBECTL_HOME=$(getent passwd "$KUBECTL_USER" | cut -d: -f6)
  KUBECTL_GROUP=$(id -gn "$KUBECTL_USER")
  [ -n "$KUBECTL_HOME" ] || continue
  install -d -m 700 -o "$KUBECTL_USER" -g "$KUBECTL_GROUP" "$KUBECTL_HOME/.kube"
  install -m 600 -o "$KUBECTL_USER" -g "$KUBECTL_GROUP" /etc/kubernetes/admin.conf "$KUBECTL_HOME/.kube/config"
  sudo -u "$KUBECTL_USER" -H kubectl get nodes --no-headers >/dev/null
  echo "kubectl configured for $KUBECTL_USER"
done
"""
        _run_traced(
            target,
            configure,
            timeout_s=60,
            stream=stream,
            display_command=(
                f"install admin kubeconfig (mode 600) for SSH user {username} "
                "and existing sudo/wheel users, then verify kubectl"
            ),
            input_summary="admin.conf content hidden",
        )
        public_id = onboard.register_cluster(build, admin_conf)
        stream.write(f"Registered as cluster {public_id}.\n")
        _step_done(step, stream.text())
    except Exception as exc:  # noqa: BLE001
        _step_fail(step, f"Cluster registration failed: {exc}")
        raise _PhaseFailed(f"onboard failed: {exc}") from exc


def _metrics_csr_approver_manifest(build: ClusterBuild, resolved) -> str:
    """Render the persistent, inventory-scoped kubelet serving CSR approver.

    Kubernetes deliberately does not auto-approve kubelet serving CSRs.
    Metrics Server needs CA-signed kubelet certificates, including after
    rotation, so a maintained approver controller is installed with a policy
    restricted to the exact node names and IPs in this build.
    """
    cps, workers, _ = _nodes_by_role(build)
    nodes = cps + workers
    names = [str(node.hostname or "").strip() for node in nodes]
    if not names or any(not name for name in names):
        raise addon_registry.AddonRenderError(
            "Metrics Server requires explicit hostnames for all nodes."
        )
    prefixes = []
    addresses = []
    try:
        for node in nodes:
            address = ipaddress.ip_address(node.address)
            addresses.append(str(address))
            prefixes.append(f"{address}/{address.max_prefixlen}")
    except ValueError as exc:
        raise addon_registry.AddonRenderError(
            "Metrics Server requires IP-literal node addresses."
        ) from exc

    provider_regex = "^(?:" + "|".join(re.escape(name) for name in names) + ")$"
    image = rewrite_manifest_images(
        f"image: {_KUBELET_CSR_APPROVER_IMAGE}\n",
        resolved.addon_image_registry,
    ).split(":", 1)[1].strip()
    host_aliases = "\n".join(
        "        - ip: "
        + json.dumps(address)
        + "\n          hostnames:\n            - "
        + json.dumps(name)
        for name, address in zip(names, addresses)
    )
    return f"""apiVersion: v1
kind: ServiceAccount
metadata:
  name: kubelet-csr-approver
  namespace: kube-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kubelet-csr-approver
rules:
  - apiGroups: ["certificates.k8s.io"]
    resources: ["certificatesigningrequests"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["certificates.k8s.io"]
    resources: ["certificatesigningrequests/approval"]
    verbs: ["update"]
  - apiGroups: ["certificates.k8s.io"]
    resources: ["signers"]
    resourceNames: ["kubernetes.io/kubelet-serving"]
    verbs: ["approve"]
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: kubelet-csr-approver
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: kubelet-csr-approver
subjects:
  - kind: ServiceAccount
    name: kubelet-csr-approver
    namespace: kube-system
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: kubelet-csr-approver
  namespace: kube-system
  labels:
    app.kubernetes.io/name: kubelet-csr-approver
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: kubelet-csr-approver
  template:
    metadata:
      labels:
        app.kubernetes.io/name: kubelet-csr-approver
    spec:
      serviceAccountName: kubelet-csr-approver
      automountServiceAccountToken: true
      # Give the approver an inventory-scoped hostname-to-IP mapping. Keeping
      # DNS verification enabled binds every requested IP SAN to that node's
      # configured hostname instead of merely allowing any cluster-node IP.
      hostAliases:
{host_aliases}
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: kubelet-csr-approver
          image: {image}
          imagePullPolicy: IfNotPresent
          args:
            - -metrics-bind-address
            - ":8080"
            - -health-probe-bind-address
            - ":8081"
          env:
            - name: PROVIDER_REGEX
              value: {json.dumps(provider_regex)}
            - name: PROVIDER_IP_PREFIXES
              value: {json.dumps(",".join(prefixes))}
            - name: MAX_EXPIRATION_SEC
              value: "31622400"
            - name: SKIP_DENY_STEP
              value: "true"
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              memory: 128Mi
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8081
          readinessProbe:
            httpGet:
              # v1.2.14 exposes /healthz but does not register /readyz; using
              # /readyz leaves a functional approver permanently 0/1 Ready.
              path: /healthz
              port: 8081
      tolerations:
        - key: node-role.kubernetes.io/control-plane
          effect: NoSchedule
"""


def _wait_for_metrics(
    build: ClusterBuild,
    target: SshTarget,
    stream: _StreamTail,
) -> None:
    cps, workers, _ = _nodes_by_role(build)
    expected = len(cps) + len(workers)

    def _all_nodes_have_metrics() -> bool:
        _check_cancelled(build)
        output = _kubectl(
            target,
            "top nodes --no-headers",
            timeout_s=60,
            stream=stream,
        )
        return len([line for line in output.splitlines() if line.strip()]) >= expected

    _wait_until(
        _all_nodes_have_metrics,
        timeout_s=600,
        interval_s=10,
        describe=f"Metrics Server to report all {expected} node(s)",
    )


_LB_PROBE_SERVICE = "kubesight-lb-probe"


def _configure_metallb(
    build: ClusterBuild,
    target: SshTarget,
    selection: Dict[str, object],
    stream: _StreamTail,
) -> None:
    """Apply the address pool, then prove a Service actually gets an address.

    Installing the manifests alone leaves MetalLB inert, so the pool is applied
    here and a throwaway LoadBalancer Service must come back with an address
    inside it before the phase is allowed to pass.
    """
    config = dict(selection.get("config") or {})
    pools = list(config.get("addressPools") or [])
    manifest = metallb_addon.METALLB.pool_manifest(config)
    remote_path = "/etc/kubernetes/kubesight-addon-metallb-pool.yaml"

    # The controller serves a validating webhook for these CRs. Its Deployment
    # is Available a moment before the webhook Service has endpoints, so the
    # first apply can lose the race with "connection refused".
    def _apply_pool() -> bool:
        _check_cancelled(build)
        _upload_and_run_traced(
            target,
            remote_path,
            manifest,
            "kubectl --kubeconfig /etc/kubernetes/admin.conf "
            f"apply -f {remote_path}",
            timeout_s=120,
            stream=stream,
        )
        return True

    _wait_until(
        _apply_pool,
        timeout_s=300,
        interval_s=10,
        describe="the MetalLB webhook to accept the address pool",
    )
    stream.write(f"MetalLB pool {metallb_addon.POOL_NAME}: {', '.join(pools)}\n")

    _kubectl(
        target,
        f"delete service {_LB_PROBE_SERVICE} --ignore-not-found",
        timeout_s=60,
        stream=stream,
    )
    try:
        _kubectl(
            target,
            f"create service loadbalancer {_LB_PROBE_SERVICE} --tcp=80:80",
            timeout_s=60,
            stream=stream,
        )
        assigned: List[str] = []

        def _address_assigned() -> bool:
            _check_cancelled(build)
            address = _kubectl(
                target,
                f"get service {_LB_PROBE_SERVICE} -o jsonpath="
                "'{.status.loadBalancer.ingress[0].ip}'",
                timeout_s=60,
                stream=stream,
            ).strip().strip("'")
            if not address:
                return False
            assigned.append(address)
            return True

        _wait_until(
            _address_assigned,
            timeout_s=300,
            interval_s=10,
            describe="MetalLB to assign a LoadBalancer address",
        )
        address = assigned[-1]
        if not metallb_addon.pool_contains(pools, address):
            raise _PhaseFailed(
                f"MetalLB assigned {address}, which is outside the configured "
                f"pool ({', '.join(pools)})."
            )
        stream.write(f"LoadBalancer probe service received {address}.\n")
    finally:
        # Best effort: a leftover probe service would otherwise hold a pool
        # address, and a retry deletes it up front anyway.
        try:
            _kubectl(
                target,
                f"delete service {_LB_PROBE_SERVICE} --ignore-not-found",
                timeout_s=60,
                stream=stream,
            )
        except (SshCommandError, SshConnectionError):
            stream.write(
                f"Warning: could not delete the {_LB_PROBE_SERVICE} service.\n"
            )


def _verify_ingress_data_path(
    build: ClusterBuild,
    target: SshTarget,
    stream: _StreamTail,
) -> None:
    """Prove the ingress NodePort answers HTTP from outside the pod network.

    No Ingress resource and no extra image are needed: the controller's default
    server answers unmatched requests with 404, so any HTTP status line proves
    kube-proxy, the CNI, and the controller are all carrying traffic.
    """
    node_port = _kubectl(
        target,
        "-n nginx-ingress get service nginx-ingress -o jsonpath="
        "'{.spec.ports[?(@.name==\"http\")].nodePort}'",
        timeout_s=60,
        stream=stream,
    ).strip().strip("'")
    if not node_port.isdigit():
        raise _PhaseFailed(
            "The NGINX Ingress service exposes no http NodePort "
            f"(got {node_port or 'nothing'})."
        )

    _, workers, _ = _nodes_by_role(build)
    probe_host = (workers[0].address if workers else target.host)
    url = f"http://{probe_host}:{node_port}/"
    script = f"""set -e
if command -v curl >/dev/null 2>&1; then
  CODE=$(curl -s -o /dev/null -m 5 -w '%{{http_code}}' {url} || true)
elif command -v wget >/dev/null 2>&1; then
  CODE=$(wget -q -O /dev/null -T 5 --server-response {url} 2>&1 \
    | awk '/^  HTTP\\//{{code=$2}} END{{print code}}')
else
  echo "SKIPPED no HTTP client"
  exit 0
fi
if [ -z "$CODE" ] || [ "$CODE" = "000" ]; then
  echo "no HTTP response from {url}"
  exit 1
fi
echo "HTTP $CODE from {url}"
"""

    def _responds() -> bool:
        _check_cancelled(build)
        output = _run_traced(
            target,
            script,
            timeout_s=60,
            stream=stream,
            display_command=f"probe the ingress NodePort at {url}",
        ).output
        if "SKIPPED" in output:
            stream.write(
                f"Warning: neither curl nor wget is installed on {target.host}; "
                "skipping the ingress data-path probe.\n"
            )
            return True
        return "HTTP " in output

    _wait_until(
        _responds,
        timeout_s=300,
        interval_s=10,
        describe=f"the ingress NodePort at {url} to answer",
    )


def _phase_addons(
    build: ClusterBuild,
    resolved,
    primary: ClusterBuildNode,
) -> None:
    selections = list(build.addons_json or [])
    if not selections:
        return
    step = _get_step(build, "addons")
    if step.status == "completed":
        return
    _step_start(step)
    stream = _StreamTail(step.id)
    target = _target_for(build, primary)
    catalog_order = {
        descriptor.id: index
        for index, descriptor in enumerate(addon_registry.available())
    }
    selections.sort(key=lambda item: catalog_order.get(item.get("id"), 999))

    try:
        for selection in selections:
            _check_cancelled(build)
            addon_id = str(selection.get("id") or "")
            descriptor = addon_registry.get(addon_id)
            if descriptor is None:
                raise addon_registry.AddonRenderError(
                    f"Add-on '{addon_id}' is not available."
                )
            version = str(selection.get("version") or descriptor.versions[0])
            stream.header(f"{descriptor.display_name} {version}")
            manifests = descriptor.render(version, resolved)
            if addon_id == "metrics-server":
                manifests.insert(
                    0,
                    _metrics_csr_approver_manifest(build, resolved),
                )
            for index, manifest in enumerate(manifests):
                remote_path = (
                    f"/etc/kubernetes/kubesight-addon-{addon_id}-{index}.yaml"
                )
                _upload_and_run_traced(
                    target,
                    remote_path,
                    manifest,
                    "kubectl --kubeconfig /etc/kubernetes/admin.conf "
                    f"apply -f {remote_path}",
                    timeout_s=600,
                    stream=stream,
                )
            if addon_id == "metrics-server":
                _kubectl(
                    target,
                    "-n kube-system rollout status "
                    "deployment/kubelet-csr-approver --timeout=600s",
                    timeout_s=660,
                    stream=stream,
                )
            for command in descriptor.readiness_commands:
                _kubectl(target, command, timeout_s=960, stream=stream)

            if addon_id == "metrics-server":
                _wait_for_metrics(build, target, stream)
            elif addon_id == "nginx-ingress":
                _kubectl(
                    target,
                    "get ingressclass nginx",
                    timeout_s=60,
                    stream=stream,
                )
                service_type = _kubectl(
                    target,
                    "-n nginx-ingress get service nginx-ingress "
                    "-o jsonpath='{.spec.type}'",
                    timeout_s=60,
                    stream=stream,
                ).strip()
                if service_type != "NodePort":
                    raise _PhaseFailed(
                        "NGINX Ingress service is not exposed as NodePort."
                    )
                _verify_ingress_data_path(build, target, stream)
            elif addon_id == "metallb":
                _configure_metallb(build, target, selection, stream)
        _check_cancelled(build)
        _step_done(step, stream.text())
    except _Cancelled:
        raise
    except (
        addon_registry.AddonConfigError,
        addon_registry.AddonRenderError,
        SshCommandError,
        SshConnectionError,
        _PhaseFailed,
    ) as exc:
        extra = getattr(exc, "output", "")
        if extra and extra not in stream.text():
            stream.write(extra)
        _step_fail(step, str(exc), stream.text())
        raise _PhaseFailed(f"add-ons failed: {exc}") from exc


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

def _run_build(app, build_id: int) -> None:
    with app.app_context():
        build = db.session.get(ClusterBuild, build_id)
        if build is None or build.status != "building":
            # Must still release the in-flight claim taken by
            # start_build_worker — a leaked entry makes every later start/retry
            # of this build a silent no-op for the life of the process.
            with _active_lock:
                _active_builds.discard(build_id)
            return
        try:
            profile_row = (
                db.session.get(BuildProfile, build.build_profile_id)
                if build.build_profile_id else None
            )
            resolved = resolve_profile(profile_row)

            _check_cancelled(build)
            _phase_base_prep(build, resolved)
            _check_cancelled(build)
            _phase_loadbalancer(build, resolved)
            _check_cancelled(build)
            _phase_pull_images(build, resolved)
            _check_cancelled(build)
            primary = _phase_init(build, resolved)
            _check_cancelled(build)
            _phase_cni(build, resolved, primary)
            _check_cancelled(build)
            _phase_join_cp(build, primary)
            _check_cancelled(build)
            _phase_join_workers(build)
            _check_cancelled(build)
            _phase_verify(build, resolved, primary)
            _phase_onboard(build, primary)
            _check_cancelled(build)
            _phase_addons(build, resolved, primary)
            _check_cancelled(build)

            # Complete only if cancellation has not won a concurrent race.
            # A conditional UPDATE prevents a late cancel request from being
            # overwritten between the final refresh and this commit.
            completed = ClusterBuild.query.filter_by(
                id=build.id, status="building"
            ).update(
                {
                    "status": "completed",
                    "error": None,
                    "finished_at": _utcnow(),
                    # Join secrets are dead weight (and risk) once the cluster
                    # stands.
                    "cert_key_cipher": None,
                    "cert_key_expires_at": None,
                    "join_command_cipher": None,
                },
                synchronize_session=False,
            )
            if completed != 1:
                db.session.rollback()
                raise _Cancelled()
            db.session.commit()
        except _Cancelled:
            build.finished_at = _utcnow()
            db.session.commit()
        except _PhaseFailed as exc:
            build.status = "failed"
            build.error = scrub(str(exc))[:2000]
            build.finished_at = _utcnow()
            db.session.commit()
        except Exception as exc:  # noqa: BLE001 — never lose a build to a crash
            logger.exception("Cluster build %s crashed", build_id)
            build.status = "failed"
            build.error = scrub(f"Internal error: {exc}")[:2000]
            build.finished_at = _utcnow()
            db.session.commit()
        finally:
            with _active_lock:
                _active_builds.discard(build_id)
            # A retry can land in the narrow interval after this worker commits
            # "failed" but before it releases the in-flight claim. In that
            # case retry's launch observes the claim and returns; notice the
            # new "building" state here and hand it to a fresh worker.
            try:
                db.session.expire_all()
                latest = db.session.get(ClusterBuild, build_id)
                if latest is not None and latest.status == "building":
                    start_build_worker(build_id)
            except Exception:  # noqa: BLE001 — status is already persisted
                logger.exception(
                    "Could not check for a raced retry of cluster build %s",
                    build_id,
                )


def start_build_worker(build_id: int) -> None:
    """Launch (or resume) the phase machine for a build already in status
    'building'. Synchronous under TESTING, thread otherwise."""
    app = current_app._get_current_object()
    with _active_lock:
        if build_id in _active_builds:
            return
        _active_builds.add(build_id)
    if app.config.get("TESTING"):
        _run_build(app, build_id)
        return
    threading.Thread(
        target=_run_build,
        args=(app, build_id),
        name=f"cluster-build-{build_id}",
        daemon=True,
    ).start()


def advance_cluster_builds() -> None:
    """Scheduler tick: resume builds orphaned by a backend restart. A build in
    'building' with no live worker and a stale heartbeat gets its running steps
    reset to pending (completed steps stay completed) and is relaunched."""
    cutoff = _utcnow() - timedelta(minutes=_STALE_BUILD_MINUTES)

    # Builds abandoned mid-preflight (backend restart while probing) would
    # otherwise sit in 'preflighting' forever — a status the wizard can neither
    # edit nor re-preflight past.
    stuck = ClusterBuild.query.filter(ClusterBuild.status == "preflighting").all()
    for build in stuck:
        updated = build.updated_at
        if updated is not None and updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if updated is not None and updated > cutoff:
            continue
        build.status = "preflight_failed"
        build.error = (
            "Preflight was interrupted (backend restart). Re-run preflight."
        )
        db.session.commit()
        logger.info("Marked interrupted preflight failed for build %s (%s)",
                    build.id, build.name)

    candidates = ClusterBuild.query.filter(ClusterBuild.status == "building").all()
    for build in candidates:
        with _active_lock:
            if build.id in _active_builds:
                continue
        updated = build.updated_at
        if updated is not None and updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if updated is not None and updated > cutoff:
            continue
        ClusterBuildStep.query.filter_by(build_id=build.id, status="running").update(
            {"status": "pending"}
        )
        build.updated_at = _utcnow()
        db.session.commit()
        logger.info("Resuming orphaned cluster build %s (%s)", build.id, build.name)
        start_build_worker(build.id)


def reset_failed_nodes(build: ClusterBuild) -> List[str]:
    """Per-node retry cleanup: kubeadm reset + CNI/iptables scrub on nodes that
    failed, plus best-effort removal of a stale etcd member for a half-joined
    control plane. Returns the list of node labels that were reset."""
    profile_row = (
        db.session.get(BuildProfile, build.build_profile_id)
        if build.build_profile_id else None
    )
    resolved = resolve_profile(profile_row)
    cps, _, _ = _nodes_by_role(build)
    primary = cps[0] if cps else None
    reset_labels: List[str] = []
    for node in build.nodes:
        if node.status != "failed":
            continue
        label = node.hostname or node.address
        try:
            adapter = _adapter_for(node)
            get_transport().run(
                _target_for(build, node),
                adapter.script_reset_node(_script_ctx(build, resolved, node)),
                timeout_s=300,
            )
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.warning("Reset script failed on %s (continuing)", label)
        if (
            node.role == "control_plane"
            and primary is not None
            and node.id != primary.id
            and primary.status == "joined"
        ):
            member_name = node.hostname or node.address
            try:
                get_transport().run(
                    _target_for(build, primary),
                    # Remove the stale etcd member, else the rejoin fails on a
                    # duplicate member entry.
                    "set -e\n"
                    "ETCDCTL_API=3 kubectl --kubeconfig /etc/kubernetes/admin.conf "
                    "-n kube-system exec etcd-$(hostname) -- etcdctl "
                    "--endpoints=https://127.0.0.1:2379 "
                    "--cacert=/etc/kubernetes/pki/etcd/ca.crt "
                    "--cert=/etc/kubernetes/pki/etcd/server.crt "
                    "--key=/etc/kubernetes/pki/etcd/server.key "
                    f"member list | grep {member_name} | cut -d, -f1 | "
                    "xargs -r -I{} sh -c 'ETCDCTL_API=3 kubectl --kubeconfig "
                    "/etc/kubernetes/admin.conf -n kube-system exec etcd-$(hostname) "
                    "-- etcdctl --endpoints=https://127.0.0.1:2379 "
                    "--cacert=/etc/kubernetes/pki/etcd/ca.crt "
                    "--cert=/etc/kubernetes/pki/etcd/server.crt "
                    "--key=/etc/kubernetes/pki/etcd/server.key member remove {}'",
                    timeout_s=120,
                )
            except Exception:  # noqa: BLE001
                logger.warning("Stale etcd member removal failed for %s (continuing)", label)
        node.status = "pending"
        node.error = None
        reset_labels.append(label)
    db.session.commit()
    return reset_labels
