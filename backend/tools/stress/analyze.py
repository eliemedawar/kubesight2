"""Summarise a stress run: server-side per-endpoint stats, page loads, vitals, kubectl.

    python analyze.py --server <server run dir> [--client <loadgen out dir>] [--since <epoch>] [--until <epoch>] [--json out.json]

Server dir holds requests.jsonl / vitals.jsonl / kubectl.jsonl (serve.py);
client dir holds requests.jsonl / pageloads.jsonl / windows.jsonl (loadgen.py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List


def load(path: str, since: float, until: float) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            t = row.get("t", 0)
            if since <= t <= until:
                out.append(row)
    return out


def pct(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = max(0, min(len(values) - 1, int(round((p / 100.0) * (len(values) - 1)))))
    return values[k]


def table(rows: Iterable[List[Any]], headers: List[str]) -> str:
    rows = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    sep = "  ".join("-" * w for w in widths)
    return "\n".join([line, sep] + ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows])


_KUBECTL_NORM = [
    (re.compile(r"/api/v1/nodes/[^/]+/proxy"), "/api/v1/nodes/<node>/proxy"),
    (re.compile(r"(-n|--namespace) \S+"), r"\1 <ns>"),
    (re.compile(r"(logs|describe|get) (pod|pods|deployment|deploy|svc|service)/?\s?[a-z0-9][a-z0-9.-]*-[a-z0-9]{5}\b"), r"\1 \2 <name>"),
]


def norm_kubectl(args: str) -> str:
    for rx, rep in _KUBECTL_NORM:
        args = rx.sub(rep, args)
    return args[:90]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--client", default="")
    parser.add_argument("--since", type=float, default=0)
    parser.add_argument("--until", type=float, default=1e12)
    parser.add_argument("--json", default="")
    parser.add_argument("--top", type=int, default=30)
    args = parser.parse_args()

    srv = load(os.path.join(args.server, "requests.jsonl"), args.since, args.until)
    vit = load(os.path.join(args.server, "vitals.jsonl"), args.since, args.until)
    kub = load(os.path.join(args.server, "kubectl.jsonl"), args.since, args.until)
    report: Dict[str, Any] = {}

    if srv:
        span = max(r["t"] for r in srv) - min(r["t"] for r in srv) or 1
        by_rule: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in srv:
            by_rule[f"{r['m']} {r['rule']}"].append(r)
        rows = []
        for rule, items in by_rule.items():
            ms = [i["ms"] for i in items]
            q = [i.get("queue_ms", 0) for i in items]
            rows.append({
                "endpoint": rule, "n": len(items), "rpm": round(len(items) / span * 60, 1),
                "p50": round(pct(ms, 50)), "p95": round(pct(ms, 95)), "p99": round(pct(ms, 99)), "max": round(max(ms)),
                "queue_p95": round(pct(q, 95)),
                "sql_avg": round(sum(i["sql"] for i in items) / len(items), 1),
                "sql_ms_avg": round(sum(i["sql_ms"] for i in items) / len(items), 1),
                "kubectl_avg": round(sum(i["kubectl"] for i in items) / len(items), 2),
                "err": sum(1 for i in items if i["status"] >= 500),
                "kb_avg": round(sum(i["bytes"] for i in items) / len(items) / 1024, 1),
                "total_s": round(sum(ms) / 1000, 1),
            })
        rows.sort(key=lambda r: -r["total_s"])
        report["endpoints"] = rows
        all_ms = [r["ms"] for r in srv]
        all_q = [r.get("queue_ms", 0) for r in srv]
        report["server_overall"] = {
            "requests": len(srv), "rps": round(len(srv) / span, 1),
            "p50": round(pct(all_ms, 50)), "p95": round(pct(all_ms, 95)), "p99": round(pct(all_ms, 99)),
            "queue_p50": round(pct(all_q, 50)), "queue_p95": round(pct(all_q, 95)), "queue_max": round(max(all_q)),
            "errors_5xx": sum(1 for r in srv if r["status"] >= 500),
            "status_counts": {str(s): sum(1 for r in srv if r["status"] == s) for s in sorted({r["status"] for r in srv})},
        }
        print("== Server overall")
        print(json.dumps(report["server_overall"]))
        print("\n== Endpoints by total server time (top %d)" % args.top)
        print(table([[r["endpoint"][:78], r["n"], r["rpm"], r["p50"], r["p95"], r["p99"], r["max"], r["queue_p95"],
                      r["sql_avg"], r["sql_ms_avg"], r["kubectl_avg"], r["err"], r["kb_avg"]] for r in rows[: args.top]],
                    ["endpoint", "n", "rpm", "p50", "p95", "p99", "max", "q95", "sql", "sqlms", "kctl", "5xx", "KB"]))

    if kub:
        span = max(r["t"] for r in kub) - min(r["t"] for r in kub) or 1
        by_args: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in kub:
            by_args[norm_kubectl(r["args"])].append(r)
        rows = sorted(([k, len(v), round(len(v) / span * 60, 1), round(pct([x["ms"] for x in v], 50)),
                        round(pct([x["ms"] for x in v], 95)), sum(1 for x in v if x["rc"] != 0),
                        sum(1 for x in v if x["on_request"])] for k, v in by_args.items()), key=lambda r: -r[1])
        report["kubectl"] = {"total": len(kub), "per_min": round(len(kub) / span * 60, 1),
                             "p50": round(pct([r["ms"] for r in kub], 50)), "p95": round(pct([r["ms"] for r in kub], 95)),
                             "failed": sum(1 for r in kub if r["rc"] != 0),
                             "by_args": [dict(zip(["args", "n", "per_min", "p50", "p95", "failed", "on_request"], r)) for r in rows]}
        print("\n== kubectl: %d processes, %.1f/min, p50 %dms p95 %dms, failed %d" % (
            len(kub), len(kub) / span * 60, pct([r["ms"] for r in kub], 50), pct([r["ms"] for r in kub], 95),
            report["kubectl"]["failed"]))
        print(table(rows[: args.top], ["args", "n", "/min", "p50", "p95", "fail", "onreq"]))

    if vit:
        rss = [v["rss_mb"] for v in vit]
        report["vitals"] = {
            "rss_start": rss[0], "rss_end": rss[-1], "rss_max": max(rss),
            "threads_max": max(v["threads"] for v in vit), "threads_end": vit[-1]["threads"],
            "cpu_avg": round(sum(v["cpu_pct"] for v in vit) / len(vit), 1), "cpu_max": max(v["cpu_pct"] for v in vit),
            "queue_max": max(v["queue"] for v in vit), "kubectl_inflight_max": max(v["kubectl_inflight"] for v in vit),
            "db_checked_out_max": max((v.get("db_checked_out") or 0) for v in vit),
            "kubeconfig_materialized_end": vit[-1].get("kubeconfig_materialized"),
            "cache_entries_end": vit[-1].get("k8s_cache_entries"),
        }
        print("\n== Vitals")
        print(json.dumps(report["vitals"]))

    if args.client:
        pages = load(os.path.join(args.client, "pageloads.jsonl"), args.since, args.until)
        creq = load(os.path.join(args.client, "requests.jsonl"), args.since, args.until)
        wins = load(os.path.join(args.client, "windows.jsonl"), args.since, args.until)
        if pages:
            by_page: Dict[str, List[float]] = defaultdict(list)
            for p in pages:
                by_page[p["page"]].append(p["ms"])
            rows = sorted(([k, len(v), round(pct(v, 50)), round(pct(v, 95)), round(max(v))] for k, v in by_page.items()), key=lambda r: -r[3])
            report["pages"] = [dict(zip(["page", "n", "p50", "p95", "max"], r)) for r in rows]
            print("\n== Page loads (client, all first-load requests answered)")
            print(table(rows, ["page", "n", "p50", "p95", "max"]))
        if creq:
            errs = [r for r in creq if r["status"] == 0 or r["status"] >= 500]
            report["client_errors"] = {"n": len(errs), "of": len(creq),
                                       "by": {k: sum(1 for e in errs if f"{e['status']} {e['norm']}" == k) for k in {f"{e['status']} {e['norm']}" for e in errs}}}
            print("\n== Client errors: %d of %d" % (len(errs), len(creq)))
            for k, v in sorted(report["client_errors"]["by"].items(), key=lambda kv: -kv[1])[:15]:
                print(f"  {v:6}  {k}")
        if wins:
            report["windows"] = wins
            print("\n== Windows")
            print(table([[w["stage"], w["users"], w["rps"], w["p50"], w["p95"], w["p99"], w["err_pct"], w["page_p50"], w["page_p95"]] for w in wins],
                        ["stage", "users", "rps", "p50", "p95", "p99", "err%", "page50", "page95"]))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)


if __name__ == "__main__":
    main()
