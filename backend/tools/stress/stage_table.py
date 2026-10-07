"""Per-stage page-load and error numbers for runs that used the same ramp.

    python stage_table.py <runs dir> run1 run2 ...   -> JSON on stdout
"""

import json
import os
import sys
from collections import defaultdict


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    return values[max(0, min(len(values) - 1, int(round(p / 100 * (len(values) - 1)))))]


def main():
    root = sys.argv[1]
    out = {}
    for run in sys.argv[2:]:
        client = os.path.join(root, run, "client")
        windows = [json.loads(l) for l in open(os.path.join(client, "windows.jsonl"), encoding="utf-8") if l.strip()]
        reqs = [json.loads(l) for l in open(os.path.join(client, "requests.jsonl"), encoding="utf-8") if l.strip()]
        pages = [json.loads(l) for l in open(os.path.join(client, "pageloads.jsonl"), encoding="utf-8") if l.strip()]
        # Map time ranges to stages from the window summaries.
        bounds = []
        for w in windows:
            bounds.append((w["t"], w["stage"]))
        def stage_at(t):
            for end, stage in bounds:
                if t <= end:
                    return stage
            return bounds[-1][1] if bounds else "?"
        by_stage = defaultdict(lambda: {"pages": [], "req": 0, "err": 0})
        for p in pages:
            by_stage[stage_at(p["t"])]["pages"].append(p["ms"])
        for r in reqs:
            s = by_stage[stage_at(r["t"])]
            s["req"] += 1
            if r["status"] == 0 or r["status"] >= 500:
                s["err"] += 1
        out[run] = {
            stage: {
                "page_p50": pct(v["pages"], 50), "page_p95": pct(v["pages"], 95),
                "err_pct": round(100 * v["err"] / v["req"], 1) if v["req"] else 0, "requests": v["req"],
            }
            for stage, v in by_stage.items()
        }
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
