"""Print a run's memory / thread / cache-size timeline (about 15 points), for leak checks.

    python vitals_timeline.py <run dir> [<run dir> ...]
"""

import json
import os
import sys

for run in sys.argv[1:]:
    with open(os.path.join(run, "vitals.jsonl"), encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    if not rows:
        continue
    step = max(1, len(rows) // 15)
    t0 = rows[0]["t"]
    points = [f"{int((r['t'] - t0) / 60)}m:{int(r['rss_mb'])}MB/{r['threads']}t/{r['k8s_cache_entries']}c" for r in rows[::step]]
    print(os.path.basename(run.rstrip("/\\")), " ".join(points))
