"""Group a run's kubectl.jsonl by the thread that launched each process."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict


def thread_kind(name: str) -> str:
    name = re.sub(r"[-_]\d+(_\d+)?$", "", name)
    name = re.sub(r"ThreadPoolExecutor-\d+", "pool", name)
    return name


def main() -> None:
    rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
    by_thread = Counter(thread_kind(r["thread"]) for r in rows)
    print(f"{len(rows)} kubectl processes")
    for name, n in by_thread.most_common(20):
        print(f"  {n:6}  {name}")
    print("\nverbs per thread kind:")
    per: dict = defaultdict(Counter)
    for r in rows:
        verb = " ".join(re.sub(r"-n \S+", "-n <ns>", re.sub(r"/nodes/[^/]+/", "/nodes/<n>/", r["args"])).split()[:4])
        per[thread_kind(r["thread"])][verb] += 1
    for name, _ in by_thread.most_common(8):
        print(f"  [{name}] " + ", ".join(f"{v}={n}" for v, n in per[name].most_common(6)))


if __name__ == "__main__":
    main()
