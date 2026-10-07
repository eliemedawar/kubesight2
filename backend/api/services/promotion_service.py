"""Promotion rules: every image climbs Dev → SIT → UAT → Pre-prod in order.

The model (``models_promotion``):

* an ordered **ladder** of environments, each with a mode (off / warn / enforce)
  and an optional soak time;
* **bindings** — the namespaces that make up each environment: exact names,
  glob patterns (``*-sit``) or whole clusters. A namespace no binding covers is
  outside the ladder entirely;
* a **ledger** of images that ran healthy in each environment.

The rule, applied by :func:`evaluate`: an image may enter environment N when it
already ran healthy in environment N−1 (for at least N−1's soak time), or when
it ran in N before — a rollback or a redeploy is never a promotion. The entry
environment takes anything. The unit is the image itself (images are built
once), so no notion of "the same application" is needed to decide.

Every path that puts a new image on a cluster calls :func:`gate` (or
:func:`evaluate`): ``deployment_service.apply_yaml``, the CI Deploy stage, deploy
automation's handoff, Helm install/upgrade, and the change bundle staging and
executor. Rollbacks (``rollout undo``) are not checked — they only go back.

The ledger is written two ways: by the deploy paths when a rollout they watched
succeeds (:func:`record_rollout`), and by :func:`observe`, which scans the bound
namespaces on the scheduler tick so an image deployed outside KubeSight
(Jenkins, kubectl) is recorded too. :func:`evaluate` also probes the bound
namespaces live before it says "missing".

This module is the rule and the ladder. What the Promotions page shows lives in
``promotion_overview``; promoting a set of applications together lives in
``promotion_releases``.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

from ..audit import log_audit
from ..db import db
from ..models_promotion import (
    PROMOTION_MODES,
    WHOLE_CLUSTER,
    PromotionBinding,
    PromotionEnvironment,
    PromotionEvent,
    PromotionPolicy,
    PromotionRecord,
)
from ..ttl_cache import TTLCache

logger = logging.getLogger(__name__)

DEFAULT_LADDER = (
    ("dev", "Dev", "Where every build lands first. Takes any image."),
    ("sit", "SIT", "System integration testing."),
    ("uat", "UAT", "User acceptance testing."),
    ("preprod", "Pre-prod", "The last stop before production."),
)

MAX_ENVIRONMENTS = 12

_SCAN_TTL_SECONDS = int(os.getenv("PROMOTION_SCAN_TTL_SECONDS", "30"))
_OBSERVE_INTERVAL_SECONDS = int(os.getenv("PROMOTION_OBSERVE_SECONDS", "300"))
_SCAN_CACHE = TTLCache("promotion-scan")

# Namespaces no pattern or whole-cluster binding covers: the platform's own.
_SYSTEM_NAMESPACE_PREFIXES = ("kube-", "calico-", "tigera-", "cattle-", "metallb-")
_SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", "cert-manager", "ingress-nginx"}

_WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}$")
_NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,251}[a-z0-9])?$")
_PATTERN_RE = re.compile(r"^[a-z0-9*?-]{1,253}$")

# Image statuses that stop a deploy (in enforce) or flag it (in warn).
BLOCKING_STATUSES = ("missing", "soaking", "mutable")


class PromotionError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def _human_time(dt: Optional[datetime]) -> str:
    dt = _aware(dt)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "an unknown time"


def _actor_name(user) -> Optional[str]:
    if user is None:
        return None
    return getattr(user, "username", None) or getattr(user, "email", None)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

_DOCKER_HUB_PREFIXES = ("docker.io/library/", "index.docker.io/library/", "docker.io/", "index.docker.io/")


def parse_image(ref: str) -> Optional[Dict[str, Any]]:
    """``repository``, ``tag``, ``digest`` and the comparison key of an image.

    The key is ``repository:tag`` — what "the same build" means once images are
    built once and promoted. A digest-only reference is keyed by its digest.
    Docker Hub spellings are folded together (``docker.io/library/redis`` is
    ``redis``). ``mutable`` marks a reference that can name different builds
    over time (``:latest``, no tag at all).
    """
    text = str(ref or "").strip()
    if not text or text == "-":
        return None
    digest = None
    if "@" in text:
        text, digest = text.split("@", 1)
        digest = digest.strip() or None
    last = text.rsplit("/", 1)[-1]
    tag = None
    if ":" in last:
        text, tag = text.rsplit(":", 1)
        tag = tag.strip() or None
    repository = text.strip()
    for prefix in _DOCKER_HUB_PREFIXES:
        if repository.startswith(prefix):
            repository = repository[len(prefix):]
            break
    if not repository:
        return None
    if tag:
        key = f"{repository}:{tag}"
    elif digest:
        key = f"{repository}@{digest}"
    else:
        key = f"{repository}:latest"
    return {
        "image": key,
        "repository": repository,
        "tag": tag or ("" if digest else "latest"),
        "digest": digest,
        "mutable": (not digest) and (tag in (None, "latest")),
        "original": str(ref).strip(),
    }


def images_in_yaml(yaml_content: str) -> List[str]:
    """Every container image a manifest would run."""
    try:
        from .registry_service import images_from_documents

        documents = [d for d in yaml.safe_load_all(yaml_content or "") if isinstance(d, dict)]
    except Exception:  # noqa: BLE001 — an unparsable manifest is validate_yaml's to report
        return []
    return images_from_documents(documents)


def first_workload_name(yaml_content: str) -> Optional[str]:
    try:
        for doc in yaml.safe_load_all(yaml_content or ""):
            if isinstance(doc, dict) and doc.get("kind") in _WORKLOAD_KINDS + ("Job", "CronJob", "Pod"):
                return ((doc.get("metadata") or {}).get("name")) or None
    except Exception:  # noqa: BLE001
        return None
    return None


def repo_short_name(repository: str) -> str:
    return repository.rsplit("/", 1)[-1]


def short_image(parsed: Dict[str, Any]) -> str:
    """``payments:v2.9.0`` — how a person names an image in a sentence."""
    name = repo_short_name(parsed.get("repository") or "")
    tag = parsed.get("tag")
    return f"{name}:{tag}" if tag else name


# ---------------------------------------------------------------------------
# Ladder, bindings, policy
# ---------------------------------------------------------------------------

def ladder() -> List[PromotionEnvironment]:
    return PromotionEnvironment.query.order_by(
        PromotionEnvironment.position.asc(), PromotionEnvironment.id.asc()
    ).all()


def policy() -> PromotionPolicy:
    row = PromotionPolicy.query.order_by(PromotionPolicy.id.asc()).first()
    if row is None:
        row = PromotionPolicy(exempt_images=[], require_versioned_tags=True)
        db.session.add(row)
        db.session.commit()
    return row


def is_system_namespace(namespace: str) -> bool:
    ns = (namespace or "").strip()
    return ns in _SYSTEM_NAMESPACES or ns.startswith(_SYSTEM_NAMESPACE_PREFIXES)


def is_pattern(namespace: str) -> bool:
    return namespace == WHOLE_CLUSTER or any(ch in (namespace or "") for ch in "*?")


def _specificity(pattern: str) -> int:
    return sum(1 for ch in pattern if ch not in "*?")


class Resolver:
    """Which environment a namespace belongs to, and which rule says so.

    Precedence: a binding naming the namespace exactly, then the most specific
    matching pattern (the one with the most literal characters; on a tie, the
    one added first), then the cluster's whole-cluster binding. Patterns and
    whole clusters never cover the platform's own namespaces.
    """

    def __init__(self, bindings: Optional[List[PromotionBinding]] = None):
        bindings = bindings if bindings is not None else PromotionBinding.query.all()
        self.exact: Dict[Tuple[str, str], PromotionBinding] = {}
        self.patterns: Dict[str, List[PromotionBinding]] = {}
        self.whole: Dict[str, PromotionBinding] = {}
        for binding in sorted(bindings, key=lambda b: b.id or 0):
            cluster = str(binding.cluster_id)
            if binding.namespace == WHOLE_CLUSTER:
                self.whole.setdefault(cluster, binding)
            elif is_pattern(binding.namespace):
                self.patterns.setdefault(cluster, []).append(binding)
            else:
                self.exact[(cluster, binding.namespace)] = binding
        for rules in self.patterns.values():
            rules.sort(key=lambda b: (-_specificity(b.namespace), b.id or 0))
        self._cache: Dict[Tuple[str, str], Optional[PromotionBinding]] = {}

    @property
    def clusters(self) -> List[str]:
        names = {c for c, _ns in self.exact} | set(self.patterns) | set(self.whole)
        return sorted(names)

    def binding_for(self, cluster_id: str, namespace: str) -> Optional[PromotionBinding]:
        key = (str(cluster_id), namespace)
        if key in self._cache:
            return self._cache[key]
        found = self.exact.get(key)
        if found is None and not is_system_namespace(namespace):
            for binding in self.patterns.get(key[0], []):
                if fnmatch.fnmatchcase(namespace, binding.namespace):
                    found = binding
                    break
            if found is None:
                found = self.whole.get(key[0])
        self._cache[key] = found
        return found

    def resolve(self, cluster_id: str, namespace: str) -> Optional[PromotionEnvironment]:
        binding = self.binding_for(cluster_id, namespace)
        return binding.environment if binding is not None else None


def environment_for(cluster_id: str, namespace: str) -> Optional[PromotionEnvironment]:
    """The environment a namespace belongs to, or None (outside the ladder)."""
    if not cluster_id or not namespace:
        return None
    return Resolver().resolve(cluster_id, namespace)


def previous_environment(env: PromotionEnvironment) -> Optional[PromotionEnvironment]:
    rungs = ladder()
    for index, rung in enumerate(rungs):
        if rung.id == env.id:
            return rungs[index - 1] if index > 0 else None
    return None


def exempt(repository: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns or []:
        pattern = str(pattern or "").strip()
        if pattern and (fnmatch.fnmatch(repository, pattern) or repository == pattern):
            return True
    return False


def env_brief(env: Optional[PromotionEnvironment]) -> Optional[Dict[str, Any]]:
    if env is None:
        return None
    return {"id": env.id, "key": env.key, "name": env.name, "mode": env.mode, "position": env.position}


# ---------------------------------------------------------------------------
# Live state of the bound namespaces
# ---------------------------------------------------------------------------

def cluster_name(cluster_id: str) -> str:
    try:
        from ..cluster_store import get_active_cluster_by_public_id

        cluster = get_active_cluster_by_public_id(cluster_id)
        if cluster is not None and cluster.name:
            return cluster.name
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..mock_data import CLUSTERS

        for cluster in CLUSTERS:
            if cluster.get("id") == cluster_id:
                return cluster.get("name") or cluster_id
    except Exception:  # noqa: BLE001
        pass
    return cluster_id


def _workload_health(kind: str, item: Dict[str, Any]) -> Tuple[str, int, int]:
    """``(state, ready, desired)`` where state is healthy / progressing /
    scaled_down. Healthy is the full ``kubectl rollout status`` condition: every
    pod on the current template and available."""
    spec = item.get("spec") or {}
    status = item.get("status") or {}
    meta = item.get("metadata") or {}
    generation = int(meta.get("generation") or 0)
    observed = int(status.get("observedGeneration") or 0)
    if kind == "DaemonSet":
        desired = int(status.get("desiredNumberScheduled") or 0)
        updated = int(status.get("updatedNumberScheduled") or 0)
        ready = int(status.get("numberAvailable") or status.get("numberReady") or 0)
        current = desired > 0 and updated >= desired and ready >= desired
    elif kind == "StatefulSet":
        desired = int(spec.get("replicas") if spec.get("replicas") is not None else 1)
        updated = int(status.get("updatedReplicas") or 0)
        ready = int(status.get("readyReplicas") or 0)
        revision_done = (status.get("currentRevision") or "") == (status.get("updateRevision") or "")
        current = desired > 0 and ready >= desired and (updated >= desired or revision_done)
    else:
        desired = int(spec.get("replicas") if spec.get("replicas") is not None else 1)
        updated = int(status.get("updatedReplicas") or 0)
        total = int(status.get("replicas") or 0)
        ready = int(status.get("availableReplicas") or 0)
        current = desired > 0 and updated >= desired and total <= updated and ready >= desired
    if generation and observed < generation:
        current = False
    if desired == 0:
        return "scaled_down", ready, desired
    return ("healthy" if current else "progressing"), ready, desired


def _scan_cluster_uncached(cluster_id: str) -> List[Dict[str, Any]]:
    from ..k8s_provider import K8sCommandError, should_use_real_k8s

    if should_use_real_k8s(cluster_id):
        from ..k8s_provider import _run_for_access, resolve_cluster_access

        access = resolve_cluster_access(cluster_id)
        if access is None:
            return []
        try:
            raw = _run_for_access(access, ["get", "deployments,statefulsets,daemonsets", "-A", "-o", "json"])
            items = json.loads(raw).get("items") or []
        except (K8sCommandError, ValueError) as exc:
            logger.warning("Promotion scan of %s failed: %s", cluster_id, exc)
            raise
    else:
        from . import promotion_mock

        items = promotion_mock.workloads(cluster_id)

    out = []
    for item in items:
        kind = item.get("kind") or ""
        if kind not in _WORKLOAD_KINDS:
            continue
        meta = item.get("metadata") or {}
        template = (item.get("spec") or {}).get("template") or {}
        pod_spec = template.get("spec") or {}
        labels = {**((template.get("metadata") or {}).get("labels") or {}), **(meta.get("labels") or {})}
        containers = [
            {"name": c.get("name"), "image": c.get("image")}
            for c in (pod_spec.get("containers") or [])
            if isinstance(c, dict) and c.get("image")
        ]
        state, ready, desired = _workload_health(kind, item)
        out.append(
            {
                "clusterId": cluster_id,
                "namespace": meta.get("namespace") or "default",
                "kind": kind,
                "name": meta.get("name") or "",
                "partOf": labels.get("app.kubernetes.io/part-of") or "",
                "containers": containers,
                "state": state,
                "ready": ready,
                "desired": desired,
            }
        )
    return out


def scan_cluster(cluster_id: str) -> List[Dict[str, Any]]:
    return _SCAN_CACHE.get_or_compute(
        f"scan:{cluster_id}", _SCAN_TTL_SECONDS, lambda: _scan_cluster_uncached(cluster_id)
    )


def invalidate_scan(cluster_id: Optional[str] = None) -> None:
    _SCAN_CACHE.invalidate(f"scan:{cluster_id}" if cluster_id else "scan:")


def scan_ladder(
    resolver: Optional[Resolver] = None,
) -> Tuple[Dict[int, List[Dict[str, Any]]], List[Dict[str, str]]]:
    """Every workload in the ladder, by environment id. ``(by_env, errors)``.

    One scan per cluster that has any binding; each workload lands in the
    environment its namespace resolves to."""
    resolver = resolver or Resolver()
    by_env: Dict[int, List[Dict[str, Any]]] = {}
    errors: List[Dict[str, str]] = []
    for cluster_id in resolver.clusters:
        try:
            items = scan_cluster(cluster_id)
        except Exception as exc:  # noqa: BLE001 — one unreachable cluster must not empty the page
            errors.append({"clusterId": cluster_id, "message": str(exc)[:300]})
            continue
        for item in items:
            env = resolver.resolve(cluster_id, item["namespace"])
            if env is not None:
                by_env.setdefault(env.id, []).append(item)
    return by_env, errors


def scan_environment(env: PromotionEnvironment) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """``(workloads, errors)`` — every workload in one environment's namespaces."""
    resolver = Resolver()
    workloads: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    clusters = {b.cluster_id for b in env.bindings}
    for cluster_id in clusters:
        try:
            items = scan_cluster(cluster_id)
        except Exception as exc:  # noqa: BLE001
            errors.append({"clusterId": cluster_id, "message": str(exc)[:300]})
            continue
        for item in items:
            resolved = resolver.resolve(cluster_id, item["namespace"])
            if resolved is not None and resolved.id == env.id:
                workloads.append(item)
    return workloads, errors


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

def _upsert_record(
    env: PromotionEnvironment,
    parsed: Dict[str, Any],
    *,
    cluster_id: str,
    namespace: str,
    kind: Optional[str],
    name: Optional[str],
    source: str,
    actor: Optional[str] = None,
    existing: Optional[Dict[str, PromotionRecord]] = None,
) -> PromotionRecord:
    now = _now()
    if existing is not None:
        row = existing.get(parsed["image"])
    else:
        row = PromotionRecord.query.filter_by(environment_id=env.id, image=parsed["image"]).first()
    if row is None:
        row = PromotionRecord(
            environment_id=env.id,
            image=parsed["image"],
            repository=parsed["repository"],
            tag=parsed["tag"] or None,
            digest=parsed["digest"],
            source=source,
            deployed_by=actor,
            first_healthy_at=now,
            last_seen_at=now,
        )
        db.session.add(row)
        if existing is not None:
            existing[parsed["image"]] = row
    row.last_seen_at = now
    row.cluster_id = cluster_id
    row.namespace = namespace
    row.workload_kind = kind
    row.workload_name = name
    if parsed["digest"] and not row.digest:
        row.digest = parsed["digest"]
    if actor and not row.deployed_by:
        row.deployed_by = actor
    return row


def record_rollout(
    cluster_id: str,
    namespace: str,
    images: Iterable[str],
    *,
    kind: Optional[str] = "Deployment",
    name: Optional[str] = None,
    source: str,
    actor: Optional[str] = None,
    commit: bool = True,
) -> None:
    """A rollout a deploy path watched has succeeded: its images passed here.

    Best-effort — the deploy already happened; the ledger must never fail it."""
    try:
        env = environment_for(cluster_id, namespace)
        if env is None:
            return
        for image in images or []:
            parsed = parse_image(image)
            if parsed:
                _upsert_record(
                    env, parsed, cluster_id=cluster_id, namespace=namespace,
                    kind=kind, name=name, source=source, actor=actor,
                )
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        invalidate_scan(cluster_id)
    except Exception:  # noqa: BLE001
        logger.exception("Could not record a promotion for %s/%s", cluster_id, namespace)
        if commit:
            db.session.rollback()


def ledger_by_env(env_ids: Iterable[int]) -> Dict[int, Dict[str, PromotionRecord]]:
    ids = list(env_ids)
    out: Dict[int, Dict[str, PromotionRecord]] = {i: {} for i in ids}
    if not ids:
        return out
    for row in PromotionRecord.query.filter(PromotionRecord.environment_id.in_(ids)).all():
        out.setdefault(row.environment_id, {})[row.image] = row
    return out


def record_healthy(
    env: PromotionEnvironment,
    workloads: List[Dict[str, Any]],
    *,
    source: str = "observed",
    existing: Optional[Dict[str, PromotionRecord]] = None,
) -> int:
    """Record every image a healthy workload runs. ``existing`` (this
    environment's ledger by image) saves a query per image."""
    if existing is None:
        existing = ledger_by_env([env.id])[env.id]
    count = 0
    for workload in workloads:
        if workload["state"] != "healthy":
            continue
        for container in workload["containers"]:
            parsed = parse_image(container["image"])
            if not parsed:
                continue
            row = existing.get(parsed["image"])
            if row is not None and row.namespace == workload["namespace"] and row.workload_name == workload["name"]:
                # Seen here before: only refresh when it is worth a write.
                if (_now() - (_aware(row.last_seen_at) or _now())).total_seconds() < 60:
                    continue
            _upsert_record(
                env, parsed, cluster_id=workload["clusterId"], namespace=workload["namespace"],
                kind=workload["kind"], name=workload["name"], source=source, existing=existing,
            )
            count += 1
    return count


def observe() -> Dict[str, Any]:
    """Scan every environment's namespaces and record what runs healthy there."""
    rungs = {env.id: env for env in ladder()}
    by_env, errors = scan_ladder()
    ledgers = ledger_by_env(rungs.keys())
    seen = 0
    for env_id, workloads in by_env.items():
        if env_id in rungs:
            seen += record_healthy(rungs[env_id], workloads, existing=ledgers.get(env_id))
    db.session.commit()
    return {"recorded": seen, "errors": errors}


_observe_lock = threading.Lock()
_last_observe = 0.0


def observe_due() -> None:
    """The scheduler's tick: observe at most every PROMOTION_OBSERVE_SECONDS."""
    global _last_observe
    if not _observe_lock.acquire(blocking=False):
        return
    try:
        if time.time() - _last_observe < _OBSERVE_INTERVAL_SECONDS:
            return
        _last_observe = time.time()
        if PromotionBinding.query.first() is None:
            return
        observe()
    except Exception:  # noqa: BLE001
        logger.exception("Promotion observer tick failed")
        db.session.rollback()
    finally:
        _observe_lock.release()


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

def _live_find(env: PromotionEnvironment, image_key: str) -> Optional[Dict[str, Any]]:
    """A workload in ``env`` running ``image_key``, preferring a healthy one."""
    try:
        workloads, _errors = scan_environment(env)
    except Exception:  # noqa: BLE001
        return None
    found = None
    for workload in workloads:
        for container in workload["containers"]:
            parsed = parse_image(container["image"])
            if parsed and parsed["image"] == image_key:
                if workload["state"] == "healthy":
                    return workload
                found = found or workload
    return found


def _ledger(env_id: int, image_key: str) -> Optional[PromotionRecord]:
    return PromotionRecord.query.filter_by(environment_id=env_id, image=image_key).first()


def soak_left(record: PromotionRecord, env: PromotionEnvironment) -> int:
    """Minutes still to soak in ``env`` before this image may move on."""
    soak = int(env.min_soak_minutes or 0)
    if soak <= 0:
        return 0
    started = _aware(record.first_healthy_at) or _now()
    left = started + timedelta(minutes=soak) - _now()
    return max(0, int((left.total_seconds() + 59) // 60))


def format_minutes(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def _judge_image(
    image: str,
    env: PromotionEnvironment,
    prev: PromotionEnvironment,
    pol: PromotionPolicy,
    *,
    probe: bool,
) -> Dict[str, Any]:
    parsed = parse_image(image)
    if parsed is None:
        return {"image": image, "status": "exempt", "detail": "Not an image reference."}
    key = parsed["image"]
    short = short_image(parsed)
    base = {"image": key, "repository": parsed["repository"], "tag": parsed["tag"]}
    if exempt(parsed["repository"], pol.exempt_images):
        return {**base, "status": "exempt", "detail": f"{short} is exempt from the ladder (third-party image)."}

    # Ran here before → a redeploy or a rollback, never a promotion.
    if _ledger(env.id, key) is not None:
        return {**base, "status": "here", "detail": f"{short} already ran in {env.name}."}

    if parsed["mutable"] and pol.require_versioned_tags:
        return {
            **base,
            "status": "mutable",
            "detail": (
                f"'{parsed['tag'] or 'latest'}' can name a different build each time, so it cannot "
                f"prove it passed {prev.name}. Deploy a versioned tag."
            ),
        }

    record = _ledger(prev.id, key)
    if record is None and probe:
        workload = _live_find(prev, key)
        if workload is not None and workload["state"] == "healthy":
            record = _upsert_record(
                prev, parsed, cluster_id=workload["clusterId"], namespace=workload["namespace"],
                kind=workload["kind"], name=workload["name"], source="observed",
            )
            db.session.commit()
        elif workload is not None:
            return {
                **base,
                "status": "missing",
                "detail": (
                    f"{short} is in {prev.name} ({workload['namespace']}/{workload['name']}) but not healthy "
                    f"there yet — {workload['ready']}/{workload['desired']} ready."
                ),
            }
        if record is None and _live_find(env, key) is not None:
            return {**base, "status": "here", "detail": f"{short} is already running in {env.name}."}
    if record is None:
        return {**base, "status": "missing", "detail": f"{short} has not run in {prev.name} yet."}

    left = soak_left(record, prev)
    if left > 0:
        return {
            **base,
            "status": "soaking",
            "detail": (
                f"{short} is soaking in {prev.name} — {format_minutes(left)} left of "
                f"{format_minutes(int(prev.min_soak_minutes))}."
            ),
            "soakMinutesLeft": left,
        }
    where = f"{record.namespace}/{record.workload_name}" if record.workload_name else prev.name
    return {
        **base,
        "status": "passed",
        "detail": f"{short} passed {prev.name} — healthy in {where} since {_human_time(record.first_healthy_at)}.",
        "passedAt": _iso(record.first_healthy_at),
    }


def evaluate(
    cluster_id: str,
    namespace: str,
    images: Iterable[str],
    *,
    probe: bool = True,
) -> Dict[str, Any]:
    """What the ladder says about putting ``images`` into ``cluster/namespace``.

    ``applies`` — the namespace is in an environment whose mode is not off.
    ``allowed`` — the deploy may go ahead (always true in warn mode).
    ``warning`` — it may, but only because the mode is warn.
    """
    images = [i for i in dict.fromkeys(str(x).strip() for x in images or []) if i]
    env = environment_for(cluster_id, namespace)
    verdict: Dict[str, Any] = {
        "applies": False,
        "allowed": True,
        "warning": False,
        "mode": env.mode if env else None,
        "environment": env_brief(env),
        "previous": None,
        "images": [],
        "clusterId": cluster_id,
        "namespace": namespace,
        "message": "",
    }
    if env is None:
        verdict["message"] = f"{namespace} is not in any environment — the ladder does not apply."
        return verdict
    if env.mode == "off":
        verdict["message"] = f"{env.name} does not enforce promotion."
        return verdict
    prev = previous_environment(env)
    verdict["previous"] = env_brief(prev)
    if prev is None:
        verdict["applies"] = True
        verdict["images"] = [
            {**(parse_image(i) or {"image": i}), "status": "entry", "detail": f"{env.name} is the entry environment."}
            for i in images
        ]
        verdict["message"] = f"{env.name} is the first environment — every image may enter it."
        return verdict

    pol = policy()
    judged = [_judge_image(image, env, prev, pol, probe=probe) for image in images]
    verdict["applies"] = True
    verdict["images"] = judged
    failing = [j for j in judged if j["status"] in BLOCKING_STATUSES]
    if not failing:
        relevant = [j for j in judged if j["status"] in ("passed", "here")]
        verdict["message"] = (
            relevant[0]["detail"] if len(relevant) == 1 else f"Every image passed {prev.name}."
        )
        return verdict

    names = ", ".join(short_image(parse_image(j["image"]) or {"repository": j["image"]}) for j in failing)
    reason = " ".join(j["detail"] for j in failing)
    if env.mode == "enforce":
        verdict["allowed"] = False
        verdict["message"] = (
            f"{env.name} only takes images that passed {prev.name}. {reason} "
            f"Promote {'it' if len(failing) == 1 else 'them'} through {prev.name} first, or ask for an exception."
        )
    else:
        verdict["warning"] = True
        verdict["message"] = f"{names} went to {env.name} without passing {prev.name} — {env.name} only warns."
    return verdict


def _remember_for_response(verdict: Dict[str, Any]) -> None:
    """Hand the verdict to the response hook so the UI gets it structured."""
    try:
        from flask import g, has_request_context

        if has_request_context():
            g.promotion_verdict = verdict
    except Exception:  # noqa: BLE001
        pass


def record_event(
    kind: str,
    verdict: Optional[Dict[str, Any]] = None,
    *,
    path: str,
    actor: Optional[str] = None,
    cluster_id: Optional[str] = None,
    namespace: Optional[str] = None,
    workload: Optional[str] = None,
    images: Optional[List[Any]] = None,
    message: Optional[str] = None,
    bundle_id: Optional[int] = None,
    release_id: Optional[int] = None,
    environment: Optional[PromotionEnvironment] = None,
    from_environment: Optional[PromotionEnvironment] = None,
    environment_name: Optional[str] = None,
    from_environment_name: Optional[str] = None,
    commit: bool = True,
) -> None:
    try:
        env = (verdict or {}).get("environment") or env_brief(environment) or {}
        prev = (verdict or {}).get("previous") or env_brief(from_environment) or {}
        row = PromotionEvent(
            kind=kind,
            environment_id=env.get("id"),
            environment_name=env.get("name") or environment_name,
            from_environment_name=prev.get("name") or from_environment_name,
            images=images
            if images is not None
            else [
                {"image": j.get("image"), "status": j.get("status")}
                for j in (verdict or {}).get("images") or []
            ],
            cluster_id=cluster_id or (verdict or {}).get("clusterId"),
            namespace=namespace or (verdict or {}).get("namespace"),
            workload_name=workload,
            path=path,
            actor=actor,
            message=(message or (verdict or {}).get("message") or "")[:2000],
            bundle_id=bundle_id,
            release_id=release_id,
        )
        db.session.add(row)
        if commit:
            db.session.commit()
        else:
            db.session.flush()
    except Exception:  # noqa: BLE001 — the activity feed must never break a deploy
        logger.exception("Could not record a promotion event")
        if commit:
            db.session.rollback()


def gate(
    cluster_id: str,
    namespace: str,
    images: Iterable[str],
    *,
    user=None,
    actor: Optional[str] = None,
    path: str,
    workload: Optional[str] = None,
    commit: bool = True,
    record_warning: bool = True,
) -> Tuple[Optional[Tuple[str, int]], Dict[str, Any]]:
    """The check every deploy path calls. ``(refusal, verdict)``.

    ``refusal`` is ``(message, 409)`` when an enforcing environment refuses the
    images, else None. A warn-mode pass is recorded in the activity feed."""
    try:
        verdict = evaluate(cluster_id, namespace, images)
    except Exception:  # noqa: BLE001 — fail open: a ladder bug must not stop every deploy
        logger.exception("Promotion check failed for %s/%s", cluster_id, namespace)
        return None, {"applies": False, "allowed": True, "warning": False}
    who = actor or _actor_name(user)
    if verdict["applies"] and not verdict["allowed"]:
        _remember_for_response(verdict)
        record_event("blocked", verdict, path=path, actor=who, workload=workload, commit=commit)
        log_audit(
            "promotion_blocked",
            actor=user,
            target_type="namespace",
            target_id=f"{cluster_id}/{namespace}",
            details={"path": path, "message": verdict["message"], "workload": workload},
            commit=commit,
        )
        return (verdict["message"], 409), verdict
    if verdict["warning"]:
        _remember_for_response(verdict)
        if record_warning:
            record_event("warned", verdict, path=path, actor=who, workload=workload, commit=commit)
    return None, verdict


def gate_yaml(
    cluster_id: str, namespace: str, yaml_content: str, *, user=None, path: str, commit: bool = True
) -> Tuple[Optional[Tuple[str, int]], Dict[str, Any]]:
    return gate(
        cluster_id,
        namespace,
        images_in_yaml(yaml_content),
        user=user,
        path=path,
        workload=first_workload_name(yaml_content),
        commit=commit,
    )


# ---------------------------------------------------------------------------
# What the Promotions page and the release flow use (their own modules)
# ---------------------------------------------------------------------------

def overview() -> Dict[str, Any]:
    from .promotion_overview import build_overview

    return build_overview()


def board() -> Dict[str, Any]:
    """Kept for callers of the first version: the overview, under its old name."""
    return overview()


def create_release(user, **kwargs) -> Dict[str, Any]:  # noqa: D401 — see promotion_releases
    from .promotion_releases import create_release as _create

    return _create(user, **kwargs)


def promote(user, *, image: str, environment_id: int, targets: List[Dict[str, Any]], note: str = "") -> Dict[str, Any]:
    from .promotion_releases import promote as _promote

    return _promote(user, image=image, environment_id=environment_id, targets=targets, note=note)


def request_exception(user, *, changes: List[Dict[str, Any]], reason: str) -> Dict[str, Any]:
    from .promotion_releases import request_exception as _request

    return _request(user, changes=changes, reason=reason)


def list_releases(**kwargs) -> List[Dict[str, Any]]:
    from .promotion_releases import list_releases as _list

    return _list(**kwargs)


def image_history(repository: str) -> List[Dict[str, Any]]:
    from .promotion_overview import image_history as _history

    return _history(repository)


# ---------------------------------------------------------------------------
# Setup — ladder, bindings, policy
# ---------------------------------------------------------------------------

def _binding_payload(b: PromotionBinding) -> Dict[str, Any]:
    whole = b.namespace == WHOLE_CLUSTER
    return {
        "id": b.id,
        "clusterId": b.cluster_id,
        "clusterName": cluster_name(b.cluster_id),
        "namespace": b.namespace,
        "wholeCluster": whole,
        "pattern": (not whole) and is_pattern(b.namespace),
    }


def serialize_environment(env: PromotionEnvironment, **extra: Any) -> Dict[str, Any]:
    data = {
        "id": env.id,
        "key": env.key,
        "name": env.name,
        "position": env.position,
        "mode": env.mode,
        "minSoakMinutes": int(env.min_soak_minutes or 0),
        "description": env.description or "",
        "bindings": [_binding_payload(b) for b in env.bindings],
    }
    from .promotion_timetable import serialize_schedule

    data["schedule"] = serialize_schedule(env)
    data.update(extra)
    return data


def serialize_policy(pol: PromotionPolicy) -> Dict[str, Any]:
    return {
        "exemptImages": list(pol.exempt_images or []),
        "requireVersionedTags": bool(pol.require_versioned_tags),
        "updatedBy": pol.updated_by,
        "updatedAt": _iso(pol.updated_at),
    }


def setup_payload() -> Dict[str, Any]:
    return {
        "environments": [serialize_environment(env) for env in ladder()],
        "policy": serialize_policy(policy()),
        "modes": list(PROMOTION_MODES),
        "defaults": [{"key": k, "name": n, "description": d} for k, n, d in DEFAULT_LADDER],
    }


def _audit_setup(user, action: str, details: Dict[str, Any]) -> None:
    log_audit(action, actor=user, target_type="promotion_ladder", target_id="ladder", details=details)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:39] or "env"


def create_default_ladder(user) -> Dict[str, Any]:
    if PromotionEnvironment.query.first() is not None:
        raise PromotionError("The ladder already has environments.", 409)
    for index, (key, name, description) in enumerate(DEFAULT_LADDER):
        db.session.add(
            PromotionEnvironment(
                key=key, name=name, position=index, description=description,
                # Start by watching, not by blocking: switch to enforce once
                # Releases shows what would have been stopped.
                mode="warn",
            )
        )
    db.session.commit()
    _audit_setup(user, "promotion_ladder_created", {"environments": [k for k, _n, _d in DEFAULT_LADDER]})
    return setup_payload()


def _clean_env_fields(data: Dict[str, Any], env: Optional[PromotionEnvironment]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if "name" in data or env is None:
        name = str(data.get("name") or "").strip()
        if not name:
            raise PromotionError("Give the environment a name.")
        if len(name) > 80:
            raise PromotionError("Keep the name under 80 characters.")
        out["name"] = name
    if "mode" in data:
        mode = str(data.get("mode") or "").strip()
        if mode not in PROMOTION_MODES:
            raise PromotionError("Mode must be off, warn or enforce.")
        out["mode"] = mode
    if "minSoakMinutes" in data:
        try:
            soak = int(data.get("minSoakMinutes") or 0)
        except (TypeError, ValueError):
            raise PromotionError("Soak time must be a number of minutes.")
        if soak < 0 or soak > 60 * 24 * 30:
            raise PromotionError("Soak time must be between 0 and 30 days.")
        out["min_soak_minutes"] = soak
    if "description" in data:
        out["description"] = str(data.get("description") or "").strip()[:500] or None
    return out


def create_environment(user, data: Dict[str, Any]) -> Dict[str, Any]:
    if PromotionEnvironment.query.count() >= MAX_ENVIRONMENTS:
        raise PromotionError(f"A ladder has at most {MAX_ENVIRONMENTS} environments.")
    fields = _clean_env_fields(data, None)
    key = str(data.get("key") or "").strip().lower() or _slug(fields["name"])
    if not _KEY_RE.match(key):
        raise PromotionError("The key may use lowercase letters, digits and dashes.")
    if PromotionEnvironment.query.filter_by(key=key).first() is not None:
        raise PromotionError(f"An environment with the key '{key}' exists already.", 409)
    last = PromotionEnvironment.query.order_by(PromotionEnvironment.position.desc()).first()
    env = PromotionEnvironment(
        key=key,
        position=(last.position + 1) if last else 0,
        mode=fields.pop("mode", "warn"),
        **fields,
    )
    db.session.add(env)
    db.session.commit()
    _audit_setup(user, "promotion_environment_created", {"key": key, "name": env.name})
    return serialize_environment(env)


def update_environment(user, env_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    env = db.session.get(PromotionEnvironment, env_id)
    if env is None:
        raise PromotionError("Environment not found.", 404)
    before = {"name": env.name, "mode": env.mode, "minSoakMinutes": env.min_soak_minutes}
    for field, value in _clean_env_fields(data, env).items():
        setattr(env, field, value)
    db.session.commit()
    _audit_setup(
        user,
        "promotion_environment_updated",
        {"id": env.id, "before": before, "after": {"name": env.name, "mode": env.mode, "minSoakMinutes": env.min_soak_minutes}},
    )
    return serialize_environment(env)


def delete_environment(user, env_id: int) -> None:
    env = db.session.get(PromotionEnvironment, env_id)
    if env is None:
        raise PromotionError("Environment not found.", 404)
    name = env.name
    PromotionRecord.query.filter_by(environment_id=env.id).delete()
    db.session.delete(env)
    db.session.commit()
    _audit_setup(user, "promotion_environment_deleted", {"id": env_id, "name": name})


def reorder(user, ids: List[int]) -> Dict[str, Any]:
    rungs = ladder()
    if sorted(int(i) for i in ids) != sorted(env.id for env in rungs):
        raise PromotionError("Send every environment exactly once.")
    by_id = {env.id: env for env in rungs}
    for position, env_id in enumerate(ids):
        by_id[int(env_id)].position = position
    db.session.commit()
    _audit_setup(user, "promotion_ladder_reordered", {"order": [by_id[int(i)].key for i in ids]})
    return setup_payload()


def _clean_binding_name(raw: Any) -> str:
    name = str(raw or "").strip().lower()
    if name in ("*", WHOLE_CLUSTER):
        return WHOLE_CLUSTER
    if is_pattern(name):
        if not _PATTERN_RE.match(name):
            raise PromotionError(
                f"'{name}' is not a valid pattern — use lowercase letters, digits, dashes, * and ?."
            )
        if _specificity(name) == 0:
            return WHOLE_CLUSTER
        return name
    if not _NAMESPACE_RE.match(name):
        raise PromotionError(f"'{name}' is not a valid namespace name.")
    if is_system_namespace(name):
        raise PromotionError(f"{name} is a system namespace — it cannot be part of an environment.")
    return name


def add_binding(user, env_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    env = db.session.get(PromotionEnvironment, env_id)
    if env is None:
        raise PromotionError("Environment not found.", 404)
    cluster_id = str(data.get("clusterId") or "").strip()
    if not cluster_id:
        raise PromotionError("Choose a cluster.")
    if data.get("wholeCluster"):
        raw_names = [WHOLE_CLUSTER]
    else:
        raw_names = data.get("namespaces")
        if raw_names is None:
            raw_names = [data.get("pattern") or data.get("namespace")]
    names = []
    for raw in raw_names or []:
        if str(raw or "").strip():
            name = _clean_binding_name(raw)
            if name not in names:
                names.append(name)
    if not names:
        raise PromotionError("Choose at least one namespace, a pattern, or the whole cluster.")
    for name in names:
        taken = PromotionBinding.query.filter_by(cluster_id=cluster_id, namespace=name).first()
        if taken is not None:
            where = "The whole cluster" if name == WHOLE_CLUSTER else name
            other = taken.environment.name if taken.environment else "another environment"
            if taken.environment_id == env.id:
                raise PromotionError(f"{where} is already in {env.name}.", 409)
            raise PromotionError(f"{where} already belongs to {other}. Remove it there first.", 409)
    actor = _actor_name(user)
    for name in names:
        db.session.add(PromotionBinding(environment_id=env.id, cluster_id=cluster_id, namespace=name, created_by=actor))
    db.session.commit()
    invalidate_scan(cluster_id)
    _record_baseline(env)
    _audit_setup(user, "promotion_binding_added", {"environment": env.name, "clusterId": cluster_id, "namespaces": names})
    return serialize_environment(env)


def _record_baseline(env: PromotionEnvironment) -> None:
    """What already runs in a newly bound namespace is where the ladder starts.

    Recorded as "baseline": those images may stay and be redeployed (they ran
    here), and the overview does not report them as having skipped the
    environment before — they were there before the ladder was."""
    try:
        workloads, _errors = scan_environment(env)
        record_healthy(env, workloads, source="baseline")
        db.session.commit()
    except Exception:  # noqa: BLE001 — the binding is saved; the observer catches up
        logger.exception("Could not record the baseline of %s", env.name)
        db.session.rollback()


def remove_binding(user, binding_id: int) -> Dict[str, Any]:
    binding = db.session.get(PromotionBinding, binding_id)
    if binding is None:
        raise PromotionError("Binding not found.", 404)
    env = binding.environment
    details = {"environment": env.name if env else None, "clusterId": binding.cluster_id, "namespace": binding.namespace}
    db.session.delete(binding)
    db.session.commit()
    _audit_setup(user, "promotion_binding_removed", details)
    return serialize_environment(env) if env else {}


def update_policy(user, data: Dict[str, Any]) -> Dict[str, Any]:
    pol = policy()
    if "exemptImages" in data:
        raw = data.get("exemptImages") or []
        if not isinstance(raw, list):
            raise PromotionError("exemptImages must be a list.")
        cleaned = []
        for item in raw:
            text = str(item or "").strip()
            if text and text not in cleaned:
                if len(text) > 200:
                    raise PromotionError("Keep each pattern under 200 characters.")
                cleaned.append(text)
        pol.exempt_images = cleaned[:100]
    if "requireVersionedTags" in data:
        pol.require_versioned_tags = bool(data.get("requireVersionedTags"))
    pol.updated_by = _actor_name(user)
    db.session.commit()
    _audit_setup(user, "promotion_policy_updated", serialize_policy(pol))
    return serialize_policy(pol)


def namespaces_for(cluster_id: str, user=None) -> List[str]:
    """Every namespace of a cluster (live where possible), system ones left out."""
    from ..k8s_provider import should_use_real_k8s

    if not should_use_real_k8s(cluster_id):
        from . import promotion_mock

        return [n for n in promotion_mock.namespaces(cluster_id) if not is_system_namespace(n)]
    try:
        from .blueprint_picker_service import list_namespaces

        data, _err, _code = list_namespaces(cluster_id, user)
        return [n for n in (data or {}).get("items") or [] if not is_system_namespace(n)]
    except Exception:  # noqa: BLE001
        return []


def namespace_map(cluster_id: str, user=None) -> Dict[str, Any]:
    """Every namespace of a cluster, the environment it falls in, and the rule
    that put it there — the gaps (no environment) included."""
    resolver = Resolver()
    rows = []
    for ns in namespaces_for(cluster_id, user):
        binding = resolver.binding_for(cluster_id, ns)
        rows.append(
            {
                "namespace": ns,
                "environmentId": binding.environment_id if binding else None,
                "environmentName": binding.environment.name if binding and binding.environment else None,
                "rule": None if binding is None else ("whole cluster" if binding.namespace == WHOLE_CLUSTER else binding.namespace),
                "ruleKind": None if binding is None else (
                    "whole" if binding.namespace == WHOLE_CLUSTER else "pattern" if is_pattern(binding.namespace) else "exact"
                ),
                "bindingId": binding.id if binding else None,
            }
        )
    return {
        "clusterId": cluster_id,
        "clusterName": cluster_name(cluster_id),
        "items": rows,
        "assigned": sum(1 for r in rows if r["environmentId"]),
        "unassigned": sum(1 for r in rows if not r["environmentId"]),
    }


def preview_binding(cluster_id: str, raw: str, environment_id: Optional[int], user=None) -> Dict[str, Any]:
    """Which namespaces a candidate rule would cover, and which it would move."""
    name = _clean_binding_name(raw)
    current = Resolver()
    env = db.session.get(PromotionEnvironment, environment_id) if environment_id else None
    # A stand-in, not a model row: nothing about a preview may reach the session.
    candidate = SimpleNamespace(
        id=10**9, environment_id=environment_id or 0, cluster_id=cluster_id, namespace=name, environment=env
    )
    after = Resolver(list(PromotionBinding.query.all()) + [candidate])
    matches = []
    for ns in namespaces_for(cluster_id, user):
        winner = after.binding_for(cluster_id, ns)
        if winner is not candidate:
            covered = (
                name == WHOLE_CLUSTER and not is_system_namespace(ns)
            ) or (name != WHOLE_CLUSTER and fnmatch.fnmatchcase(ns, name))
            if covered:
                holder = current.binding_for(cluster_id, ns)
                matches.append(
                    {
                        "namespace": ns,
                        "takes": False,
                        "keptBy": holder.environment.name if holder and holder.environment else None,
                    }
                )
            continue
        before = current.resolve(cluster_id, ns)
        matches.append(
            {
                "namespace": ns,
                "takes": True,
                "from": before.name if before is not None else None,
            }
        )
    return {
        "pattern": name,
        "matches": matches,
        "count": sum(1 for m in matches if m["takes"]),
        "moved": sum(1 for m in matches if m["takes"] and m.get("from") and (env is None or m["from"] != env.name)),
    }


def list_events(limit: int = 100, environment_id: Optional[int] = None, kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    query = PromotionEvent.query
    if environment_id:
        query = query.filter_by(environment_id=environment_id)
    if kinds:
        query = query.filter(PromotionEvent.kind.in_(kinds))
    rows = query.order_by(PromotionEvent.created_at.desc(), PromotionEvent.id.desc()).limit(max(1, min(limit, 500))).all()
    return [
        {
            "id": row.id,
            "kind": row.kind,
            "environmentId": row.environment_id,
            "environmentName": row.environment_name,
            "fromEnvironmentName": row.from_environment_name,
            "images": row.images or [],
            "clusterId": row.cluster_id,
            "clusterName": cluster_name(row.cluster_id) if row.cluster_id else None,
            "namespace": row.namespace,
            "workloadName": row.workload_name,
            "path": row.path,
            "actor": row.actor,
            "message": row.message,
            "bundleId": row.bundle_id,
            "releaseId": row.release_id,
            "createdAt": _iso(row.created_at),
        }
        for row in rows
    ]
