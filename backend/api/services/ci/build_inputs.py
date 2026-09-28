"""Grammar for the values the engine itself splices into a generated build.

A handful of build variables are not questions for a person but inputs the
engine reads — which Dockerfile to build, what to tag the image, what to call
the repository. Those end up inside the image stage's generated shell (the
``buildctl`` / ``trivy`` / ``crane`` lines), in the one container that has the
registry push credentials mounted. So they are checked against the grammar of
the thing they name wherever they enter — at trigger time, at pipeline save —
and the runner re-checks them and shell-quotes them before use.

Every check returns a user-facing problem string, or None when the value is
fine; callers raise their own error type with it.
"""

from __future__ import annotations

import re
from typing import Optional

# Repository-relative: no leading '/', no '..' segment, a plain filename alphabet.
DOCKERFILE_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")

# The OCI distribution spec's tag grammar.
IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")

# A tag the build's own shell finishes, e.g. ``V${VERSION}-${KUBESIGHT_BUILD_NUMBER}``.
# Only a pipeline author may write one (stage env); a trigger may not. Nothing in
# this alphabet can end the double-quoted string it lands in or run a command.
IMAGE_TAG_TEMPLATE_RE = re.compile(r"^[A-Za-z0-9._${}-]{1,255}$")

# OCI repository name components. Case-insensitive because the engine lowercases
# IMAGE_NAME itself, and existing automation sends mixed case.
_REPO_COMPONENT = r"[A-Za-z0-9]+(?:(?:[._]|__|-+)[A-Za-z0-9]+)*"
IMAGE_NAME_RE = re.compile(rf"^{_REPO_COMPONENT}(?:/{_REPO_COMPONENT})*$")

# host[:port] — a DNS name, an IPv4 address, or a bracketed IPv6 address.
_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
REGISTRY_HOST_RE = re.compile(
    rf"^(?:{_HOST_LABEL}(?:\.{_HOST_LABEL})*|\[[0-9A-Fa-f:.]+\])(?::[0-9]{{1,5}})?$"
)

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

# Names a build trigger may not set as free-form variables: each one changes how
# the stage's shell, loader or tooling behaves rather than passing it a value.
# A pipeline author can still declare them as parameters on purpose.
_DENIED_ENV_NAMES = frozenset(
    {
        "PATH", "HOME", "SHELL", "IFS", "ENV", "BASH_ENV", "SHELLOPTS", "BASHOPTS",
        "PS4", "CDPATH", "PROMPT_COMMAND", "DOCKER_CONFIG", "BUILDKIT_HOST",
        "BUILDKIT_CACHE_DIR", "KUBESIGHT_ENV", "KUBESIGHT_WORKSPACE",
        "KUBESIGHT_SOURCE", "KUBESIGHT_CACHE", "KUBESIGHT_CACHE_DIR",
        "KUBESIGHT_SHARED_CACHE_DIR", "KUBESIGHT_NODE_MODULES_DIR",
    }
)
_DENIED_ENV_PREFIXES = ("LD_", "DYLD_", "BASH_FUNC_")


def _text(value) -> str:
    return "" if value is None else str(value)


def dockerfile_path_problem(value) -> Optional[str]:
    text = _text(value)
    if not DOCKERFILE_PATH_RE.match(text):
        return (
            "DOCKERFILE_PATH must be a repository-relative path of letters, "
            "digits, '.', '_', '-' and '/' (at most 255 characters)."
        )
    if text.startswith("/"):
        return "DOCKERFILE_PATH must be relative to the repository, not absolute."
    parts = text.split("/")
    if ".." in parts:
        return "DOCKERFILE_PATH may not step outside the repository ('..')."
    if not parts[-1] or parts[-1] == ".":
        return "DOCKERFILE_PATH must name a file."
    return None


def working_directory_problem(value) -> Optional[str]:
    """A build context / working directory: repository-relative, no '..'."""
    text = _text(value).strip()
    if not text:
        return None
    if any(ch in text for ch in "\x00\r\n"):
        return "The working directory contains control characters."
    if text.startswith("/"):
        return "The working directory must be relative to the repository."
    if ".." in text.replace("\\", "/").split("/"):
        return "The working directory may not step outside the repository ('..')."
    return None


def image_tag_problem(value, *, allow_template: bool = False) -> Optional[str]:
    text = _text(value)
    if allow_template and "$" in text:
        if IMAGE_TAG_TEMPLATE_RE.match(text):
            return None
        return (
            "An image tag template may use letters, digits, '.', '_', '-' and "
            "${VARIABLE} only."
        )
    if not IMAGE_TAG_RE.match(text):
        return (
            "IMAGE_TAG must be a valid image tag: letters, digits, '_', '.' and "
            "'-', not starting with '.' or '-', at most 128 characters."
        )
    return None


def image_name_problem(value) -> Optional[str]:
    text = _text(value)
    if len(text) > 255 or not IMAGE_NAME_RE.match(text):
        return (
            "IMAGE_NAME must be a valid image repository name: path components of "
            "letters and digits separated by '.', '_', '__' or '-', joined by '/'."
        )
    return None


def registry_host_problem(value) -> Optional[str]:
    text = _text(value)
    if len(text) > 255 or not REGISTRY_HOST_RE.match(text):
        return "The registry host is not a valid host[:port]."
    return None


def env_name_problem(name) -> Optional[str]:
    text = _text(name)
    if not ENV_NAME_RE.match(text):
        return (
            f"'{text[:64]}' is not a valid variable name: use letters, digits and "
            "'_', not starting with a digit."
        )
    upper = text.upper()
    if upper in _DENIED_ENV_NAMES or upper.startswith(_DENIED_ENV_PREFIXES):
        return f"'{text}' is managed by the build environment and cannot be set."
    return None


# The reserved variables whose values reach generated shell, with their checks.
RESERVED_VALUE_CHECKS = {
    "DOCKERFILE_PATH": dockerfile_path_problem,
    "IMAGE_TAG": image_tag_problem,
    "IMAGE_NAME": image_name_problem,
}


def reserved_value_problem(name: str, value) -> Optional[str]:
    """Check one trigger-supplied value. Empty means "use the default"."""
    check = RESERVED_VALUE_CHECKS.get(name)
    if check is None or _text(value) == "":
        return None
    return check(value)
