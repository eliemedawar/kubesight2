#!/usr/bin/env python3
"""kubesight-oke-token: kubectl's exec plugin for OKE clusters made by workload identity (K20).

KubeSight keeps no fixed credential for these clusters: the kubeconfig it
stores names this plugin, and every token is made here, on demand, by OKE
workload identity as the backend's own service account. It prints what
``oci ce cluster generate-token`` prints, as a client.authentication
ExecCredential that expires in four minutes.

A token is reused by the next kubectl calls for up to three minutes (the
backend runs many short kubectl processes); the cache lives in this pod's
/tmp, 0600, and holds nothing longer-lived than the token itself.

Standalone on purpose: it imports nothing of KubeSight, so kubectl starts it fast.

    kubesight-oke-token --cluster-id ocid1.cluster... --region me-dubai-1
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

TOKEN_SECONDS = 240          # what OKE's own CLI announces
REUSE_SECONDS = 180          # reuse a token while it has a minute left
CACHE_DIR = os.path.join(tempfile.gettempdir(), "kubesight-oke-tokens")


def _credential(token: str, expires: float) -> str:
    stamp = datetime.fromtimestamp(expires, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({
        "apiVersion": "client.authentication.k8s.io/v1beta1",
        "kind": "ExecCredential",
        "status": {"token": token, "expirationTimestamp": stamp},
    })


def _cache_path(cluster_id: str, region: str) -> str:
    key = hashlib.sha256(f"{region}/{cluster_id}".encode("utf-8")).hexdigest()[:32]
    return os.path.join(CACHE_DIR, f"{key}.json")


def _cached(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("made", 0) + REUSE_SECONDS > time.time():
            return data["token"], data["made"] + TOKEN_SECONDS
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _store(path: str, token: str, made: float) -> None:
    try:
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=CACHE_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"token": token, "made": made}, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass  # the cache is only a speed-up


def make_token(cluster_id: str, region: str, signer=None) -> str:
    try:
        import requests
    except ImportError:  # the OCI SDK ships its own copy
        from oci._vendor import requests

    if signer is None:
        from oci.auth.signers import get_oke_workload_identity_resource_principal_signer

        signer = get_oke_workload_identity_resource_principal_signer()
    url = f"https://containerengine.{region}.oraclecloud.com/cluster_request/{cluster_id}"
    prepared = requests.Request("GET", url).prepare()
    signer(prepared)
    headers = {k.lower(): v for k, v in prepared.headers.items()}
    query = urllib.parse.urlencode({"authorization": headers["authorization"], "date": headers["date"]})
    return base64.urlsafe_b64encode(f"{url}?{query}".encode("utf-8")).decode("utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="kubesight-oke-token")
    parser.add_argument("--cluster-id", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"ocid1\.cluster\.[a-z0-9.\-]+", args.cluster_id) or \
            not re.fullmatch(r"[a-z]{2,}-[a-z]+-\d+", args.region):
        print("kubesight-oke-token: bad cluster id or region", file=sys.stderr)
        return 2
    path = _cache_path(args.cluster_id, args.region)
    hit = _cached(path)
    if hit:
        print(_credential(*hit))
        return 0
    try:
        token = make_token(args.cluster_id, args.region)
    except Exception as exc:  # noqa: BLE001 — kubectl shows stderr
        print(f"kubesight-oke-token: workload identity could not make a token: {exc}", file=sys.stderr)
        return 1
    made = time.time()
    _store(path, token, made)
    print(_credential(token, made + TOKEN_SECONDS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
