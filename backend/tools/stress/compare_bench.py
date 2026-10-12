"""Compare two bench_endpoints.py result folders (stdlib only).

    python compare_bench.py <before dir> <after dir> [--out compare.md] [--min-ms 20] [--min-pct 20]

A change counts as real only when it is bigger than BOTH --min-ms and --min-pct
(medians of 5 calls on a shared machine wobble). SQL and kubectl counts are
deterministic enough to compare exactly. Writes markdown to --out (default
<after dir>/compare.md) and prints the headline.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict, List, Tuple


def load(d: str) -> Dict[str, Any]:
    with open(os.path.join(d, "results.json"), encoding="utf-8") as fh:
        return json.load(fh)


def f(v: Any, nd: int = 0) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--out")
    ap.add_argument("--min-ms", type=float, default=20.0)
    ap.add_argument("--min-pct", type=float, default=20.0)
    ap.add_argument("--top", type=int, default=30)
    a = ap.parse_args()
    A, B = load(a.before), load(a.after)
    ia = {(r["role"], r["rule"]): r for r in A["sequential"]}
    ib = {(r["role"], r["rule"]): r for r in B["sequential"]}
    keys = sorted(set(ia) & set(ib))
    rows: List[Dict[str, Any]] = []
    for k in keys:
        x, y = ia[k], ib[k]
        mx, my = x.get("median_ms") or 0.0, y.get("median_ms") or 0.0
        d = my - mx
        p = (d / mx * 100) if mx else 0.0
        real = abs(d) >= a.min_ms and abs(p) >= a.min_pct
        rows.append({"role": k[0], "rule": k[1], "before": mx, "after": my, "delta": d, "pct": p, "real": real,
                     "sql_b": x.get("sql"), "sql_a": y.get("sql"), "kc_b": x.get("kubectl"), "kc_a": y.get("kubectl"),
                     "cold_b": x.get("cold_ms"), "cold_a": y.get("cold_ms"),
                     "st_b": "/".join(map(str, x.get("statuses", []))), "st_a": "/".join(map(str, y.get("statuses", [])))})
    faster = [r for r in rows if r["real"] and r["delta"] < 0]
    slower = [r for r in rows if r["real"] and r["delta"] > 0]
    sql_less = [r for r in rows if (r["sql_a"] or 0) < (r["sql_b"] or 0)]
    sql_more = [r for r in rows if (r["sql_a"] or 0) > (r["sql_b"] or 0)]
    status_changed = [r for r in rows if r["st_a"] != r["st_b"]]
    sum_b = sum(r["before"] for r in rows)
    sum_a = sum(r["after"] for r in rows)
    sqlsum_b = sum((r["sql_b"] or 0) for r in rows)
    sqlsum_a = sum((r["sql_a"] or 0) for r in rows)

    L: List[str] = []
    L.append("# Benchmark comparison\n")
    for name, R, d in (("before", A, a.before), ("after", B, a.after)):
        m, c = R["meta"], R.get("calibration", {})
        L.append(f"- **{name}**: `{d}` - {m.get('started')} git `{m.get('git')}`; kubectl spawn median {f(c.get('kubectl_spawn_median_ms'))} ms, "
                 f"python spawn {f(c.get('python_spawn_median_ms'))} ms")
    L.append("")
    L.append(f"- endpoints compared: {len(rows)} (only in before: {len(set(ia) - set(ib))}, only in after: {len(set(ib) - set(ia))})")
    L.append(f"- sum of medians: {sum_b:.0f} ms -> {sum_a:.0f} ms ({(sum_a - sum_b) / sum_b * 100 if sum_b else 0:+.1f}%)")
    L.append(f"- sum of SQL per request (medians): {sqlsum_b:.0f} -> {sqlsum_a:.0f}")
    L.append(f"- real changes (>= {a.min_ms:.0f} ms and >= {a.min_pct:.0f}%): {len(faster)} faster, {len(slower)} slower")
    L.append(f"- SQL count: {len(sql_less)} endpoints fewer, {len(sql_more)} more; status changed: {len(status_changed)}")
    cal_b = A.get("calibration", {}).get("kubectl_spawn_median_ms") or 0
    cal_a = B.get("calibration", {}).get("kubectl_spawn_median_ms") or 0
    if cal_b and cal_a and (max(cal_a, cal_b) / max(1, min(cal_a, cal_b)) > 1.5):
        L.append(f"- WARNING: kubectl spawn cost differs {cal_b:.0f} vs {cal_a:.0f} ms between runs - kubectl-bound timings are not comparable; trust SQL/kubectl counts and DB-only endpoints.")
    L.append("")

    def table(title: str, sel: List[Dict[str, Any]]) -> None:
        L.append(f"## {title}\n")
        L.append("| role | endpoint | before ms | after ms | delta | % | SQL b->a | kubectl b->a | cold b->a | status b->a |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in sel:
            L.append(f"| {r['role']} | `{r['rule']}` | {r['before']:.0f} | {r['after']:.0f} | {r['delta']:+.0f} | {r['pct']:+.0f}% | "
                     f"{f(r['sql_b'])} -> {f(r['sql_a'])} | {f(r['kc_b'])} -> {f(r['kc_a'])} | {f(r['cold_b'])} -> {f(r['cold_a'])} | {r['st_b']} -> {r['st_a']} |")
        L.append("")

    table("Biggest improvements", sorted(faster, key=lambda r: r["delta"])[: a.top])
    table("Biggest regressions", sorted(slower, key=lambda r: -r["delta"])[: a.top])
    table("SQL count changes", sorted(sql_less + sql_more, key=lambda r: (r["sql_a"] or 0) - (r["sql_b"] or 0))[: a.top * 2])
    table("Status changes", status_changed)

    ca = {c["vus"]: c for c in A.get("concurrency", [])}
    cb = {c["vus"]: c for c in B.get("concurrency", [])}
    if ca or cb:
        L.append("## Concurrency\n")
        L.append("| VUs | req/s b->a | p50 b->a | p95 b->a | p99 b->a | error rate b->a | queue p95 b->a | kubectl total b->a | SQL total b->a |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for v in sorted(set(ca) | set(cb)):
            x, y = ca.get(v, {}), cb.get(v, {})
            g = lambda d, k: d.get(k, "-")
            L.append(f"| {v} | {g(x,'throughput_rps')} -> {g(y,'throughput_rps')} | {g(x,'p50_ms')} -> {g(y,'p50_ms')} | {g(x,'p95_ms')} -> {g(y,'p95_ms')} | "
                     f"{g(x,'p99_ms')} -> {g(y,'p99_ms')} | {x.get('error_rate', 0) * 100:.2f}% -> {y.get('error_rate', 0) * 100:.2f}% | "
                     f"{g(x,'server_queue_p95_ms')} -> {g(y,'server_queue_p95_ms')} | {g(x,'server_kubectl_total')} -> {g(y,'server_kubectl_total')} | "
                     f"{g(x,'server_sql_total')} -> {g(y,'server_sql_total')} |")
        L.append("")
    out = a.out or os.path.join(a.after, "compare.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("\n".join(L[: 14]))
    print(f"\nfull comparison: {out}")


if __name__ == "__main__":
    main()
