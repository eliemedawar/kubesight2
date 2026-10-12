"""Fine-grained access rule evaluation."""

from __future__ import annotations

import os
import threading
import time
from itertools import chain
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from flask import current_app, g
from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from .models import AccessRule, Permission, Role, User, role_permissions

RESOURCE_SPECIFICITY = {
    "cluster": 10,
    "namespace": 20,
    "deployment": 30,
    "replicaset": 30,
    "statefulset": 30,
    "daemonset": 30,
    "job": 30,
    "cronjob": 30,
    "pod": 30,
    "service": 30,
    "container": 40,
    "service_port": 45,
}

# Broad management keys imply granular user/role permissions.
PERMISSION_MANAGED_BY = {
    "users:view": ("users:manage",),
    "users:create": ("users:manage",),
    "users:update": ("users:manage",),
    "users:disable": ("users:manage",),
    "roles:view": ("roles:manage", "users:manage"),
    "roles:manage": ("users:manage",),
}

PERMISSION_ALIASES = {
    "namespaces:view": ("namespaces:view", "resources:view"),
    "pods:view": ("pods:view", "resources:view"),
    "deployments:view": ("deployments:view", "resources:view"),
    "replicasets:view": ("replicasets:view", "deployments:view", "resources:view"),
    "statefulsets:view": ("statefulsets:view", "deployments:view", "resources:view"),
    "daemonsets:view": ("daemonsets:view", "resources:view"),
    "jobs:view": ("jobs:view", "resources:view"),
    "cronjobs:view": ("cronjobs:view", "resources:view"),
    "services:view": ("services:view", "resources:view"),
    "resources:view": (
        "resources:view",
        "pods:view",
        "deployments:view",
        "replicasets:view",
        "statefulsets:view",
        "daemonsets:view",
        "jobs:view",
        "cronjobs:view",
        "services:view",
    ),
    "inventory:view": ("inventory:view", "resources:view"),
}

NAMESPACE_RESOURCE_LIST_KEYS = (
    "pods",
    "deployments",
    "replicasets",
    "statefulsets",
    "daemonsets",
    "jobs",
    "cronjobs",
    "services",
    "configmaps",
    "secrets",
    "ingress",
)


def _request_memo(name: str) -> Optional[Dict[int, Any]]:
    """Per-request memo dict stored on flask.g, or None outside a request.

    Disabled under TESTING so tests that mutate roles inside one app context
    never observe stale permission sets."""
    try:
        if current_app.config.get("TESTING"):
            return None
        cache = getattr(g, name, None)
        if cache is None:
            cache = {}
            setattr(g, name, cache)
        return cache
    except RuntimeError:
        return None


def is_admin(user: User) -> bool:
    # Memoized per request: this runs once per row when filtering large
    # resource lists, and rebuilding the permission set each time is O(perms).
    memo = _request_memo("_is_admin_by_user_id")
    if memo is not None and user.id in memo:
        return memo[user.id]
    result = _compute_is_admin(user)
    if memo is not None:
        memo[user.id] = result
    return result


# --- Role permission cache ----------------------------------------------------
# Every authenticated request used to load the user's role joined to all its
# permissions (100+ rows) just to answer "is this an admin / may they do X".
# Roles change rarely, so (name, keys) per role id is kept process-wide for a
# short TTL. Any ORM write to a Role or Permission (which is how roles are
# edited: role_service, seed, migrations) drops the whole cache at flush, so
# this process never serves a stale answer for its own edits; another replica's
# edit is seen within the TTL. ROLE_PERMISSION_CACHE_SECONDS=0 turns it off.
_role_cache: Dict[int, Tuple[float, Optional[str], FrozenSet[str]]] = {}
_role_cache_lock = threading.Lock()
_role_cache_generation = 0


def _role_cache_ttl() -> float:
    try:
        return max(0.0, float(os.getenv("ROLE_PERMISSION_CACHE_SECONDS", "30")))
    except ValueError:
        return 30.0


def invalidate_role_permission_cache() -> None:
    global _role_cache_generation
    with _role_cache_lock:
        _role_cache.clear()
        _role_cache_generation += 1


@event.listens_for(Session, "after_flush")
def _drop_role_cache_on_role_writes(session, _flush_context) -> None:
    for obj in chain(session.new, session.dirty, session.deleted):
        if isinstance(obj, (Role, Permission)):
            invalidate_role_permission_cache()
            return


@event.listens_for(Session, "do_orm_execute")
def _drop_role_cache_on_bulk_role_writes(state) -> None:
    # Query.update()/delete() and update()/delete() statements skip the flush.
    if not (state.is_update or state.is_delete):
        return
    mapper = state.bind_mapper
    if mapper is not None and mapper.class_ in (Role, Permission):
        invalidate_role_permission_cache()


def _role_snapshot(user: User) -> Tuple[Optional[str], FrozenSet[str]]:
    """(role name, permission keys) of the user's role; (None, empty) without one.

    A role already loaded on the user is read in memory, exactly as before (it
    may carry changes not flushed yet). Otherwise the answer comes from the
    role cache, filled by one narrow SELECT instead of the role's joined load.
    """
    state = sa_inspect(user, raiseerr=False)
    role_id = getattr(user, "role_id", None)
    ttl = _role_cache_ttl()
    if state is None or "role" not in state.unloaded or role_id is None or ttl <= 0:
        role = user.role
        if not role:
            return None, frozenset()
        return role.name, frozenset(perm.key for perm in role.permissions)

    now = time.monotonic()
    entry = _role_cache.get(role_id)
    if entry is not None and entry[0] > now:
        return entry[1], entry[2]
    generation = _role_cache_generation
    from .db import db

    rows = db.session.execute(
        db.select(Role.name, Permission.key)
        .select_from(Role)
        .outerjoin(role_permissions, role_permissions.c.role_id == Role.id)
        .outerjoin(Permission, Permission.id == role_permissions.c.permission_id)
        .where(Role.id == role_id)
    ).all()
    if not rows:
        name, keys = None, frozenset()
    else:
        name = rows[0][0]
        keys = frozenset(key for _, key in rows if key is not None)
    with _role_cache_lock:
        if generation == _role_cache_generation:
            _role_cache[role_id] = (now + ttl, name, keys)
    return name, keys


def _compute_is_admin(user: User) -> bool:
    name, granted = _role_snapshot(user)
    if name is None:
        return False
    if name == "admin":
        return True
    from .rbac_data import ALL_PERMISSION_KEYS

    return set(ALL_PERMISSION_KEYS).issubset(granted)


def get_user_permission_keys(user: User) -> Set[str]:
    memo = _request_memo("_perm_keys_by_user_id")
    if memo is not None and user.id in memo:
        return memo[user.id]
    keys = set(_role_snapshot(user)[1])
    if memo is not None:
        memo[user.id] = keys
    return keys


def user_has_permission(user: User, permission_key: str) -> bool:
    if is_admin(user):
        return True
    return _role_grants(user, permission_key)


def _role_grants(user: User, permission_key: str) -> bool:
    """``user_has_permission`` for a user already known not to be an admin."""
    keys = get_user_permission_keys(user)
    if permission_key in keys:
        return True
    for manager in PERMISSION_MANAGED_BY.get(permission_key, ()):
        if manager in keys:
            return True
    for alias in PERMISSION_ALIASES.get(permission_key, ()):
        if alias in keys:
            return True
    return False


def _role_allows_permission(user: User, permission_key: str) -> bool:
    return user_has_permission(user, permission_key)


def _access_rules_cache() -> Dict[int, List[AccessRule]]:
    cache = getattr(g, "_access_rules_by_user_id", None)
    if cache is None:
        cache = {}
        g._access_rules_by_user_id = cache
    return cache


def invalidate_access_rules_cache(user_id: Optional[int] = None) -> None:
    """Drop cached AccessRule rows after mutations or for all users in this request."""
    try:
        caches = (
            getattr(g, "_access_rules_by_user_id", None),
            getattr(g, "_access_rule_index_by_user_id", None),
        )
    except RuntimeError:
        return
    for cache in caches:
        if cache is None:
            continue
        if user_id is None:
            cache.clear()
        else:
            cache.pop(user_id, None)


def _load_rules(user: User) -> List[AccessRule]:
    try:
        cache = _access_rules_cache()
        cached = cache.get(user.id)
        if cached is not None:
            return cached
        if "access_rules" not in sa_inspect(user).unloaded:
            rules = list(user.access_rules)
        else:
            rules = AccessRule.query.filter_by(user_id=user.id).all()
        cache[user.id] = rules
        return rules
    except RuntimeError:
        return AccessRule.query.filter_by(user_id=user.id).all()


def _uses_legacy_only(user: User, rules: List[AccessRule]) -> bool:
    return len(rules) == 0


def _legacy_cluster_ids(user: User) -> Set[str]:
    return {row.cluster_id for row in user.cluster_access_entries if row.can_view}


def _legacy_namespace_pairs(user: User) -> Set[Tuple[str, str]]:
    return {
        (row.cluster_id, row.namespace)
        for row in user.namespace_access_entries
        if row.can_view
    }


def rule_specificity(rule: AccessRule) -> int:
    base = RESOURCE_SPECIFICITY.get(rule.resource_type or "cluster", 5)
    if rule.resource_name:
        base += 5
    if rule.container_name:
        base += 3
    if rule.port is not None:
        base += 2
    if rule.namespace:
        base += 1
    return base


class _PreparedRule:
    """One AccessRule with the normalisation ``_rule_matches`` does per call
    done once: stripped namespace/type/name and the specificity score."""

    __slots__ = (
        "cluster_id",
        "permission_key",
        "ns",
        "rt",
        "name",
        "container",
        "port",
        "effect",
        "specificity",
        "raw_ns",
        "raw_rt",
        "is_cluster_type",
    )

    def __init__(self, rule: AccessRule):
        self.cluster_id = rule.cluster_id
        self.permission_key = rule.permission_key
        self.ns = (rule.namespace or "").strip() or None
        self.rt = (rule.resource_type or "cluster").strip()
        self.name = (rule.resource_name or "").strip() or None
        self.container = rule.container_name
        self.port = rule.port
        self.effect = rule.effect
        self.specificity = rule_specificity(rule)
        # Unstripped forms, for the comparisons that have always used them.
        self.raw_ns = rule.namespace or ""
        self.raw_rt = rule.resource_type or ""
        self.is_cluster_type = (rule.resource_type or "cluster") == "cluster"


class _RuleIndex:
    """A user's rules bucketed by cluster, plus a per-request memo of answers.

    Lives on flask.g beside the rule list it was built from (and is rebuilt
    when that list changes), so ``invalidate_access_rules_cache`` drops both.
    Filtering a namespace's pods or a page of alerts asks the same question
    many times over; each distinct question is now answered once.
    """

    __slots__ = ("rules", "by_cluster", "answers", "scoped_namespaces")

    def __init__(self, rules: List[AccessRule]):
        self.rules = rules
        self.by_cluster: Dict[str, List[_PreparedRule]] = {}
        for rule in rules:
            self.by_cluster.setdefault(rule.cluster_id, []).append(_PreparedRule(rule))
        self.answers: Dict[tuple, bool] = {}
        self.scoped_namespaces: Dict[str, Set[str]] = {}

    def cluster(self, cluster_id: str) -> List[_PreparedRule]:
        return self.by_cluster.get(cluster_id) or []


def _rule_index(user: User, rules: Optional[List[AccessRule]] = None) -> _RuleIndex:
    if rules is None:
        rules = _load_rules(user)
    try:
        cache = getattr(g, "_access_rule_index_by_user_id", None)
        if cache is None:
            cache = {}
            g._access_rule_index_by_user_id = cache
    except RuntimeError:
        return _RuleIndex(rules)
    index = cache.get(user.id)
    if index is None or index.rules is not rules:
        index = _RuleIndex(rules)
        cache[user.id] = index
    return index


def _scoped_namespaces(index: _RuleIndex, cluster_id: str) -> Set[str]:
    """Namespaces a user's allow rules single out on a cluster (may be empty)."""
    scoped = index.scoped_namespaces.get(cluster_id)
    if scoped is None:
        scoped = {
            p.ns
            for p in index.cluster(cluster_id)
            if p.effect == "allow"
            and p.permission_key in ("namespaces:view", "resources:view")
            and (p.raw_rt or "cluster") in ("namespace", "pod", "deployment", "service")
            and p.ns
        }
        index.scoped_namespaces[cluster_id] = scoped
    return scoped


def _prepared_matches(
    p: _PreparedRule,
    *,
    req_ns: Optional[str],
    req_rt: Optional[str],
    req_name: Optional[str],
    container_name: Optional[str],
    port: Optional[int],
    permission_key: str,
) -> bool:
    """``_rule_matches`` over a prepared rule already known to be on the cluster."""
    if not _rule_permission_matches(p.permission_key, permission_key):
        return False
    rule_ns = p.ns
    if rule_ns and req_ns and rule_ns != req_ns:
        return False
    if rule_ns and not req_ns and p.rt not in ("cluster",):
        return False
    rule_rt = p.rt
    if rule_rt == "cluster":
        return True
    if rule_rt == "namespace":
        return req_ns == rule_ns if rule_ns else bool(req_ns)
    if not req_rt:
        return False
    if rule_rt != req_rt:
        return False
    if p.name and req_name and p.name != req_name:
        return False
    if p.name and not req_name:
        return False
    if rule_rt in ("pod", "container") and p.container:
        req_container = (container_name or "").strip() or None
        if req_container and p.container != req_container:
            return False
    if rule_rt == "service_port":
        if p.port is not None and port is not None and int(p.port) != int(port):
            return False
        if p.port is not None and port is None:
            return False
    return True


def _evaluate_rules(
    index: _RuleIndex,
    *,
    cluster_id: str,
    permission_key: str,
    namespace: Optional[str],
    resource_type: Optional[str],
    resource_name: Optional[str],
    container_name: Optional[str],
    port: Optional[int],
) -> bool:
    """The rule half of ``evaluate_access``: the most specific matching rules
    decide, deny over allow among equals. Same answer as matching every rule
    with ``_rule_matches`` and sorting by ``rule_specificity``."""
    key = (cluster_id, permission_key, namespace, resource_type, resource_name, container_name, port)
    cached = index.answers.get(key)
    if cached is not None:
        return cached

    bucket = index.cluster(cluster_id)
    # _rules_for_evaluation: a namespace with named grants of this type ignores
    # broad (cluster-type, unnamed) rules there.
    if namespace and resource_type and any(
        p.raw_ns == namespace and p.raw_rt == resource_type and p.name and p.effect == "allow"
        for p in bucket
    ):
        bucket = [p for p in bucket if not p.is_cluster_type or p.name]

    req_ns = (namespace or "").strip() or None
    req_rt = (resource_type or "").strip() or None
    req_name = (resource_name or "").strip() or None
    best = None
    deny = allow = False
    for p in bucket:
        if not _prepared_matches(
            p,
            req_ns=req_ns,
            req_rt=req_rt,
            req_name=req_name,
            container_name=container_name,
            port=port,
            permission_key=permission_key,
        ):
            continue
        score = p.specificity
        if best is None or score > best:
            best, deny, allow = score, p.effect == "deny", p.effect == "allow"
        elif score == best:
            deny = deny or p.effect == "deny"
            allow = allow or p.effect == "allow"
    result = best is not None and not deny and allow
    index.answers[key] = result
    return result


def _rule_permission_matches(rule_key: str, requested_key: str) -> bool:
    if rule_key == requested_key:
        return True
    return rule_key in PERMISSION_ALIASES.get(requested_key, ())


def _rule_matches(
    rule: AccessRule,
    *,
    cluster_id: str,
    namespace: Optional[str],
    resource_type: Optional[str],
    resource_name: Optional[str],
    container_name: Optional[str],
    port: Optional[int],
    permission_key: str,
) -> bool:
    if rule.cluster_id != cluster_id:
        return False
    if not _rule_permission_matches(rule.permission_key, permission_key):
        return False

    rule_ns = (rule.namespace or "").strip() or None
    req_ns = (namespace or "").strip() or None

    rule_rt = (rule.resource_type or "cluster").strip()
    req_rt = (resource_type or "").strip() or None

    if rule_ns and req_ns and rule_ns != req_ns:
        return False
    if rule_ns and not req_ns and rule_rt not in ("cluster",):
        return False

    if rule_rt == "cluster":
        return True

    if rule_rt == "namespace":
        return req_ns == rule_ns if rule_ns else bool(req_ns)

    if not req_rt:
        return False
    if rule_rt != req_rt:
        return False

    rule_name = (rule.resource_name or "").strip() or None
    req_name = (resource_name or "").strip() or None
    if rule_name and req_name and rule_name != req_name:
        return False
    if rule_name and not req_name:
        return False

    if rule_rt in ("pod", "container") and rule.container_name:
        req_container = (container_name or "").strip() or None
        if req_container and rule.container_name != req_container:
            return False

    if rule_rt == "service_port":
        if rule.port is not None and port is not None and int(rule.port) != int(port):
            return False
        if rule.port is not None and port is None:
            return False

    return True


def _rules_for_evaluation(
    rules: List[AccessRule],
    *,
    cluster_id: str,
    namespace: Optional[str],
    resource_type: Optional[str],
) -> List[AccessRule]:
    """When a namespace has named resource grants, ignore broad cluster-level rules there."""
    if not namespace or not resource_type:
        return rules
    has_named = any(
        rule.cluster_id == cluster_id
        and (rule.namespace or "") == namespace
        and (rule.resource_type or "") == resource_type
        and (rule.resource_name or "").strip()
        and rule.effect == "allow"
        for rule in rules
    )
    if not has_named:
        return rules
    return [
        rule
        for rule in rules
        if (rule.resource_type or "cluster") != "cluster" or (rule.resource_name or "").strip()
    ]


def evaluate_access(
    user: User,
    *,
    cluster_id: str,
    permission_key: str,
    namespace: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_name: Optional[str] = None,
    container_name: Optional[str] = None,
    port: Optional[int] = None,
) -> bool:
    if is_admin(user):
        return True
    if not _role_grants(user, permission_key):
        return False

    rules = _load_rules(user)
    if _uses_legacy_only(user, rules):
        return _legacy_evaluate(
            user,
            cluster_id=cluster_id,
            permission_key=permission_key,
            namespace=namespace,
            resource_type=resource_type,
            resource_name=resource_name,
        )

    return _evaluate_rules(
        _rule_index(user, rules),
        cluster_id=cluster_id,
        permission_key=permission_key,
        namespace=namespace,
        resource_type=resource_type,
        resource_name=resource_name,
        container_name=container_name,
        port=port,
    )


def _legacy_evaluate(
    user: User,
    *,
    cluster_id: str,
    permission_key: str,
    namespace: Optional[str],
    resource_type: Optional[str],
    resource_name: Optional[str],
) -> bool:
    clusters = _legacy_cluster_ids(user)
    if cluster_id not in clusters:
        return False
    ns_pairs = _legacy_namespace_pairs(user)
    cluster_ns = {ns for cid, ns in ns_pairs if cid == cluster_id}
    if not cluster_ns:
        return True
    if not namespace:
        return permission_key == "clusters:view"
    if namespace not in cluster_ns:
        return False
    if resource_type and resource_name:
        return True
    return True


def can_access_cluster(user: User, cluster_id: str) -> bool:
    return evaluate_access(user, cluster_id=cluster_id, permission_key="clusters:view", resource_type="cluster")


def can_access_namespace(user: User, cluster_id: str, namespace: str) -> bool:
    if is_admin(user):
        return True
    rules = _load_rules(user)
    if rules:
        scoped_namespaces = _scoped_namespaces(_rule_index(user, rules), cluster_id)
        if scoped_namespaces:
            return namespace in scoped_namespaces
    return evaluate_access(
        user,
        cluster_id=cluster_id,
        namespace=namespace,
        permission_key="namespaces:view",
        resource_type="namespace",
    )


def _pod_matches_deployment_name(pod_name: str, deployment_name: str) -> bool:
    if not pod_name or not deployment_name:
        return False
    if pod_name == deployment_name:
        return True
    return pod_name.startswith(f"{deployment_name}-")


def can_access_resource(
    user: User,
    cluster_id: str,
    namespace: str,
    resource_type: str,
    resource_name: str,
) -> bool:
    perm_map = {
        "pod": "pods:view",
        "deployment": "deployments:view",
        "replicaset": "replicasets:view",
        "statefulset": "statefulsets:view",
        "daemonset": "daemonsets:view",
        "job": "jobs:view",
        "cronjob": "cronjobs:view",
        "service": "services:view",
    }
    permission_key = perm_map.get(resource_type, "resources:view")
    if evaluate_access(
        user,
        cluster_id=cluster_id,
        namespace=namespace,
        permission_key=permission_key,
        resource_type=resource_type,
        resource_name=resource_name,
    ):
        return True

    if resource_type != "pod":
        return False

    for p in _rule_index(user).cluster(cluster_id):
        if p.effect != "allow" or p.raw_ns != namespace:
            continue
        if p.raw_rt != "deployment" or not p.name:
            continue
        if not _pod_matches_deployment_name(resource_name, p.name):
            continue
        if evaluate_access(
            user,
            cluster_id=cluster_id,
            namespace=namespace,
            permission_key="deployments:view",
            resource_type="deployment",
            resource_name=p.name,
        ):
            return True
    return False


def can_view_logs(
    user: User,
    cluster_id: str,
    namespace: str,
    pod_name: str,
    container_name: Optional[str] = None,
) -> bool:
    if evaluate_access(
        user,
        cluster_id=cluster_id,
        namespace=namespace,
        permission_key="logs:view",
        resource_type="pod",
        resource_name=pod_name,
        container_name=container_name,
    ):
        return True
    if container_name and evaluate_access(
        user,
        cluster_id=cluster_id,
        namespace=namespace,
        permission_key="logs:view",
        resource_type="container",
        resource_name=pod_name,
        container_name=container_name,
    ):
        return True
    return evaluate_access(
        user,
        cluster_id=cluster_id,
        namespace=namespace,
        permission_key="logs:view",
        resource_type="namespace",
    )


def can_view_service_port(
    user: User,
    cluster_id: str,
    namespace: str,
    service_name: str,
    port: int,
) -> bool:
    if not _role_allows_permission(user, "services:ports:view"):
        return False
    return evaluate_access(
        user,
        cluster_id=cluster_id,
        namespace=namespace,
        permission_key="services:ports:view",
        resource_type="service_port",
        resource_name=service_name,
        port=port,
    )


def get_accessible_cluster_ids(user: User, all_cluster_ids: List[str]) -> Set[str]:
    if is_admin(user):
        return set(all_cluster_ids)
    return {cid for cid in all_cluster_ids if can_access_cluster(user, cid)}


def filter_clusters_for_user(user: User, cluster_items: List[dict]) -> List[dict]:
    if is_admin(user):
        return cluster_items
    allowed = get_accessible_cluster_ids(user, [c.get("id") for c in cluster_items if c.get("id")])
    return [item for item in cluster_items if item.get("id") in allowed]


def allowed_namespace_names_for_cluster(user: User, cluster_id: str) -> Optional[Set[str]]:
    """
    Namespace names the user may access for a cluster.
    None means all namespaces in the cluster (full cluster grant).
  """
    if is_admin(user):
        return None
    if not can_access_cluster(user, cluster_id):
        return set()

    rules = _load_rules(user)
    if _uses_legacy_only(user, rules):
        cluster_ns = {ns for cid, ns in _legacy_namespace_pairs(user) if cid == cluster_id}
        return None if not cluster_ns else cluster_ns

    scoped = _scoped_namespaces(_rule_index(user, rules), cluster_id)
    if not scoped:
        return None
    return set(scoped)


def filter_namespaces_for_user(user: User, cluster_id: str, namespaces: List) -> List:
    if is_admin(user):
        return namespaces
    allowed = allowed_namespace_names_for_cluster(user, cluster_id)
    if allowed is None:
        return namespaces
    filtered = []
    for ns in namespaces:
        name = ns.get("name") if isinstance(ns, dict) else ns
        if name and name in allowed:
            filtered.append(ns)
    return filtered


def filter_namespace_resources(user: User, cluster_id: str, resources: Dict[str, Any]) -> Dict[str, Any]:
    if is_admin(user):
        return resources
    namespace = resources.get("namespace") or ""
    pods = []
    for pod in resources.get("pods") or []:
        name = pod.get("name")
        if not name or not can_access_resource(user, cluster_id, namespace, "pod", name):
            continue
        pod_copy = dict(pod)
        actions = list(pod_copy.get("actions") or [])
        logs_allowed = can_view_logs(user, cluster_id, namespace, name)
        if "logs" in actions and not logs_allowed:
            actions = [a for a in actions if a != "logs"]
        pod_copy["actions"] = actions
        pod_copy["canViewLogs"] = logs_allowed
        pods.append(pod_copy)

    deployments = [
        d
        for d in resources.get("deployments") or []
        if d.get("name")
        and can_access_resource(user, cluster_id, namespace, "deployment", d["name"])
    ]

    replicasets = []
    for rs in resources.get("replicasets") or []:
        name = rs.get("name")
        if not name:
            continue
        owner = (rs.get("owner") or "").strip()
        if owner and owner != "-" and can_access_resource(user, cluster_id, namespace, "deployment", owner):
            replicasets.append(rs)
        elif can_access_resource(user, cluster_id, namespace, "replicaset", name):
            replicasets.append(rs)

    workload_types = (
        ("statefulsets", "statefulset"),
        ("daemonsets", "daemonset"),
        ("jobs", "job"),
        ("cronjobs", "cronjob"),
    )
    filtered_workloads: Dict[str, List[Dict[str, Any]]] = {}
    for list_key, resource_type in workload_types:
        filtered_workloads[list_key] = [
            item
            for item in resources.get(list_key) or []
            if item.get("name")
            and can_access_resource(user, cluster_id, namespace, resource_type, item["name"])
        ]

    services = []
    for svc in resources.get("services") or []:
        name = svc.get("name")
        if not name or not can_access_resource(user, cluster_id, namespace, "service", name):
            continue
        svc_copy = dict(svc)
        if not _role_allows_permission(user, "services:ports:view"):
            svc_copy["ports"] = []
            svc_copy["portsDetail"] = []
            svc_copy["canViewPorts"] = False
        else:
            ports_raw = svc_copy.get("ports") or []
            if isinstance(ports_raw, str):
                ports_raw = [p.strip() for p in ports_raw.split(",") if p.strip()]
            visible_ports = []
            ports_detail = []
            for p in ports_raw:
                try:
                    port_num = int(p)
                except (TypeError, ValueError):
                    continue
                if can_view_service_port(user, cluster_id, namespace, name, port_num):
                    visible_ports.append(port_num)
                    ports_detail.append({"port": port_num, "allowed": True})
            svc_copy["ports"] = visible_ports
            svc_copy["portsDetail"] = ports_detail
            svc_copy["canViewPorts"] = bool(visible_ports)
        services.append(svc_copy)

    # ConfigMaps and Secrets have no per-resource access rules; they are visible
    # to any non-admin who can reach the namespace. The list payloads never carry
    # secret values (only names/types/labels), so this matches resources:view.
    namespace_visible = can_access_namespace(user, cluster_id, namespace)
    configmaps = (resources.get("configmaps") or []) if namespace_visible else []
    secrets = (resources.get("secrets") or []) if namespace_visible else []
    # Ingresses have no per-resource access rules; like ConfigMaps/Secrets they are
    # visible to any non-admin who can reach the namespace and hold resources:view.
    ingress = (resources.get("ingress") or []) if namespace_visible else []

    return {
        "namespace": namespace,
        "pods": pods,
        "deployments": deployments,
        "replicasets": replicasets,
        "statefulsets": filtered_workloads["statefulsets"],
        "daemonsets": filtered_workloads["daemonsets"],
        "jobs": filtered_workloads["jobs"],
        "cronjobs": filtered_workloads["cronjobs"],
        "services": services,
        "configmaps": configmaps,
        "secrets": secrets,
        "ingress": ingress,
    }


_INVOLVED_KIND_TO_RESOURCE_TYPE = {
    "Pod": "pod",
    "Deployment": "deployment",
    "ReplicaSet": "replicaset",
    "StatefulSet": "statefulset",
    "DaemonSet": "daemonset",
    "Job": "job",
    "CronJob": "cronjob",
    "Service": "service",
}


def filter_namespace_events(
    user: User,
    cluster_id: str,
    namespace: str,
    events_payload: Dict[str, Any],
) -> Dict[str, Any]:
    if is_admin(user):
        return events_payload
    if can_access_namespace(user, cluster_id, namespace):
        resource_scoped = any(
            p.raw_ns == namespace
            and p.raw_rt in ("pod", "deployment", "service")
            and p.name
            for p in _rule_index(user).cluster(cluster_id)
        )
        if not resource_scoped:
            return events_payload
    filtered = []
    for event in events_payload.get("items") or []:
        kind = event.get("involvedKind") or ""
        name = event.get("involvedName") or ""
        if not kind or not name:
            filtered.append(event)
            continue
        resource_type = _INVOLVED_KIND_TO_RESOURCE_TYPE.get(kind)
        if resource_type:
            if can_access_resource(user, cluster_id, namespace, resource_type, name):
                filtered.append(event)
        else:
            filtered.append(event)
    return {
        **events_payload,
        "items": filtered,
        "count": len(filtered),
    }


def can_view_alert(user: User, alert: Dict[str, Any]) -> bool:
    if is_admin(user):
        return True
    if not _role_allows_permission(user, "alerts:view"):
        return False

    cluster_id = alert.get("clusterId") or alert.get("cluster")
    if not cluster_id or not can_access_cluster(user, cluster_id):
        return False

    namespace = (alert.get("namespace") or "").strip() or None
    resource_name = (alert.get("pod") or alert.get("resourceName") or alert.get("resource") or "").strip() or None

    if resource_name and namespace:
        if evaluate_access(
            user,
            cluster_id=cluster_id,
            namespace=namespace,
            permission_key="alerts:view",
            resource_type="pod",
            resource_name=resource_name,
        ):
            return True

    if namespace:
        return evaluate_access(
            user,
            cluster_id=cluster_id,
            namespace=namespace,
            permission_key="alerts:view",
            resource_type="namespace",
        )

    return evaluate_access(
        user,
        cluster_id=cluster_id,
        permission_key="alerts:view",
        resource_type="cluster",
    )


def filter_alerts_for_user(user: User, alerts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if is_admin(user):
        return alerts
    return [alert for alert in alerts if can_view_alert(user, alert)]
