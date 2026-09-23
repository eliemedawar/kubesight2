"""What one build stage is allowed to use, and who gets to decide it.

Three places can say something about a stage's resource envelope. Narrowest
wins, and "says nothing" is a real answer at every level:

  1. the STAGE's own ``resources`` — per stage, saved with the pipeline;
  2. the SERVICE's ``build_resources`` — Service Catalog -> Settings, the one
     most installations will ever touch;
  3. the INSTALLATION's environment defaults — ``k8s/ci-backend-config.yaml``.

A key that is absent inherits from the level below it. A key set to one of
:data:`OFF_WORDS` means "leave this off the manifest entirely", which is not the
same as absent: it stops the level below from applying.

Ephemeral storage is OPEN by default — no request, no limit, no ``sizeLimit`` on
the shared workspace. A build writes what the node has, and nothing is reserved
on its behalf. That is a deliberate choice about which failure is worse on a
small cluster: a capped build dies mid-run to an eviction that reads, in the
build log, as a stage that stopped for no stated reason, while an uncapped one
shows up as node disk pressure, in a place operators already watch. Put the caps
back per service in Settings, or installation-wide with
``CI_STAGE_EPHEMERAL_LIMIT``, on a cluster where build pods share a node with
something that matters.

This module is the single source of truth for all of it: the runner sizes pods
from it, the catalog validates what a user types against it, and the API reports
the resolved defaults from it so the Settings card can name what "Default"
currently means rather than hardcoding a number that drifts.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, Optional

# Fields a user may set, in the order the Settings card shows them.
FIELDS = ("cpu", "memory", "ephemeralStorage")

FIELD_LABELS = {
    "cpu": "CPU",
    "memory": "Memory",
    "ephemeralStorage": "Ephemeral storage",
}

# Blanking a variable cannot mean "no value": an empty environment variable is
# indistinguishable from an unset one, and both fall back to the default. A
# resource that should be left off the manifest therefore needs a word for it.
OFF_WORDS = {"off", "none", "no", "0", "false", "unlimited"}

# A Kubernetes quantity as a human types one: 2, 1.5, 500m, 256Mi, 8Gi.
_QUANTITY_RE = re.compile(r"^\d+(\.\d+)?(m|[KMGTPE]i?|k)?$")

_SUFFIX_MULTIPLIERS = {
    "": 1,
    "k": 1000,
    "K": 1000,
    "M": 1000 ** 2,
    "G": 1000 ** 3,
    "T": 1000 ** 4,
    "P": 1000 ** 5,
    "E": 1000 ** 6,
    "Ki": 1024,
    "Mi": 1024 ** 2,
    "Gi": 1024 ** 3,
    "Ti": 1024 ** 4,
    "Pi": 1024 ** 5,
    "Ei": 1024 ** 6,
}


class ResourceError(ValueError):
    """A resource value a user typed that Kubernetes would reject."""


def env(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def is_off(value: Any) -> bool:
    return str(value or "").strip().lower() in OFF_WORDS


def is_set(value: Any) -> bool:
    """True when ``value`` names a real quantity — not absent, not off."""
    return bool(str(value or "").strip()) and not is_off(value)


def quantity_bytes(value: Any) -> Optional[int]:
    """Bytes for a storage quantity, or None when it is not one.

    Only ever used to COMPARE two quantities — which of these two caps is
    larger. Never to rewrite a value: what a user typed reaches the manifest
    exactly as they typed it.
    """
    text = str(value or "").strip()
    if not text or not _QUANTITY_RE.match(text):
        return None
    match = re.match(r"^(\d+(?:\.\d+)?)(.*)$", text)
    if not match:
        return None
    number, suffix = match.group(1), match.group(2)
    if suffix == "m":  # milli-units: meaningless for storage, but parseable
        return int(float(number) / 1000)
    multiplier = _SUFFIX_MULTIPLIERS.get(suffix)
    if multiplier is None:
        return None
    return int(float(number) * multiplier)


def larger(first: Any, second: Any) -> Any:
    """The bigger of two quantities, preferring whichever one is comparable."""
    left, right = quantity_bytes(first), quantity_bytes(second)
    if left is None:
        return second
    if right is None:
        return first
    return first if left >= right else second


def normalize(value: Any, *, source: str = "Resources") -> Optional[Dict[str, str]]:
    """Validate a user-supplied ``{cpu, memory, ephemeralStorage}`` map.

    Returns None for "nothing set", which is what clears an override. Unknown
    keys are dropped rather than rejected, so a newer client cannot break an
    older one — but a value Kubernetes would refuse is an error HERE. The
    alternative is a pod that fails to create hours later, with the reason in an
    event nobody is watching.
    """
    if value in (None, "", {}):
        return None
    if not isinstance(value, dict):
        raise ResourceError(f"{source} must be an object.")

    out: Dict[str, str] = {}
    for field in FIELDS:
        if field not in value:
            continue
        raw = str(value.get(field) or "").strip()
        if not raw:
            # Empty means "inherit" — how the Settings card clears a field back
            # to the installation default.
            continue
        if is_off(raw):
            out[field] = "off"
            continue
        if len(raw) > 32 or not _QUANTITY_RE.match(raw):
            raise ResourceError(
                f'{FIELD_LABELS[field]} must be a Kubernetes quantity '
                f'(2, 500m, 512Mi, 8Gi) or "off" for no limit — got "{raw}".'
            )
        out[field] = raw
    return out or None


def merge(*layers: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Collapse resource maps, narrowest first. Absent inherits; "off" does not."""
    out: Dict[str, str] = {}
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        for field in FIELDS:
            if field in out:
                continue
            raw = str(layer.get(field) or "").strip()
            if raw:
                out[field] = raw
    return out


def installation_defaults() -> Dict[str, str]:
    """What "Default" resolves to right now, for the Settings card to name.

    Reported rather than hardcoded in the UI: an installation that sets
    ``CI_STAGE_MEMORY_LIMIT`` should see ITS number beside "Default", not the one
    this file happens to ship.
    """
    return {
        "cpu": env("CI_STAGE_CPU_LIMIT", "2"),
        "memory": env("CI_STAGE_MEMORY_LIMIT", "4Gi"),
        "ephemeralStorage": env("CI_STAGE_EPHEMERAL_LIMIT", "off"),
    }


def ephemeral_limit(resources: Optional[Dict[str, Any]], *, scanning: bool) -> str:
    """The ephemeral-storage LIMIT for one stage, or an off word.

    ``resources`` is the merged stage+service map. Only when it says nothing does
    the installation default apply — and only then is a scanned image stage
    raised, because such a stage parks the whole image as an uncompressed archive
    on /workspace between the build and the push. Quietly raising a limit
    somebody chose would be worse than the eviction it avoids: they asked for a
    ceiling, and a ceiling that moves on its own is not one.
    """
    chosen = str((resources or {}).get("ephemeralStorage") or "").strip()
    if chosen:
        return chosen

    default = env("CI_STAGE_EPHEMERAL_LIMIT", "off")
    if not scanning or is_off(default):
        # Open by default, and a scan gate on an uncapped installation needs no
        # bump — there is no ceiling to bump.
        return default
    return larger(default, env("CI_IMAGE_SCAN_EPHEMERAL_LIMIT", "8Gi"))


def ephemeral_request(limit: Any) -> str:
    """The ephemeral-storage REQUEST that belongs with ``limit``.

    The scheduler matches REQUESTS. A limit with no request makes Kubernetes
    default the request to the limit, so an 8Gi cap would silently demand 8Gi of
    free disk on every candidate node — unschedulable exactly where somebody set
    a cap because disk is tight. A modest floor is therefore requested whenever a
    limit is in force, unless the installation names its own.

    With no limit there is nothing to protect against and nothing is requested:
    that is the open default. Its trade, stated once — a pod with no request is
    first in line for eviction when another tenant fills the node, because
    eviction ranks by usage above request.
    """
    configured = env("CI_STAGE_EPHEMERAL_REQUEST", "")
    if configured:
        return configured
    return "256Mi" if is_set(limit) else "off"


def workspace_size_limit(chosen_limits: Iterable[Any], *, scanning: bool) -> str:
    """``sizeLimit`` for the shared /workspace emptyDir.

    ``chosen_limits`` is what the plan's stages were EXPLICITLY granted — by a
    service or a stage, not by the default underneath. An installation is free to
    leave containers uncapped and still cap the volume they share, so an inherited
    default says nothing here; a choice does.

    A second ceiling, enforced by kubelet EVICTING the pod, independent of the
    per-container ephemeral-storage limit — so it has to honour "off" too, or
    removing the limits still leaves a cap in place, and it has to rise with
    them, or a stage granted 8Gi still dies at the workspace's 2Gi with nothing
    in the build log to say why.
    """
    default = env("CI_WORKSPACE_SIZE_LIMIT", "off")
    if scanning and not is_off(default):
        default = larger(default, env("CI_IMAGE_SCAN_WORKSPACE_SIZE_LIMIT", "8Gi"))

    if is_off(default):
        return default

    for limit in chosen_limits:
        if is_off(limit):
            # A stage deliberately uncapped is not capped by the back door.
            return "off"
        if is_set(limit):
            default = larger(default, limit)
    return default
