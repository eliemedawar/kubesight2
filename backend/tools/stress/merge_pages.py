"""Replace pages in a pages.json with the same-named pages from another capture.

    python merge_pages.py <base pages.json> <patch pages.json>   (writes base in place)
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    base_path, patch_path = sys.argv[1], sys.argv[2]
    base = json.load(open(base_path, encoding="utf-8"))
    patch = json.load(open(patch_path, encoding="utf-8"))
    by_name = {p["page"]: p for p in patch["pages"]}
    base["pages"] = [by_name.pop(p["page"], p) for p in base["pages"]] + list(by_name.values())
    for key, value in patch.get("subs", {}).items():
        if value and value != "none":
            base["subs"][key] = value
    json.dump(base, open(base_path, "w", encoding="utf-8"), indent=1)
    print(f"merged {len(patch['pages'])} pages; subs={base['subs']}")


if __name__ == "__main__":
    main()
