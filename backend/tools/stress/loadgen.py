"""Simulated KubeSight users, replaying what the real frontend does.

Input is the pages.json written by capture_pages.py: for every page, the API
requests the browser made on first load and the ones it kept making while the
page stayed open (polling). Each virtual user:

  1. logs in once (as stress-user-NN when those exist, else the given user);
  2. picks a page by weight, picks a cluster and namespace (so caches see the
     spread of real users rather than one hot key), and fires that page's
     first-load requests six at a time like a browser;
  3. stays on the page for a think time, replaying the page's polling requests;
  4. moves on.

Only GET requests are replayed (plus login), so the load is read-only.

Stages ramp the user count: ``--stages 10:120,50:180,100:180`` means 10 users
for 120 s, then 50 for 180 s, and so on. Every request is written to
``requests.jsonl`` and a summary line per window is printed and appended to
``windows.jsonl``. Page loads (all first-load requests answered) go to
``pageloads.jsonl``.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ksclient import KubeSight  # noqa: E402

PAGE_WEIGHTS = {
    "dashboard": 18, "clusters": 6, "cluster-overview": 3, "namespaces": 5,
    "resources-pods": 12, "resources-deployments": 7, "resources-services": 3, "resources-events": 3,
    "inventory": 5, "inventory-helm": 1, "my-requests": 2, "change-bundles": 2, "approvals": 3,
    "logs": 5, "alerts-open": 6, "alerts-history": 2, "alerts-policies": 1,
    "service-catalog": 4, "service-detail-overview": 3, "service-detail-builds": 4, "service-detail-pipeline": 1,
    "pipelines": 1, "audit-logs": 2, "users": 1, "upgrade": 1, "cluster-management": 1,
}
# Think time (seconds) per page; dashboards and lists people leave open longer.
THINK = {"dashboard": (30, 120), "alerts-open": (20, 90), "resources-pods": (15, 60), "logs": (20, 90)}
DEFAULT_THINK = (8, 40)

_PERCENTILES = (50, 95, 99)


def pct(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = max(0, min(len(values) - 1, int(round((p / 100.0) * (len(values) - 1)))))
    return values[k]


class Http:
    """Minimal async HTTP/1.0 client (the server closes after each response)."""

    def __init__(self, host: str, port: int, timeout: float):
        self.host, self.port, self.timeout = host, port, timeout

    async def request(self, method: str, path: str, headers: Dict[str, str], body: Optional[bytes] = None) -> Tuple[int, bytes, int]:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)
        try:
            lines = [f"{method} {path} HTTP/1.0", f"Host: {self.host}:{self.port}", "Accept-Encoding: gzip", "Connection: close"]
            lines += [f"{k}: {v}" for k, v in headers.items()]
            if body is not None:
                lines.append(f"Content-Length: {len(body)}")
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + (body or b""))
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(-1), self.timeout)
        finally:
            writer.close()
        head, _, payload = raw.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        status = int(status_line.split()[1]) if len(status_line.split()) > 1 else 0
        size = len(payload)
        if b"content-encoding: gzip" in head.lower():
            try:
                payload = gzip.decompress(payload)
            except OSError:
                pass
        return status, payload, size


class Stats:
    def __init__(self, out_dir: str):
        self.window: List[Dict[str, Any]] = []
        self.req_log = open(os.path.join(out_dir, "requests.jsonl"), "a", encoding="utf-8", buffering=1 << 16)
        self.page_log = open(os.path.join(out_dir, "pageloads.jsonl"), "a", encoding="utf-8", buffering=1 << 16)
        self.win_log = open(os.path.join(out_dir, "windows.jsonl"), "a", encoding="utf-8", buffering=1)
        self.pageloads: List[Dict[str, Any]] = []
        self.active = 0
        self.stage = ""

    def record(self, rec: Dict[str, Any]) -> None:
        self.window.append(rec)
        self.req_log.write(json.dumps(rec) + "\n")

    def record_page(self, rec: Dict[str, Any]) -> None:
        self.pageloads.append(rec)
        self.page_log.write(json.dumps(rec) + "\n")

    def flush_window(self, seconds: float) -> Dict[str, Any]:
        reqs, self.window = self.window, []
        pages, self.pageloads = self.pageloads, []
        lat = [r["ms"] for r in reqs if r["status"]]
        errors = [r for r in reqs if r["status"] == 0 or r["status"] >= 500]
        by_path: Dict[str, List[float]] = defaultdict(list)
        for r in reqs:
            by_path[r["norm"]].append(r["ms"])
        worst = sorted(((pct(v, 95), k, len(v)) for k, v in by_path.items()), reverse=True)[:5]
        page_ms = [p["ms"] for p in pages]
        summary = {
            "t": round(time.time(), 1), "stage": self.stage, "users": self.active,
            "rps": round(len(reqs) / seconds, 1), "n": len(reqs),
            **{f"p{p}": round(pct(lat, p)) for p in _PERCENTILES},
            "err": len(errors), "err_pct": round(100 * len(errors) / max(1, len(reqs)), 2),
            "timeouts": sum(1 for r in errors if r["status"] == 0),
            "page_p50": round(pct(page_ms, 50)), "page_p95": round(pct(page_ms, 95)), "pages": len(pages),
            "worst_p95": [[round(w[0]), w[1], w[2]] for w in worst],
        }
        self.win_log.write(json.dumps(summary) + "\n")
        self.req_log.flush()
        self.page_log.flush()
        return summary


_ID = re.compile(r"/(\d+|[0-9a-f]{8,}(?:-[0-9a-f]{4,})*)(?=/|$)")


def normalize(path: str) -> str:
    path = path.split("?")[0]
    path = re.sub(r"/clusters/custom-\d+", "/clusters/<c>", path)
    path = re.sub(r"/namespaces/[^/]+", "/namespaces/<ns>", path)
    path = re.sub(r"/pods/[^/]+", "/pods/<pod>", path)
    return _ID.sub("/<id>", path)


class Journey:
    def __init__(self, pages_json: Dict[str, Any]):
        self.subs = pages_json["subs"]
        self.pages: Dict[str, Dict[str, Any]] = {}
        for page in pages_json["pages"]:
            settled = page.get("settled_s") or 0
            reqs = [r for r in page["requests"] if r["m"] == "GET" and "/stream" not in r["path"] and "/auth/" not in r["path"]]
            first = [r for r in reqs if r["start_s"] <= settled + 0.01]
            poll = [r for r in reqs if r["start_s"] > settled + 0.01]
            self.pages[page["page"]] = {"first": first, "poll": poll, "dwell": page.get("dwell_s") or 20, "settled": settled}
        weights = {k: v for k, v in PAGE_WEIGHTS.items() if k in self.pages}
        for k in self.pages:
            weights.setdefault(k, 0.5)
        self.names = list(weights)
        self.weights = [weights[n] for n in self.names]

    def pick(self, rng: random.Random) -> str:
        return rng.choices(self.names, self.weights)[0]


def substitute(path: str, query: str, subs: Dict[str, str], cluster: str, ns: str, svc: str = "") -> str:
    if svc and subs.get("svc"):
        path = re.sub(r"/ci/services/" + re.escape(subs["svc"]) + r"(?=/|$)", f"/ci/services/{svc}", path)
    if subs.get("c"):
        path = path.replace(subs["c"], cluster)
        query = query.replace(subs["c"], cluster)
    if subs.get("ns"):
        path = re.sub(r"/namespaces/" + re.escape(subs["ns"]) + r"(?=/|$)", f"/namespaces/{ns}", path)
        query = re.sub(r"(^|&)(namespace|ns)=" + re.escape(subs["ns"]) + r"(?=&|$)", lambda m: f"{m.group(1)}{m.group(2)}={ns}", query)
    return path + (f"?{query}" if query else "")


async def virtual_user(idx: int, http: Http, token: str, journey: Journey, stats: Stats,
                       clusters: List[Tuple[str, List[str]]], stop: asyncio.Event, rng: random.Random,
                       think_scale: float, hot_cluster_bias: float, services: List[str]) -> None:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "X-Stress-User": f"vu{idx}"}
    sem = asyncio.Semaphore(6)  # browser per-host connection limit
    stats.active += 1
    try:
        while not stop.is_set():
            name = journey.pick(rng)
            page = journey.pages[name]
            # People mostly look at the big production cluster, but not only.
            cluster, namespaces = clusters[0] if rng.random() < hot_cluster_bias else rng.choice(clusters)
            ns = rng.choice(namespaces) if namespaces else journey.subs.get("ns", "default")
            svc = rng.choice(services) if services else ""
            page_headers = {**headers, "X-Stress-Page": name}

            async def one(r: Dict[str, Any]) -> int:
                url = substitute(r["path"], r.get("query", ""), journey.subs, cluster, ns, svc)
                async with sem:
                    started = time.perf_counter()
                    status, size = 0, 0
                    err = ""
                    try:
                        status, _, size = await http.request("GET", url, page_headers)
                    except Exception as exc:  # timeout / reset
                        err = type(exc).__name__
                    ms = (time.perf_counter() - started) * 1000
                stats.record({"t": round(time.time(), 3), "vu": idx, "page": name, "norm": normalize(r["path"]),
                              "status": status, "ms": round(ms, 1), "bytes": size, "err": err})
                return status

            started = time.perf_counter()
            results = await asyncio.gather(*(one(r) for r in page["first"]))
            stats.record_page({"t": round(time.time(), 1), "page": name, "ms": round((time.perf_counter() - started) * 1000),
                               "n": len(results), "failed": sum(1 for s in results if s == 0 or s >= 500), "users": stats.active})
            lo, hi = THINK.get(name, DEFAULT_THINK)
            think = rng.uniform(lo, hi) * think_scale
            end = time.perf_counter() + think
            poll = page["poll"]
            dwell = max(page["dwell"] - page["settled"], 1)
            cycle_start = time.perf_counter()
            i = 0
            while not stop.is_set() and time.perf_counter() < end:
                if poll:
                    r = poll[i % len(poll)]
                    cycle = i // len(poll)
                    due = cycle_start + cycle * dwell + (r["start_s"] - page["settled"])
                    wait = due - time.perf_counter()
                    if wait > 0:
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=min(wait, end - time.perf_counter()))
                        except asyncio.TimeoutError:
                            pass
                        if time.perf_counter() >= end or stop.is_set():
                            break
                    asyncio.ensure_future(one(r))
                    i += 1
                else:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=max(0.0, end - time.perf_counter()))
                    except asyncio.TimeoutError:
                        pass
    finally:
        stats.active -= 1


async def run(args: argparse.Namespace) -> None:
    with open(args.pages, encoding="utf-8") as fh:
        journey = Journey(json.load(fh))
    host, _, port = args.base.replace("http://", "").partition(":")
    http = Http(host, int(port or 80), args.timeout)
    out = args.out
    os.makedirs(out, exist_ok=True)
    stats = Stats(out)

    # Discover clusters and namespaces once (as admin).
    admin = KubeSight(args.base)
    admin.login(args.admin_user, args.admin_password)
    _, body, _, _ = admin.get("/api/clusters")
    items = ((body or {}).get("data") or {}).get("clusters") or ((body or {}).get("data") or {}).get("items") or []
    clusters: List[Tuple[str, List[str]]] = []
    for c in sorted(items, key=lambda c: 0 if "prod-large" in str(c.get("name")) else 1):
        _, nb, _, _ = admin.get(f"/api/clusters/{c['id']}/namespaces", lite=1)
        data = (nb or {}).get("data") or {}
        names = [n.get("name") if isinstance(n, dict) else n for n in (data.get("namespaces") or data.get("items") or [])]
        names = [n for n in names if n and n not in ("kube-system", "default", "monitoring", "ingress-nginx")]
        clusters.append((c["id"], names))
    print(f"[load] clusters: {[(c, len(n)) for c, n in clusters]}", flush=True)
    _, sb, _, _ = admin.get("/api/ci/services")
    sdata = (sb or {}).get("data") or {}
    services = [str(s.get("id")) for s in (sdata.get("services") or sdata.get("items") or []) if s.get("id")]
    print(f"[load] ci services: {len(services)}", flush=True)

    # Tokens: one login per virtual user (stress users if they exist).
    tokens: List[str] = []
    max_users = max(int(s.split(":")[0]) for s in args.stages.split(","))
    probe = KubeSight(args.base)
    use_stress_users = True
    try:
        probe.login("stress-user-01", args.user_password)
    except Exception:
        use_stress_users = False
    # One session per stress user that can log in (some are seeded disabled,
    # like real installations); virtual users share them round-robin.
    sessions: List[str] = []
    if use_stress_users and not args.admin_only:
        for n in range(1, args.stress_users + 1):
            k = KubeSight(args.base)
            try:
                k.login(f"stress-user-{n:02d}", args.user_password)
                sessions.append(k.token or "")
            except Exception:
                continue
    if not sessions:
        k = KubeSight(args.base)
        k.login(args.admin_user, args.admin_password)
        sessions.append(k.token or "")
    for i in range(max_users):
        tokens.append(sessions[i % len(sessions)])
    print(f"[load] {len(tokens)} virtual users over {len(sessions)} logged-in accounts", flush=True)

    stop_users: List[asyncio.Event] = []
    tasks: List[asyncio.Task] = []
    rng = random.Random(args.seed)
    global_stop = asyncio.Event()

    async def reporter():
        while not global_stop.is_set():
            try:
                await asyncio.wait_for(global_stop.wait(), timeout=args.window)
            except asyncio.TimeoutError:
                pass
            s = stats.flush_window(args.window)
            worst = " | ".join(f"{w[1]} p95={w[0]}ms n={w[2]}" for w in s["worst_p95"][:3])
            print(f"[load] {time.strftime('%H:%M:%S')} {s['stage']:>10} users={s['users']:3} rps={s['rps']:6} "
                  f"p50={s['p50']:5} p95={s['p95']:6} p99={s['p99']:6} err={s['err_pct']}% (to={s['timeouts']}) "
                  f"page p50={s['page_p50']} p95={s['page_p95']} || {worst}", flush=True)

    rep = asyncio.ensure_future(reporter())
    for stage in args.stages.split(","):
        users, seconds = (int(x) for x in stage.split(":"))
        stats.stage = f"{users}u"
        # Grow or shrink to the target, staggering arrivals over ~20 s.
        while len(tasks) < users:
            idx = len(tasks)
            ev = asyncio.Event()
            stop_users.append(ev)
            tasks.append(asyncio.ensure_future(virtual_user(
                idx, http, tokens[idx], journey, stats, clusters, ev, random.Random(rng.random()),
                args.think_scale, args.hot_cluster_bias, services)))
            await asyncio.sleep(min(20.0 / max(users, 1), 0.5))
        while len(tasks) > users:
            stop_users.pop().set()
            tasks.pop()
        await asyncio.sleep(seconds)
    for ev in stop_users:
        ev.set()
    await asyncio.sleep(2)
    global_stop.set()
    await rep


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:5091")
    parser.add_argument("--pages", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--stages", default="10:120,25:120,50:180,100:180")
    parser.add_argument("--window", type=float, default=15.0)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--think-scale", type=float, default=1.0)
    parser.add_argument("--hot-cluster-bias", type=float, default=0.5)
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password", default="admin123")
    parser.add_argument("--user-password", default="Stress-Test-1!")
    parser.add_argument("--stress-users", type=int, default=60)
    parser.add_argument("--admin-only", action="store_true")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
