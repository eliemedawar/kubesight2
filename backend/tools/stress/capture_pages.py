"""Open every KubeSight page in a real browser and record what it asks the API for.

For each page the script logs in once, navigates by URL hash, stays on the page
for ``--dwell`` seconds (long enough to see its polling), and records:

  * every /api request: method, path, status, duration, response size, and
    whether it was made during the first load or later (polling);
  * time until the page's first-load requests have all answered;
  * console errors and failed requests.

The output (pages.json) feeds the load generator's user journeys and the
per-page frontend report.

    python capture_pages.py --base http://127.0.0.1:5091 --out pages.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ksclient import KubeSight  # noqa: E402

# (name, hash route). {c} = a cluster id, {ns} = a namespace on it,
# {svc} = a CI service id, {pod} = a pod name.
PAGES = [
    ("dashboard", "#/dashboard?cluster={c}"),
    ("clusters", "#/clusters?cluster={c}"),
    ("cluster-overview", "#/clusters/{c}/overview"),
    ("cluster-management", "#/cluster-management"),
    ("namespaces", "#/namespaces?cluster={c}"),
    ("resources-pods", "#/resources/pods?cluster={c}&ns={ns}"),
    ("resources-deployments", "#/resources/deployments?cluster={c}&ns={ns}"),
    ("resources-services", "#/resources/services?cluster={c}&ns={ns}"),
    ("resources-events", "#/resources/events?cluster={c}&ns={ns}"),
    ("inventory", "#/inventory?cluster={c}"),
    ("inventory-helm", "#/inventory/helm?cluster={c}"),
    ("my-requests", "#/my-requests"),
    ("change-bundles", "#/change-bundles"),
    ("approvals", "#/deployment-requests"),
    ("ticketing", "#/ticketing"),
    ("logs", "#/logs?cluster={c}&ns={ns}"),
    ("alerts-open", "#/alerts/open?cluster={c}"),
    ("alerts-history", "#/alerts/history?cluster={c}"),
    ("alerts-policies", "#/alerts/policies"),
    ("service-catalog", "#/service-catalog"),
    ("service-detail-overview", "#/service-catalog/{svc}/overview"),
    ("service-detail-builds", "#/service-catalog/{svc}/builds"),
    ("service-detail-pipeline", "#/service-catalog/{svc}/pipeline"),
    ("pipelines", "#/pipelines"),
    ("blueprints", "#/blueprints"),
    ("app-services", "#/app-services"),
    ("application-intelligence", "#/application-intelligence"),
    ("components", "#/components"),
    ("clients", "#/clients"),
    ("users", "#/users/users"),
    ("roles", "#/users/roles"),
    ("audit-logs", "#/audit-logs"),
    ("api-tokens", "#/api-tokens"),
    ("image-registries", "#/image-registries"),
    ("integrations", "#/integrations"),
    ("settings", "#/settings"),
    ("mobile-apps", "#/mobile-apps"),
    ("cluster-builder", "#/cluster-builder"),
    ("upgrade", "#/upgrade?cluster={c}"),
]

_ID_SEGMENT = re.compile(r"/(\d+|[0-9a-f]{8,}(?:-[0-9a-f]{4,})*)(?=/|$)")


def normalize(path: str) -> str:
    return _ID_SEGMENT.sub("/<id>", path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5091")
    parser.add_argument("--out", required=True)
    parser.add_argument("--dwell", type=float, default=25.0)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="admin123")
    parser.add_argument("--only", default="", help="comma-separated page names")
    parser.add_argument("--screens", default="", help="directory for a screenshot per page")
    args = parser.parse_args()

    api = KubeSight(args.base)
    api.login(args.user, args.password)
    _, me, _, _ = api.get("/api/auth/me")
    user_id = ((me or {}).get("data") or {}).get("user", {}).get("id") or ((me or {}).get("data") or {}).get("id") or 1
    _, clusters, _, _ = api.get("/api/clusters")
    cluster_items = ((clusters or {}).get("data") or {}).get("clusters") or ((clusters or {}).get("data") or {}).get("items") or []
    cluster = next((c for c in cluster_items if "prod-large" in str(c.get("name"))), cluster_items[0] if cluster_items else {})
    cid = cluster.get("id", "")
    _, svcs, _, _ = api.get("/api/ci/services")
    svc_list = ((svcs or {}).get("data") or {}).get("services") or ((svcs or {}).get("data") or {}).get("items") or []
    svc = str(svc_list[0].get("id") or svc_list[0].get("slug")) if svc_list else "none"
    subs = {"c": cid, "ns": "payments", "svc": svc, "pod": ""}
    print(f"[capture] user={user_id} cluster={cid} service={svc}", flush=True)

    from playwright.sync_api import sync_playwright

    only = {p.strip() for p in args.only.split(",") if p.strip()}
    results: List[Dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 900})
        context.add_init_script(
            "try{for(let i=1;i<200;i++){localStorage.setItem('kubesight.coachmarks.v1.'+i, JSON.stringify({seen:{},muted:true}));}}catch(e){}"
        )
        page = context.new_page()
        page.goto(args.base + "/", wait_until="domcontentloaded")
        page.get_by_label("Username").fill(args.user)
        page.locator("input[type=password]").first.fill(args.password)
        page.keyboard.press("Enter")
        page.wait_for_timeout(4000)

        for name, route in PAGES:
            if only and name not in only:
                continue
            hash_route = route.format(**subs)
            log: List[Dict[str, Any]] = []
            errors: List[str] = []
            failed: List[str] = []
            starts: Dict[Any, float] = {}
            t0 = [time.perf_counter()]

            def on_request(req):
                if "/api/" in req.url:
                    starts[req] = time.perf_counter()

            def on_finished(req):
                if req in starts:
                    resp = req.response()
                    try:
                        size = len(resp.body()) if resp else 0
                    except Exception:
                        size = -1
                    path = req.url.split("://", 1)[-1].split("/", 1)[-1]
                    path = "/" + path
                    log.append({
                        "m": req.method, "path": path.split("?")[0], "query": path.split("?")[1] if "?" in path else "",
                        "norm": normalize(path.split("?")[0]),
                        "status": resp.status if resp else 0,
                        "start_s": round(starts[req] - t0[0], 2),
                        "ms": round((time.perf_counter() - starts[req]) * 1000, 1), "bytes": size,
                    })

            def on_failed(req):
                if "/api/" in req.url:
                    failed.append(f"{req.method} {req.url} {req.failure}")

            def on_console(msg):
                if msg.type == "error":
                    errors.append(msg.text[:300])

            page.on("request", on_request)
            page.on("requestfinished", on_finished)
            page.on("requestfailed", on_failed)
            page.on("console", on_console)
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {str(exc)[:300]}"))
            t0[0] = time.perf_counter()
            page.evaluate(f"location.hash = {json.dumps(hash_route[1:])}")
            # settled = no new /api request for 1.5s and none in flight
            settled_at = None
            deadline = time.perf_counter() + 60
            while time.perf_counter() < deadline:
                page.wait_for_timeout(250)
                inflight = len(starts) - len(log) - len(failed)
                last = max([e["start_s"] + e["ms"] / 1000 for e in log], default=0)
                if inflight <= 0 and (time.perf_counter() - t0[0]) - last > 1.5 and (time.perf_counter() - t0[0]) > 1.0:
                    settled_at = round(last, 2)
                    break
            remaining = args.dwell - (time.perf_counter() - t0[0])
            if remaining > 0:
                page.wait_for_timeout(remaining * 1000)
            if args.screens:
                os.makedirs(args.screens, exist_ok=True)
                page.screenshot(path=os.path.join(args.screens, f"{name}.png"))
            page.remove_listener("request", on_request)
            page.remove_listener("requestfinished", on_finished)
            page.remove_listener("requestfailed", on_failed)
            page.remove_listener("console", on_console)
            first_load = [e for e in log if settled_at is not None and e["start_s"] <= settled_at + 0.01]
            entry = {
                "page": name, "route": hash_route, "settled_s": settled_at,
                "first_load_requests": len(first_load),
                "requests": log, "console_errors": errors, "failed": failed,
                "dwell_s": args.dwell,
            }
            results.append(entry)
            dupes = {}
            for e in first_load:
                key = (e["m"], e["path"], e["query"])
                dupes[key] = dupes.get(key, 0) + 1
            dup_count = sum(v - 1 for v in dupes.values() if v > 1)
            slowest = max(first_load, key=lambda e: e["ms"], default=None)
            print(f"[capture] {name:26} settled={settled_at}s first={len(first_load):3} total={len(log):3} dup={dup_count} "
                  f"errors={len(errors)} failed={len(failed)} slowest={slowest['norm'] + ' ' + str(slowest['ms']) + 'ms' if slowest else '-'}",
                  flush=True)
        browser.close()

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"base": args.base, "subs": subs, "pages": results}, fh, indent=1)
    print(f"[capture] wrote {args.out}")


if __name__ == "__main__":
    main()
