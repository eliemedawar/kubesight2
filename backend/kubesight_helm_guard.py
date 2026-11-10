#!/usr/bin/env python3
"""kubesight-helm-guard: Helm post-renderer for K4 (see api/provisioner_guard.py).

Helm hands it every rendered manifest of an install or upgrade on stdin; it
refuses (exit 1) anything that would run as kubesight-provisioner, mint a token
for it, or (on KubeSight's own cluster) touch its namespace, and otherwise
passes the manifests through unchanged. Loads the guard module by path so it
starts without the rest of KubeSight.
"""

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("provisioner_guard", os.path.join(_HERE, "api", "provisioner_guard.py"))
_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guard)

if __name__ == "__main__":
    sys.exit(_guard.main())
