"""Repeatable per-endpoint benchmark of a running KubeSight (GET routes only).

Run against a server started by tools/stress/serve.py (or the frozen copy the
bench environment uses), so every request's SQL count and kubectl count can be
read back from the server's request log:

    python bench_endpoints.py --base http://127.0.0.1:5091 \
        --server-log <work>/server --out <results dir> \
        [--user stress-user-NN --user-password Stress-Test-1!]

Phases
  0. calibration: how long this machine takes to spawn kubectl / python (the
     dominant cost of every uncached Kubernetes read; record it so runs taken
     on a busy machine can be told apart from code changes).
  1. sequential: every GET route from the server's url_map (routes.json written
     by the bench serve.py, else a fresh import of the app), path parameters
     filled from list responses, streaming/download/side-effect routes skipped.
     For admin and for a non-admin user: 1 warm-up (= cold) call + N timed
     calls; status, median/max ms, bytes, SQL and kubectl counts per call.
  2. concurrency: closed-loop thread pool (no think time) over ~30 common
     dashboard/list endpoints at each --vus level for --duration seconds;
     throughput, p50/p95/p99, error rate, server-side queue wait.

Outputs <out>/results.json and <out>/report.md. compare_bench.py diffs two.
Python standard library only.
"""

from __future__ import annotations

import argparse
import gzip
import http.client
import json
import os
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Client:
    def __init__(self, base: str, timeout: float):
        b = base.replace("http://", "").rstrip("/")
        host, _, port = b.partition(":")
        self.host, self.port, self.timeout = host, int(port or 80), timeout

    def get(self, path: str, token: Optional[str] = None, tag: str = "", user: str = "",
            timeout: Optional[float] = None) -> Tuple[int, Any, float, int]:
        """(status, parsed json or None, ms, wire bytes). status 0 = transport error/timeout."""
        return self.request("GET", path, None, token, tag, user, timeout)

    def request(self, method: str, path: str, body: Any, token: Optional[str], tag: str = "", user: str = "",
                timeout: Optional[float] = None) -> Tuple[int, Any, float, int]:
        hdrs = {"Accept": "application/json", "Accept-Encoding": "gzip", "Connection": "close"}
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        if tag:
            hdrs["X-Stress-Page"] = tag
        if user:
            hdrs["X-Stress-User"] = user
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        started = time.perf_counter()
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
            enc = resp.getheader("Content-Encoding", "")
        except Exception:
            return 0, None, (time.perf_counter() - started) * 1000, 0
        finally:
            conn.close()
        ms = (time.perf_counter() - started) * 1000
        size = len(raw)
        try:
            if enc == "gzip":
                raw = gzip.decompress(raw)
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = None
        return status, parsed, ms, size

    def login(self, username: str, password: str) -> str:
        status, body, _, _ = self.request("POST", "/api/auth/login", {"username": username, "password": password}, None)
        if status != 200:
            raise RuntimeError(f"login failed for {username}: {status} {str(body)[:300]}")
        data = (body or {}).get("data") or {}
        token = data.get("token") or data.get("accessToken") or (data.get("session") or {}).get("token")
        if not token:
            raise RuntimeError(f"login for {username} returned no token: {json.dumps(body)[:300]}")
        return token


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def pct(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    k = max(0, min(len(v) - 1, int(round(p / 100.0 * (len(v) - 1)))))
    return v[k]


def items_of(body: Any) -> List[Dict[str, Any]]:
    """The first list of objects in a response (data itself, or data.<something>)."""
    data = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        preferred = ("items", "rows", "results", "services", "builds", "clusters", "namespaces", "pods",
                     "resources", "bundles", "requests", "users", "roles", "policies", "releases", "templates",
                     "apps", "clients", "applications", "blueprints", "runners", "pipelines", "triggers",
                     "webhooks", "connections", "integrations", "components", "entries", "versions", "runs",
                     "artifacts", "stages", "inventory", "workloads", "charts", "data")
        for key in preferred:
            val = data.get(key)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val
        for val in data.values():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val
    return []


def first(body: Any, *keys: str, where: Optional[Callable[[Dict[str, Any]], bool]] = None) -> Optional[str]:
    for item in items_of(body):
        if where and not where(item):
            continue
        for k in keys:
            v = item.get(k)
            if v not in (None, ""):
                return str(v)
    return None


# Routes never called: streams/follows, file downloads, exports, side-effect
# GETs (email approval links), secrets, the MCP endpoint, static files.
SKIP_PATTERNS = [
    (r"/stream$", "streaming"), (r"/download$", "download"), (r"/export$", "export"),
    (r"/approve$", "side-effect GET"), (r"/decline$", "side-effect GET"), (r"/secret$", "secret"),
    (r"/kubeconfig$", "credential download"), (r"^/api/mcp", "MCP protocol"), (r"^/api/internal/", "internal"),
    (r"^/assets/", "static"), (r"^/static/", "static"), (r"^/config\.js$", "static"), (r"^/$", "static"),
    (r"/code-scan/report$", "pdf report"), (r"/logs/download$", "download"),
]

# Query strings some routes need to do real work (filled with resolved values).
QUERY = {
    "/api/logs": "cluster={cluster_id}&namespace={namespace}&pod={pod_name}&tail=200",
    "/api/clusters/<cluster_id>/namespaces/<namespace>/pods/<pod_name>/containers/<container_name>/logs": "tailLines=200",
    "/api/inventory/actions/rollout-history": "cluster={cluster_id}&namespace={namespace}&workloadName={deployment_name}",
    "/api/inventory/workloads": "cluster={cluster_id}&namespace={namespace}",
    "/api/application-services/picker/deployments": "clusterId={cluster_id}&namespace={namespace}",
    "/api/application-services/picker/pods": "clusterId={cluster_id}&namespace={namespace}",
    "/api/application-services/picker/workloads": "clusterId={cluster_id}&namespace={namespace}",
    "/api/service-blueprints/pickers/namespaces": "clusterId={cluster_id}",
    "/api/service-blueprints/pickers/resources": "clusterId={cluster_id}&namespace={namespace}&kind=deployment",
    "/api/service-blueprints/pickers/cluster-resources": "clusterId={cluster_id}&kind=deployment",
    "/api/helm/releases": "cluster={cluster_id}",
    "/api/helm/releases/<release_name>": "cluster={cluster_id}&namespace={namespace}",
    "/api/clusters/<cluster_id>/namespaces": "",
    "/api/promotions/namespaces": "clusterId={cluster_id}",
    "/api/deployment-requests/eligibility": "cluster={cluster_id}&namespace={namespace}",
    "/api/dashboard/summary": "clusterId={cluster_id}",
    "/api/upgrades/info": "clusterId={cluster_id}",
    "/api/ci/deploy-targets": "clusterId={cluster_id}&namespace={namespace}",
    "/api/promotions/namespace-map": "clusterId={cluster_id}",
}

# The ~30 endpoints a dashboard/list-heavy day hits most (concurrency phase).
# Weight = relative frequency (dashboard + clusters + pods dominate in loadgen's PAGE_WEIGHTS).
HOT = [
    ("/api/auth/me", 8), ("/api/dashboard/summary", 18), ("/api/clusters", 10), ("/api/clusters/custom", 2),
    ("/api/clusters/<cluster_id>/overview", 3), ("/api/clusters/<cluster_id>/namespaces", 6),
    ("/api/clusters/<cluster_id>/nodes", 2), ("/api/clusters/<cluster_id>/pod-issues", 2),
    ("/api/clusters/<cluster_id>/namespaces/<namespace>/pods", 12),
    ("/api/clusters/<cluster_id>/namespaces/<namespace>/resources/<resource_type>", 8),
    ("/api/clusters/<cluster_id>/namespaces/<namespace>/events", 3),
    ("/api/inventory", 5), ("/api/helm/releases", 1), ("/api/deployment-requests/mine", 2),
    ("/api/change-bundles", 2), ("/api/approvals/inbox", 3), ("/api/approvals/count", 4),
    ("/api/alerts", 6), ("/api/alert-policies/history", 2), ("/api/alert-policies", 1),
    ("/api/ci/services", 4), ("/api/ci/services/<service_id>/summary", 3), ("/api/ci/services/<service_id>/builds", 4),
    ("/api/ci/services/<service_id>/pipelines", 1), ("/api/ci/builds", 2), ("/api/ci/shared-pipelines", 1),
    ("/api/audit-logs", 2), ("/api/users", 1), ("/api/settings", 2), ("/api/promotions/board", 1),
    ("/api/upgrades/info", 1),
]


# ---------------------------------------------------------------------------
# Path-parameter resolution
# ---------------------------------------------------------------------------


class Resolver:
    """Fills <params> from list responses (as admin), cached per source."""

    def __init__(self, client: Client, token: str, cluster_name: Optional[str], nonadmin_clusters: List[str]):
        self.c, self.token = client, token
        self.cache: Dict[str, Optional[str]] = {}
        self.cluster_name = cluster_name
        self.nonadmin_clusters = nonadmin_clusters
        self.log: Dict[str, str] = {}

    def _get(self, path: str) -> Any:
        # Generous timeout + one retry: on a slow machine a cold kubectl-backed
        # list can take minutes, and an unresolved id silently drops routes.
        for _ in range(2):
            status, body, _, _ = self.c.get(path, self.token, tag="bench-resolve", user="resolver", timeout=300)
            if status == 200:
                return body
            if status != 0:
                return None
        return None

    def value(self, name: str) -> Optional[str]:
        if name not in self.cache:
            fn = getattr(self, "src_" + name, None)
            try:
                self.cache[name] = fn() if fn else None
            except Exception as exc:  # resolution must never kill the run
                self.log[name] = f"error: {exc}"
                self.cache[name] = None
        return self.cache[name]

    # -- clusters / kubernetes ----------------------------------------------
    def src_cluster_id(self):
        body = self._get("/api/clusters/custom")
        items = [{"id": str(c.get("publicId") or f"custom-{c.get('id')}"), "name": c.get("name")} for c in items_of(body)]
        allowed = set(self.nonadmin_clusters)
        for it in items:
            cid = str(it.get("id") or "")
            if self.cluster_name and it.get("name") == self.cluster_name and (not allowed or cid in allowed):
                return cid
        for it in items:
            cid = str(it.get("id") or "")
            if cid.startswith("custom-") and (not allowed or cid in allowed):
                return cid
        return None

    def namespaces_of(self, cluster: str) -> List[str]:
        body = self._get(f"/api/clusters/{quote(cluster)}/namespaces?lite=1")
        names = [str(it.get("name")) for it in items_of(body) if it.get("name")]
        if not names and isinstance(body, dict) and isinstance(body.get("data"), list):
            names = [str(x) for x in body["data"] if isinstance(x, str)]
        return [n for n in names if not n.startswith("kube-") and n not in ("default",)] or names

    def src_namespace(self):
        cid = self.value("cluster_id")
        if not cid:
            return None
        # The first application namespace that actually has pods and deployments.
        for ns in self.namespaces_of(cid)[:4]:
            pods = self._get(f"/api/clusters/{quote(cid)}/namespaces/{quote(ns)}/pods")
            if items_of(pods):
                self.cache["_pods_body"] = json.dumps(pods)
                return ns
        return None

    def _pod(self) -> Optional[Dict[str, Any]]:
        self.value("namespace")
        raw = self.cache.get("_pods_body")
        items = items_of(json.loads(raw)) if raw else []
        running = [p for p in items if str(p.get("status", "")).lower() == "running"]
        return (running or items or [None])[0]

    def src_pod_name(self):
        pod = self._pod()
        return pod and str(pod.get("name"))

    def src_container_name(self):
        pod = self._pod()
        if not pod:
            return None
        containers = pod.get("containers") or []
        if containers and isinstance(containers[0], dict):
            return str(containers[0].get("name"))
        if containers and isinstance(containers[0], str):
            return containers[0]
        cid, ns = self.value("cluster_id"), self.value("namespace")
        body = self._get(f"/api/clusters/{quote(cid)}/namespaces/{quote(ns)}/pods/{quote(str(pod.get('name')))}/containers")
        return first(body, "name")

    def src_resource_type(self):
        return "deployments"

    def src_resource_kind(self):
        return "deployment"

    def src_deployment_name(self):
        cid, ns = self.value("cluster_id"), self.value("namespace")
        if not (cid and ns):
            return None
        return first(self._get(f"/api/clusters/{quote(cid)}/namespaces/{quote(ns)}/resources/deployments"), "name")

    def src_resource_name(self):
        return self.value("deployment_name")

    def src_release_name(self):
        cid = self.value("cluster_id")
        return first(self._get(f"/api/helm/releases?cluster={quote(cid or '')}"), "name", "release")

    # -- CI -------------------------------------------------------------------
    def src_ci_service(self):
        # A service with builds (the detail pages people open).
        return first(self._get("/api/ci/services"), "id", where=lambda s: (s.get("kind") or "service") != "pipeline")

    def src_ci_build(self):
        sid = self.value("ci_service")
        return first(self._get(f"/api/ci/services/{sid}/builds"), "id") or first(self._get("/api/ci/builds"), "id")

    def src_ci_stage(self):
        bid = self.value("ci_build")
        body = self._get(f"/api/ci/builds/{bid}")
        data = (body or {}).get("data") or {}
        build = data.get("build", data) if isinstance(data, dict) else {}
        stages = build.get("stages") or data.get("stages") or []
        return str(stages[0].get("id")) if stages else None

    def src_ci_artifact(self):
        return first(self._get(f"/api/ci/builds/{self.value('ci_build')}/artifacts"), "id")

    def src_ci_trigger(self):
        return first(self._get(f"/api/ci/services/{self.value('ci_service')}/webhooks"), "id")

    def src_ci_pipeline(self):
        return first(self._get(f"/api/ci/services/{self.value('ci_service')}/pipelines"), "id")

    def src_ci_check(self):
        return first(self._get(f"/api/ci/services/{self.value('ci_service')}/merge-checks/runs"), "id")

    def src_ci_analysis(self):
        return first(self._get(f"/api/ci/services/{self.value('ci_service')}/analyses"), "id")

    def src_runner_id(self):
        return first(self._get("/api/ci/runners"), "id")

    def src_home_id(self):
        return first(self._get("/api/ci/shared-pipelines"), "id")

    # -- everything else ------------------------------------------------------
    def src_cluster_build(self):
        return first(self._get("/api/cluster-builds"), "id")

    def src_bundle_id(self):
        return first(self._get("/api/change-bundles"), "id")

    def src_item_id(self):
        body = self._get(f"/api/change-bundles/{self.value('bundle_id')}")
        data = (body or {}).get("data") or {}
        bundle = data.get("bundle", data) if isinstance(data, dict) else {}
        items = bundle.get("items") or data.get("items") or []
        return str(items[0].get("id")) if items else None

    def src_client_id(self):
        return first(self._get("/api/clients"), "id")

    def src_client_service(self):
        return first(self._get(f"/api/clients/{self.value('client_id')}/services"), "id", "serviceId")

    def src_app_service_id(self):
        return first(self._get("/api/app-services"), "id")

    def src_application_service(self):
        return first(self._get("/api/application-services"), "id")

    def src_app_id(self):
        return first(self._get("/api/mobile-apps"), "id")

    def src_application_id(self):
        return first(self._get("/api/applications"), "id")

    def src_app_analysis(self):
        return first(self._get(f"/api/applications/{self.value('application_id')}/analyses"), "id")

    def src_connection_id(self):
        return first(self._get("/api/vsphere-connections"), "id")

    def src_user_id(self):
        return first(self._get("/api/users"), "id", where=lambda u: str(u.get("username", "")).startswith("stress-user"))

    def src_role_id(self):
        return first(self._get("/api/roles"), "id")

    def src_policy_id(self):
        return first(self._get("/api/alert-policies"), "id")

    def src_key(self):
        return first(self._get("/api/integrations"), "key", "id")

    def src_inventory_id(self):
        return first(self._get("/api/inventory"), "id", "inventoryId")

    def src_version_id(self):
        inv = self.value("inventory_id")
        return inv and first(self._get(f"/api/inventory/{quote(inv, safe='')}/versions"), "id")

    def src_template_id(self):
        return first(self._get("/api/inventory/deploy/wizard/templates"), "id")

    def src_entry_id(self):
        return None

    def src_blueprint_id(self):
        return first(self._get("/api/service-blueprints"), "id")

    def src_component_id(self):
        return first(self._get("/api/topology-components"), "id")

    def src_release_id(self):
        return first(self._get("/api/promotions/releases"), "id")

    def src_slug(self):
        return first(self._get("/api/helm/chart-templates"), "slug", "id")

    def src_provider_key(self):
        return "zoho"

    def src_request_id(self):
        return first(self._get("/api/deployment-requests"), "id")


# Which resolver source fills a parameter, by route prefix (first match wins).
PARAM_SOURCES: List[Tuple[str, str, str]] = [
    (r"^/api/cluster-builds/<int:build_id>", "build_id", "cluster_build"),
    (r"^/api/ci/", "build_id", "ci_build"),
    (r"^/api/ci/", "stage_id", "ci_stage"),
    (r"^/api/ci/", "artifact_id", "ci_artifact"),
    (r"^/api/ci/", "trigger_id", "ci_trigger"),
    (r"^/api/ci/", "pipeline_id", "ci_pipeline"),
    (r"^/api/ci/", "check_id", "ci_check"),
    (r"^/api/ci/analyses/", "analysis_id", "ci_analysis"),
    (r"^/api/ci/", "service_id", "ci_service"),
    (r"^/api/ci/", "ci_service_id", "ci_service"),
    (r"^/api/application-services/", "service_id", "application_service"),
    (r"^/api/clients/", "service_id", "client_service"),
    (r"^/api/application-analyses/", "analysis_id", "app_analysis"),
    (r".", "cluster_id", "cluster_id"),
]


def resolve_path(rule: str, args: List[str], res: Resolver) -> Tuple[Optional[str], Dict[str, str], str]:
    """(concrete path or None, values used, reason if unresolved)."""
    values: Dict[str, str] = {}
    for arg in args:
        source = next((src for pat, a, src in PARAM_SOURCES if a == arg and re.search(pat, rule)), arg)
        v = res.value(source)
        if v in (None, "", "None"):
            return None, values, f"no value for <{arg}> (source {source})"
        values[arg] = v
    path = rule
    for m in re.finditer(r"<(?:(\w+):)?(\w+)>", rule):
        conv, name = m.group(1), m.group(2)
        v = values[name]
        path = path.replace(m.group(0), v if conv == "path" else quote(v, safe=""), 1)
    q = QUERY.get(rule)
    if q:
        need = re.findall(r"\{(\w+)\}", q)
        extra = {n: res.value(n) or "" for n in need}
        path += "?" + q.format(**{k: quote(v, safe="") for k, v in extra.items()})
    return path, values, ""


# ---------------------------------------------------------------------------
# Server log
# ---------------------------------------------------------------------------


class ServerLog:
    def __init__(self, server_dir: Optional[str]):
        self.path = os.path.join(server_dir, "requests.jsonl") if server_dir else None
        self.offset = os.path.getsize(self.path) if self.path and os.path.exists(self.path) else 0

    def read_new(self) -> List[Dict[str, Any]]:
        if not self.path or not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self.offset)
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
            self.offset = fh.tell()
        return out


def load_routes(server_dir: Optional[str], backend: Optional[str]) -> Tuple[List[Dict[str, Any]], str]:
    if server_dir and os.path.exists(os.path.join(server_dir, "routes.json")):
        with open(os.path.join(server_dir, "routes.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data["routes"], data.get("backend", "")
    if not backend:
        raise SystemExit("no routes.json in --server-log and no --backend to import the app from")
    code = (
        "import os,sys,json;sys.path.insert(0,sys.argv[1]);os.environ['KUBECONFIG']=os.devnull;"
        "from api import create_app\n"
        "class C:\n TESTING=True\n SQLALCHEMY_DATABASE_URI='sqlite:///:memory:'\n SQLALCHEMY_TRACK_MODIFICATIONS=False\n JWT_SECRET_KEY='x'\n"
        "app=create_app(C)\n"
        "print(json.dumps([{'rule':r.rule,'endpoint':r.endpoint,'args':sorted(r.arguments)} for r in app.url_map.iter_rules() if 'GET' in r.methods]))"
    )
    out = subprocess.run([sys.executable, "-c", code, backend], capture_output=True, text=True, timeout=300)
    return json.loads(out.stdout.strip().splitlines()[-1]), backend


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


def calibrate(samples: int = 3) -> Dict[str, Any]:
    def timed(cmd: List[str]) -> Optional[float]:
        try:
            t = time.perf_counter()
            subprocess.run(cmd, capture_output=True, timeout=120)
            return round((time.perf_counter() - t) * 1000, 1)
        except Exception:
            return None

    kubectl = shutil.which("kubectl")
    out: Dict[str, Any] = {"kubectl_path": kubectl, "python": sys.executable}
    if kubectl:
        ks = [timed([kubectl, "version", "--client", "-o", "json"]) for _ in range(samples)]
        out["kubectl_spawn_ms"] = ks
        out["kubectl_spawn_median_ms"] = statistics.median([k for k in ks if k is not None] or [0])
    ps = [timed([sys.executable, "-c", "pass"]) for _ in range(samples)]
    out["python_spawn_ms"] = ps
    out["python_spawn_median_ms"] = statistics.median([p for p in ps if p is not None] or [0])
    return out


def sequential(client: Client, users: List[Dict[str, Any]], endpoints: List[Dict[str, Any]], n: int,
               slog: ServerLog, progress: bool) -> List[Dict[str, Any]]:
    results = []
    seq = 0
    total = len(endpoints) * len(users)
    done = 0
    for user in users:
        for ep in endpoints:
            done += 1
            calls = []
            for i in range(n + 1):
                seq += 1
                tag = f"bench-{seq}"
                status, _, ms, size = client.get(ep["path"], user["token"], tag=tag, user=user["username"])
                calls.append({"tag": tag, "status": status, "ms": round(ms, 1), "bytes": size, "warmup": i == 0})
            results.append({"rule": ep["rule"], "path": ep["path"], "role": user["role"], "user": user["username"], "calls": calls})
            if progress:
                worst = max(c["ms"] for c in calls)
                print(f"[seq {done}/{total}] {user['role']:8} {calls[-1]['status']} {worst:8.0f}ms  {ep['rule']}", flush=True)
    # Join with the server's own accounting (SQL / kubectl per request).
    by_tag = {r.get("page"): r for r in slog.read_new() if str(r.get("page", "")).startswith("bench-")}
    for r in results:
        for c in r["calls"]:
            s = by_tag.get(c["tag"])
            if s:
                c.update({"sql": s.get("sql"), "sql_ms": s.get("sql_ms"), "kubectl": s.get("kubectl"),
                          "kubectl_ms": s.get("kubectl_ms"), "server_ms": s.get("ms")})
        timed = [c for c in r["calls"] if not c["warmup"]]
        warm = r["calls"][0]
        ms = [c["ms"] for c in timed]
        statuses = sorted({c["status"] for c in r["calls"]})
        def med(key):
            vals = [c.get(key) for c in timed if c.get(key) is not None]
            return statistics.median(vals) if vals else None
        r.update({
            "status": timed[-1]["status"] if timed else warm["status"], "statuses": statuses,
            "median_ms": round(statistics.median(ms), 1) if ms else None, "max_ms": round(max(ms), 1) if ms else None,
            "min_ms": round(min(ms), 1) if ms else None, "cold_ms": warm["ms"], "bytes": timed[-1]["bytes"] if timed else warm["bytes"],
            "sql": med("sql"), "sql_max": max((c.get("sql") or 0) for c in r["calls"]), "kubectl": med("kubectl"),
            "kubectl_cold": warm.get("kubectl"), "sql_cold": warm.get("sql"),
        })
    return results


def concurrency(client: Client, users: List[Dict[str, Any]], hot: List[Tuple[str, int, List[str]]],
                vus: int, duration: float, slog: ServerLog, seed: int) -> Dict[str, Any]:
    """Closed loop: each VU fires its next request as soon as the previous answers."""
    rng_master = random.Random(seed + vus)
    recs: List[Tuple[float, float, int, str]] = []
    lock = threading.Lock()
    stop_at = time.perf_counter() + duration
    tag = f"conc-{vus}"
    weights = [w for _, w, _ in hot]

    def vu(idx: int) -> None:
        rng = random.Random(rng_master.random() + idx)
        user = users[idx % len(users)]
        while time.perf_counter() < stop_at:
            rule, _, paths = rng.choices(hot, weights=weights)[0]
            path = rng.choice(paths)
            t0 = time.perf_counter()
            status, _, ms, _ = client.get(path, user["token"], tag=tag, user=user["username"])
            with lock:
                recs.append((t0, ms, status, rule))

    started = time.perf_counter()
    threads = [threading.Thread(target=vu, args=(i,), daemon=True) for i in range(vus)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=duration + client.timeout + 30)
    wall = time.perf_counter() - started
    server = [r for r in slog.read_new() if r.get("page") == tag]
    ms_all = [r[1] for r in recs]
    errors = [r for r in recs if r[2] == 0 or r[2] >= 500]
    client_4xx = [r for r in recs if 400 <= r[2] < 500]
    per_rule: Dict[str, Dict[str, Any]] = {}
    for rule, _, _ in hot:
        sub = [r for r in recs if r[3] == rule]
        if not sub:
            continue
        lat = [r[1] for r in sub]
        per_rule[rule] = {"n": len(sub), "p50": round(pct(lat, 50), 1), "p95": round(pct(lat, 95), 1),
                          "errors": sum(1 for r in sub if r[2] == 0 or r[2] >= 500)}
    queue = [r.get("queue_ms", 0) for r in server]
    return {
        "vus": vus, "duration_s": duration, "wall_s": round(wall, 1), "requests": len(recs),
        "throughput_rps": round(len(recs) / wall, 2) if wall else 0,
        "p50_ms": round(pct(ms_all, 50), 1), "p95_ms": round(pct(ms_all, 95), 1), "p99_ms": round(pct(ms_all, 99), 1),
        "max_ms": round(max(ms_all), 1) if ms_all else 0,
        "errors": len(errors), "error_rate": round(len(errors) / len(recs), 4) if recs else 0,
        "timeouts_or_transport": sum(1 for r in recs if r[2] == 0), "http_5xx": sum(1 for r in recs if r[2] >= 500),
        "http_4xx": len(client_4xx),
        "server_queue_p50_ms": round(pct(queue, 50), 1), "server_queue_p95_ms": round(pct(queue, 95), 1),
        "server_sql_total": sum(r.get("sql", 0) for r in server), "server_kubectl_total": sum(r.get("kubectl", 0) for r in server),
        "per_endpoint": per_rule,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def fmt(v: Any, nd: int = 0) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def write_report(out: str, res: Dict[str, Any]) -> None:
    rows = res["sequential"]
    L: List[str] = []
    meta = res["meta"]
    L.append(f"# KubeSight endpoint benchmark\n")
    L.append(f"- when: {meta['started']}  backend: `{meta['backend']}`  git: `{meta.get('git', '')}`")
    L.append(f"- users: admin=`{meta['admin_user']}`, non-admin=`{meta['nonadmin_user']}` ({meta.get('nonadmin_role', '')}, "
             f"{len(meta.get('nonadmin_clusters', []))} clusters)  cluster under test: `{meta.get('cluster_id')}`  N={meta['n']} (+1 warm-up)")
    cal = res.get("calibration", {})
    L.append(f"- machine calibration: kubectl spawn median {fmt(cal.get('kubectl_spawn_median_ms'))} ms "
             f"{cal.get('kubectl_spawn_ms')}, python spawn median {fmt(cal.get('python_spawn_median_ms'))} ms")
    L.append(f"- server env: {meta.get('env')}")
    L.append(f"- endpoints measured: {len(rows) // max(1, len(meta['roles']))} per user; skipped: {len(res['skipped'])}\n")

    def table(title: str, sel: List[Dict[str, Any]]) -> None:
        L.append(f"## {title}\n")
        L.append("| # | role | endpoint | status | median ms | max ms | cold ms | SQL | SQL cold | kubectl | kubectl cold | KB |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(sel, 1):
            L.append(f"| {i} | {r['role']} | `{r['rule']}` | {'/'.join(map(str, r['statuses']))} | {fmt(r['median_ms'])} | {fmt(r['max_ms'])} | "
                     f"{fmt(r['cold_ms'])} | {fmt(r['sql'])} | {fmt(r['sql_cold'])} | {fmt(r['kubectl'])} | {fmt(r['kubectl_cold'])} | {r['bytes'] / 1024:.1f} |")
        L.append("")

    table("Slowest (median of timed calls)", sorted(rows, key=lambda r: -(r["median_ms"] or 0))[:40])
    table("Slowest cold call (warm-up)", sorted(rows, key=lambda r: -(r["cold_ms"] or 0))[:25])
    table("Most SQL queries per request (median)", sorted(rows, key=lambda r: -(r["sql"] or 0))[:30])
    bad = [r for r in rows if any(s >= 500 or s == 0 for s in r["statuses"])]
    table("5xx / transport errors", bad)
    flaky = [r for r in rows if len(r["statuses"]) > 1 or ((r["median_ms"] or 0) > 50 and (r["max_ms"] or 0) > 3 * (r["median_ms"] or 1))]
    table("Flaky (status changed across calls, or max > 3x median)", flaky)

    conc = res.get("concurrency") or []
    if conc:
        L.append("## Concurrency (closed loop, no think time)\n")
        L.append("| VUs | requests | req/s | p50 ms | p95 ms | p99 ms | max ms | errors | error rate | 5xx | timeouts | 4xx | queue p95 ms | SQL total | kubectl total |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for c in conc:
            L.append(f"| {c['vus']} | {c['requests']} | {c['throughput_rps']} | {c['p50_ms']} | {c['p95_ms']} | {c['p99_ms']} | {c['max_ms']} | "
                     f"{c['errors']} | {c['error_rate'] * 100:.2f}% | {c['http_5xx']} | {c['timeouts_or_transport']} | {c['http_4xx']} | "
                     f"{c['server_queue_p95_ms']} | {c['server_sql_total']} | {c['server_kubectl_total']} |")
        L.append("")
        for c in conc:
            L.append(f"### {c['vus']} VUs per endpoint\n")
            L.append("| endpoint | n | p50 | p95 | errors |")
            L.append("|---|---|---|---|---|")
            for rule, s in sorted(c["per_endpoint"].items(), key=lambda kv: -kv[1]["p95"]):
                L.append(f"| `{rule}` | {s['n']} | {s['p50']} | {s['p95']} | {s['errors']} |")
            L.append("")

    table("All endpoints (by rule)", sorted(rows, key=lambda r: (r["rule"], r["role"])))
    L.append("## Skipped routes\n")
    for s in res["skipped"]:
        L.append(f"- `{s['rule']}`: {s['reason']}")
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def save(out: str, result: Dict[str, Any]) -> None:
    with open(os.path.join(out, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1)
    write_report(out, result)


def pick_nonadmin(client: Client, admin_token: str, password: str, prefer_cluster_name: Optional[str]) -> Tuple[str, str, List[str]]:
    """An active operator (else viewer) stress user with access to the preferred cluster and 2-4 clusters total."""
    _, cbody, _, _ = client.get("/api/clusters/custom", admin_token, tag="bench-setup")
    by_name = {str(c.get("name")): str(c.get("publicId") or f"custom-{c.get('id')}") for c in items_of(cbody)}
    want = by_name.get(prefer_cluster_name or "")
    status, body, _, _ = client.get("/api/users", admin_token, tag="bench-setup")
    users = [u for u in items_of(body) if str(u.get("username", "")).startswith("stress-user-")
             and u.get("isActive", True) and not u.get("isLocked") and not u.get("mfaEnabled") and not u.get("mustChangePassword")]
    for role_name in ("operator", "viewer", "cluster_admin"):
        cands = [u for u in users if (u.get("role") if isinstance(u.get("role"), str) else (u.get("role") or {}).get("name")) == role_name]
        for u in sorted(cands, key=lambda x: x["username"]):
            access = [str(x) for x in (u.get("clusterAccess") or [])]
            if not (2 <= len(access) <= 4) or (want and want not in access):
                continue
            try:
                client.login(u["username"], password)
            except Exception:
                continue
            return u["username"], role_name, sorted(access)
    raise SystemExit("no suitable non-admin stress user found (run seed_volume first)")


def git_rev(backend: str) -> str:
    marker = os.path.join(os.path.dirname(os.path.abspath(backend)), "COMMIT.txt")  # exported (git archive) trees
    if os.path.exists(marker):
        with open(marker, encoding="utf-8") as fh:
            return fh.read().strip()
    try:
        return subprocess.run(["git", "-C", backend, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=60).stdout.strip() + (
            " (dirty)" if subprocess.run(["git", "-C", backend, "status", "--porcelain", "--untracked-files=no"],
                                         capture_output=True, text=True, timeout=60).stdout.strip() else "")
    except Exception:
        return ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:5091")
    ap.add_argument("--server-log", help="serve.py --out dir (requests.jsonl, routes.json)")
    ap.add_argument("--backend", help="backend dir to import routes from when routes.json is missing")
    ap.add_argument("--out", required=True)
    ap.add_argument("--admin-user", default="admin")
    ap.add_argument("--admin-password", default="admin123")
    ap.add_argument("--user", help="non-admin username (default: auto-pick an operator stress user)")
    ap.add_argument("--user-password", default="Stress-Test-1!")
    ap.add_argument("--cluster", default="prod-large", help="cluster name used for cluster-scoped routes")
    ap.add_argument("-n", type=int, default=5, help="timed calls per endpoint (after 1 warm-up)")
    ap.add_argument("--vus", default="25,50")
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--only", help="regex: only routes matching")
    ap.add_argument("--skip-sequential", action="store_true")
    ap.add_argument("--skip-concurrency", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--resolve-only", action="store_true", help="print the endpoint list and exit")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    client = Client(args.base, args.timeout)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    routes, backend = load_routes(args.server_log, args.backend)
    slog = ServerLog(args.server_log)

    print("[bench] calibrating process spawn cost...", flush=True)
    cal = calibrate()
    print(f"[bench] kubectl spawn {cal.get('kubectl_spawn_ms')} ms, python spawn {cal.get('python_spawn_ms')} ms", flush=True)

    admin_token = client.login(args.admin_user, args.admin_password)
    if args.user:
        tok = client.login(args.user, args.user_password)
        _, cb, _, _ = client.get("/api/clusters/custom", tok, tag="bench-setup")
        na_user, na_role, na_clusters = args.user, "user", [str(c.get("publicId") or f"custom-{c.get('id')}") for c in items_of(cb)]
    else:
        na_user, na_role, na_clusters = pick_nonadmin(client, admin_token, args.user_password, args.cluster)
    na_token = client.login(na_user, args.user_password)
    print(f"[bench] non-admin: {na_user} ({na_role}) clusters={na_clusters}", flush=True)

    res = Resolver(client, admin_token, args.cluster, na_clusters)
    endpoints, skipped = [], []
    for r in sorted(routes, key=lambda x: x["rule"]):
        rule = r["rule"]
        if args.only and not re.search(args.only, rule):
            continue
        reason = next((why for pat, why in SKIP_PATTERNS if re.search(pat, rule)), None)
        if reason:
            skipped.append({"rule": rule, "reason": reason})
            continue
        if rule.startswith("/api/") or rule == "/health":
            path, _, why = resolve_path(rule, r["args"], res)
            if path is None:
                skipped.append({"rule": rule, "reason": why})
                continue
            endpoints.append({"rule": rule, "path": path})
    print(f"[bench] {len(endpoints)} endpoints to measure, {len(skipped)} skipped", flush=True)
    if args.resolve_only:
        for e in endpoints:
            print("  RUN ", e["path"])
        for s_ in skipped:
            print("  SKIP", s_["rule"], "-", s_["reason"])
        print("resolved:", {k: v for k, v in res.cache.items() if not k.startswith("_")}, res.log)
        return

    users = [
        {"username": args.admin_user, "role": "admin", "token": admin_token},
        {"username": na_user, "role": "nonadmin", "token": na_token},
    ]
    result: Dict[str, Any] = {
        "meta": {"started": started, "base": args.base, "backend": backend, "git": git_rev(backend) if backend else "",
                 "admin_user": args.admin_user, "nonadmin_user": na_user, "nonadmin_role": na_role,
                 "nonadmin_clusters": na_clusters, "cluster_id": res.value("cluster_id"), "namespace": res.value("namespace"),
                 "n": args.n, "roles": ["admin", "nonadmin"], "platform": platform.platform(),
                 "env": {k: os.environ.get(k) for k in ("KUBESIGHT_DIRECT_API_READS", "ALERT_POLICY_SCHEDULER",
                                                        "COMPONENT_HEALTH_AUTO_REFRESH", "CACHE_WARM_ENABLED")},
                 "resolved": {k: v for k, v in res.cache.items() if not k.startswith("_")}},
        "calibration": cal, "skipped": skipped, "sequential": [], "concurrency": [],
    }
    if not args.skip_sequential:
        t0 = time.perf_counter()
        result["sequential"] = sequential(client, users, endpoints, args.n, slog, not args.quiet)
        result["meta"]["sequential_s"] = round(time.perf_counter() - t0, 1)
        result["meta"]["resolved"] = {k: v for k, v in res.cache.items() if not k.startswith("_")}
        save(args.out, result)  # checkpoint: the sequential phase alone is useful if concurrency is interrupted

    if not args.skip_concurrency:
        # Build the hot set: cluster-scoped routes spread over every cluster the
        # user may see (3 app namespaces each), others as resolved above.
        per_cluster_ns: Dict[str, List[str]] = {}
        all_clusters = [str(c.get("publicId") or f"custom-{c.get('id')}")
                        for c in items_of(client.get("/api/clusters/custom", admin_token, tag="bench-setup")[1])]
        for cid in all_clusters:
            per_cluster_ns[cid] = res.namespaces_of(cid)[:3]
        hot: List[Tuple[str, int, List[str]]] = []
        for rule, w in HOT:
            args_in = re.findall(r"<(?:\w+:)?(\w+)>", rule)
            q = QUERY.get(rule, "")
            if "cluster_id" in args_in or "{cluster_id}" in q:
                paths = []
                for cid in (na_clusters or all_clusters):
                    for ns in (per_cluster_ns.get(cid) or [""])[: (3 if ("namespace" in args_in or "{namespace}" in q) else 1)]:
                        p = rule.replace("<cluster_id>", quote(cid, safe="")).replace("<namespace>", quote(ns, safe=""))
                        p = p.replace("<resource_type>", "deployments")
                        if q:
                            p += "?" + q.format(cluster_id=quote(cid, safe=""), namespace=quote(ns, safe=""))
                        paths.append(p)
            else:
                p, _, why = resolve_path(rule, args_in, res)
                paths = [p] if p else []
            if paths:
                hot.append((rule, w, paths))
        result["meta"]["hot_endpoints"] = [h[0] for h in hot]
        conc_users = [users[0], users[1]]  # alternate admin / non-admin VUs
        for level in [int(x) for x in args.vus.split(",") if x.strip()]:
            print(f"[bench] concurrency {level} VUs for {args.duration:.0f}s ...", flush=True)
            c = concurrency(client, conc_users, hot, level, args.duration, slog, args.seed)
            print(f"[bench]   {c['requests']} req, {c['throughput_rps']} req/s, p50 {c['p50_ms']} p95 {c['p95_ms']} p99 {c['p99_ms']} ms, "
                  f"errors {c['error_rate'] * 100:.2f}%", flush=True)
            result["concurrency"].append(c)

    save(args.out, result)
    print(f"[bench] wrote {os.path.join(args.out, 'results.json')} and report.md", flush=True)


if __name__ == "__main__":
    main()
