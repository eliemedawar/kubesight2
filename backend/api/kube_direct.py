"""Read-only Kubernetes API calls without a kubectl subprocess (opt-in).

Every cluster read in KubeSight runs ``kubectl``: a process that starts, loads
the kubeconfig, decodes the API's JSON and encodes it again for us to decode a
third time. Under load that is most of the CPU the backend uses — a stress
test with a hundred users had well over a hundred kubectl processes running at
once. For the plain reads that dominate (``get <kind> [-n ns] -o json``,
``get <kind> <name> -o json``, ``get --raw <path>``, ``version -o json``) this
module sends the same GET straight to the API server with the kubeconfig's own
credentials and returns what kubectl would have printed.

Enabled with ``KUBESIGHT_DIRECT_API_READS=true``. Anything it does not
understand — another verb or flag, exec/auth-provider credentials, a proxy —
returns None and the caller runs kubectl as before, so turning it on can only
change *how* a supported read is made, never *which* reads work.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import ssl
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return os.getenv("KUBESIGHT_DIRECT_API_READS", "false").strip().lower() in {"1", "true", "yes", "on"}


# Reads served directly vs. handed to kubectl (for diagnostics).
stats = {"direct": 0, "kubectl": 0}

_client_version: Optional[Dict[str, Any]] = None


def _kubectl_client_version() -> Dict[str, Any]:
    """kubectl's own clientVersion, read once (``version -o json`` reports it)."""
    global _client_version
    if _client_version is None:
        import subprocess

        try:
            out = subprocess.run(
                ["kubectl", "version", "--client", "-o", "json"],
                capture_output=True, text=True, timeout=20, check=False,
            ).stdout
            _client_version = json.loads(out).get("clientVersion") or {}
        except Exception:
            _client_version = {}
    return _client_version


class DirectReadError(RuntimeError):
    """The API answered with an error; ``message`` is what kubectl would print."""

    def __init__(self, message: str, *, network: bool = False):
        super().__init__(message)
        self.network = network


# kind or alias -> (group, version, plural resource, namespaced, Kind)
_KINDS: Dict[str, Tuple[str, str, str, bool, str]] = {}


def _register(group: str, version: str, plural: str, namespaced: bool, kind: str, *aliases: str) -> None:
    entry = (group, version, plural, namespaced, kind)
    for name in (plural, kind.lower(), *aliases):
        _KINDS[name] = entry


_register("", "v1", "pods", True, "Pod", "pod", "po")
_register("", "v1", "services", True, "Service", "service", "svc")
_register("", "v1", "endpoints", True, "Endpoints", "ep")
_register("", "v1", "configmaps", True, "ConfigMap", "configmap", "cm")
_register("", "v1", "secrets", True, "Secret", "secret")
_register("", "v1", "events", True, "Event", "event", "ev")
_register("", "v1", "persistentvolumeclaims", True, "PersistentVolumeClaim", "pvc")
_register("", "v1", "serviceaccounts", True, "ServiceAccount", "sa")
_register("", "v1", "resourcequotas", True, "ResourceQuota", "quota")
_register("", "v1", "limitranges", True, "LimitRange", "limits")
_register("", "v1", "nodes", False, "Node", "node", "no")
_register("", "v1", "namespaces", False, "Namespace", "namespace", "ns")
_register("", "v1", "persistentvolumes", False, "PersistentVolume", "pv")
_register("apps", "v1", "deployments", True, "Deployment", "deployment", "deploy")
_register("apps", "v1", "replicasets", True, "ReplicaSet", "replicaset", "rs")
_register("apps", "v1", "statefulsets", True, "StatefulSet", "statefulset", "sts")
_register("apps", "v1", "daemonsets", True, "DaemonSet", "daemonset", "ds")
_register("batch", "v1", "jobs", True, "Job", "job")
_register("batch", "v1", "cronjobs", True, "CronJob", "cronjob", "cj")
_register("networking.k8s.io", "v1", "ingresses", True, "Ingress", "ingress", "ing")
_register("networking.k8s.io", "v1", "networkpolicies", True, "NetworkPolicy", "networkpolicy", "netpol")
_register("storage.k8s.io", "v1", "storageclasses", False, "StorageClass", "storageclass", "sc")
_register("autoscaling", "v2", "horizontalpodautoscalers", True, "HorizontalPodAutoscaler", "hpa")
_register("policy", "v1", "poddisruptionbudgets", True, "PodDisruptionBudget", "pdb")


class _Plan:
    __slots__ = ("path", "query", "list_kind", "version", "raw")

    def __init__(self, path: str, query: Dict[str, str], list_kind: Optional[Tuple[str, str]] = None,
                 version: bool = False, raw: bool = False):
        self.path = path
        self.query = query
        self.list_kind = list_kind  # (apiVersion, Kind) for list responses
        self.version = version
        self.raw = raw


def plan_for(args: List[str]) -> Optional[_Plan]:
    """The API request equivalent to these kubectl args, or None if not a plain read."""
    args = [str(a) for a in args]
    if args == ["version", "-o", "json"]:
        return _Plan("/version", {}, version=True)
    if len(args) == 3 and args[0] == "get" and args[1] == "--raw" and args[2].startswith("/"):
        return _Plan(args[2], {}, raw=True)
    if not args or args[0] != "get":
        return None

    rest = args[1:]
    namespace: Optional[str] = None
    all_namespaces = False
    output_json = False
    selector: Optional[str] = None
    positional: List[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token in ("-n", "--namespace") and i + 1 < len(rest):
            namespace = rest[i + 1]
            i += 2
            continue
        if token.startswith("--namespace="):
            namespace = token.split("=", 1)[1]
        elif token in ("-A", "--all-namespaces"):
            all_namespaces = True
        elif token == "-o" and i + 1 < len(rest):
            if rest[i + 1] != "json":
                return None
            output_json = True
            i += 2
            continue
        elif token == "-o=json" or token == "--output=json":
            output_json = True
        elif token in ("-l", "--selector") and i + 1 < len(rest):
            selector = rest[i + 1]
            i += 2
            continue
        elif token.startswith("-"):
            return None  # any other flag: leave it to kubectl
        else:
            positional.append(token)
        i += 1

    if not output_json or not positional or len(positional) > 2:
        return None
    kind_arg = positional[0]
    if "," in kind_arg or "/" in kind_arg:
        return None
    entry = _KINDS.get(kind_arg.lower())
    if entry is None:
        return None
    group, version, plural, namespaced, kind = entry
    base = f"/api/{version}" if not group else f"/apis/{group}/{version}"
    api_version = version if not group else f"{group}/{version}"
    if namespaced and not all_namespaces:
        if not namespace:
            return None  # kubectl would use the kubeconfig's default namespace
        base += f"/namespaces/{urllib.parse.quote(namespace, safe='')}"
    base += f"/{plural}"
    query: Dict[str, str] = {}
    if selector:
        query["labelSelector"] = selector
    if len(positional) == 2:
        if selector or all_namespaces:
            return None
        return _Plan(f"{base}/{urllib.parse.quote(positional[1], safe='')}", query)
    return _Plan(base, query, list_kind=(api_version, kind))


# ---------------------------------------------------------------------------
# Connection settings from a kubeconfig
# ---------------------------------------------------------------------------


class _Endpoint:
    __slots__ = ("server", "context", "headers")

    def __init__(self, server: str, ssl_context: Optional[ssl.SSLContext], headers: Dict[str, str]):
        self.server = server.rstrip("/")
        self.context = ssl_context
        self.headers = headers


_endpoints: Dict[Tuple[str, int, int, str], Optional[_Endpoint]] = {}
_endpoints_lock = threading.Lock()


def _decode_b64(value: str) -> bytes:
    return base64.b64decode(value.encode() if isinstance(value, str) else value)


def _load_endpoint(kubeconfig_text: str, context_name: Optional[str]) -> Optional[_Endpoint]:
    import yaml

    doc = yaml.safe_load(kubeconfig_text) or {}
    ctx_name = context_name or doc.get("current-context")
    ctx = next((c.get("context") or {} for c in doc.get("contexts") or [] if c.get("name") == ctx_name), None)
    if ctx is None:
        return None
    cluster = next((c.get("cluster") or {} for c in doc.get("clusters") or [] if c.get("name") == ctx.get("cluster")), None)
    user = next((u.get("user") or {} for u in doc.get("users") or [] if u.get("name") == ctx.get("user")), {})
    if not cluster or not cluster.get("server"):
        return None
    if cluster.get("proxy-url") or user.get("exec") or user.get("auth-provider") or user.get("username"):
        return None  # leave anything beyond token / client-cert auth to kubectl

    server = str(cluster["server"])
    headers: Dict[str, str] = {"Accept": "application/json", "User-Agent": "kubesight-direct-read"}
    token = user.get("token")
    if not token and user.get("tokenFile"):
        try:
            with open(user["tokenFile"], encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError:
            return None
    if token:
        headers["Authorization"] = f"Bearer {token}"

    ssl_context: Optional[ssl.SSLContext] = None
    if server.startswith("https://"):
        if cluster.get("insecure-skip-tls-verify"):
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        elif cluster.get("certificate-authority-data"):
            ssl_context = ssl.create_default_context(cadata=_decode_b64(cluster["certificate-authority-data"]).decode())
        elif cluster.get("certificate-authority"):
            ssl_context = ssl.create_default_context(cafile=cluster["certificate-authority"])
        else:
            ssl_context = ssl.create_default_context()
        # Accept what kubectl (Go's TLS stack) accepts: Python 3.13+ also turns
        # on X.509 strict mode, which rejects e.g. cluster CAs without a key
        # usage extension that kubectl connects to fine.
        ssl_context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        if cluster.get("tls-server-name"):
            return None
        cert = user.get("client-certificate-data")
        key = user.get("client-key-data")
        if cert and key:
            # load_cert_chain only reads files: write them privately, load, delete.
            tmpdir = tempfile.mkdtemp(prefix="kubesight-direct-")
            try:
                os.chmod(tmpdir, 0o700)
            except OSError:
                pass
            cert_path = os.path.join(tmpdir, "c.pem")
            key_path = os.path.join(tmpdir, "k.pem")
            try:
                for path, data in ((cert_path, cert), (key_path, key)):
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(_decode_b64(data))
                ssl_context.load_cert_chain(cert_path, key_path)
            finally:
                for path in (cert_path, key_path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                try:
                    os.rmdir(tmpdir)
                except OSError:
                    pass
        elif user.get("client-certificate") and user.get("client-key"):
            ssl_context.load_cert_chain(user["client-certificate"], user["client-key"])
        elif cert or key or user.get("client-certificate") or user.get("client-key"):
            return None
    elif not server.startswith("http://"):
        return None
    return _Endpoint(server, ssl_context, headers)


def _endpoint_for(kubeconfig_path: Optional[str], context: Optional[str]) -> Optional[_Endpoint]:
    if not kubeconfig_path:
        return None  # discovered contexts use kubectl's own config resolution
    try:
        stat = os.stat(kubeconfig_path)
    except OSError:
        return None
    key = (str(kubeconfig_path), stat.st_mtime_ns, stat.st_size, context or "")
    with _endpoints_lock:
        if key in _endpoints:
            return _endpoints[key]
    from .kubeconfig_vault import read_kubeconfig_path

    try:
        endpoint = _load_endpoint(read_kubeconfig_path(str(kubeconfig_path)), context)
    except Exception:
        logger.warning("direct API reads: could not use kubeconfig %s; using kubectl", kubeconfig_path, exc_info=True)
        endpoint = None
    with _endpoints_lock:
        # Old versions of an edited kubeconfig are dropped with their key.
        for stale in [k for k in _endpoints if k[0] == key[0] and k[3] == key[3]]:
            _endpoints.pop(stale, None)
        _endpoints[key] = endpoint
    return endpoint


def forget_endpoints() -> None:
    with _endpoints_lock:
        _endpoints.clear()


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def _get(endpoint: _Endpoint, path: str, query: Dict[str, str], timeout: float) -> bytes:
    url = endpoint.server + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
    request = urllib.request.Request(url, headers=endpoint.headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=endpoint.context) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() or b""
        message = ""
        reason = exc.reason if isinstance(exc.reason, str) else ""
        try:
            status = json.loads(body)
            message = status.get("message") or ""
            reason = status.get("reason") or reason
        except ValueError:
            message = body.decode("utf-8", "replace")[:300]
        raise DirectReadError(f"Error from server ({reason or exc.code}): {message}".strip()) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise DirectReadError(f"Unable to connect to the server: net/http: request canceled (Client.Timeout exceeded) {exc}", network=True) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise DirectReadError("Unable to connect to the server: i/o timeout", network=True) from exc
        raise DirectReadError(f"Unable to connect to the server: {reason}", network=True) from exc
    except (ConnectionError, OSError) as exc:
        raise DirectReadError(f"Unable to connect to the server: {exc}", network=True) from exc


def _without_managed_fields(obj: Any) -> Any:
    """Drop metadata.managedFields, as ``kubectl get -o json`` does by default
    (--show-managed-fields=false). It is server-side-apply bookkeeping that
    real clusters attach to every object, often the bulk of its size."""
    if isinstance(obj, dict):
        meta = obj.get("metadata")
        if isinstance(meta, dict):
            meta.pop("managedFields", None)
    return obj


def try_read(args: List[str], kubeconfig_path: Optional[str], context: Optional[str], timeout: float) -> Optional[str]:
    """What ``kubectl <args>`` would print, or None to fall back to kubectl.

    Raises DirectReadError when the API answered with an error or could not be
    reached (``network=True``) — the same outcomes kubectl would fail with."""
    if not enabled():
        return None
    plan = plan_for(args)
    endpoint = _endpoint_for(kubeconfig_path, context) if plan is not None else None
    if plan is None or endpoint is None:
        stats["kubectl"] += 1
        return None
    stats["direct"] += 1

    if plan.version:
        server_version = json.loads(_get(endpoint, plan.path, plan.query, timeout))
        return json.dumps({"clientVersion": _kubectl_client_version(), "serverVersion": server_version}, indent=2)
    if plan.raw:
        return _get(endpoint, plan.path, plan.query, timeout).decode("utf-8", "replace")
    if plan.list_kind is None:
        return json.dumps(_without_managed_fields(json.loads(_get(endpoint, plan.path, plan.query, timeout))))

    # Lists are read in pages of 500 like kubectl does, then joined into the
    # same {"kind": "List", "items": [...]} document kubectl prints, with each
    # item's apiVersion/kind filled in (the API leaves them off list items).
    api_version, kind = plan.list_kind
    items: List[Dict[str, Any]] = []
    query = dict(plan.query, limit="500")
    while True:
        page = json.loads(_get(endpoint, plan.path, query, timeout))
        for item in page.get("items") or []:
            item.setdefault("apiVersion", api_version)
            item.setdefault("kind", kind)
            items.append(_without_managed_fields(item))
        token = (page.get("metadata") or {}).get("continue")
        if not token:
            break
        query["continue"] = token
    return json.dumps({"apiVersion": "v1", "items": items, "kind": "List", "metadata": {"resourceVersion": ""}})
