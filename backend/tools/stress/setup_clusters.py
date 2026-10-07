"""Register the fake clusters with a KubeSight instance through its own API.

    python setup_clusters.py --kubeconfig-dir <dir written by fake_k8s.py> [--base http://127.0.0.1:5091]

Idempotent: clusters that already exist by name are skipped.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ksclient import KubeSight  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5091")
    parser.add_argument("--kubeconfig-dir", required=True)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="admin123")
    args = parser.parse_args()

    ks = KubeSight(args.base)
    ks.login(args.user, args.password)
    status, body, _, _ = ks.get("/api/clusters/custom")
    existing = {c.get("name") for c in ((body or {}).get("data") or {}).get("clusters", [])} if status == 200 else set()
    for path in sorted(glob.glob(os.path.join(args.kubeconfig_dir, "*.yaml"))):
        name = os.path.splitext(os.path.basename(path))[0]
        if name in existing:
            print(f"skip {name} (exists)")
            continue
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        status, body, ms, _ = ks.post("/api/clusters/custom", {"name": name, "connectionMethod": "kubeconfig", "kubeconfigContent": content})
        test = ((body or {}).get("data") or {}).get("test") if isinstance(body, dict) else None
        print(f"{name}: {status} in {ms:.0f}ms test={json.dumps(test)[:160] if test else body}")


if __name__ == "__main__":
    main()
