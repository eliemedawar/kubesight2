"""One line per run: throughput, latency, queueing, errors, page loads, kubectl, CPU.

    python compare_runs.py <runs dir> run1 run2 ...
"""

from __future__ import annotations

import json
import os
import sys


def pct(values, p):
    if not values:
        return 0
    values = sorted(values)
    return values[max(0, min(len(values) - 1, int(round(p / 100 * (len(values) - 1)))))]


def load(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def main() -> None:
    root = sys.argv[1]
    print(f"{'run':14} {'req':>6} {'rps':>5} {'p50':>6} {'p95':>6} {'p99':>6} {'q95':>6} {'5xx':>4} {'cli err':>8} "
          f"{'page50':>7} {'page95':>7} {'kctl/min':>8} {'cpu%':>5} {'rssMB':>6}")
    for run in sys.argv[2:]:
        d = os.path.join(root, run)
        srv = load(os.path.join(d, "requests.jsonl"))
        cli = load(os.path.join(d, "client", "requests.jsonl"))
        pages = load(os.path.join(d, "client", "pageloads.jsonl"))
        kub = load(os.path.join(d, "kubectl.jsonl"))
        vit = load(os.path.join(d, "vitals.jsonl"))
        if not srv:
            print(f"{run:14} (no data)")
            continue
        span = (max(r["t"] for r in srv) - min(r["t"] for r in srv)) or 1
        kspan = ((max(r["t"] for r in kub) - min(r["t"] for r in kub)) or 1) if kub else 1
        ms = [r["ms"] for r in srv]
        q = [r.get("queue_ms", 0) for r in srv]
        cerr = sum(1 for r in cli if r["status"] == 0 or r["status"] >= 500)
        pm = [p["ms"] for p in pages]
        print(f"{run:14} {len(srv):6} {len(srv) / span:5.1f} {pct(ms, 50):6.0f} {pct(ms, 95):6.0f} {pct(ms, 99):6.0f} "
              f"{pct(q, 95):6.0f} {sum(1 for r in srv if r['status'] >= 500):4} {cerr:4}/{len(cli):<5} "
              f"{pct(pm, 50):7.0f} {pct(pm, 95):7.0f} {len(kub) / kspan * 60:8.0f} "
              f"{(sum(v['cpu_pct'] for v in vit) / len(vit)) if vit else 0:5.0f} {max((v['rss_mb'] for v in vit), default=0):6.0f}")


if __name__ == "__main__":
    main()
