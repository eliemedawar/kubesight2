"""Time a few endpoints one at a time (single user, no load).

    python probe.py --repeat 3 /api/clusters "/api/upgrades/info?clusterId={c}" ...

``{c}`` is replaced by the prod-large cluster id, ``{ns}`` by "payments".
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ksclient import KubeSight  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--base", default="http://127.0.0.1:5091")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--pause", type=float, default=0.0)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="admin123")
    args = parser.parse_args()
    ks = KubeSight(args.base)
    ks.login(args.user, args.password)
    _, body, _, _ = ks.get("/api/clusters")
    items = ((body or {}).get("data") or {}).get("clusters") or ((body or {}).get("data") or {}).get("items") or []
    cid = next((c["id"] for c in items if "prod-large" in str(c.get("name"))), items[0]["id"] if items else "")
    for raw in args.paths:
        path = raw.replace("{c}", cid).replace("{ns}", "payments")
        times = []
        for _ in range(args.repeat):
            status, data, ms, size = ks.request("GET", path)
            times.append(f"{ms:.0f}ms/{status}/{size // 1024}KB")
            if args.pause:
                time.sleep(args.pause)
        err = ""
        if isinstance(data, dict) and data.get("error"):
            err = f" error={str(data.get('error'))[:120]}"
        print(f"{path[:90]:90} {'  '.join(times)}{err}", flush=True)


if __name__ == "__main__":
    main()
