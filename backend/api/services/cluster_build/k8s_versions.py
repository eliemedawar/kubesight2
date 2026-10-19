"""Kubernetes version policy + dynamic patch discovery for the Cluster Builder.

Two separate ideas that must never be collapsed into one:

  * **Support** is a KubeSight decision, declared here as an explicit table of
    minors. A minor is enabled only once the *whole* creation stack has been
    verified against it: the kubeadm config API it needs, its exact pause image
    tag, package repositories, and pinned CNI/add-on manifests that upstream
    tests against it.
  * **Discovery** is upstream's answer to "what is the newest stable patch of
    minor X", read from the official ``https://dl.k8s.io/release/stable-<minor>.txt``
    endpoints. For the table's minors, discovery only chooses *which patch* to
    offer.

A minor upstream publishes *after* the table was written is discovered too, by
probing ``stable-<next minor>.txt`` past the newest table entry. It is offered
only once its pause tag has been read from kubeadm's own source at that exact
patch, and the newest table minor's CNI and add-on versions are carried forward
to it. Such a minor is marked auto-discovered: the wizard says so and preflight
warns, because nobody has validated that pairing yet.

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
import os
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
# After a failed fetch, wait this long before asking upstream again. An
# air-gapped install would otherwise spend the full HTTP timeout on every
# options request; meanwhile the last known good answer (or the static set)
# is served.
def _retry_after_failure_s() -> float:
    try:
        return max(0.0, float(os.environ.get("KUBESIGHT_K8S_DISCOVERY_RETRY_S", "600")))
    except ValueError:
        return 600.0
# The payload is one version string. Anything larger is not a release file.
_MAX_RESPONSE_BYTES = 64

# kubeadm's PauseVersion constant, read at the exact discovered patch so an
# auto-discovered minor never guesses its sandbox image tag.
PAUSE_SOURCE_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/kubernetes/kubernetes/"
    "v{version}/cmd/kubeadm/app/constants/constants.go"
)
_PAUSE_RE = re.compile(r'PauseVersion\s*=\s*"(\d+\.\d+(?:\.\d+)?)"')
_MAX_SOURCE_BYTES = 512 * 1024
# How far past the newest table minor discovery looks. Upstream ships three
# minors a year, so this covers a year of KubeSight not being updated.
_MAX_MINORS_AHEAD = 3

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
    # True for a minor found upstream after this table was written: offered,
    # but carried on the newest table minor's CNI/add-on versions untested.
    auto_discovered: bool = False


# Which CNI plugins and add-ons cover which minor is declared on the
# descriptors themselves, per version, and enforced by preflight — this table
# deliberately does not restate it, so there is one source of truth.

# 1.33 was the one gap in the enabled range until Calico 3.31.7 was vendored
# for it (3.32 starts at 1.34, 3.28.2 stops at 1.32).

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
        enabled=True,
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
    """The support record covering ``value``'s minor, enabled or not.

    A minor newer than the table is looked up among auto-discovered minors,
    discovering it on demand when it has not been seen yet — so a build pinned
    to one keeps resolving after a restart.
    """
    minor = minor_of(value)
    if not minor:
        return None
    record = _BY_MINOR.get(minor)
    if record is not None or not _is_ahead_of_table(minor):
        return record
    return _discover_record(minor, allow_network=not _network_disabled())


def enabled_records() -> List[MinorSupport]:
    """Enabled minors, newest first: auto-discovered ones, then the table."""
    with _lock:
        discovered = list(_discovered.values())
    discovered.sort(key=lambda record: _minor_key(record.minor), reverse=True)
    return discovered + [record for record in SUPPORTED_MINORS if record.enabled]


def enabled_minors() -> List[str]:
    return [record.minor for record in enabled_records()]


def is_supported(value: str) -> bool:
    record = record_for(value)
    return record is not None and record.enabled


# The documented static fallback. Kept under the historical name because it is
# still exactly what it always was — the versions KubeSight ships when it cannot
# reach upstream — and it remains the last line of defence for the wizard.
STATIC_FALLBACK_VERSIONS: Tuple[str, ...] = tuple(
    record.fallback_patch for record in SUPPORTED_MINORS if record.enabled
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
    record = record_for(normalized)
    if record is None or not record.enabled:
        supported = ", ".join(enabled_minors()) or "none"
        raise ValueError(
            f"Kubernetes {minor_of(normalized)} is not supported by the Cluster "
            f"Builder. Supported minors: {supported}."
        )
    return normalized


_CAPSULE_PACKAGE_RE = re.compile(
    r"^(kubeadm|kubelet|kubectl)\.\S+\s+(?:\d+:)?(\d+\.\d+\.\d+)-", re.MULTILINE
)


def capsule_versions(output: str) -> List[str]:
    """From ``dnf --showduplicates list kubeadm kubelet kubectl``: the versions
    for which the repositories carry all three packages, newest first."""
    found: Dict[str, set] = {"kubeadm": set(), "kubelet": set(), "kubectl": set()}
    for name, version in _CAPSULE_PACKAGE_RE.findall(output or ""):
        found[name].add(version)
    return sort_versions(found["kubeadm"] & found["kubelet"] & found["kubectl"])


def newest_patch_of(versions, wanted: str) -> Optional[str]:
    """``wanted`` when it is in ``versions``, else the newest of ``versions`` in
    its minor, else None."""
    target = normalize_version(wanted)
    ordered = sort_versions(versions)
    if target in ordered:
        return target
    return next((v for v in ordered if minor_of(v) == minor_of(target)), None)


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
# minor -> monotonic time before which a failed minor is not fetched again.
_failed_until: Dict[str, float] = {}
# minor -> record synthesized for a minor newer than the table. Never expires:
# a released minor's pause tag does not change.
_discovered: Dict[str, MinorSupport] = {}
# Monotonic time before which minors past the table are not probed again.
_newer_probe_until: float = 0.0


def reset_cache() -> None:
    """Drop discovery state. Exposed for tests; nothing in production calls it."""
    with _lock:
        _cache.clear()
        _last_known_good.clear()
        _failed_until.clear()
        _discovered.clear()
        global _newer_probe_until
        _newer_probe_until = 0.0


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


def _http_get_source(url: str, timeout: float) -> str:
    """Read a source file (kubeadm's constants.go). Isolated for tests."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read(_MAX_SOURCE_BYTES).decode("utf-8", "replace")


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
        # 404 is the normal answer when probing for a minor not released yet.
        level = logging.INFO if getattr(exc, "code", None) == 404 else logging.WARNING
        logger.log(
            level, "Kubernetes release discovery failed for minor %s: %s", minor, exc
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


def _minor_key(minor: str) -> Tuple[int, int]:
    major, _, rest = minor.partition(".")
    return (int(major), int(rest))


def _newest_table_minor() -> str:
    return max((record.minor for record in SUPPORTED_MINORS if record.enabled),
               key=_minor_key)


def _next_minor(minor: str) -> str:
    major, number = _minor_key(minor)
    return f"{major}.{number + 1}"


def _is_ahead_of_table(minor: str) -> bool:
    if not _MINOR_RE.match(minor):
        return False
    newest = _minor_key(_newest_table_minor())
    major, number = _minor_key(minor)
    return major == newest[0] and newest[1] < number <= newest[1] + _MAX_MINORS_AHEAD


def _fetch_pause_tag(version: str) -> Optional[str]:
    """kubeadm's PauseVersion at ``version``, else ``None``. Never raises."""
    url = PAUSE_SOURCE_URL_TEMPLATE.format(version=version)
    try:
        body = _http_get_source(url, HTTP_TIMEOUT_S)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.warning("Reading kubeadm's pause tag for %s failed: %s", version, exc)
        return None
    match = _PAUSE_RE.search(body)
    if match is None:
        logger.warning("kubeadm %s source names no PauseVersion; not offering it", version)
        return None
    return match.group(1)


def _discover_record(minor: str, allow_network: bool) -> Optional[MinorSupport]:
    """The record for a minor newer than the table, discovering it if needed.

    ``None`` unless upstream publishes a stable patch for ``minor`` *and*
    kubeadm's source at that patch names its pause tag.
    """
    now = time.monotonic()
    with _lock:
        known = _discovered.get(minor)
        if known is not None:
            return known
        if not allow_network or _failed_until.get(minor, 0.0) > now:
            return None
    patch = _fetch_stable_patch(minor)
    pause = _fetch_pause_tag(patch) if patch else None
    with _lock:
        if not (patch and pause):
            _failed_until[minor] = time.monotonic() + _retry_after_failure_s()
            return None
        record = _discovered.setdefault(minor, MinorSupport(
            minor=minor,
            pause_image_tag=pause,
            # v1beta4 is current kubeadm's only supported config API.
            kubeadm_config_api=KUBEADM_API_V1BETA4,
            fallback_patch=patch,
            enabled=True,
            auto_discovered=True,
        ))
        _cache[minor] = (time.monotonic() + CACHE_TTL_S, patch)
        _last_known_good[minor] = patch
        _failed_until.pop(minor, None)
    logger.info("Discovered Kubernetes %s upstream (v%s, pause %s)", minor, patch, pause)
    return record


def _discover_newer_minors(allow_network: bool) -> None:
    """Probe upstream for minors released after the table, at most every TTL.

    Walks ``stable-<next>.txt`` until the first minor upstream has not
    published. Minors already found get their newest patch refreshed on the way.
    """
    global _newer_probe_until
    now = time.monotonic()
    with _lock:
        if not allow_network or _newer_probe_until > now:
            return
        _newer_probe_until = now + CACHE_TTL_S
    minor = _next_minor(_newest_table_minor())
    for _ in range(_MAX_MINORS_AHEAD):
        record = _discover_record(minor, allow_network)
        if record is None:
            return
        latest_patch(record, allow_network)
        minor = _next_minor(minor)


def carry_forward(minors) -> Tuple[str, ...]:
    """A CNI/add-on version's minors, extended to auto-discovered minors.

    A version validated on the newest table minor is the best-tested choice for
    the minors upstream shipped after it, so it is offered there too.
    Preflight warns on those pairings rather than silently trusting them.
    """
    minors = tuple(minors)
    if not _discovered or _newest_table_minor() not in minors:
        return minors
    with _lock:
        newer = sorted(_discovered, key=_minor_key)
    return minors + tuple(minor for minor in newer if minor not in minors)


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
        recently_failed = _failed_until.get(record.minor, 0.0) > now

    if allow_network is None:
        allow_network = not _network_disabled()
    if allow_network and not recently_failed:
        discovered = _fetch_stable_patch(record.minor)
        if discovered:
            with _lock:
                _cache[record.minor] = (time.monotonic() + CACHE_TTL_S, discovered)
                _last_known_good[record.minor] = discovered
                _failed_until.pop(record.minor, None)
            return discovered, "upstream"
        with _lock:
            _failed_until[record.minor] = time.monotonic() + _retry_after_failure_s()

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
    table = [record for record in SUPPORTED_MINORS if record.enabled]
    # Decided here, on the caller's thread, while the app context still exists.
    allow_network = not _network_disabled()
    with ThreadPoolExecutor(
        max_workers=len(table) + 1, thread_name_prefix="k8s-version-discovery"
    ) as pool:
        # The probe for newer minors runs beside the per-minor fetches, so it
        # adds no wall-clock time in the common case.
        newer = pool.submit(_discover_newer_minors, allow_network)
        results = list(pool.map(
            lambda record: latest_patch(record, allow_network), table
        ))
        newer.result()

    with _lock:
        discovered = list(_discovered.values())
    records = discovered + table
    # The probe has just refreshed these, so this is a cache read.
    results = [latest_patch(record, False) for record in discovered] + results
    entries = [
        {"minor": record.minor, "version": version, "source": source,
         "validated": not record.auto_discovered}
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
             "source": "fallback", "validated": not record.auto_discovered}
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
        "autoDiscoveredMinors": [
            entry["minor"] for entry in entries if not entry.get("validated", True)
        ],
        "releases": entries,
        "staticFallback": list(STATIC_FALLBACK_VERSIONS),
        "source": STABLE_URL_TEMPLATE,
        "cacheTtlSeconds": CACHE_TTL_S,
    }
