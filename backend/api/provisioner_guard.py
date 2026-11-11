"""Nobody acts as ``kubesight-provisioner`` through KubeSight (K4).

OCI's policy lets the service account ``kubesight-provisioner`` create and
delete OKE clusters, and Kubernetes cannot limit which account a pod runs as.
KubeSight manages the cluster it runs in, so without this a person with deploy
rights there could, through KubeSight, start a pod as that account, exec into
a provisioner Job's pod, mint a token Secret for it, or read the namespace's
Secrets.

  * On KubeSight's own cluster (the in-cluster context: no kubeconfig file),
    in the provisioner's namespace, every kubectl action is refused except
    plain reads of non-secret objects and logs; Secrets there are never read,
    not even across all namespaces; Helm releases there are refused.
  * On every cluster, a manifest (kubectl ``-f`` or Helm's rendered output,
    through ``kubesight-helm-guard`` as a post-renderer) is refused when it runs
    anything as the provisioner or makes a token Secret for it; on KubeSight's
    own cluster also when it puts anything in the provisioner's namespace.
  * A file that cannot be read or parsed is refused (fail closed).

The provisioner Jobs themselves are launched by ``provisioning.remote`` through
its own kubectl, from a fixed template. The platform's admission rule
(deploy/ai-cluster/oke/provisioner-admission.yaml) holds the same line inside
Kubernetes, whoever asks.

Active when the install runs OKE by workload identity
(``KUBESIGHT_PROVISIONER_SERVICE_ACCOUNT`` set).

``KUBESIGHT_ALLOW_SELF_DEPLOY`` (off by default) lets one change through on
KubeSight's own cluster: ``kubectl apply -f`` of KubeSight's own Deployments
(``KUBESIGHT_SELF_DEPLOY_DEPLOYMENTS``) whose every container runs one of its
own images (``KUBESIGHT_SELF_DEPLOY_IMAGES`` prefixes), so its CI can roll it
out. The provisioner checks above still apply to those manifests.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, List, Optional

import yaml

_READ_VERBS = {"get", "describe", "logs", "top", "events", "explain", "api-resources", "api-versions",
               "version", "auth", "wait", "config", "cluster-info"}
_TOKEN_ANNOTATION = "kubernetes.io/service-account.name"


class ProvisionerGuardError(RuntimeError):
    pass


def active() -> bool:
    return bool(os.getenv("KUBESIGHT_PROVISIONER_SERVICE_ACCOUNT", "").strip())


def namespace() -> str:
    return (os.getenv("KUBESIGHT_PROVISIONER_NAMESPACE") or os.getenv("CI_BACKEND_NAMESPACE") or "kubesight").strip()


def service_account() -> str:
    return (os.getenv("KUBESIGHT_PROVISIONER_SERVICE_ACCOUNT") or "kubesight-provisioner").strip()


def namespace_of(args: List[str]) -> Optional[str]:
    """The namespace kubectl/helm would use: the LAST -n/--namespace wins."""
    found: Optional[str] = None
    for index, token in enumerate(args):
        if token == "--":
            break
        if token in ("-n", "--namespace") and index + 1 < len(args):
            found = args[index + 1]
        elif token.startswith("--namespace="):
            found = token.split("=", 1)[1]
        elif token.startswith("-n=") or (token.startswith("-n") and len(token) > 2 and not token.startswith("--")):
            found = token[3:] if token.startswith("-n=") else token[2:]
    return found


def _files(args: List[str]) -> List[str]:
    out = []
    for index, token in enumerate(args):
        if token in ("-f", "--filename") and index + 1 < len(args):
            out.append(args[index + 1])
        elif token.startswith("--filename=") or token.startswith("-f="):
            out.append(token.split("=", 1)[1])
    return out


def self_deploy_allowed() -> bool:
    """KUBESIGHT_ALLOW_SELF_DEPLOY: KubeSight's CI may roll out KubeSight itself
    (its own Deployments, its own images) in its own namespace. Off by default:
    K4 keeps that namespace read-only, because whoever changes the backend's
    image also drives the provisioner Jobs the backend launches."""
    return (os.getenv("KUBESIGHT_ALLOW_SELF_DEPLOY") or "").strip().lower() in ("1", "true", "yes", "on")


def _csv(name: str, default: str) -> List[str]:
    return [part.strip() for part in (os.getenv(name) or default).split(",") if part.strip()]


def self_deploy_deployments() -> List[str]:
    return _csv("KUBESIGHT_SELF_DEPLOY_DEPLOYMENTS", "kubesight-backend,kubesight-frontend")


def self_deploy_images() -> List[str]:
    return _csv("KUBESIGHT_SELF_DEPLOY_IMAGES",
                "registry.areeba.com/kubesight-backend:,registry.areeba.com/kubesight-frontend:")


def _is_self_deploy(document) -> bool:
    """One of KubeSight's own Deployments, every container on KubeSight's own
    images: the only change self-deploy lets into the provisioner's namespace."""
    if not self_deploy_allowed() or not isinstance(document, dict):
        return False
    meta = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    if document.get("kind") != "Deployment" or meta.get("name") not in self_deploy_deployments():
        return False
    if meta.get("namespace") not in (None, namespace()):
        return False
    pod = (((document.get("spec") or {}).get("template") or {}).get("spec") or {})
    containers = list(pod.get("containers") or []) + list(pod.get("initContainers") or [])
    if not containers or pod.get("ephemeralContainers"):
        return False
    prefixes = self_deploy_images()
    return all(isinstance(c, dict) and any(str(c.get("image") or "").startswith(p) for p in prefixes)
               for c in containers)


def _only_self_deploys(text: str) -> bool:
    try:
        documents = [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError:
        return False
    return bool(documents) and all(_is_self_deploy(d) for d in documents)


def _walk(node) -> Iterable[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def check_manifest_text(text: str, *, self_cluster: bool) -> None:
    """Refuse a manifest that runs anything as the provisioner, makes a token
    for it, or (on KubeSight's own cluster) touches the provisioner's namespace."""
    if not active() or not text:
        return
    sa = service_account()
    ns = namespace()
    try:
        documents = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        raise ProvisionerGuardError("Refused: the manifest could not be read to check it.") from exc
    for document in documents:
        for node in _walk(document):
            if node.get("serviceAccountName") == sa or node.get("serviceAccount") == sa:
                raise ProvisionerGuardError(
                    f"Refused: nothing may run as {sa} through KubeSight (it may create and delete "
                    "OKE clusters)."
                )
            annotations = (node.get("metadata") or {}).get("annotations") if isinstance(node.get("metadata"), dict) else None
            if isinstance(annotations, dict) and annotations.get(_TOKEN_ANNOTATION) == sa:
                raise ProvisionerGuardError(f"Refused: no token Secret for {sa}.")
        if self_cluster and isinstance(document, dict) and not _is_self_deploy(document):
            for node in _walk(document):
                meta = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
                if meta.get("namespace") == ns or (node.get("kind") == "Namespace" and meta.get("name") == ns):
                    raise ProvisionerGuardError(
                        f"Refused: KubeSight does not change the namespace {ns} of its own cluster."
                    )


def _mentions_secrets(args: List[str]) -> bool:
    for token in args[1:]:
        if token == "--":
            break
        if token.startswith("-"):
            continue
        for part in re.split(r"[,/]", token.lower()):
            if part.split(".")[0] in ("secret", "secrets"):
                return True
    return False


def check_kubectl(args: List[str], *, kubeconfig_path: Optional[str]) -> None:
    if not active() or not args:
        return
    self_cluster = not kubeconfig_path
    verb = args[0]
    files = _files(args)
    self_deploy = bool(files) and verb == "apply" and "--prune" not in args
    for path in files:
        if path == "-":
            self_deploy = False
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise ProvisionerGuardError("Refused: the manifest file could not be read to check it.") from exc
        check_manifest_text(text, self_cluster=self_cluster)
        self_deploy = self_deploy and _only_self_deploys(text)
    if not self_cluster:
        return
    target = namespace_of(args)
    everywhere = "-A" in args or "--all-namespaces" in args
    if verb in _READ_VERBS:
        if (target == namespace() or everywhere) and _mentions_secrets(args):
            raise ProvisionerGuardError(f"Refused: KubeSight does not read the Secrets of {namespace()}.")
        return
    if self_deploy and not everywhere:
        # KUBESIGHT_ALLOW_SELF_DEPLOY: only KubeSight's own Deployments on its own images.
        return
    if target == namespace() or everywhere:
        raise ProvisionerGuardError(
            f"Refused: '{verb}' in the namespace {namespace()} of KubeSight's own cluster. Only reads are "
            "allowed there: its pods may act as the OKE provisioner."
        )


def check_helm(namespace_name: Optional[str], *, kubeconfig_path: Optional[str]) -> None:
    if active() and not kubeconfig_path and namespace_name == namespace():
        raise ProvisionerGuardError(
            f"Refused: Helm changes in the namespace {namespace()} of KubeSight's own cluster."
        )


HELM_WRITE_VERBS = ("install", "upgrade")
POST_RENDERER = "kubesight-helm-guard"


def helm_post_renderer_args(args: List[str], *, kubeconfig_path: Optional[str]) -> List[str]:
    """Extra helm flags that make Helm hand its rendered manifests to the guard
    before anything reaches the cluster (install and upgrade)."""
    if not active() or not args or args[0] not in HELM_WRITE_VERBS:
        return []
    if any(a == "--post-renderer" or a.startswith("--post-renderer=") for a in args):
        raise ProvisionerGuardError("Refused: Helm post-renderers are KubeSight's own here.")
    return ["--post-renderer", POST_RENDERER]


def helm_guard_env(*, kubeconfig_path: Optional[str]) -> dict:
    return {"KUBESIGHT_GUARD_SELF_CLUSTER": "1" if not kubeconfig_path else "0"}


def main() -> int:  # pragma: no cover — exercised through kubesight_helm_guard.py
    import sys

    text = sys.stdin.read()
    try:
        check_manifest_text(text, self_cluster=os.getenv("KUBESIGHT_GUARD_SELF_CLUSTER") == "1")
    except ProvisionerGuardError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0
