"""Getting something into a cluster, and the approvals in the way.

This is the domain where "full access" and "safe" have to be the same sentence,
and the way they are is that **none of these tools implement a deploy**. Each one
calls the service the UI calls, and the gates live inside those services:

* ``apply_yaml`` asks ``assert_deploy_allowed`` before it touches kubectl. On a
  cluster configured to require approvals, a deploy without a live approved
  request fails with the reason, and an agent cannot route around that because
  there is no second path to kubectl in this server.
* ``install_or_upgrade_release`` demands an exact confirmation phrase, the same
  one a person types into the Helm dialog. It is generated from the release and
  namespace, so producing it means having named the right release in the right
  place — which is the point of the phrase. It, rollback and uninstall are also
  under the same per-cluster approval rule as ``apply_yaml``.
* Nobody is exempt from that rule — not admins, and not an agent holding an
  admin's token. A requester can never vote on their own request.
* A change bundle is approved as a unit and executed by KubeSight afterwards.
  An agent can build one and submit it; only an approver's vote runs it.

So the shape of the domain is: **check, preview, then ask**. ``eligibility``
says whether a deploy would be allowed, ``dry_run`` and ``diff`` say what it
would do, and the write tools do it. An agent that skips the first two arrives
at a refusal it could have predicted, and reports it as a failure rather than a
rule.

The one thing deliberately absent is approving. ``deployment_requests:manage``
and ``change_bundles:manage`` are the permissions that let somebody approve a
change, and no tool here calls them even for a token that holds them — an agent
that can both request and approve is an approval process with one participant.
"""

from __future__ import annotations

from typing import Any, Dict

from ..protocol import ToolError
from .common import cluster_name, pick, require_namespace, resolve_cluster, take, unwrap
from .registry import MAX_ROWS, _limit, tool as _register


def tool(name, **kwargs):
    kwargs.setdefault("domain", "deploys")
    return _register(name, **kwargs)


def _user():
    from ...auth_utils import get_current_user

    try:
        return get_current_user()
    except Exception:
        return None


def _yaml_of(arguments: Dict[str, Any]) -> str:
    content = str(arguments.get("yaml") or "").strip()
    if not content:
        raise ToolError("'yaml' is required — the manifest to apply, as text.")
    return content


# ---------------------------------------------------------------------------
# Would this be allowed, and what would it do
# ---------------------------------------------------------------------------

@tool(
    "kubesight_deploy_eligibility",
    permission="apps:deploy",
    description=(
        "Whether this token may deploy to a cluster right now. Says how many "
        "approvals the cluster requires, whether there is a live approved "
        "request covering this user, and when its window ends. Call this BEFORE "
        "attempting a deploy on any cluster you have not deployed to in this "
        "conversation — the refusal it predicts is a rule, not a failure."
    ),
    schema={
        "type": "object",
        "properties": {"cluster": {"type": "string"}},
        "required": ["cluster"],
    },
)
def _deploy_eligibility(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.deployment_request_service import deploy_eligibility

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    return deploy_eligibility(user, cluster_id)


@tool(
    "kubesight_deploy_validate",
    permission="apps:dryrun",
    description=(
        "Parse a manifest and report what it contains and whether any kind in it "
        "is blocked for this token. No cluster is contacted. The cheapest check "
        "before anything else."
    ),
    schema={
        "type": "object",
        "properties": {
            "yaml": {"type": "string"},
            "namespace": {"type": "string"},
        },
        "required": ["yaml", "namespace"],
    },
)
def _deploy_validate(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.deployment_service import validate_yaml

    namespace = str(arguments.get("namespace") or "").strip()
    if not namespace:
        raise ToolError("'namespace' is required.")
    return unwrap(validate_yaml(_yaml_of(arguments), namespace, user=_user()), what="validate") or {}


@tool(
    "kubesight_deploy_dry_run",
    permission="apps:dryrun",
    description=(
        "Run the manifest against the cluster's API server without persisting "
        "anything (`kubectl apply --dry-run=server`). This is what catches an "
        "invalid field, a missing CRD or a rejected admission webhook — things "
        "validation alone cannot see."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "yaml": {"type": "string"},
        },
        "required": ["cluster", "namespace", "yaml"],
    },
)
def _deploy_dry_run(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.deployment_service import dry_run_yaml

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    return unwrap(
        dry_run_yaml(user, cluster_id, namespace, _yaml_of(arguments)), what="dry run"
    ) or {}


@tool(
    "kubesight_deploy_diff",
    permission="apps:diff",
    description=(
        "What applying this manifest would change against what is live "
        "(`kubectl diff`). Read this out before deploying: it is the difference "
        "between 'this updates the image' and 'this also drops three env vars'."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "yaml": {"type": "string"},
        },
        "required": ["cluster", "namespace", "yaml"],
    },
)
def _deploy_diff(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.deployment_service import diff_yaml

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    return unwrap(diff_yaml(user, cluster_id, namespace, _yaml_of(arguments)), what="diff") or {}


@tool(
    "kubesight_deploy_apply",
    permission="apps:deploy",
    description=(
        "Apply a manifest to a cluster for real. Creates the namespace if it is "
        "missing, verifies every image exists in its linked registry, and fails "
        "if it does not."
    ),
    approval=(
        "On a cluster configured to require approvals this fails unless the "
        "token's user has a live approved deployment request — check "
        "kubesight_deploy_eligibility first, and use "
        "kubesight_deployment_request_create to ask."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "yaml": {"type": "string"},
        },
        "required": ["cluster", "namespace", "yaml"],
    },
)
def _deploy_apply(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    """Apply, through the same path the Deploy screen posts to.

    The empty ``confirmation`` below is not a gate being skipped. ``apply_yaml``
    takes the argument and never reads it — the deploy screen's confirmation is
    a UI affordance, and the real gate is ``assert_deploy_allowed``, which runs
    inside the service before anything reaches kubectl. Helm is the opposite
    case: there the phrase *is* checked, which is why ``kubesight_helm_upgrade``
    demands one.
    """
    from ...services.deployment_service import apply_yaml

    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    data = unwrap(
        apply_yaml(user, cluster_id, namespace, _yaml_of(arguments), ""), what="apply"
    ) or {}
    return {"changed": f"applied to {cluster_id}/{namespace}", **data}


# ---------------------------------------------------------------------------
# Asking for permission to deploy
# ---------------------------------------------------------------------------

_REQUEST_FIELDS = (
    "id", "clusterId", "clusterName", "status", "message", "requestedBy",
    "windowLabel", "windowStart", "windowEnd", "approvals", "requiredApprovals",
    "createdAt", "decidedAt",
)


@tool(
    "kubesight_deployment_requests_list",
    permission="deployment_requests:view",
    description=(
        "Deployment approval requests and where each one stands: pending, "
        "approved, declined or expired, with its window and its vote tally."
    ),
    schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["pending", "approved", "declined", "expired", "all"]},
            "cluster": {"type": "string"},
            "mine": {"type": "boolean", "description": "Only this token's own requests."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
    },
)
def _deployment_requests_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.deployment_request_service import list_requests, list_requests_for_user

    user = _user()
    rows = (
        list_requests_for_user(user) if arguments.get("mine") else list_requests()
    )
    status = str(arguments.get("status") or "").strip().lower()
    if status and status != "all":
        rows = [row for row in rows if str(row.get("status", "")).lower() == status]
    if arguments.get("cluster"):
        cluster_id = resolve_cluster(user, arguments.get("cluster"))
        rows = [row for row in rows if str(row.get("clusterId")) == cluster_id]
    total = len(rows)
    rows = [pick(row, _REQUEST_FIELDS) for row in take(rows, _limit(arguments, MAX_ROWS))]
    return {"totalMatching": total, "count": len(rows), "requests": rows}


@tool(
    "kubesight_deployment_request_create",
    permission="deployment_requests:request",
    description=(
        "Ask for approval to deploy to a cluster during a time window. This does "
        "not deploy anything and does not approve anything — it emails the "
        "approvers and waits. Once approved, the same user may deploy to that "
        "cluster until the window ends. The window must start in the future."
    ),
    approval="This tool IS the approval request; a person still has to vote on it.",
    write=True,
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "message": {"type": "string", "description": "Why, in the requester's words. Shown to approvers."},
            "windowStart": {"type": "string", "description": "ISO 8601. Must be in the future."},
            "windowEnd": {"type": "string", "description": "ISO 8601. After the start."},
            "timezone": {"type": "string", "description": "IANA name, e.g. Asia/Beirut."},
        },
        "required": ["cluster", "message", "windowStart", "windowEnd"],
    },
)
def _deployment_request_create(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.deployment_request_service import DeploymentRequestError, create_request

    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    try:
        data = create_request(
            user,
            cluster_id,
            cluster_name(cluster_id),
            str(arguments.get("message") or ""),
            window_start=arguments.get("windowStart"),
            window_end=arguments.get("windowEnd"),
            window_timezone=arguments.get("timezone"),
        )
    except DeploymentRequestError as exc:
        raise ToolError(str(exc))
    return {"changed": f"requested approval to deploy to {cluster_id}", **(data or {})}


# ---------------------------------------------------------------------------
# Change bundles — a batch of changes approved as one
# ---------------------------------------------------------------------------

_BUNDLE_FIELDS = (
    "id", "title", "status", "clusterId", "clusterName", "createdBy", "itemCount",
    "approvals", "requiredApprovals", "windowLabel", "createdAt", "submittedAt",
)


@tool(
    "kubesight_change_bundles_list",
    permission="change_bundles:view",
    description=(
        "Change bundles — batches of changes submitted for approval as one unit "
        "and executed by KubeSight once approved. Shows status, cluster, item "
        "count and votes."
    ),
    schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
    },
)
def _change_bundles_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.change_bundle_service import list_bundles_for_approval

    status = str(arguments.get("status") or "").strip() or None
    rows = list_bundles_for_approval(status=status)
    total = len(rows)
    rows = [pick(row, _BUNDLE_FIELDS) for row in take(rows, _limit(arguments, MAX_ROWS))]
    return {"totalMatching": total, "count": len(rows), "bundles": rows}


@tool(
    "kubesight_change_bundle_get",
    permission="change_bundles:view",
    description=(
        "One change bundle with every item in it: what each one changes, and "
        "whether it still validates against the cluster as it is now."
    ),
    schema={
        "type": "object",
        "properties": {"bundleId": {"type": "integer"}},
        "required": ["bundleId"],
    },
)
def _change_bundle_get(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.change_bundle_service import (
        ChangeBundleError,
        get_bundle_or_error,
        serialize_bundle,
    )

    try:
        row = get_bundle_or_error(int(arguments.get("bundleId") or 0))
    except (ChangeBundleError, TypeError, ValueError) as exc:
        raise ToolError(str(exc) or "Name the bundle by its id.")
    return serialize_bundle(row, include_items=True)


# ---------------------------------------------------------------------------
# Helm
# ---------------------------------------------------------------------------

_RELEASE_FIELDS = (
    "name", "namespace", "revision", "status", "chart", "appVersion", "updated",
)


@tool(
    "kubesight_helm_releases",
    permission="helm:view",
    description=(
        "Helm releases in a cluster, optionally in one namespace: chart, "
        "version, revision and status. A release stuck in pending-upgrade is "
        "usually the answer to 'why will this not deploy'."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
        "required": ["cluster"],
    },
)
def _helm_releases(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.helm_service import list_releases

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = str(arguments.get("namespace") or "").strip()
    if namespace:
        namespace = require_namespace(user, cluster_id, namespace)
    rows = list_releases(cluster_id, namespace or None) or []
    total = len(rows)
    rows = [pick(row, _RELEASE_FIELDS) for row in take(rows, _limit(arguments, MAX_ROWS))]
    return {"clusterId": cluster_id, "totalMatching": total, "count": len(rows), "releases": rows}


@tool(
    "kubesight_helm_release_get",
    permission="helm:view",
    description=(
        "One Helm release in full: its values, its revision history and the "
        "manifest it rendered."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "release": {"type": "string"},
        },
        "required": ["cluster", "namespace", "release"],
    },
)
def _helm_release_get(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.helm_service import get_release_detail

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    release = str(arguments.get("release") or "").strip()
    if not release:
        raise ToolError("Name the release.")
    detail = get_release_detail(cluster_id, namespace, release)
    if not detail:
        raise ToolError(f"No release '{release}' in {cluster_id}/{namespace}, or Helm is unavailable.")
    return {"clusterId": cluster_id, "namespace": namespace, **detail}


@tool(
    "kubesight_helm_upgrade",
    permission="helm:upgrade",
    description=(
        "Install or upgrade a Helm release. Requires an exact confirmation "
        "phrase — 'UPGRADE <release> IN <namespace>' for an existing release, "
        "'INSTALL <release> IN <namespace>' for a new one, lowercase release "
        "name. The phrase is not busywork: it is the check that you named the "
        "release and namespace you meant."
    ),
    approval=(
        "Needs the exact confirmation phrase AND, on a cluster configured to "
        "require approvals, a live approved deployment request for the token's "
        "user — check kubesight_deploy_eligibility first."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "release": {"type": "string", "description": "Lowercase."},
            "chartName": {"type": "string"},
            "chartVersion": {"type": "string", "description": "Omit or 'latest' for the newest."},
            "repositoryName": {"type": "string"},
            "repositoryUrl": {"type": "string", "description": "Added if the repo is not known yet."},
            "valuesYaml": {"type": "string"},
            "confirmation": {"type": "string"},
        },
        "required": ["cluster", "namespace", "release", "chartName", "confirmation"],
    },
)
def _helm_upgrade(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.helm_service import install_or_upgrade_release

    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    payload = {
        "clusterId": cluster_id,
        "namespace": namespace,
        "releaseName": str(arguments.get("release") or "").strip().lower(),
        "chartName": arguments.get("chartName"),
        "chartVersion": arguments.get("chartVersion") or "",
        "repositoryName": arguments.get("repositoryName") or "",
        "repositoryUrl": arguments.get("repositoryUrl") or "",
        "valuesYaml": arguments.get("valuesYaml") or "",
    }
    data = unwrap(
        install_or_upgrade_release(user, payload, str(arguments.get("confirmation") or "")),
        what="helm",
    ) or {}
    return {"changed": f"released {payload['releaseName']} in {cluster_id}/{namespace}", **data}


@tool(
    "kubesight_helm_rollback",
    permission="helm:rollback",
    description=(
        "Roll a Helm release back to an earlier revision, or to the previous one "
        "if no revision is named. Read kubesight_helm_release_get first — the "
        "revision numbers are in its history."
    ),
    approval=(
        "On a cluster configured to require approvals this fails unless the "
        "token's user has a live approved deployment request — check "
        "kubesight_deploy_eligibility first."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "release": {"type": "string"},
            "revision": {"type": "integer", "minimum": 1},
        },
        "required": ["cluster", "namespace", "release"],
    },
)
def _helm_rollback(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.helm_service import rollback_release

    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    release = str(arguments.get("release") or "").strip()
    if not release:
        raise ToolError("Name the release.")
    revision = arguments.get("revision")
    data = unwrap(
        rollback_release(user, cluster_id, namespace, release, revision), what=release
    ) or {}
    return {"changed": f"rolled back {release} in {cluster_id}/{namespace}", **data}


@tool(
    "kubesight_helm_uninstall",
    permission="helm:uninstall",
    description=(
        "Remove a Helm release and everything it created. This deletes running "
        "workloads and, depending on the chart, their PersistentVolumeClaims — "
        "which is not something a rollback brings back. Confirm with a person "
        "before calling it."
    ),
    approval=(
        "On a cluster configured to require approvals this fails unless the "
        "token's user has a live approved deployment request — check "
        "kubesight_deploy_eligibility first."
    ),
    write=True,
    destructive=True,
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "release": {"type": "string"},
        },
        "required": ["cluster", "namespace", "release"],
    },
)
def _helm_uninstall(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.helm_service import uninstall_release

    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    release = str(arguments.get("release") or "").strip()
    if not release:
        raise ToolError("Name the release.")
    data = unwrap(uninstall_release(user, cluster_id, namespace, release), what=release) or {}
    return {"changed": f"uninstalled {release} from {cluster_id}/{namespace}", **data}
