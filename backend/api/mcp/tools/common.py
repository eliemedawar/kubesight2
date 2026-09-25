"""What every non-CI domain needs before it can answer anything.

Three jobs, and each exists because the alternative was worse:

**Unwrapping the service layer.** Nearly every service in this backend returns
``(data, error, status)`` rather than raising, because that is what a Flask route
wants. A tool wants the opposite — an answer or a refusal the agent can read. So
``unwrap`` converts once, here, instead of nine times with nine different
wordings.

**Access, not just permission.** A permission says *what* a user may do; the
access engine says *where*. ``resources:view`` does not mean every namespace, and
the registry's permission check cannot know that, so a tool that names a cluster
or a namespace asks here as well. Both checks are the same ones the HTTP route
makes, in the same order.

**Naming a cluster the way a person does.** Clusters have ids that nobody says
out loud. An agent is given "production" or "the DR cluster" and has to turn that
into an id; making it call a listing tool first to do that is a wasted round trip
on almost every question, so the resolution lives here and every cluster-scoped
tool accepts either.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ...access_engine import (
    can_access_cluster,
    can_access_namespace,
    filter_clusters_for_user,
    is_admin,
)
from ..protocol import ToolError


def _refuse(message: str, what: str) -> ToolError:
    """The service's words, made useful to an agent.

    Kept verbatim except in one case. A service denying access answers the bare
    word "Forbidden", which is right for an HTTP status and useless to an agent:
    it cannot tell a missing permission from a namespace outside its scope, so
    it reports "access denied" and the person is no further forward. The two
    possibilities are named instead, because they have different fixes.
    """
    text = (message or "").strip() or "KubeSight refused the request."
    if text.lower().strip(".") == "forbidden":
        text = (
            "Forbidden — this token either lacks the permission for this action "
            "or has no access to that cluster or namespace. kubesight_roles_list "
            "shows which role grants what."
        )
    return ToolError(f"{what}: {text}" if what else text)


def unwrap(result: Tuple[Optional[Any], Optional[Any], int], *, what: str = "") -> Any:
    """``(data, error, status)`` in, data out — or the error, as a refusal.

    The shape nearly every service in this backend returns, because it is what a
    Flask route wants. A tool wants the opposite, so the conversion happens once.
    """
    data, error, _status = result
    if error:
        raise _refuse(str(error), what)
    return data


def unwrap2(result: Tuple[Optional[Any], Optional[Any]], *, what: str = "") -> Any:
    """The two-item variant, where the error is already a Flask error response.

    ``error_response`` returns a ``(Response, status)`` tuple rather than a bare
    Response, so the body is one unwrapping further in than it looks. Getting
    that wrong is silent — every message becomes the generic fallback and the
    reason the call failed is simply gone.
    """
    data, error = result
    if error is None:
        return data

    response = error[0] if isinstance(error, tuple) and error else error
    message = ""
    try:
        body = response.get_json(silent=True) or {}
        message = str(body.get("error") or body.get("message") or "")
    except Exception:
        message = ""
    raise _refuse(message, what)


# ---------------------------------------------------------------------------
# Clusters, by whichever name the question used
# ---------------------------------------------------------------------------

def cluster_items() -> List[Dict[str, Any]]:
    """Every cluster KubeSight knows about, real or custom, before filtering."""
    from ...k8s_provider import K8sCommandError, list_clusters_from_k8s, should_use_real_k8s

    items: List[Dict[str, Any]] = []
    if should_use_real_k8s():
        try:
            items = list(list_clusters_from_k8s().get("items") or [])
        except K8sCommandError:
            items = []
    else:
        from ...mock_data import CLUSTERS

        items = list(CLUSTERS)
    try:
        from ...k8s_provider import _custom_clusters_as_items

        custom = _custom_clusters_as_items()
        known = {item.get("id") for item in items}
        items.extend(item for item in custom if item.get("id") not in known)
    except Exception:
        pass
    return items


def visible_clusters(user: Any) -> List[Dict[str, Any]]:
    items = cluster_items()
    return filter_clusters_for_user(user, items) if user else items


def resolve_cluster(user: Any, reference: Any) -> str:
    """A cluster id from an id or a display name, checked for access.

    An unknown name lists what there is rather than saying no: the agent asked
    with the word a person used, and the useful answer to a wrong word is the
    right ones.
    """
    raw = str(reference or "").strip()
    if not raw:
        raise ToolError("Name the cluster, by id or name.")
    items = visible_clusters(user)
    by_id = {str(item.get("id")): item for item in items}
    if raw in by_id:
        cluster_id = raw
    else:
        match = next(
            (item for item in items if str(item.get("name", "")).lower() == raw.lower()), None
        )
        if match is None:
            known = ", ".join(sorted(str(item.get("id")) for item in items)) or "none visible"
            raise ToolError(f"No cluster '{raw}'. Clusters you can see: {known}.")
        cluster_id = str(match.get("id"))

    if user is not None and not can_access_cluster(user, cluster_id):
        raise ToolError(f"This token has no access to cluster '{cluster_id}'.")
    return cluster_id


def require_namespace(user: Any, cluster_id: str, namespace: str) -> str:
    """The namespace, once the token is known to reach it."""
    name = str(namespace or "").strip()
    if not name:
        raise ToolError("Name the namespace.")
    if user is not None and not is_admin(user) and not can_access_namespace(user, cluster_id, name):
        raise ToolError(f"This token has no access to '{cluster_id}/{name}'.")
    return name


def cluster_access_or_error(cluster_id: str):
    """The kubectl access record for a cluster, or None in mock mode."""
    from ...k8s_provider import resolve_cluster_access, should_use_real_k8s

    if not should_use_real_k8s(cluster_id):
        return None
    access = resolve_cluster_access(cluster_id)
    if not access:
        raise ToolError(f"Cluster '{cluster_id}' is not reachable — no kubeconfig resolved for it.")
    return access


def cluster_name(cluster_id: str) -> str:
    for item in cluster_items():
        if str(item.get("id")) == str(cluster_id):
            return str(item.get("name") or cluster_id)
    return str(cluster_id)


# ---------------------------------------------------------------------------
# Trimming
# ---------------------------------------------------------------------------

def take(rows: Any, limit: int) -> List[Any]:
    """A list, capped, tolerant of a service that returned something else."""
    if not isinstance(rows, list):
        return []
    return rows[:limit]


def pick(row: Any, keys: Tuple[str, ...]) -> Dict[str, Any]:
    """Only the fields named, and only the ones present.

    Tools send the fields an answer needs rather than whatever the service
    happened to serialise. A payload the size of a namespace listing is most of
    a context window, and almost none of it is the answer.
    """
    if not isinstance(row, dict):
        return {}
    return {key: row[key] for key in keys if key in row}


def changed_or_queued(data: Dict[str, Any], changed: str) -> Dict[str, Any]:
    """A write's result, honest about whether it happened yet.

    On a cluster that requires approvals, a write without a live approved request
    is not refused: it is sent for approval as a change bundle and KubeSight
    applies it once approved. Saying "applied" then would be the one wrong
    answer, so the summary names the bundle instead.
    """
    if data.get("pendingApproval"):
        return {
            "changed": (
                f"NOT applied yet — sent for approval as change bundle #{data.get('bundleId')}; "
                "KubeSight applies it automatically once it is approved"
            ),
            **data,
        }
    return {"changed": changed, **data}
