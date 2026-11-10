#!/usr/bin/env python3
"""KubeSight's provisioner: one OpenTofu step of one run, inside a Kubernetes Job.

KubeSight's backend never holds create or delete rights in Oracle Cloud. When
a build needs OCI resources made, changed or removed, the backend launches a
Job in the namespace ``kubesight`` running as the service account
``kubesight-provisioner`` (the only identity OCI's policy lets do that), from
a fixed template, with this program as its command. The Job's environment says
which run and which phase; everything else it asks the backend for.

Phases (``KUBESIGHT_PHASE``):

  plan   ``tofu plan -out``; sends the backend the encrypted plan file, its
         sha256, OpenTofu's text output (sensitive values already masked by
         OpenTofu) and the plan's resource changes with every sensitive value
         replaced. A person then approves that exact hash (K13).
  apply  fetches the approved plan, checks its sha256 against the approved
         one, applies that file only, then reports the cluster's facts (K22):
         never the state itself.
  bind   on a created cluster: binds the backend's subject to its named role,
         and installs the admission policy that keeps every LoadBalancer
         Service internal and flexible (K20, K21).

Credentials, all short-lived, none written to disk:

  * OCI: OKE workload identity (OCI_RESOURCE_PRINCIPAL_VERSION=2.2): the
    provider and the CLI exchange the pod's own service-account token at
    $KUBERNETES_SERVICE_HOST:12250.
  * The state key (K15): OpenBao's Transit engine. The Job signs in to OpenBao
    with its own service-account token; the key never leaves OpenBao.
  * The backend (K22): a projected service-account token whose audience is
    ``kubesight-backend``, re-read before every call. OpenTofu's HTTP state
    backend reaches the backend through a loopback proxy here that adds a
    fresh token to each request, so OpenTofu never sees one.

Never ``set -x``, never TF_LOG, never ``--debug`` (K23): debug output holds
signed request headers and tokens, and every pod log is archived.

Standard library only: the image's Python comes with the OCI CLI.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import http.server
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

BACKEND_TOKEN_FILE = os.environ.get("KUBESIGHT_BACKEND_TOKEN_FILE", "/var/run/kubesight/token")
SA_TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
LOCK_FILE = "/opt/kubesight/tofu/.terraform.lock.hcl"
PLAN_FILE = "job.tfplan"
PLAN_TEXT_LIMIT = 400_000
SENSITIVE = "(sensitive value)"
# Never inherited into OpenTofu or the CLI, whatever the pod's environment says (K23).
_DEBUG_ENV = ("TF_LOG", "TF_LOG_PATH", "TF_LOG_CORE", "TF_LOG_PROVIDER", "OCI_CLI_DEBUG")


class ProvisionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def say(message: str) -> None:
    print(message, flush=True)


def audit(event: str, **fields: Any) -> None:
    """One structured line for the platform's log store (K19). Never a token,
    key, kubeconfig or state: only the fields named here."""
    line = {"kubesight_audit": "provisioner", "event": event, "time": _now(), **fields}
    print(json.dumps(line, sort_keys=True, default=str), flush=True)


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------

def _backend_token() -> str:
    # The kubelet renews the projected token file; read it each time.
    with open(BACKEND_TOKEN_FILE, encoding="utf-8") as handle:
        token = handle.read().strip()
    if not token:
        raise ProvisionError(f"{BACKEND_TOKEN_FILE} is empty.")
    return token


class Backend:
    def __init__(self, base_url: str, run_id: int,
                 token: Callable[[], str] = _backend_token,
                 opener: Optional[Callable[..., Any]] = None):
        self.base = base_url.rstrip("/")
        self.run_id = int(run_id)
        self.token = token
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open

    def call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
             *, attempts: int = 4) -> Dict[str, Any]:
        url = f"{self.base}/api/internal/provisioner/runs/{self.run_id}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        last = ""
        for attempt in range(attempts):
            request = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
            try:
                with self.opener(request, timeout=60) as response:
                    raw = response.read()
                    return json.loads(raw.decode("utf-8")) if raw else {}
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")[:500]
                if exc.code < 500:
                    raise ProvisionError(f"The backend refused {method} {path or '/'} (HTTP {exc.code}): {text}") from exc
                last = f"HTTP {exc.code}: {text}"
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last = str(getattr(exc, "reason", exc))
            time.sleep(min(15, 2 * (attempt + 1)))
        raise ProvisionError(f"The backend did not answer {method} {path or '/'}: {last}")


class StateProxy:
    """A loopback HTTP proxy in front of the backend's OpenTofu state endpoint.

    OpenTofu's HTTP backend reads its password once, at start; the projected
    token lives ten minutes and an apply can take longer. The proxy adds the
    current token to every request, so OpenTofu never holds one."""

    def __init__(self, backend_base: str, token: Callable[[], str] = _backend_token,
                 username: str = "kubesight-provisioner"):
        target = urllib.parse.urlsplit(backend_base)
        self.scheme = target.scheme
        self.netloc = target.netloc
        self.prefix = target.path.rstrip("/")
        self.token = token
        self.username = username
        self.server: Optional[http.server.ThreadingHTTPServer] = None

    def _connection(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.netloc, timeout=120, context=ssl.create_default_context())
        return http.client.HTTPConnection(self.netloc, timeout=120)

    def start(self) -> str:
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # no access log: paths only, but keep the log clean
                return

            def _forward(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                basic = base64.b64encode(f"{proxy.username}:{proxy.token()}".encode()).decode()
                headers = {"Authorization": f"Basic {basic}"}
                if self.headers.get("Content-Type"):
                    headers["Content-Type"] = self.headers["Content-Type"]
                if body is not None:
                    headers["Content-Length"] = str(len(body))
                try:
                    connection = proxy._connection()
                    connection.request(self.command, proxy.prefix + self.path, body=body, headers=headers)
                    answer = connection.getresponse()
                    payload = answer.read()
                    status = answer.status
                    content_type = answer.getheader("Content-Type")
                    connection.close()
                except (OSError, http.client.HTTPException) as exc:
                    payload = f"backend unreachable: {exc}\n".encode()
                    status, content_type = 502, "text/plain"
                self.send_response(status)
                if content_type:
                    self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            do_GET = do_POST = do_DELETE = do_PUT = _forward

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()


# ---------------------------------------------------------------------------
# OpenBao (K15)
# ---------------------------------------------------------------------------

def openbao_login(encryption: Dict[str, Any]) -> str:
    """Sign in to OpenBao with this pod's own service-account token. Returns
    the client token; it is never printed and lives two hours."""
    address = str(encryption["address"]).rstrip("/")
    with open(SA_TOKEN_FILE, encoding="utf-8") as handle:
        jwt = handle.read().strip()
    request = urllib.request.Request(
        f"{address}/v1/{encryption.get('loginPath') or 'auth/kubernetes/login'}",
        data=json.dumps({"role": encryption["role"], "jwt": jwt}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    context = ssl.create_default_context(cafile=encryption.get("caFile") or None)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(request, timeout=30) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as exc:
        raise ProvisionError(
            f"OpenBao refused the sign-in for role {encryption['role']} (HTTP {exc.code}). "
            "Nothing was planned or applied."
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProvisionError(f"OpenBao did not answer at {address}: {getattr(exc, 'reason', exc)}. "
                             "Nothing was planned or applied.") from exc
    token = ((answer or {}).get("auth") or {}).get("client_token")
    if not token:
        raise ProvisionError("OpenBao's sign-in answer had no token.")
    return token


def encryption_config(encryption: Dict[str, Any]) -> str:
    """The body of OpenTofu's ``encryption`` block, for TF_ENCRYPTION: the
    openbao key provider (Transit), aes_gcm, enforced for state and plan."""
    key = encryption["keyName"]
    path = encryption.get("transitPath") or "/transit"
    for value in (key, path):
        if not all(c.isalnum() or c in "-_/." for c in value):
            raise ProvisionError(f"Refusing an odd OpenBao key or path: {value!r}")
    # A state written before encryption was switched on is read once through
    # the unencrypted fallback and written back encrypted (the backend says so).
    migrate = bool(encryption.get("migrateFromPlain"))
    return (
        'key_provider "openbao" "state" {\n'
        f'  key_name            = "{key}"\n'
        f'  transit_engine_path = "{path}"\n'
        "}\n"
        'method "aes_gcm" "state" {\n'
        "  keys = key_provider.openbao.state\n"
        "}\n"
        + ('method "unencrypted" "migrate" {}\n' if migrate else "")
        + "state {\n"
        "  method   = method.aes_gcm.state\n"
        # OpenTofu refuses enforced together with an unencrypted fallback
        # (tested with 1.13.9): the one migration run still WRITES encrypted
        # (aes_gcm is the method); every later run is enforced.
        + ("  fallback {\n    method = method.unencrypted.migrate\n  }\n" if migrate else "  enforced = true\n")
        + "}\n"
        "plan {\n"
        "  method   = method.aes_gcm.state\n"
        "  enforced = true\n"
        "}\n"
    )


# ---------------------------------------------------------------------------
# OpenTofu
# ---------------------------------------------------------------------------

def tofu_env(spec: Dict[str, Any], state_url: str, bao_token: Optional[str]) -> Dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _DEBUG_ENV}
    # Workload identity only (K2): no API-key variables may reach the provider.
    for key in ("OCI_TENANCY_OCID", "OCI_USER_OCID", "OCI_FINGERPRINT", "OCI_PRIVATE_KEY_PATH",
                "OCI_PRIVATE_KEY_PASSWORD", "TF_HTTP_PASSWORD", "TF_HTTP_USERNAME"):
        env.pop(key, None)
    build_id = int(spec["buildId"])
    base = f"{state_url}/api/internal/tofu-state/{build_id}"
    env.update({
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "CHECKPOINT_DISABLE": "1",
        "TF_HTTP_ADDRESS": base,
        "TF_HTTP_LOCK_ADDRESS": f"{base}/lock",
        "TF_HTTP_UNLOCK_ADDRESS": f"{base}/unlock",
        "TF_HTTP_LOCK_METHOD": "POST",
        "TF_HTTP_UNLOCK_METHOD": "POST",
        "TF_HTTP_RETRY_MAX": "5",
    })
    encryption = spec.get("encryption") or {}
    if encryption.get("enabled"):
        env["TF_ENCRYPTION"] = encryption_config(encryption)
        env["BAO_ADDR"] = str(encryption["address"])
        if encryption.get("caFile"):
            env["BAO_CACERT"] = str(encryption["caFile"])
        env["BAO_TOKEN"] = bao_token or ""
    return env


def run(args: List[str], env: Dict[str, str], cwd: str, *, echo: bool = True,
        timeout_s: int = 3600) -> Tuple[int, str]:
    """Run a command; stream its output to the log (``echo``) and return it."""
    process = subprocess.Popen(
        args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    lines: List[str] = []
    timer = threading.Timer(timeout_s, process.kill)
    timer.daemon = True
    timer.start()
    try:
        for line in process.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\n")
            lines.append(line)
            if echo:
                say(line)
        process.wait()
    finally:
        timer.cancel()
    return int(process.returncode or 0), "\n".join(lines)


def tofu(args: List[str], env: Dict[str, str], cwd: str, *, show: bool = True,
         echo: bool = True) -> Tuple[int, str]:
    if show:
        say(f"$ tofu {' '.join(args)}")
    return run(["tofu", *args], env, cwd, echo=echo)


def prepare_workdir(work: str, config: Dict[str, Any]) -> None:
    if os.path.isdir(work):
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "main.tf.json"), "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
    if os.path.exists(LOCK_FILE):
        shutil.copyfile(LOCK_FILE, os.path.join(work, ".terraform.lock.hcl"))


def init(env: Dict[str, str], work: str) -> None:
    args = ["init", "-input=false", "-no-color"]
    if os.path.exists(os.path.join(work, ".terraform.lock.hcl")):
        args.append("-lockfile=readonly")
    code, _ = tofu(args, env, work)
    if code:
        raise ProvisionError("OpenTofu could not initialise (see the lines above).")


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# What leaves the Job
# ---------------------------------------------------------------------------

def _mask(value: Any, marks: Any) -> Any:
    """``value`` with every part ``marks`` flags as sensitive replaced. The
    marks mirror the value's shape: True for sensitive, nested dict/list."""
    if marks is True:
        return SENSITIVE
    if isinstance(value, dict):
        sub = marks if isinstance(marks, dict) else {}
        return {key: _mask(item, sub.get(key)) for key, item in value.items()}
    if isinstance(value, list):
        sub = marks if isinstance(marks, list) else []
        return [_mask(item, sub[i] if i < len(sub) else None) for i, item in enumerate(value)]
    return value


def redact_plan(document: Dict[str, Any]) -> Dict[str, Any]:
    """The plan's resource changes, with every value OpenTofu marks sensitive
    replaced. Nothing else of the plan (variables, prior state, configuration)
    leaves the Job."""
    changes = []
    for change in document.get("resource_changes") or []:
        body = change.get("change") or {}
        changes.append({
            "address": change.get("address"),
            "mode": change.get("mode"),
            "type": change.get("type"),
            "name": change.get("name"),
            "index": change.get("index"),
            "change": {
                "actions": body.get("actions") or [],
                "before": _mask(body.get("before"), body.get("before_sensitive")),
                "after": _mask(body.get("after"), body.get("after_sensitive")),
            },
        })
    return {"format_version": document.get("format_version"), "resource_changes": changes}


def _resources(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def walk(module: Dict[str, Any]) -> None:
        out.extend(module.get("resources") or [])
        for child in module.get("child_modules") or []:
            walk(child)

    walk(((document.get("values") or {}).get("root_module")) or {})
    return [r for r in out if r.get("mode") == "managed"]


def facts_from_state(document: Dict[str, Any]) -> Dict[str, Any]:
    """The fixed list of facts the backend may know about what exists (K22).
    Read from ``tofu show -json`` of the state; only these fields leave."""
    resources = _resources(document)
    facts: Dict[str, Any] = {"resourceCount": len(resources), "cluster": None,
                             "nodePools": [], "nsgs": []}
    for item in resources:
        values = item.get("values") or {}
        rtype = item.get("type")
        if rtype == "oci_containerengine_cluster":
            endpoints = (values.get("endpoints") or [{}])[0] or {}
            private = str(endpoints.get("private_endpoint") or "")
            host, _, port = private.rpartition(":") if ":" in private else (private, "", "")
            facts["cluster"] = {
                "id": values.get("id"),
                "name": values.get("name"),
                "kubernetesVersion": values.get("kubernetes_version"),
                "type": values.get("type"),
                "state": values.get("state"),
                "privateEndpoint": {"address": host or None, "port": int(port) if port.isdigit() else None},
                "vcnId": values.get("vcn_id"),
                "endpointSubnetId": ((values.get("endpoint_config") or [{}])[0] or {}).get("subnet_id"),
                "serviceLbSubnetIds": ((values.get("options") or [{}])[0] or {}).get("service_lb_subnet_ids") or [],
                "cniType": (((values.get("cluster_pod_network_options") or [{}])[0]) or {}).get("cni_type"),
            }
        elif rtype == "oci_containerengine_node_pool":
            config = (values.get("node_config_details") or [{}])[0] or {}
            shape = (values.get("node_shape_config") or [{}])[0] or {}
            facts["nodePools"].append({
                "id": values.get("id"),
                "name": values.get("name"),
                "kubernetesVersion": values.get("kubernetes_version"),
                "shape": values.get("node_shape"),
                "ocpus": shape.get("ocpus"),
                "memoryGb": shape.get("memory_in_gbs"),
                "size": config.get("size"),
                "nodeCount": len(values.get("nodes") or []),
                "nodes": [
                    {"id": n.get("id"), "name": n.get("name"), "privateIp": n.get("private_ip"),
                     "state": n.get("state")}
                    for n in values.get("nodes") or [] if isinstance(n, dict)
                ],
            })
        elif rtype == "oci_core_network_security_group":
            facts["nsgs"].append({"id": values.get("id"), "name": values.get("display_name"),
                                  "address": item.get("address")})
    return facts


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def _start(spec: Dict[str, Any], backend_base: str) -> Tuple[str, StateProxy, Dict[str, str]]:
    work = os.path.join(os.environ.get("KUBESIGHT_WORKDIR", "/work"), f"run-{spec['runId']}")
    prepare_workdir(work, spec["config"])
    bao_token = None
    if (spec.get("encryption") or {}).get("enabled"):
        bao_token = openbao_login(spec["encryption"])
        say("Signed in to OpenBao for the state key (the key stays in OpenBao).")
    proxy = StateProxy(backend_base)
    env = tofu_env(spec, proxy.start(), bao_token)
    return work, proxy, env


def phase_plan(spec: Dict[str, Any], backend: Backend, backend_base: str) -> int:
    work, proxy, env = _start(spec, backend_base)
    try:
        init(env, work)
        args = ["plan", "-input=false", "-no-color", "-lock-timeout=60s", f"-out={PLAN_FILE}"]
        if spec.get("operation") == "destroy":
            args.insert(1, "-destroy")
        code, _ = tofu(args, env, work)
        if code:
            raise ProvisionError("OpenTofu could not make a plan (see the lines above).")
        code, shown = tofu(["show", "-json", PLAN_FILE], env, work, echo=False, show=False)
        if code:
            raise ProvisionError("OpenTofu could not read back its own plan.")
        document = json.loads(shown.strip().splitlines()[-1] if shown.strip() else "{}")
        code, text = tofu(["show", "-no-color", PLAN_FILE], env, work, echo=False, show=False)
        if code:
            raise ProvisionError("OpenTofu could not print its own plan.")
        with open(os.path.join(work, PLAN_FILE), "rb") as handle:
            plan_bytes = handle.read()
    finally:
        proxy.stop()
    digest = sha256_of(plan_bytes)
    backend.call("POST", "/plan", attempts=25, body={
        "planB64": base64.b64encode(plan_bytes).decode("ascii"),
        "planSha256": digest,
        "planText": text[:PLAN_TEXT_LIMIT],
        "plan": redact_plan(document),
        "encrypted": bool((spec.get("encryption") or {}).get("enabled")),
    })
    audit("plan", runId=spec["runId"], buildId=spec["buildId"], operation=spec.get("operation"),
          planSha256=digest, result="planned")
    say(f"Plan saved. Its sha256 is {digest}; it is applied only once someone else approves that hash.")
    return 0


def phase_apply(spec: Dict[str, Any], backend: Backend, backend_base: str) -> int:
    approved = str(spec.get("approvedPlanSha256") or "")
    plan_bytes = base64.b64decode(spec.get("planB64") or "")
    digest = sha256_of(plan_bytes)
    if not approved or digest != approved:
        audit("apply", runId=spec["runId"], buildId=spec["buildId"], planSha256=digest,
              approvedPlanSha256=approved or None, result="refused-hash-mismatch")
        raise ProvisionError(
            f"The saved plan's sha256 ({digest}) is not the approved one ({approved or 'none'}). "
            "Nothing was applied."
        )
    work, proxy, env = _start(spec, backend_base)
    code = 1
    try:
        with open(os.path.join(work, PLAN_FILE), "wb") as handle:
            handle.write(plan_bytes)
        init(env, work)
        say(f"Applying the approved plan {digest}.")
        code, _ = tofu(["apply", "-input=false", "-no-color", "-lock-timeout=60s", PLAN_FILE], env, work)
        errored = os.path.join(work, "errored.tfstate")
        if code and os.path.exists(errored):
            # OpenTofu could not save the state; keep it with the backend as an
            # opaque blob before the pod (and its emptyDir) goes.
            with open(errored, "rb") as handle:
                backend.call("POST", "/errored-state", {"stateB64": base64.b64encode(handle.read()).decode("ascii")})
            say("OpenTofu could not save its state; the errored state was handed to KubeSight.")
        report: Dict[str, Any] = {"result": "succeeded" if code == 0 else "failed", "planSha256": digest}
        shown_code, shown = tofu(["show", "-json"], env, work, echo=False, show=False)
        if shown_code == 0 and shown.strip():
            try:
                report["facts"] = facts_from_state(json.loads(shown.strip().splitlines()[-1]))
            except ValueError:
                report["factsError"] = "OpenTofu's state could not be read back."
        else:
            report["factsError"] = "OpenTofu's state could not be read back."
        backend.call("POST", "/facts", report, attempts=25)
        audit("apply" if spec.get("operation") != "destroy" else "destroy",
              runId=spec["runId"], buildId=spec["buildId"], planSha256=digest,
              clusterId=((report.get("facts") or {}).get("cluster") or {}).get("id"),
              result=report["result"])
    finally:
        proxy.stop()
    if code:
        raise ProvisionError("OpenTofu's apply failed (see the lines above).")
    return 0


def phase_bind(spec: Dict[str, Any], backend: Backend) -> int:
    """Bind the backend's subject to its named role on the created cluster,
    and install the admission policy for LoadBalancer Services (K20, K21)."""
    bind = spec.get("bind") or {}
    work = os.path.join(os.environ.get("KUBESIGHT_WORKDIR", "/work"), f"run-{spec['runId']}")
    os.makedirs(work, exist_ok=True)
    kubeconfig = os.path.join(work, "kubeconfig")
    env = {key: value for key, value in os.environ.items() if key not in _DEBUG_ENV}
    env["KUBECONFIG"] = kubeconfig
    code, _ = run([
        "oci", "ce", "cluster", "create-kubeconfig",
        "--cluster-id", bind["clusterId"], "--region", bind["region"],
        "--file", kubeconfig, "--token-version", "2.0.0",
        "--kube-endpoint", "PRIVATE_ENDPOINT", "--overwrite",
    ], env, work)
    if code:
        raise ProvisionError("The OCI CLI could not write a kubeconfig for the cluster.")
    deadline = time.monotonic() + int(bind.get("waitSeconds") or 600)
    while True:
        code, out = run(["kubectl", "version", "-o", "json", "--request-timeout=20s"], env, work, echo=False)
        if code == 0:
            break
        if time.monotonic() > deadline:
            raise ProvisionError("The cluster's API did not answer:\n" + out[-800:])
        say("Waiting for the cluster's API to answer...")
        time.sleep(15)
    _, whoami = run(["kubectl", "auth", "whoami", "-o", "json"], env, work, echo=False)
    try:
        provisioner_subject = (json.loads(whoami).get("status") or {}).get("userInfo", {}).get("username")
    except ValueError:
        provisioner_subject = None
    manifests = {"apiVersion": "v1", "kind": "List", "items": list(bind.get("manifests") or [])}
    path = os.path.join(work, "bind.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifests, handle)
    code, out = run(["kubectl", "apply", "--server-side", "--field-manager=kubesight-provisioner",
                     "--force-conflicts", "-f", path], env, work)
    result = {"ok": code == 0, "provisionerSubject": provisioner_subject,
              "output": out[-4000:]}
    backend.call("POST", "/bound", result, attempts=25)
    audit("bind", runId=spec["runId"], buildId=spec["buildId"], clusterId=bind.get("clusterId"),
          subject=bind.get("subject"), role=bind.get("roleName"), result="bound" if code == 0 else "failed")
    if code:
        raise ProvisionError("The cluster refused the role binding or the admission policy (see above).")
    return 0


def main() -> int:
    backend_base = os.environ["KUBESIGHT_BACKEND_URL"]
    run_id = int(os.environ["KUBESIGHT_RUN_ID"])
    phase = os.environ["KUBESIGHT_PHASE"]
    if phase not in ("plan", "apply", "bind"):
        say(f"Unknown phase {phase!r}.")
        return 2
    backend = Backend(backend_base, run_id)
    say(f"KubeSight provisioner: run {run_id}, phase {phase}.")
    try:
        spec = backend.call("GET", f"?phase={phase}")
        if spec.get("phase") != phase:
            raise ProvisionError("The backend has no such step waiting for this Job.")
        if phase == "plan":
            return phase_plan(spec, backend, backend_base)
        if phase == "apply":
            return phase_apply(spec, backend, backend_base)
        return phase_bind(spec, backend)
    except ProvisionError as exc:
        say(f"Error: {exc}")
        try:
            backend.call("POST", "/failed", {"phase": phase, "message": str(exc)[:4000]}, attempts=2)
        except ProvisionError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
