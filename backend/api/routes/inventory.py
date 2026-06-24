from flask import Blueprint, request

from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services.app_catalog_service import (
    get_entry_by_id,
    register_existing_app,
    remove_from_inventory,
    update_catalog_entry,
    create_or_update_from_deployment,
    _serialize_entry,
)
from ..services.deployment_service import (
    apply_yaml,
    diff_yaml,
    dry_run_yaml,
    sanitize_yaml_preview,
    validate_yaml,
)
from ..services.inventory_actions_service import (
    get_rollout_history,
    restart_deployment,
    rollback_deployment,
    scale_deployment,
)
from ..services.inventory_service import (
    get_inventory_detail,
    list_inventory,
    list_namespace_workloads,
)
from ..services.manifest_generator import generate_manifests
from ..services.wizard_manifest_generator import generate_wizard_manifests, validate_k8s_name
from ..services.prerequisite_validator import validate_prerequisites
from ..services.wizard_templates import list_templates, get_template
from ..services.template_resolver import resolve_template
from ..services.user_template_service import (
    create_user_template,
    delete_user_template,
    delete_user_template_category,
    get_user_template_detail,
    list_user_template_summaries,
    update_user_template,
)
from ..services.application_version_service import (
    create_deployment_version,
    list_versions_for_inventory,
    get_version,
    compare_versions,
    rollback_to_version,
)
from ..k8s_provider import K8sCommandError, resolve_cluster_access, _run_for_access
from ..access_engine import can_access_namespace, is_admin

inventory_bp = Blueprint("inventory", __name__, url_prefix="/api/inventory")


def _query_filters() -> dict:
    return {
        key: value
        for key, value in {
            "cluster": request.args.get("cluster") or request.args.get("clusterId"),
            "namespace": request.args.get("namespace"),
            "name": request.args.get("name") or request.args.get("applicationName"),
            "status": request.args.get("status"),
            "workloadType": request.args.get("workloadType"),
            "imageTag": request.args.get("imageTag"),
            "search": request.args.get("search"),
        }.items()
        if value
    }


def _body() -> dict:
    return request.get_json(silent=True) or {}


@inventory_bp.route("", methods=["GET"])
@require_permission("inventory:view")
def inventory_list():
    user = get_current_user()
    items, error, status = list_inventory(user, _query_filters())
    if error:
        return error_response(error, status)
    return success_response(items)


@inventory_bp.route("/actions/rollout-history", methods=["GET"])
@require_permission("apps:deploy")
def inventory_rollout_history():
    user = get_current_user()
    cluster_id = request.args.get("cluster") or request.args.get("clusterId") or ""
    namespace = request.args.get("namespace") or ""
    workload_name = request.args.get("workloadName") or request.args.get("workload_name") or ""
    if not cluster_id or not namespace or not workload_name:
        return error_response("clusterId, namespace, and workloadName are required", 400)
    data, error, status = get_rollout_history(user, cluster_id, namespace, workload_name)
    if error:
        return error_response(error, status)
    return success_response(data)


@inventory_bp.route("/actions/restart", methods=["POST"])
@require_permission("apps:deploy")
def inventory_restart():
    user = get_current_user()
    data, error, status = restart_deployment(user, _body())
    if error:
        return error_response(error, status)
    return success_response(data)


@inventory_bp.route("/actions/scale", methods=["POST"])
@require_permission("apps:deploy")
def inventory_scale():
    user = get_current_user()
    data, error, status = scale_deployment(user, _body())
    if error:
        return error_response(error, status)
    return success_response(data)


@inventory_bp.route("/actions/rollback", methods=["POST"])
@require_permission("apps:deploy")
def inventory_rollback():
    user = get_current_user()
    data, error, status = rollback_deployment(user, _body())
    if error:
        return error_response(error, status)
    return success_response(data)


@inventory_bp.route("/workloads", methods=["GET"])
@require_permission("inventory:register")
def inventory_workloads():
    user = get_current_user()
    cluster_id = request.args.get("cluster") or request.args.get("clusterId") or ""
    namespace = request.args.get("namespace") or ""
    if not cluster_id or not namespace:
        return error_response("cluster and namespace are required", 400)
    workloads, error, status = list_namespace_workloads(user, cluster_id, namespace)
    if error:
        return error_response(error, status)
    return success_response(workloads)


@inventory_bp.route("/versions/compare", methods=["GET"])
@require_permission("inventory:view")
def inventory_versions_compare():
    user = get_current_user()
    try:
        version_a = int(request.args.get("versionA") or request.args.get("a") or 0)
        version_b = int(request.args.get("versionB") or request.args.get("b") or 0)
    except (TypeError, ValueError):
        return error_response("versionA and versionB are required integers", 400)
    if not version_a or not version_b:
        return error_response("versionA and versionB are required", 400)
    data, error, status = compare_versions(user, version_a, version_b)
    if error:
        return error_response(error, status)
    return success_response(data)


@inventory_bp.route("/versions/<int:version_id>", methods=["GET"])
@require_permission("inventory:view")
def inventory_version_detail(version_id: int):
    user = get_current_user()
    include_yaml = request.args.get("includeYaml", "true").lower() != "false"
    data, error, status = get_version(user, version_id, include_yaml=include_yaml)
    if error:
        return error_response(error, status)
    return success_response(data)


@inventory_bp.route("/versions/<int:version_id>/rollback", methods=["POST"])
@require_permission("apps:deploy")
def inventory_version_rollback(version_id: int):
    user = get_current_user()
    confirmation = (_body().get("confirmation") or "").strip()
    data, error, status = rollback_to_version(user, version_id, confirmation)
    if error:
        return error_response(error, status)
    return success_response(data)


@inventory_bp.route("/<path:inventory_id>/versions", methods=["GET"])
@require_permission("inventory:view")
def inventory_versions(inventory_id: str):
    user = get_current_user()
    data, error, status = list_versions_for_inventory(user, inventory_id)
    if error:
        return error_response(error, status)
    return success_response(data)


@inventory_bp.route("/<path:inventory_id>", methods=["GET"])
@require_permission("inventory:view")
def inventory_detail(inventory_id: str):
    user = get_current_user()
    data, error, status = get_inventory_detail(user, inventory_id)
    if error:
        return error_response(error, status)
    return success_response(data)


@inventory_bp.route("/register", methods=["POST"])
@require_permission("inventory:register")
def inventory_register():
    user = get_current_user()
    data, error, status = register_existing_app(user, _body())
    if error:
        return error_response(error, status)
    return success_response(data, status)


@inventory_bp.route("/<int:catalog_id>", methods=["PUT"])
@require_permission("inventory:update")
def inventory_update(catalog_id: int):
    user = get_current_user()
    data, error, status = update_catalog_entry(user, catalog_id, _body())
    if error:
        return error_response(error, status)
    return success_response(data)


@inventory_bp.route("/<int:catalog_id>", methods=["DELETE"])
@require_permission("inventory:remove")
def inventory_remove(catalog_id: int):
    user = get_current_user()
    data, error, status = remove_from_inventory(user, catalog_id)
    if error:
        return error_response(error, status)
    return success_response(data)


@inventory_bp.route("/catalog/<int:entry_id>", methods=["GET"])
@require_permission("inventory:view")
def catalog_entry_detail(entry_id: int):
    entry = get_entry_by_id(entry_id)
    if not entry or not entry.is_active:
        return error_response("Catalog entry not found", 404)
    return success_response(_serialize_entry(entry))


@inventory_bp.route("/catalog/<int:entry_id>", methods=["PUT"])
@require_permission("inventory:update")
def catalog_entry_update(entry_id: int):
    user = get_current_user()
    data, error, status = update_catalog_entry(user, entry_id, _body())
    if error:
        return error_response(error, status)
    return success_response(data)


@inventory_bp.route("/catalog/<int:entry_id>", methods=["DELETE"])
@require_permission("inventory:remove")
def catalog_entry_remove(entry_id: int):
    user = get_current_user()
    data, error, status = remove_from_inventory(user, entry_id)
    if error:
        return error_response(error, status)
    return success_response(data)


@inventory_bp.route("/deploy/yaml/validate", methods=["POST"])
@require_permission("apps:dryrun")
def deploy_yaml_validate():
    user = get_current_user()
    body = _body()
    yaml_content = body.get("yaml") or body.get("yamlContent") or ""
    namespace = (body.get("namespace") or "").strip()
    if not namespace:
        return error_response("namespace is required", 400)
    data, error, status = validate_yaml(yaml_content, namespace, user=user)
    if error:
        return error_response(error, status)
    preview = sanitize_yaml_preview(yaml_content)
    return success_response({**data, "preview": preview})


@inventory_bp.route("/deploy/yaml/dry-run", methods=["POST"])
@require_permission("apps:dryrun")
def deploy_yaml_dry_run():
    user = get_current_user()
    body = _body()
    cluster_id = body.get("clusterId") or body.get("cluster") or ""
    namespace = (body.get("namespace") or "").strip()
    yaml_content = body.get("yaml") or body.get("yamlContent") or ""
    if not cluster_id or not namespace:
        return error_response("clusterId and namespace are required", 400)
    data, error, status = dry_run_yaml(user, cluster_id, namespace, yaml_content)
    if error:
        return error_response(error, status)
    data["preview"] = sanitize_yaml_preview(yaml_content)
    return success_response(data)


@inventory_bp.route("/deploy/yaml/diff", methods=["POST"])
@require_permission("apps:diff")
def deploy_yaml_diff():
    user = get_current_user()
    body = _body()
    cluster_id = body.get("clusterId") or body.get("cluster") or ""
    namespace = (body.get("namespace") or "").strip()
    yaml_content = body.get("yaml") or body.get("yamlContent") or ""
    if not cluster_id or not namespace:
        return error_response("clusterId and namespace are required", 400)
    data, error, status = diff_yaml(user, cluster_id, namespace, yaml_content)
    if error:
        return error_response(error, status)
    return success_response(data)


@inventory_bp.route("/deploy/yaml/apply", methods=["POST"])
@require_permission("apps:deploy")
def deploy_yaml_apply():
    user = get_current_user()
    body = _body()
    cluster_id = body.get("clusterId") or body.get("cluster") or ""
    namespace = (body.get("namespace") or "").strip()
    yaml_content = body.get("yaml") or body.get("yamlContent") or ""
    confirmation = body.get("confirmation") or ""
    if not cluster_id or not namespace:
        return error_response("clusterId and namespace are required", 400)
    data, error, status = apply_yaml(user, cluster_id, namespace, yaml_content, confirmation)
    if error:
        return error_response(error, status)

    description = body.get("description") or ""
    deployment_name = body.get("deploymentName") or body.get("deploymentNameOverride") or ""
    resources = data.get("resources") or []
    app_name = deployment_name
    if not app_name:
        for res in resources:
            if res.get("kind") == "Deployment":
                app_name = res.get("name")
                break
        if not app_name and resources:
            app_name = resources[0].get("name")

    if app_name:
        create_or_update_from_deployment(
            user,
            cluster_id=cluster_id,
            namespace=namespace,
            display_name=app_name,
            workload_type="Deployment",
            workload_name=app_name,
            description=description or None,
        )

    return success_response(data)


@inventory_bp.route("/deploy/image/generate", methods=["POST"])
@require_permission("apps:dryrun")
def deploy_image_generate():
    body = _body()
    yaml_content, summary, error = generate_manifests(body)
    if error:
        return error_response(error, 400)
    return success_response({"yaml": yaml_content, "summary": summary, "preview": yaml_content})


@inventory_bp.route("/deploy/image/dry-run", methods=["POST"])
@require_permission("apps:dryrun")
def deploy_image_dry_run():
    user = get_current_user()
    body = _body()
    cluster_id = body.get("clusterId") or body.get("cluster") or ""
    namespace = (body.get("namespace") or "").strip()
    yaml_content, _, gen_error = generate_manifests(body)
    if gen_error:
        return error_response(gen_error, 400)
    if not cluster_id or not namespace:
        return error_response("clusterId and namespace are required", 400)
    data, error, status = dry_run_yaml(user, cluster_id, namespace, yaml_content)
    if error:
        return error_response(error, status)
    data["yaml"] = yaml_content
    data["preview"] = yaml_content
    return success_response(data)


@inventory_bp.route("/deploy/image/apply", methods=["POST"])
@require_permission("apps:deploy")
def deploy_image_apply():
    user = get_current_user()
    body = _body()
    cluster_id = body.get("clusterId") or body.get("cluster") or ""
    namespace = (body.get("namespace") or "").strip()
    confirmation = body.get("confirmation") or ""
    yaml_content, summary, gen_error = generate_manifests(body)
    if gen_error:
        return error_response(gen_error, 400)
    if not cluster_id or not namespace:
        return error_response("clusterId and namespace are required", 400)
    data, error, status = apply_yaml(user, cluster_id, namespace, yaml_content, confirmation)
    if error:
        return error_response(error, status)

    app_name = summary.get("appName") or body.get("appName") or ""
    create_or_update_from_deployment(
        user,
        cluster_id=cluster_id,
        namespace=namespace,
        display_name=app_name,
        workload_type="Deployment",
        workload_name=app_name,
        owner_team=body.get("ownerTeam") or body.get("owner_team"),
        environment=body.get("environment"),
        criticality=body.get("criticality"),
        description=body.get("description"),
        contact_email=body.get("contactEmail") or body.get("contact_email"),
        tags=body.get("tags"),
    )
    return success_response({**data, "yaml": yaml_content, "summary": summary})


def _can_manage_templates() -> bool:
    """Admin-authored templates are visible to and managed by admins only."""
    from ..auth_utils import auth_required_enabled

    if not auth_required_enabled():
        return True
    user = get_current_user()
    return bool(user and is_admin(user))


@inventory_bp.route("/deploy/wizard/templates", methods=["GET"])
@require_permission("inventory:view")
def deploy_wizard_templates():
    templates = list_templates()
    if _can_manage_templates():
        templates = templates + list_user_template_summaries()
    return success_response(templates)


@inventory_bp.route("/deploy/wizard/templates/<template_id>", methods=["GET"])
@require_permission("inventory:view")
def deploy_wizard_template_detail(template_id: str):
    template = get_template(template_id)
    if not template and _can_manage_templates():
        template = get_user_template_detail(template_id)
    if not template:
        return error_response("Template not found", 404)
    return success_response(template)


@inventory_bp.route("/deploy/wizard/templates", methods=["POST"])
@require_permission("inventory:view")
def deploy_wizard_template_create():
    if not _can_manage_templates():
        return error_response("Forbidden", 403)
    user = get_current_user()
    data, error, status = create_user_template(_body(), actor_user_id=user.id if user else None)
    if error:
        return error_response(error, status)
    return success_response(data, status_code=status)


@inventory_bp.route("/deploy/wizard/templates/<template_id>", methods=["PUT"])
@require_permission("inventory:view")
def deploy_wizard_template_update(template_id: str):
    if not _can_manage_templates():
        return error_response("Forbidden", 403)
    if get_template(template_id) is not None:
        return error_response("Built-in templates cannot be edited.", 400)
    user = get_current_user()
    data, error, status = update_user_template(template_id, _body(), actor_user_id=user.id if user else None)
    if error:
        return error_response(error, status)
    return success_response(data, status_code=status)


@inventory_bp.route("/deploy/wizard/templates/<template_id>", methods=["DELETE"])
@require_permission("inventory:view")
def deploy_wizard_template_delete(template_id: str):
    if not _can_manage_templates():
        return error_response("Forbidden", 403)
    if get_template(template_id) is not None:
        return error_response("Built-in templates cannot be deleted.", 400)
    user = get_current_user()
    data, error, status = delete_user_template(template_id, actor_user_id=user.id if user else None)
    if error:
        return error_response(error, status)
    return success_response(data, status_code=status)


@inventory_bp.route("/deploy/wizard/templates/categories/<path:category>", methods=["DELETE"])
@require_permission("inventory:view")
def deploy_wizard_template_category_delete(category: str):
    if not _can_manage_templates():
        return error_response("Forbidden", 403)
    user = get_current_user()
    data, error, status = delete_user_template_category(category, actor_user_id=user.id if user else None)
    if error:
        return error_response(error, status)
    return success_response(data, status_code=status)


@inventory_bp.route("/deploy/wizard/templates/<template_id>/resolve", methods=["POST"])
@require_permission("apps:dryrun")
def deploy_wizard_template_resolve(template_id: str):
    """Merge a template's schema with deployment answers into a deploy payload.

    The returned payload has the exact shape the wizard generate/dry-run/apply
    routes consume, so the deployer never re-enters anything the template locks.
    """
    template = get_template(template_id)
    if not template and _can_manage_templates():
        template = get_user_template_detail(template_id)
    if not template:
        return error_response("Template not found", 404)
    payload, error = resolve_template(template, _body())
    if error:
        return error_response(error, 400)
    yaml_content, summary, gen_error = generate_wizard_manifests(payload)
    if gen_error:
        return error_response(gen_error, 400)
    return success_response({"payload": payload, "yaml": yaml_content, "summary": summary})


@inventory_bp.route("/deploy/wizard/validate-name", methods=["POST"])
@require_permission("apps:dryrun")
def deploy_wizard_validate_name():
    user = get_current_user()
    body = _body()
    basics = body.get("basics") or body
    app_name = (basics.get("appName") or "").strip()
    namespace = (basics.get("namespace") or "").strip()
    cluster_id = basics.get("clusterId") or basics.get("cluster") or ""
    workload_type = body.get("workloadType") or "Deployment"

    name_err = validate_k8s_name(app_name.lower().replace("_", "-"))
    if name_err:
        return success_response({"valid": False, "error": name_err})

    if user and cluster_id and namespace and not can_access_namespace(user, cluster_id, namespace):
        return error_response("Forbidden", 403)

    exists = False
    if cluster_id and namespace:
        access = resolve_cluster_access(cluster_id)
        if access:
            kind_map = {
                "Deployment": "deployment",
                "StatefulSet": "statefulset",
                "DaemonSet": "daemonset",
                "Job": "job",
                "CronJob": "cronjob",
                "Service": "service",
                "ConfigMap": "configmap",
                "Secret": "secret",
                "PersistentVolumeClaim": "pvc",
                "HorizontalPodAutoscaler": "hpa",
                "Ingress": "ingress",
            }
            kind = kind_map.get(workload_type, "deployment")
            sanitized = app_name.lower().replace("_", "-")
            try:
                _run_for_access(access, ["get", kind, sanitized, "-n", namespace])
                exists = True
            except K8sCommandError:
                exists = False

    return success_response({
        "valid": True,
        "exists": exists,
        "warning": "Resource already exists — apply will update it" if exists else None,
    })


@inventory_bp.route("/deploy/wizard/generate", methods=["POST"])
@require_permission("apps:dryrun")
def deploy_wizard_generate():
    body = _body()
    yaml_content, summary, gen_error = generate_wizard_manifests(body)
    if gen_error:
        return error_response(gen_error, 400)
    return success_response({"yaml": yaml_content, "summary": summary, "preview": yaml_content})


@inventory_bp.route("/deploy/wizard/validate-prerequisites", methods=["POST"])
@require_permission("apps:dryrun")
def deploy_wizard_validate_prerequisites():
    user = get_current_user()
    data, error, status = validate_prerequisites(user, _body())
    if error:
        return error_response(error, status)
    return success_response(data)


@inventory_bp.route("/deploy/wizard/dry-run", methods=["POST"])
@require_permission("apps:dryrun")
def deploy_wizard_dry_run():
    user = get_current_user()
    body = _body()
    basics = body.get("basics") or {}
    cluster_id = basics.get("clusterId") or basics.get("cluster") or ""
    namespace = (basics.get("namespace") or "").strip()
    yaml_content, summary, gen_error = generate_wizard_manifests(body)
    if gen_error:
        return error_response(gen_error, 400)
    if not cluster_id or not namespace:
        return error_response("clusterId and namespace are required", 400)
    data, error, status = dry_run_yaml(user, cluster_id, namespace, yaml_content)
    if error:
        return error_response(error, status)
    data["yaml"] = yaml_content
    data["summary"] = summary
    data["preview"] = yaml_content
    return success_response(data)


@inventory_bp.route("/deploy/wizard/diff", methods=["POST"])
@require_permission("apps:diff")
def deploy_wizard_diff():
    user = get_current_user()
    body = _body()
    basics = body.get("basics") or {}
    cluster_id = basics.get("clusterId") or basics.get("cluster") or ""
    namespace = (basics.get("namespace") or "").strip()
    yaml_content, _, gen_error = generate_wizard_manifests(body)
    if gen_error:
        return error_response(gen_error, 400)
    if not cluster_id or not namespace:
        return error_response("clusterId and namespace are required", 400)
    data, error, status = diff_yaml(user, cluster_id, namespace, yaml_content)
    if error:
        return error_response(error, status)
    return success_response(data)


@inventory_bp.route("/deploy/wizard/apply", methods=["POST"])
@require_permission("apps:deploy")
def deploy_wizard_apply():
    user = get_current_user()
    body = _body()
    basics = body.get("basics") or {}
    cluster_id = basics.get("clusterId") or basics.get("cluster") or ""
    namespace = (basics.get("namespace") or "").strip()
    confirmation = body.get("confirmation") or ""
    change_summary = body.get("changeSummary") or body.get("change_summary") or ""

    yaml_content, summary, gen_error = generate_wizard_manifests(body)
    if gen_error:
        return error_response(gen_error, 400)
    if not cluster_id or not namespace:
        return error_response("clusterId and namespace are required", 400)

    data, error, status = apply_yaml(user, cluster_id, namespace, yaml_content, confirmation)
    if error:
        return error_response(error, status)

    app_name = summary.get("appName") or basics.get("appName") or ""
    workload_type = summary.get("workloadType") or body.get("workloadType") or "Deployment"

    catalog_entry = create_or_update_from_deployment(
        user,
        cluster_id=cluster_id,
        namespace=namespace,
        display_name=app_name,
        workload_type=workload_type,
        workload_name=app_name,
        owner_team=basics.get("ownerTeam") or basics.get("owner_team"),
        environment=basics.get("environment"),
        criticality=basics.get("criticality"),
        description=basics.get("description"),
        contact_email=basics.get("contactEmail") or basics.get("contact_email"),
        tags=basics.get("tags"),
    )

    catalog_id = catalog_entry.id if catalog_entry else None
    version = create_deployment_version(
        user,
        cluster_id=cluster_id,
        namespace=namespace,
        app_name=app_name,
        workload_type=workload_type,
        yaml_snapshot=yaml_content,
        change_summary=change_summary or f"Deployed {workload_type} {app_name}",
        wizard_config=body,
        catalog_entry_id=catalog_id,
    )

    return success_response({
        **data,
        "yaml": yaml_content,
        "summary": summary,
        "version": {
            "id": version.id,
            "versionLabel": version.version_label,
            "createdAt": version.created_at.isoformat() if version.created_at else None,
        },
    })
