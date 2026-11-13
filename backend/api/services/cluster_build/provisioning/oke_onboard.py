"""Registering an OKE cluster made by workload identity (K16, K20, K21).

No fixed credential for the cluster is stored anywhere. The backend reaches
its API only with short-lived tokens made on demand by workload identity (as
``kubesight-backend``); the kubeconfig KubeSight keeps names an exec plugin
(``kubesight-oke-token``), never a token.

  1. read the cluster's CA (OKE's kubeconfig API) and wait for its API;
  2. ask the API who the backend is (SelfSubjectReview: works with no binding);
  3. a provisioner Job binds that subject to the backend's named role (never
     cluster-admin) and installs the LoadBalancer admission policy;
  4. check the backend can now read the cluster;
  5. put the read-only registry pull Secret on it (K16), if one is configured;
  6. register the cluster with the token-less kubeconfig.
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
from typing import Any, Callable, Dict, Optional, Tuple

from ....db import db
from ....models import ClusterBuild, ClusterProvisionJob
from . import cloud_build, remote
from .clouds import accounts, oci_wi

_API_WAIT_S = 10 * 60
PULL_SECRET = "kubesight-registry"


class OnboardError(RuntimeError):
    pass


# Test seam: (method, url, token, body, ca_data) -> (status, json)
_api_override: Optional[Callable[..., Tuple[int, Dict[str, Any]]]] = None


def set_api(fn) -> None:
    global _api_override
    _api_override = fn


_ca_override: Optional[Callable[..., str]] = None


def set_ca_reader(fn) -> None:
    """Test seam: (account, region, cluster_id) -> base64 CA."""
    global _ca_override
    _ca_override = fn


def _api(server: str, ca_data: str, method: str, path: str, *, region: str, cluster_id: str,
         body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
    if _api_override is not None:
        return _api_override(method, server + path, body, ca_data)
    token = oci_wi.cluster_token(region, cluster_id)  # fresh each call: short-lived
    opener = cloud_build._bypass_proxy_opener(ca_data)
    return cloud_build._kube(opener, server, token, method, path, body)


def _cluster_ca(account, region: str, cluster_id: str) -> str:
    """The cluster's CA, from OKE's kubeconfig API (the backend may 'use clusters')."""
    import yaml

    from .clouds import oci

    engine = oci._endpoint("containerengine", region)
    raw, _ = oci._call(
        account, "POST", f"{engine}/clusters/{urllib.parse.quote(cluster_id, safe='')}/kubeconfig/content",
        body={"tokenVersion": "2.0.0", "endpoint": "PRIVATE_ENDPOINT"},
        what="reading the OKE cluster's kubeconfig", accept="application/x-yaml, text/plain, */*",
    )
    try:
        document = yaml.safe_load(raw.decode("utf-8")) or {}
        return document["clusters"][0]["cluster"]["certificate-authority-data"]
    except (ValueError, KeyError, IndexError, TypeError, yaml.YAMLError) as exc:
        raise OnboardError("OKE's kubeconfig for the cluster could not be read.") from exc


def _wait_for_api(server: str, ca_data: str, region: str, cluster_id: str, on_line) -> None:
    deadline = time.monotonic() + _API_WAIT_S
    last = ""
    while True:
        try:
            status, _ = _api(server, ca_data, "GET", "/version", region=region, cluster_id=cluster_id)
            if status == 200:
                return
            last = f"HTTP {status}"
        except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
            last = str(getattr(exc, "reason", exc))
        if time.monotonic() > deadline:
            raise OnboardError(
                f"The cluster's API at {server} did not answer within {_API_WAIT_S // 60} minutes ({last}). "
                "KubeSight reaches it over the hub firewall on 6443."
            )
        on_line(f"Waiting for the cluster's API at {server}: {last}")
        time.sleep(10)


def _whoami(server: str, ca_data: str, region: str, cluster_id: str) -> str:
    status, answer = _api(server, ca_data, "POST", "/apis/authentication.k8s.io/v1/selfsubjectreviews",
                          region=region, cluster_id=cluster_id,
                          body={"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"})
    if status not in (200, 201):
        raise OnboardError(
            f"The cluster refused 'who am I' for the backend (HTTP {status}): "
            f"{answer.get('message') or answer}. OKE may refuse workload-identity tokens: stop and tell "
            "the platform owner."
        )
    username = (((answer.get("status") or {}).get("userInfo") or {}).get("username")) or ""
    if not username:
        raise OnboardError("The cluster did not say who the backend is.")
    return username


def _pull_secret(server: str, ca_data: str, region: str, cluster_id: str, settings: Dict[str, Any],
                 on_line) -> None:
    """K16: the read-only registry account, on the new cluster only (default
    namespace and its default service account). Never in OpenTofu."""
    username = (os.getenv("KUBESIGHT_OKE_PULL_USERNAME") or "").strip()
    password = os.getenv("KUBESIGHT_OKE_PULL_PASSWORD") or ""
    host = settings.get("registryHost") or ""
    if not username or not password or not host:
        on_line("No registry pull account configured (KUBESIGHT_OKE_PULL_USERNAME): skipped the pull Secret.")
        return
    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    docker = json.dumps({"auths": {host: {"auth": auth}}}).encode("utf-8")
    secret = {
        "apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
        "metadata": {"name": PULL_SECRET, "namespace": "default",
                     "labels": {"app.kubernetes.io/managed-by": "kubesight"}},
        "data": {".dockerconfigjson": base64.b64encode(docker).decode("ascii")},
    }
    status, answer = _api(server, ca_data, "POST", "/api/v1/namespaces/default/secrets",
                          region=region, cluster_id=cluster_id, body=secret)
    if status == 409:
        status, answer = _api(server, ca_data, "PUT", f"/api/v1/namespaces/default/secrets/{PULL_SECRET}",
                              region=region, cluster_id=cluster_id, body=secret)
    if status not in (200, 201):
        raise OnboardError(f"The cluster refused the registry pull Secret (HTTP {status}): {answer.get('message')}")
    patch = {"apiVersion": "v1", "kind": "ServiceAccount",
             "metadata": {"name": "default", "namespace": "default"},
             "imagePullSecrets": [{"name": PULL_SECRET}]}
    status, answer = _api(server, ca_data, "PUT", "/api/v1/namespaces/default/serviceaccounts/default",
                          region=region, cluster_id=cluster_id, body=patch)
    if status not in (200, 201):
        on_line(f"The default service account kept its pull Secrets (HTTP {status}).")
    on_line(f"Registry pull Secret {PULL_SECRET} put on the cluster (default namespace).")


def _bind(job: ClusterProvisionJob, build: ClusterBuild, on_line) -> Optional[Dict[str, Any]]:
    """Sign in, have a provisioner Job bind the backend's subject to its role,
    and check the backend can read the cluster. None in simulation."""
    facts = build.cloud_facts_json or {}
    cluster = facts.get("cluster") or {}
    cluster_id = cluster.get("id")
    endpoint = cluster.get("privateEndpoint") or {}
    if not cluster_id or not endpoint.get("address"):
        raise OnboardError("The provisioner Job did not report the cluster's OCID and private endpoint.")
    spec = build.provisioning_json or {}
    region = spec["region"]
    _, account = cloud_build.account_for(build)
    server = f"https://{endpoint['address']}:{int(endpoint.get('port') or 6443)}"
    if not re.fullmatch(r"https://[0-9.]+:\d{2,5}", server):
        raise OnboardError(f"{server} is not a private API endpoint.")

    if accounts.simulated() and _api_override is None:
        remote.audit("bind", job=job, build=build, subject="simulated", result="skipped")
        on_line("Simulated cloud: the cluster is not registered (there is no API to reach).")
        return None

    ca_data = (_ca_override or _cluster_ca)(account, region, cluster_id)
    on_line(f"Signing in to {server} with a short-lived workload-identity token")
    _wait_for_api(server, ca_data, region, cluster_id, on_line)
    subject = _whoami(server, ca_data, region, cluster_id)
    on_line(f"The backend is {subject} on this cluster.")

    manifests = remote.bind_manifests(subject)
    remote._set_results(job, "bind", {"spec": {
        "clusterId": cluster_id, "region": region, "subject": subject,
        "roleName": remote.backend_role(), "manifests": manifests, "waitSeconds": 600,
    }})
    db.session.commit()
    remote.run_phase(job.id, "bind", on_line, 20 * 60)
    remote.audit("bind", job=job, build=build, subject=subject, role=remote.backend_role(), result="bound")

    status, answer = _api(server, ca_data, "GET", "/api/v1/namespaces/default", region=region, cluster_id=cluster_id)
    if status != 200:
        raise OnboardError(f"After the binding the backend still cannot read the cluster (HTTP {status}).")
    if remote.full_rights():
        _drop_backend_fence(server, ca_data, region, cluster_id, on_line)
    build.provisioning_json = {**(build.provisioning_json or {}), "backendRole": remote.backend_role()}
    return {"server": server, "ca": ca_data, "clusterId": cluster_id, "region": region,
            "account": account, "endpoint": endpoint}


def _drop_backend_fence(server: str, ca_data: str, region: str, cluster_id: str, on_line) -> None:
    """With full rights the old fence (no kube-system writes) goes, so charts
    may install there. The backend deletes it itself, now cluster-admin."""
    base = "/apis/admissionregistration.k8s.io/v1"
    for kind in ("validatingadmissionpolicybindings", "validatingadmissionpolicies"):
        status, _ = _api(server, ca_data, "DELETE", f"{base}/{kind}/{remote.BACKEND_LIMITS}",
                         region=region, cluster_id=cluster_id)
        if status not in (200, 202, 404):
            raise OnboardError(f"Could not remove the {kind[:-1]} {remote.BACKEND_LIMITS} (HTTP {status}).")
    on_line("KubeSight has full rights on this cluster (cluster-admin); the load-balancer rule (K21) stays.")


def rebind(job: ClusterProvisionJob, build: ClusterBuild, on_line=lambda line: None) -> None:
    """Day two: bind the backend again with the role set now (e.g. full rights);
    the cluster's registration in KubeSight stays as it is."""
    _bind(job, build, on_line)


def onboard(job: ClusterProvisionJob, build: ClusterBuild, on_line=lambda line: None) -> Optional[str]:
    from .. import onboard as build_onboard

    bound = _bind(job, build, on_line)
    if bound is None:
        return None
    server, ca_data, cluster_id, region = bound["server"], bound["ca"], bound["clusterId"], bound["region"]
    account, endpoint = bound["account"], bound["endpoint"]
    _pull_secret(server, ca_data, region, cluster_id, account.settings, on_line)

    host = endpoint["address"]
    build.control_plane_endpoint = f"{host}:{int(endpoint.get('port') or 6443)}"
    kubeconfig = remote.exec_kubeconfig(build.name, server, ca_data, cluster_id, region)
    if "token:" in kubeconfig:
        raise OnboardError("Refusing to store a kubeconfig that holds a token.")
    public_id = build_onboard.register_cluster(build, kubeconfig)
    on_line(f"Registered as cluster {public_id}; KubeSight reaches it with short-lived tokens only.")
    remote.audit("registered", job=job, build=build, clusterId=public_id)
    return public_id


# ---------------------------------------------------------------------------
# The pull Secret where an app goes (K16: the read-only registry login lives on
# the cluster only; onboarding puts it in "default", an app may use another
# namespace)
# ---------------------------------------------------------------------------

_runner_override = None


def set_kubectl_runner(fn) -> None:
    """Test seam: ``fn(cluster_id, args) -> stdout``."""
    global _runner_override
    _runner_override = fn


def _kubectl_on(cluster_id: str, args):
    if _runner_override is not None:
        return _runner_override(cluster_id, args)
    from ...deployment_service import _run_kubectl_for_cluster

    return _run_kubectl_for_cluster(cluster_id, args)


def ensure_pull_secret(cluster_id: str, namespace: str, on_line=lambda line: None) -> bool:
    """Copy the registry pull Secret from "default" into ``namespace`` and
    attach it to that namespace's default service account. False (and a log
    line) when there is none to copy; never raises."""
    import tempfile

    if not namespace or namespace == "default":
        return True
    try:
        raw = _kubectl_on(cluster_id, ["get", "secret", PULL_SECRET, "-n", "default", "-o", "json"])
        source = json.loads(raw or "{}")
    except Exception as exc:  # noqa: BLE001
        on_line(f"No registry pull Secret to copy into {namespace} ({str(exc)[:120]}): images from the "
                "private registry will not pull there unless the chart brings its own.")
        return False
    secret = {
        "apiVersion": "v1", "kind": "Secret", "type": source.get("type") or "kubernetes.io/dockerconfigjson",
        "metadata": {"name": PULL_SECRET, "namespace": namespace,
                     "labels": {"app.kubernetes.io/managed-by": "kubesight"}},
        "data": source.get("data") or {},
    }
    namespace_doc = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump({"apiVersion": "v1", "kind": "List", "items": [namespace_doc, secret]}, handle)
            path = handle.name
        _kubectl_on(cluster_id, ["apply", "-f", path])
        patch = json.dumps({"imagePullSecrets": [{"name": PULL_SECRET}]})
        for attempt in range(10):
            try:
                _kubectl_on(cluster_id, ["patch", "serviceaccount", "default", "-n", namespace, "-p", patch])
                break
            except Exception:  # noqa: BLE001 — the controller makes "default" a moment after the namespace
                if attempt == 9:
                    raise
                time.sleep(1)
    except Exception as exc:  # noqa: BLE001
        on_line(f"Could not put the registry pull Secret in {namespace}: {str(exc)[:200]}")
        return False
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    on_line(f"Registry pull Secret {PULL_SECRET} is in {namespace} (its default service account uses it).")
    return True
