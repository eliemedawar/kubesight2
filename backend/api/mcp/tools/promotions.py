"""The promotion ladder: Dev → SIT → UAT → Pre-prod, and moving an image up it.

Part of the deploys domain. The ladder is enforced inside the services every
deploy goes through (apply_yaml, deploy automation, the CI Deploy stage, Helm,
change bundles), so these tools add no gate of their own — they let an agent
see the ladder before it acts, so a refusal is predicted instead of hit:

* ``kubesight_promotion_board`` — what runs in each environment, and which
  images are ready to move up;
* ``kubesight_promotion_check`` — what the ladder would say about a deploy;
* ``kubesight_promotion_promote`` — send the image that passed one environment
  to the next, through apply_yaml (so the cluster's approval rule still holds).

Deliberately absent: asking for an exception. Skipping an environment is a
person's judgment, made in the UI with a written reason, and approved by
someone else.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..protocol import ToolError
from .common import require_namespace, resolve_cluster
from .registry import tool as _register


def tool(name, **kwargs):
    kwargs.setdefault("domain", "deploys")
    return _register(name, **kwargs)


def _user():
    from ...auth_utils import get_current_user

    try:
        return get_current_user()
    except Exception:
        return None


def _environment(reference: Any):
    from ...services import promotion_service as svc

    raw = str(reference or "").strip().lower()
    rungs = svc.ladder()
    for env in rungs:
        if raw in (env.key.lower(), env.name.lower(), str(env.id)):
            return env
    names = ", ".join(env.name for env in rungs) or "none — the ladder is not set up"
    raise ToolError(f"No environment '{reference}'. The ladder: {names}.")


@tool(
    "kubesight_promotion_board",
    permission="promotions:view",
    cluster_scoped=False,
    description=(
        "The promotion ladder (e.g. Dev → SIT → UAT → Pre-prod) and, per "
        "application image, the tag running in each environment and the state of "
        "each step: ready (the image passed the lower environment and can be "
        "promoted), in_sync, waiting (not healthy below yet), soaking, blocked, "
        "not_deployed. Also lists drift — an environment running an image that "
        "never passed the one below. Call this before deploying to any "
        "environment above the first one."
    ),
    schema={
        "type": "object",
        "properties": {
            "application": {
                "type": "string",
                "description": "Optional: only applications whose name, system or image repository contains this.",
            }
        },
    },
)
def _board(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services import promotion_service as svc

    data = svc.overview()
    envs = {env["id"]: env["name"] for env in data["environments"]}
    needle = str(arguments.get("application") or "").strip().lower()
    rows: List[Dict[str, Any]] = []
    for row in data["apps"]:
        if needle and not any(needle in (row[k] or "").lower() for k in ("name", "repository", "system")):
            continue
        rows.append(
            {
                "application": row["name"],
                "system": row["system"],
                "repository": row["repository"],
                "lag": row["lag"],
                "running": {
                    envs[cell["environmentId"]]: (", ".join(cell["tags"]) or None) for cell in row["cells"]
                },
                "steps": [
                    {
                        "from": envs[step["fromEnvironmentId"]],
                        "to": envs[step["toEnvironmentId"]],
                        "state": step["state"],
                        "image": step["image"],
                        "detail": step["detail"],
                    }
                    for step in row["steps"]
                ],
                "drift": [
                    f"{envs[cell['environmentId']]} runs {d['tag']}, which never passed {d['skipped']}"
                    for cell in row["cells"]
                    for d in cell["drift"]
                ],
            }
        )
    return {
        "ladder": [
            {"name": env["name"], "mode": env["mode"], "minSoakMinutes": env["minSoakMinutes"],
             "namespaces": [f"{b['clusterId']}/{b['namespace']}" for b in env["bindings"]]}
            for env in data["environments"]
        ],
        "gates": [
            {
                "from": envs[g["fromEnvironmentId"]],
                "to": envs[g["toEnvironmentId"]],
                **{k: v for k, v in g["counts"].items() if v},
            }
            for g in data["gates"]
        ],
        "applications": rows[:100],
        "truncated": len(rows) > 100,
        "summary": data["summary"],
        "errors": data["errors"],
    }


@tool(
    "kubesight_promotion_check",
    permission="promotions:view",
    description=(
        "What the promotion ladder says about deploying these images into this "
        "namespace: which environment it is, whether each image passed the one "
        "before it (or ran here already — a rollback is always allowed), and "
        "whether the deploy would be refused (enforce) or only flagged (warn)."
    ),
    schema={
        "type": "object",
        "properties": {
            "cluster": {"type": "string"},
            "namespace": {"type": "string"},
            "images": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["cluster", "namespace", "images"],
    },
)
def _check(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services import promotion_service as svc

    user = _user()
    cluster_id = resolve_cluster(user, arguments.get("cluster"))
    namespace = require_namespace(user, cluster_id, arguments.get("namespace"))
    images = arguments.get("images")
    if not isinstance(images, list) or not images:
        raise ToolError("'images' is required — the image references to check.")
    return svc.evaluate(cluster_id, namespace, [str(i) for i in images])


@tool(
    "kubesight_promotion_promote",
    permission="apps:deploy",
    cluster_scoped=True,
    description=(
        "Promote an application to the next environment: deploy the image that "
        "passed the environment below to its workloads in the named environment. "
        "Name the application (as kubesight_promotion_board shows it) and the "
        "target environment; the image defaults to the one the board offers for "
        "that step. Each workload is changed through the normal deploy path."
    ),
    approval=(
        "The ladder and the cluster's approval rule both apply: a step that is "
        "not ready is refused, and on a cluster that requires approvals the "
        "change is sent as a change bundle and applied once approved. The result "
        "says which happened for each workload — tell the person."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "application": {"type": "string"},
            "environment": {"type": "string", "description": "Target environment, e.g. UAT."},
            "image": {"type": "string", "description": "Optional: the exact image to promote."},
            "note": {"type": "string"},
        },
        "required": ["application", "environment"],
    },
)
def _promote(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...mcp.access import check_cluster
    from ...services import promotion_service as svc

    env = _environment(arguments.get("environment"))
    needle = str(arguments.get("application") or "").strip().lower()
    if not needle:
        raise ToolError("Name the application to promote.")
    data = svc.overview()
    rows = [r for r in data["apps"] if needle in (r["name"].lower(), r["repository"].lower())] or [
        r for r in data["apps"] if needle in r["name"].lower() or needle in r["repository"].lower()
    ]
    if not rows:
        raise ToolError(f"No application '{arguments.get('application')}' on the promotion board.")
    if len(rows) > 1:
        raise ToolError("Several applications match: " + ", ".join(r["name"] for r in rows) + ". Name one.")
    row = rows[0]
    step = next((s for s in row["steps"] if s["toEnvironmentId"] == env.id), None)
    if step is None:
        raise ToolError(f"{env.name} is the first environment — deploy to it directly; nothing is promoted into it.")
    image = str(arguments.get("image") or step["image"] or "").strip()
    if not image:
        raise ToolError(f"Nothing to promote: {step['detail']}")
    if not step["targets"]:
        raise ToolError(step["detail"] or f"{row['name']} has no workload in {env.name}.")
    for target in step["targets"]:
        check_cluster(target["clusterId"])
    result = svc.promote(
        user,
        image=image,
        environment_id=env.id,
        targets=step["targets"],
        note=str(arguments.get("note") or "").strip()[:500] or f"Promoted by an agent to {env.name}",
    )
    counts: Dict[str, int] = {}
    for item in result["results"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    applied = counts.get("applied", 0)
    queued = counts.get("pending_approval", 0)
    refused = counts.get("refused", 0)
    parts = []
    if applied:
        parts.append(f"{result['image']} applied to {applied} workload(s) in {env.name}")
    if queued:
        parts.append(f"NOT applied yet on {queued} workload(s) — sent for approval as change bundle(s)")
    if refused:
        parts.append(f"refused on {refused} workload(s) — see results")
    return {"changed": "; ".join(parts) or "nothing changed", **result}
