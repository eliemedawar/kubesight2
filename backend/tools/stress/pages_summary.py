"""Print per-page first-load and polling request patterns from pages.json."""

from __future__ import annotations

import json
import sys
from collections import Counter


def main() -> None:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    for page in data["pages"]:
        settled = page.get("settled_s") or 0
        first = Counter(r["norm"] for r in page["requests"] if r["start_s"] <= settled + 0.01)
        poll = Counter(r["norm"] for r in page["requests"] if r["start_s"] > settled + 0.01)
        dupes = {k: v for k, v in first.items() if v > 1}
        print(f"\n## {page['page']}  settled={settled}s  dwell={page.get('dwell_s')}s  errors={len(page['console_errors'])} failed={len(page['failed'])}")
        print("  first:", ", ".join(f"{k}{' x' + str(v) if v > 1 else ''}" for k, v in first.items()))
        if poll:
            print("  poll :", ", ".join(f"{k} x{v}" for k, v in poll.items()))
        if dupes:
            print("  DUPLICATE first-load:", dupes)
        for e in page["console_errors"][:3]:
            print("  console:", e[:200])
        for f in page["failed"][:3]:
            print("  failed:", f[:200])


if __name__ == "__main__":
    main()
