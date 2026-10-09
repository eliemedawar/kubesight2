"""Compare kubectl output with kube_direct output for the same reads.

    python direct_parity.py <kubeconfig.yaml> [more kubeconfigs...]

Runs each read both ways and diffs the parsed JSON (lists compared item by
item by namespace/name; resourceVersion and churn-volatile status fields are
ignored). Exit code 1 if anything differs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
os.environ["KUBESIGHT_DIRECT_API_READS"] = "true"

from api import kube_direct  # noqa: E402

READS = [
    ["get", "pods", "--all-namespaces", "-o", "json"],
    ["get", "deployments", "--all-namespaces", "-o", "json"],
    ["get", "services", "--all-namespaces", "-o", "json"],
    ["get", "nodes", "-o", "json"],
    ["get", "namespaces", "-o", "json"],
    ["get", "pods", "-n", "kube-system", "-o", "json"],
    ["get", "deployments", "-n", "kube-system", "-o", "json"],
    ["get", "configmaps", "-n", "monitoring", "-o", "json"],
    ["get", "ingress", "-n", "kube-system", "-o", "json"],
    ["get", "statefulsets", "-A", "-o", "json"],
    ["get", "cronjobs", "-A", "-o", "json"],
    ["get", "storageclasses", "-o", "json"],
    ["version", "-o", "json"],
]
VOLATILE = {"resourceVersion", "restartCount", "state", "lastState", "ready", "started", "conditions",
            "readyReplicas", "availableReplicas", "unavailableReplicas", "subsets", "lastTimestamp", "count"}


def scrub(value):
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def first_diff_path(a, b, path=""):
    """The first field path where two parsed documents differ, or None."""
    if type(a) is not type(b):
        return f"{path or '.'} (type)"
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f"{path}.{key} (only in {'kubectl' if key in a else 'direct'})"
            found = first_diff_path(a[key], b[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path} (length {len(a)} vs {len(b)})"
        for i, (x, y) in enumerate(zip(a, b)):
            found = first_diff_path(x, y, f"{path}[{i}]")
            if found:
                return found
        return None
    return None if a == b else f"{path} ({str(a)[:40]!r} vs {str(b)[:40]!r})"


def keyed(doc):
    if isinstance(doc, dict) and "items" in doc:
        return {(i["metadata"].get("namespace", ""), i["metadata"]["name"]): scrub(i) for i in doc["items"]}
    return scrub(doc)


def main() -> int:
    failures = 0
    for kubeconfig in sys.argv[1:]:
        for args in READS:
            kubectl = json.loads(subprocess.run(["kubectl", "--kubeconfig", kubeconfig, *args],
                                                capture_output=True, text=True, check=True).stdout)
            direct = json.loads(kube_direct.try_read(args, kubeconfig, None, 30))
            if args[0] == "version":
                same = kubectl.get("serverVersion") == direct.get("serverVersion") and bool(direct.get("clientVersion"))
                detail = ""
            else:
                a, b = keyed(kubectl), keyed(direct)
                same = a == b and kubectl.get("kind") == direct.get("kind")
                detail = f"kubectl={len(a)} direct={len(b)}"
                if not same and isinstance(a, dict):
                    missing = set(a) ^ set(b)
                    diff = next((k for k in a if k in b and a[k] != b[k]), None)
                    detail += f" only-one-side={list(missing)[:3]} first-diff={diff}"
                    if diff is not None:
                        detail += f" at {first_diff_path(a[diff], b[diff])}"
            print(f"{'OK  ' if same else 'DIFF'} {os.path.basename(kubeconfig):16} {' '.join(args):45} {detail}")
            failures += 0 if same else 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
