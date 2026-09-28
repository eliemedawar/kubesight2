"""Validation for user-supplied Kubernetes names before they reach kubectl.

kubectl (cobra/pflag) parses flags anywhere in argv, including after
positional arguments. A pod "name" of ``--server=https://attacker`` would make
kubectl send the cluster bearer token elsewhere, ``--all`` on a delete wipes a
namespace, ``--as=...`` impersonates. Every name that comes from a request, a
body or an MCP tool call is therefore checked against the Kubernetes naming
rules here before any argv is built:

* resource names (pods, deployments, ...): DNS-1123 subdomain, max 253
* namespaces and container names: DNS-1123 label, max 63

Neither rule can match a value that starts with ``-``, but that is also refused
explicitly so the guarantee does not depend on the regexes.

``unsafe_kubectl_arg`` is the defence-in-depth check the kubectl runners apply
to every argv: connection/identity flags are only ever added by the runner
itself, so any of them appearing in the caller-built args is an injection;
``--all`` is never used, and ``-n`` / ``-c`` values must be valid labels.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

DNS1123_SUBDOMAIN_MAX = 253
DNS1123_LABEL_MAX = 63

_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


class K8sNameError(ValueError):
    """A user-supplied Kubernetes name is not a valid, safe kubectl argument."""


def _check(value: object, what: str, pattern: "re.Pattern[str]", max_len: int, rule: str) -> str:
    if not isinstance(value, str):
        raise K8sNameError(f"Invalid {what}: a string is required.")
    text = value  # exact: the checked string is the one that reaches argv
    if not text:
        raise K8sNameError(f"Invalid {what}: a value is required.")
    if text.startswith("-"):
        raise K8sNameError(f"Invalid {what} {text[:80]!r}: names cannot start with '-'.")
    if len(text) > max_len or not pattern.fullmatch(text):
        raise K8sNameError(
            f"Invalid {what} {text[:80]!r}: must be a {rule} (lowercase letters, "
            f"digits and '-'{', ' + repr('.') if pattern is _SUBDOMAIN_RE else ''}; "
            f"start and end alphanumeric; at most {max_len} characters)."
        )
    return text


def validate_resource_name(value: object, what: str = "resource name") -> str:
    """DNS-1123 subdomain (pods, deployments, services, ...). Returns the name."""
    return _check(value, what, _SUBDOMAIN_RE, DNS1123_SUBDOMAIN_MAX, "DNS-1123 subdomain")


def validate_namespace(value: object, what: str = "namespace") -> str:
    """DNS-1123 label. Returns the name."""
    return _check(value, what, _LABEL_RE, DNS1123_LABEL_MAX, "DNS-1123 label")


def validate_container_name(value: object, what: str = "container name") -> str:
    """DNS-1123 label. Returns the name."""
    return _check(value, what, _LABEL_RE, DNS1123_LABEL_MAX, "DNS-1123 label")


def name_error(
    *,
    namespace: object = None,
    names: Iterable[tuple] = (),
    containers: Iterable[tuple] = (),
) -> Optional[str]:
    """Return the first validation message, or None when everything is valid.

    ``names`` / ``containers`` are ``(value, what)`` pairs; a ``None`` container
    value means "not given" and is skipped. ``namespace=None`` skips the check.
    """
    try:
        if namespace is not None:
            validate_namespace(namespace)
        for value, what in names:
            validate_resource_name(value, what)
        for value, what in containers:
            if value is None or value == "":
                continue
            validate_container_name(value, what)
    except K8sNameError as exc:
        return str(exc)
    return None


# Global flags that change WHERE kubectl connects or WHO it authenticates as.
# The runners add --kubeconfig/--context themselves, before the caller's args;
# a caller-built argv never legitimately contains any of these.
_FORBIDDEN_LONG_FLAGS = (
    "--server",
    "--kubeconfig",
    "--context",
    "--cluster",
    "--user",
    "--token",
    "--as",
    "--as-group",
    "--as-uid",
    "--username",
    "--password",
    "--certificate-authority",
    "--client-certificate",
    "--client-key",
    "--insecure-skip-tls-verify",
    "--tls-server-name",
    "--cache-dir",
    "--profile",
    "--profile-output",
)


_CONTAINER_FLAG_VERBS = {"logs", "exec", "attach", "cp", "debug"}
_MUTATING_VERBS = {
    "delete", "rollout", "scale", "label", "annotate", "patch", "replace",
    "set", "edit", "taint", "drain", "cordon", "uncordon", "autoscale",
}


def _flag_value(args: list, index: int, token: str, short: str, long: str):
    """Value of ``-n X`` / ``-nX`` / ``--namespace X`` / ``--namespace=X`` at index, else None."""
    if token == short or token == long:
        return args[index + 1] if index + 1 < len(args) else ""
    if token.startswith(long + "="):
        return token.split("=", 1)[1]
    if token.startswith(short) and not token.startswith("--") and len(token) > len(short):
        value = token[len(short):]
        return value[1:] if value.startswith("=") else value
    return None


def unsafe_kubectl_arg(args: Iterable[object]) -> Optional[str]:
    """Return a description of the first unsafe token in ``args``, else None.

    Refused: connection/identity flags (only the runner adds those), ``--all``
    anywhere, ``-A``/``--all-namespaces`` on mutating verbs, and a namespace
    (``-n``) or, for logs/exec/cp/attach/debug, a container (``-c``) value
    that is not a DNS-1123 label. Tokens after a bare ``--`` belong to the
    command run by ``kubectl exec`` and are not kubectl flags, so scanning
    stops there.
    """
    argv = [str(item) for item in args]
    verb = ""
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token in ("-n", "--namespace"):
            skip_next = True
            continue
        if not token.startswith("-"):
            verb = token
            break
    for index, token in enumerate(argv):
        if token == "--":
            return None
        if token.startswith("--"):
            flag = token.split("=", 1)[0]
            if flag in _FORBIDDEN_LONG_FLAGS or flag == "--all":
                return token
            if flag == "--all-namespaces" and verb in _MUTATING_VERBS:
                return token
        elif token.startswith("-s") and token != "-":
            # -s is the shorthand for --server (pflag also accepts -sVALUE).
            return token
        elif token == "-A" and verb in _MUTATING_VERBS:
            return token
        namespace = _flag_value(argv, index, token, "-n", "--namespace")
        if namespace is not None:
            try:
                validate_namespace(namespace)
            except K8sNameError:
                return f"namespace {namespace!r}"
        if verb in _CONTAINER_FLAG_VERBS:
            container = _flag_value(argv, index, token, "-c", "--container")
            if container is not None:
                try:
                    validate_container_name(container)
                except K8sNameError:
                    return f"container {container!r}"
    return None


# helm is a cobra/pflag CLI too: the same smuggling works on it. run_helm adds
# --kubeconfig/--kube-context itself; the rest redirect the connection or the
# credentials (--kube-apiserver, --kube-token, --kube-as-*), the repo/registry
# config, or run an arbitrary binary (--post-renderer).
_FORBIDDEN_HELM_FLAGS = (
    "--kubeconfig",
    "--post-renderer",
    "--post-renderer-args",
    "--registry-config",
    "--repository-config",
    "--repository-cache",
)
_ALLOWED_HELM_KUBE_FLAGS = ("--kube-version",)


def unsafe_helm_arg(args: Iterable[object]) -> Optional[str]:
    """Return the first unsafe token in a caller-built helm argv, else None."""
    argv = [str(item) for item in args]
    for index, token in enumerate(argv):
        if token == "--":
            return None
        if token.startswith("--"):
            flag = token.split("=", 1)[0]
            if flag in _FORBIDDEN_HELM_FLAGS:
                return token
            if flag.startswith("--kube-") and flag not in _ALLOWED_HELM_KUBE_FLAGS:
                return token
        namespace = _flag_value(argv, index, token, "-n", "--namespace")
        if namespace is not None:
            try:
                validate_namespace(namespace)
            except K8sNameError:
                return f"namespace {namespace!r}"
    return None
