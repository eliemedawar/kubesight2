"""Kubernetes version policy + dynamic patch discovery for the Cluster Builder.

Two separate ideas that must never be collapsed into one:

  * **Support** is a KubeSight decision, declared here as an explicit table of
    minors. A minor is enabled only once the *whole* creation stack has been
    verified against it: the kubeadm config API it needs, its exact pause image
    tag, package repositories, and pinned CNI/add-on manifests that upstream
    tests against it.
  * **Discovery** is upstream's answer to "what is the newest stable patch of
    minor X", read from the official ``https://dl.k8s.io/release/stable-<minor>.txt``
    endpoints. Discovery only ever chooses *which patch* of an already-supported
    minor to offer — a newly published minor never becomes selectable on its own.

Validation is therefore minor-scoped, not list-scoped: any well-formed patch of
an enabled minor is accepted. That is deliberate — a build pinned to 1.32.4 must
stay editable the day 1.32.5 is published, and every version the wizard shows is
by construction a patch of an enabled minor.

Discovery never fails the caller: a refresh error falls back to the last known
good value, then to the static per-minor fallback baked into the table below, so
``/api/cluster-builds/options`` keeps working through an upstream outage.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Official Kubernetes release endpoint. One small text file per minor holding
# that branch's newest stable patch, e.g. "v1.32.4".
STABLE_URL_TEMPLATE = "https://dl.k8s.io/release/stable-{minor}.txt"

# Strict, short: opening the creation wizard must not wait on dl.k8s.io. Each
# minor is fetched concurrently, so this is very nearly the wall-clock budget.
HTTP_TIMEOUT_S = 3.0
# stable-<minor>.txt changes a handful of times a year; six hours is plenty
# fresh while keeping the wizard off the network almost always.
CACHE_TTL_S = 6 * 60 * 60
# The payload is one version string. Anything larger is not a release file.
_MAX_RESPONSE_BYTES = 64

# Kubeadm configuration API versions. v1beta4 exists from kubeadm 1.31; v1beta3
# is deprecated upstream and only kept here for minors that predate v1beta4.
KUBEADM_API_V1BETA3 = "kubeadm.k8s.io/v1beta3"
KUBEADM_API_V1BETA4 = "kubeadm.k8s.io/v1beta4"

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_MINOR_RE = re.compile(r"^\d+\.\d+$")


@dataclass(frozen=True)
class MinorSupport:
    """Everything the creation stack needs to know about one Kubernetes minor.

    A minor is exposed to users only when ``enabled`` is true, and it may only
    be set true once every field below has been verified end to end — including
    a CNI and add-on set whose *pinned, digest-checked* manifests upstream tests
    against this minor. ``blockers`` records why a known minor is still off.
    """

    minor: str
    # kubeadm's PauseVersion constant for this branch. Derived from the release
    # source, never guessed: the containerd sandbox_image, the kubeadm image
    # list, and the verify smoke pod must all name the same tag, or a mirrored
    # or offline registry that only carries kubeadm's list cannot serve it.
    pause_image_tag: str
    kubeadm_config_api: str
    # Served when discovery fails and nothing was ever cached. Keeping one per
    # minor means an upstream outage still yields a working, tested version.
    fallback_patch: str
    enabled: bool = False
    blockers: Tuple[str, ...] = field(default_factory=tuple)


# Which CNI plugins and add-ons cover which minor is declared on the
# descriptors themselves, per version, and enforced by preflight — this table
# deliberately does not restate it, so there is one source of truth.

# 1.33 is the one gap in the enabled range: no vendored CNI release covers it.
# Calico 3.32 starts at 1.34 and 3.28.2 stops at 1.32, so closing this means
# vendoring a Calico 3.30/3.31 build with its SHA-256 digest.
_NO_CNI_FOR_MINOR = (
    "No vendored CNI release covers Kubernetes 1.33: Calico 3.32.1 is "
    "validated from 1.34 and Calico 3.28.2 only to 1.32. Vendor a Calico "
    "3.30/3.31 manifest with its digest to close the gap.",
)

# Ordered newest first. Records exist for known-but-disabled minors on purpose:
# the pause tag and kubeadm API for those are already resolved, so enabling one
# later is a reviewed flag change rather than fresh research.
SUPPORTED_MINORS: Tuple[MinorSupport, ...] = (
    # Upstream-maintained minors. Calico 3.32.1, Metrics Server 0.9.0, NGINX
    # Ingress 5.5.4 and MetalLB 0.16.1 are vendored and digest-pinned for these.
    MinorSupport(
        minor="1.36",
        pause_image_tag="3.10.2",
        kubeadm_config_api=KUBEADM_API_V1BETA4,
        fallback_patch="1.36.0",
        enabled=True,
    ),
    MinorSupport(
        minor="1.35",
        pause_image_tag="3.10.1",
        kubeadm_config_api=KUBEADM_API_V1BETA4,
        fallback_patch="1.35.0",
        enabled=True,
    ),
    MinorSupport(
        minor="1.34",
        pause_image_tag="3.10.1",
        kubeadm_config_api=KUBEADM_API_V1BETA4,
        fallback_patch="1.34.0",
        enabled=True,
    ),
    MinorSupport(
        minor="1.33",
        pause_image_tag="3.10",
        kubeadm_config_api=KUBEADM_API_V1BETA4,
        fallback_patch="1.33.0",
        enabled=False,
        blockers=_NO_CNI_FOR_MINOR,
    ),
    # Upstream-EOL, retained so existing builds and drafts stay buildable.
    MinorSupport(
        minor="1.32",
        pause_image_tag="3.10",
        kubeadm_config_api=KUBEADM_API_V1BETA4,
        fallback_patch="1.32.4",
        enabled=True,
    ),
    MinorSupport(
        minor="1.31",
        pause_image_tag="3.10",
        kubeadm_config_api=KUBEADM_API_V1BETA4,
        fallback_patch="1.31.8",
        enabled=True,
    ),
    MinorSupport(
        minor="1.30",
        pause_image_tag="3.9",
        # v1beta4 only exists from kubeadm 1.31 — these two must stay v1beta3.
        kubeadm_config_api=KUBEADM_API_V1BETA3,
        fallback_patch="1.30.12",
        enabled=True,
    ),
    MinorSupport(
        minor="1.29",
        pause_image_tag="3.9",
        kubeadm_config_api=KUBEADM_API_V1BETA3,
        fallback_patch="1.29.15",
        enabled=True,
    ),
)

_BY_MINOR: Dict[str, MinorSupport] = {record.minor: record for record in SUPPORTED_MINORS}


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def parse_version(value: str) -> Optional[Tuple[int, int, int]]:
    """``"v1.32.4"`` → ``(1, 32, 4)``; anything else (including prereleases and
    release candidates, which carry a ``-`` suffix) → ``None``."""
    match = _VERSION_RE.match(str(value or "").strip().lstrip("v"))
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def normalize_version(value: str) -> str:
    """Canonical storage form: no leading ``v``, or ``""`` when malformed."""
    parsed = parse_version(value)
    return "" if parsed is None else "{}.{}.{}".format(*parsed)


def minor_of(value: str) -> str:
    parsed = parse_version(value)
    return "" if parsed is None else f"{parsed[0]}.{parsed[1]}"


def record_for(value: str) -> Optional[MinorSupport]:
    """The support record covering ``value``'s minor, enabled or not."""
    minor = minor_of(value)
    return _BY_MINOR.get(minor) if minor else None


def enabled_records() -> List[MinorSupport]:
    return [record for record in SUPPORTED_MINORS if record.enabled]


def enabled_minors() -> List[str]:
    return [record.minor for record in enabled_records()]


def is_supported(value: str) -> bool:
    record = record_for(value)
    return record is not None and record.enabled


# The documented static fallback. Kept under the historical name because it is
# still exactly what it always was — the versions KubeSight ships when it cannot
# reach upstream — and it remains the last line of defence for the wizard.
STATIC_FALLBACK_VERSIONS: Tuple[str, ...] = tuple(
    record.fallback_patch for record in enabled_records()
)


def validate_version(value: str) -> str:
    """Return the normalized version, or raise ``ValueError`` with a reason.

    Minor-scoped by design: every version the wizard offers is a patch of an
    enabled minor, and a build pinned to an older patch of an enabled minor
    stays valid after a newer patch is discovered.
    """
    raw = str(value or "").strip()
    normalized = normalize_version(raw)
    if not normalized:
        raise ValueError(
            f"k8sVersion must be an exact patch release such as "
            f"{STATIC_FALLBACK_VERSIONS[0] if STATIC_FALLBACK_VERSIONS else '1.32.4'} "
            "(release candidates and prereleases are not supported); "
            f"got '{raw}'."
        )
    record = _BY_MINOR.get(minor_of(normalized))
    if record is None or not record.enabled:
        supported = ", ".join(enabled_minors()) or "none"
        raise ValueError(
            f"Kubernetes {minor_of(normalized)} is not supported by the Cluster "
            f"Builder. Supported minors: {supported}."
        )
    return normalized


def sort_versions(versions) -> List[str]:
    """Newest first, by semantic ordering — "1.9.0" must not beat "1.32.4"."""
    parsed = [(parse_version(v), normalize_version(v)) for v in versions]
    return [
        normalized
        for order, normalized in sorted(
            (item for item in parsed if item[0] is not None),
            key=lambda item: item[0],
            reverse=True,
        )
    ]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

_lock = threading.Lock()
# minor -> (fresh_until_monotonic, version)
_cache: Dict[str, Tuple[float, str]] = {}
# minor -> version. Never expires: a successful fetch stays usable as the
# last known good answer for as long as the process lives.
_last_known_good: Dict[str, str] = {}


def reset_cache() -> None:
    """Drop discovery state. Exposed for tests; nothing in production calls it."""
    with _lock:
        _cache.clear()
        _last_known_good.clear()


def _network_disabled() -> bool:
    """No outbound calls under Flask TESTING, mirroring ``ttl_cache``.

    Tests that exercise discovery patch this to ``False`` and patch
    ``_http_get_text``, so no test ever depends on real network access.
    """
    try:
        from flask import current_app

        return bool(getattr(current_app, "config", {}).get("TESTING"))
    except Exception:  # noqa: BLE001 — outside an app context, behave normally
        return False


def _http_get_text(url: str, timeout: float) -> str:
    """The only network call in this module. Isolated so tests can replace it."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read(_MAX_RESPONSE_BYTES).decode("utf-8", "replace")


def _fetch_stable_patch(minor: str) -> Optional[str]:
    """Newest stable patch upstream publishes for ``minor``, else ``None``.

    Every failure mode collapses to ``None`` — timeout, 404, DNS, a body that is
    not a release version, or a body naming a different minor than the one asked
    for. The exception text is logged, never returned to a user.
    """
    url = STABLE_URL_TEMPLATE.format(minor=minor)
    try:
        body = _http_get_text(url, HTTP_TIMEOUT_S)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.warning(
            "Kubernetes release discovery failed for minor %s: %s", minor, exc
        )
        return None
    normalized = normalize_version(body.strip())
    if not normalized:
        logger.warning(
            "Kubernetes release discovery for minor %s returned an unusable "
            "payload (%d bytes)", minor, len(body),
        )
        return None
    if minor_of(normalized) != minor:
        logger.warning(
            "Kubernetes release discovery for minor %s returned %s; ignoring "
            "the mismatched branch.", minor, normalized,
        )
        return None
    return normalized


def latest_patch(
    record: MinorSupport, allow_network: Optional[bool] = None
) -> Tuple[str, str]:
    """``(version, source)`` for one minor. Never raises.

    ``source`` is one of ``cache``/``upstream``/``last-known-good``/``fallback``
    and feeds the additive options metadata, so an operator can see whether the
    wizard is showing live data.

    ``allow_network`` must be supplied by callers running this off the request
    thread: ``_network_disabled()`` reads ``current_app``, which is unavailable
    in a worker thread and would otherwise silently read as "network allowed".
    """
    now = time.monotonic()
    with _lock:
        entry = _cache.get(record.minor)
        if entry is not None and entry[0] > now:
            return entry[1], "cache"

    if allow_network is None:
        allow_network = not _network_disabled()
    if allow_network:
        discovered = _fetch_stable_patch(record.minor)
        if discovered:
            with _lock:
                _cache[record.minor] = (time.monotonic() + CACHE_TTL_S, discovered)
                _last_known_good[record.minor] = discovered
            return discovered, "upstream"

    with _lock:
        previous = _last_known_good.get(record.minor)
    if previous:
        return previous, "last-known-good"
    return record.fallback_patch, "fallback"


def discover_versions() -> List[Dict[str, str]]:
    """One entry per enabled minor: ``{minor, version, source}``, newest first.

    Minors are fetched concurrently so the wizard waits roughly one timeout in
    the worst case rather than one per minor.
    """
    records = enabled_records()
    if not records:
        return []
    # Decided here, on the caller's thread, while the app context still exists.
    allow_network = not _network_disabled()
    if len(records) == 1:
        results = [latest_patch(records[0], allow_network)]
    else:
        with ThreadPoolExecutor(
            max_workers=len(records), thread_name_prefix="k8s-version-discovery"
        ) as pool:
            results = list(pool.map(
                lambda record: latest_patch(record, allow_network), records
            ))

    entries = [
        {"minor": record.minor, "version": version, "source": source}
        for record, (version, source) in zip(records, results)
    ]
    order = {version: index for index, version in enumerate(
        sort_versions(entry["version"] for entry in entries)
    )}
    entries.sort(key=lambda entry: order.get(entry["version"], len(order)))
    return entries


def releases() -> List[Dict[str, str]]:
    """``discover_versions()`` that cannot raise.

    Every caller on the options path goes through here: an upstream problem
    degrades to the static set rather than taking the endpoint down.
    """
    try:
        return discover_versions()
    except Exception:  # noqa: BLE001 — the wizard must open regardless
        logger.exception("Kubernetes version discovery failed; using the static set")
        return [
            {"minor": record.minor, "version": record.fallback_patch,
             "source": "fallback"}
            for record in enabled_records()
        ]


def supported_versions(entries: Optional[List[Dict[str, str]]] = None) -> List[str]:
    """The wizard's ``k8sVersions``: newest stable patch per enabled minor.

    Non-empty while any minor is enabled. Pass ``entries`` from ``releases()``
    to build the version list and its metadata from one discovery pass.
    """
    return [entry["version"] for entry in (releases() if entries is None else entries)]


def version_metadata(
    entries: Optional[List[Dict[str, str]]] = None
) -> Dict[str, object]:
    """Additive provenance for the options payload. Purely informational."""
    entries = releases() if entries is None else entries
    return {
        "supportedMinors": [entry["minor"] for entry in entries],
        "releases": entries,
        "staticFallback": list(STATIC_FALLBACK_VERSIONS),
        "source": STABLE_URL_TEMPLATE,
        "cacheTtlSeconds": CACHE_TTL_S,
    }
