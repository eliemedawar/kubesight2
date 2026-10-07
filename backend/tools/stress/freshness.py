"""How long until a change in the cluster shows up in KubeSight?

Crashes one running pod in the fake prod-large cluster (control endpoint of
fake_k8s.py), then polls the pages that should reflect it once a second and
reports the delay for each. Caches are warmed first, so this measures the
realistic worst case: the change lands just after every cache was filled.

    python freshness.py [--base http://127.0.0.1:5091] [--fake http://127.0.0.1:7101]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ksclient import KubeSight  # noqa: E402


def control(fake: str, op: str, body: dict) -> dict:
    req = urllib.request.Request(f"{fake}/__control/{op}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5091")
    parser.add_argument("--fake", default="http://127.0.0.1:7101")
    parser.add_argument("--namespace", default="payments")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="admin123")
    args = parser.parse_args()

    ks = KubeSight(args.base)
    ks.login(args.user, args.password)
    _, body, _, _ = ks.get("/api/clusters")
    items = ((body or {}).get("data") or {}).get("clusters") or ((body or {}).get("data") or {}).get("items") or []
    cid = next(c["id"] for c in items if "prod-large" in str(c.get("name")))
    ns = args.namespace

    pods_path = f"/api/clusters/{cid}/namespaces/{ns}/resources/pods"
    _, body, _, _ = ks.get(pods_path)
    pods = ((body or {}).get("data") or {}).get("pods") or []
    victim = next(p for p in pods if p.get("status") == "Running" and (p.get("labels") or {}).get("pod-template-hash"))
    pod, owner = victim["name"], victim["labels"]["app"]
    print(f"[fresh] cluster={cid} ns={ns} pod={pod} deployment={owner}", flush=True)

    def pod_status() -> str:
        _, b, _, _ = ks.get(pods_path)
        for p in ((b or {}).get("data") or {}).get("pods") or []:
            if p.get("name") == pod:
                return p.get("status") or ""
        return ""

    def dashboard_mentions() -> bool:
        _, b, _, _ = ks.get("/api/dashboard/summary", clusterId=cid)
        return pod in json.dumps(b)

    def deployment_ready() -> str:
        _, b, _, _ = ks.get(f"/api/clusters/{cid}/namespaces/{ns}/resources/deployments")
        for d in ((b or {}).get("data") or {}).get("deployments") or []:
            if d.get("name") == owner:
                return f"{d.get('ready')}/{d.get('desired')}"
        return ""

    def inventory_ready() -> str:
        _, b, _, _ = ks.get("/api/inventory", clusterId=cid, namespace=ns)
        data = (b or {}).get("data")
        rows = data if isinstance(data, list) else (data or {}).get("items") or []
        for r in rows:
            if owner in (r.get("workloadNames") or []) or r.get("name") == owner:
                return f"{r.get('readyReplicas')}/{r.get('replicas')} {r.get('status')}"
        return ""

    checks = {
        "resources: pod list": (pod_status, lambda before, now: now != before),
        "resources: deployment ready count": (deployment_ready, lambda before, now: now != before),
        "inventory: app ready count": (inventory_ready, lambda before, now: now != before),
        "dashboard: failing pod listed": (dashboard_mentions, lambda before, now: now and not before),
    }
    # Warm every cache, then record the "before" values.
    before = {name: fn() for name, (fn, _) in checks.items()}
    before = {name: fn() for name, (fn, _) in checks.items()}
    print(f"[fresh] before: {before}", flush=True)

    control(args.fake, "crash", {"namespace": ns, "pod": pod, "crashing": True})
    t0 = time.time()
    seen = {}
    while len(seen) < len(checks) and time.time() - t0 < args.timeout:
        for name, (fn, changed) in checks.items():
            if name in seen:
                continue
            try:
                now = fn()
            except Exception as exc:  # keep polling
                now = f"error {exc}"
            if changed(before[name], now):
                seen[name] = (round(time.time() - t0, 1), now)
                print(f"[fresh] {name:36} visible after {seen[name][0]:6.1f}s  ({before[name]} -> {now})", flush=True)
        time.sleep(1)
    for name in checks:
        if name not in seen:
            print(f"[fresh] {name:36} NOT visible within {args.timeout}s", flush=True)
    control(args.fake, "crash", {"namespace": ns, "pod": pod, "crashing": False})
    print(json.dumps({"pod": pod, "results": {k: v[0] for k, v in seen.items()}}), flush=True)


if __name__ == "__main__":
    main()
