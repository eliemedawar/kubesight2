from __future__ import annotations



from datetime import datetime, timezone

from typing import Any, Dict, Optional, Tuple



from ..audit import log_audit

from ..k8s_provider import K8sCommandError, _run_for_access, resolve_cluster_access, should_use_real_k8s

from ..upgrade_provider import (

    build_upgrade_info,

    mock_precheck,

    mock_upgrade_info,

    normalize_version,

    run_extended_prechecks,

    run_upgrade_workflow,

    validate_target_version,

)





def _mock_context_for_cluster(cluster_id: str) -> str:

    if "kind" in cluster_id.lower():

        return "kind-test"

    if "minikube" in cluster_id.lower():

        return "minikube"

    return "docker-desktop"





def get_upgrade_info(

    cluster_id: str,

    target_version: str,

    *,

    actor_user_id: Optional[int] = None,

) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:

    if not cluster_id:

        return None, "clusterId is required.", 400



    valid, error = validate_target_version(target_version)

    if not valid:

        return None, error, 400



    normalized_target = normalize_version(target_version)



    if should_use_real_k8s(cluster_id):

        access = resolve_cluster_access(cluster_id)

        if not access:

            return None, "Cluster not found", 404

        try:

            data = build_upgrade_info(access, normalized_target, _run_for_access)

            log_audit(

                "upgrade_info_viewed",

                actor_user_id=actor_user_id,

                target_type="cluster",

                target_id=cluster_id,

                details={"targetVersion": normalized_target, "provider": data.get("provider", {}).get("provider")},

            )

            return data, None, 200

        except K8sCommandError as exc:

            return None, f"Failed to load upgrade info: {exc}", 503



    context = _mock_context_for_cluster(cluster_id)

    data = mock_upgrade_info(cluster_id, normalized_target, context)

    log_audit(

        "upgrade_info_viewed",

        actor_user_id=actor_user_id,

        target_type="cluster",

        target_id=cluster_id,

        details={"targetVersion": normalized_target, "mode": "mock"},

    )

    return data, None, 200





def run_precheck(

    cluster_id: str,

    target_version: str,

    *,

    actor_user_id: Optional[int] = None,

) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:

    if not cluster_id:

        return None, "clusterId is required.", 400



    valid, error = validate_target_version(target_version)

    if not valid:

        return None, error, 400



    normalized_target = normalize_version(target_version)



    if should_use_real_k8s(cluster_id):

        access = resolve_cluster_access(cluster_id)

        if not access:

            return None, "Cluster not found", 404

        try:

            data = run_extended_prechecks(access, normalized_target, _run_for_access)

            log_audit(

                "upgrade_precheck_run",

                actor_user_id=actor_user_id,

                target_type="cluster",

                target_id=cluster_id,

                details={

                    "targetVersion": normalized_target,

                    "canUpgrade": data.get("canUpgrade"),

                    "provider": data.get("provider", {}).get("provider"),

                    "checkSummary": {

                        c["name"]: c["status"] for c in data.get("checks", [])

                    },

                },

            )

            return data, None, 200

        except K8sCommandError as exc:

            log_audit(

                "upgrade_precheck_failed",

                actor_user_id=actor_user_id,

                target_type="cluster",

                target_id=cluster_id,

                details={"targetVersion": normalized_target, "error": str(exc)},

            )

            return None, f"Upgrade precheck failed: {exc}", 503



    context = _mock_context_for_cluster(cluster_id)

    data = mock_precheck(cluster_id, normalized_target, context)

    log_audit(

        "upgrade_precheck_run",

        actor_user_id=actor_user_id,

        target_type="cluster",

        target_id=cluster_id,

        details={"targetVersion": normalized_target, "mode": "mock", "canUpgrade": data.get("canUpgrade")},

    )

    return data, None, 200





def run_start(

    cluster_id: str,

    target_version: str,

    *,

    confirmation: Optional[str] = None,

    actor_user_id: Optional[int] = None,

) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:

    if not cluster_id:

        return None, "clusterId is required.", 400



    valid, error = validate_target_version(target_version)

    if not valid:

        return None, error, 400



    normalized_target = normalize_version(target_version)



    if should_use_real_k8s(cluster_id):

        access = resolve_cluster_access(cluster_id)

        if not access:

            return None, "Cluster not found", 404

        try:

            result = run_upgrade_workflow(

                access,

                normalized_target,

                _run_for_access,

                confirmation=confirmation,

            )

            status = result.get("status")

            if status == "blocked":

                log_audit(

                    "upgrade_start_blocked",

                    actor_user_id=actor_user_id,

                    target_type="cluster",

                    target_id=cluster_id,

                    details={"targetVersion": normalized_target, "reason": result.get("message")},

                )

                return result, None, 409

            if status == "confirmation_required":

                log_audit(

                    "upgrade_start_confirmation_required",

                    actor_user_id=actor_user_id,

                    target_type="cluster",

                    target_id=cluster_id,

                    details={"targetVersion": normalized_target},

                )

                return result, None, 200

            action = "upgrade_plan_generated"
            if status == "running":
                action = "upgrade_start_run"
            elif status != "manual_required":
                action = "upgrade_start_run"

            log_audit(

                action,

                actor_user_id=actor_user_id,

                target_type="cluster",

                target_id=cluster_id,

                details={

                    "targetVersion": normalized_target,

                    "upgradeId": result.get("upgradeId") or result.get("jobId"),

                    "status": status,

                    "provider": result.get("provider", {}).get("provider"),

                    "executionSupported": result.get("executionSupported"),

                },

            )

            return result, None, 200

        except ValueError as exc:
            return None, str(exc), 400

        except K8sCommandError as exc:

            log_audit(

                "upgrade_start_failed",

                actor_user_id=actor_user_id,

                target_type="cluster",

                target_id=cluster_id,

                details={"targetVersion": normalized_target, "error": str(exc)},

            )

            return None, f"Upgrade start failed: {exc}", 503



    from ..upgrade_provider import generate_upgrade_plan, get_provider_support



    context = _mock_context_for_cluster(cluster_id)

    provider_support = get_provider_support("docker-desktop" if "docker" in context else "unknown")

    plan = generate_upgrade_plan(provider_support)

    steps = [

        {"step": s["step"], "name": s["name"], "status": "manual", "message": "Manual upgrade required in mock mode."}

        for s in plan.get("steps", [])

    ]

    result = {

        "upgradeId": f"upg-mock-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",

        "clusterId": cluster_id,

        "targetVersion": normalized_target,

        "currentVersion": "v1.29.0",

        "status": "manual_required",

        "message": provider_support.get("reason", "Manual upgrade required."),

        "steps": steps,

        "activeStep": 0,

        "provider": provider_support,

        "upgradePlan": plan,

        "instructions": provider_support.get("instructions"),

        "executionSupported": False,

        "startedAt": datetime.now(timezone.utc).isoformat(),

    }

    log_audit(

        "upgrade_plan_generated",

        actor_user_id=actor_user_id,

        target_type="cluster",

        target_id=cluster_id,

        details={"targetVersion": normalized_target, "mode": "mock", "upgradeId": result["upgradeId"]},

    )

    return result, None, 200


def get_upgrade_job(job_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    from ..upgrade_jobs import get_job

    job = get_job((job_id or "").strip())
    if not job:
        return None, "Upgrade job not found", 404
    return job, None, 200

