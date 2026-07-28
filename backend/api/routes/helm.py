from flask import Blueprint, request

from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services.app_catalog_service import create_or_update_from_helm
from ..services.helm_service import (
    add_repository,
    check_helm_available,
    dry_run_release,
    expected_confirmation,
    get_release_detail,
    install_or_upgrade_release,
    list_releases,
    list_repositories,
    release_exists_from_payload,
    render_template,
    rollback_release,
    search_charts,
    uninstall_release,
    update_repositories,
    _resolve_access,
)
from ..services.helm_chart_template_service import (
    ChartTemplateError,
    add_chart_version,
    delete_chart_template,
    delete_chart_version,
    discover_git_paths,
    discover_git_refs,
    get_chart_template,
    import_archive_chart,
    import_git_chart,
    import_yaml_chart,
    list_chart_templates,
    set_current_chart_version,
)

helm_bp = Blueprint("helm", __name__, url_prefix="/api/helm")


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _cluster_id() -> str:
    return request.args.get("cluster") or request.args.get("clusterId") or ""


def _namespace() -> str:
    return request.args.get("namespace") or ""


def _catalog_chart_metadata(body: dict) -> tuple:
    template_id = body.get("chartTemplateId") or body.get("chart_template_id")
    template = get_chart_template(str(template_id)) if template_id else None
    return (
        body.get("chartName")
        or body.get("chart_name")
        or (template or {}).get("name"),
        body.get("chartVersion")
        or body.get("chart_version")
        or (template or {}).get("version"),
    )


@helm_bp.route("/status", methods=["GET"])
@require_permission("helm:view")
def helm_status():
    return success_response(check_helm_available())


@helm_bp.route("/releases", methods=["GET"])
@require_permission("helm:view")
def helm_list_releases():
    cluster_id = _cluster_id()
    namespace = _namespace() or None
    if not cluster_id:
        return error_response("cluster query parameter is required", 400)
    releases = list_releases(cluster_id, namespace)
    return success_response(releases)


@helm_bp.route("/releases/<release_name>", methods=["GET"])
@require_permission("helm:view")
def helm_get_release(release_name: str):
    cluster_id = _cluster_id()
    namespace = _namespace()
    if not cluster_id or not namespace:
        return error_response("cluster and namespace query parameters are required", 400)
    detail = get_release_detail(cluster_id, namespace, release_name)
    if not detail:
        return error_response("Release not found or Helm unavailable", 404)
    return success_response(detail)


@helm_bp.route("/repos", methods=["GET"])
@require_permission("helm:view")
def helm_list_repos():
    cluster_id = _cluster_id()
    if not cluster_id:
        return error_response("cluster query parameter is required", 400)
    try:
        access = _resolve_access(cluster_id)
        repos = list_repositories(access)
        return success_response(repos)
    except Exception as exc:
        return error_response(str(exc), 503)


@helm_bp.route("/repos", methods=["POST"])
@require_permission("helm:install")
def helm_add_repo():
    body = _body()
    cluster_id = body.get("clusterId") or body.get("cluster") or ""
    repo_name = body.get("repositoryName") or body.get("repoName") or ""
    repo_url = body.get("repositoryUrl") or body.get("repoUrl") or ""
    if not cluster_id or not repo_name or not repo_url:
        return error_response("clusterId, repositoryName, and repositoryUrl are required", 400)
    try:
        access = _resolve_access(cluster_id)
        output = add_repository(repo_name, repo_url, access)
        update_repositories(access)
        return success_response({"added": True, "output": output})
    except Exception as exc:
        return error_response(str(exc), 400)


@helm_bp.route("/charts", methods=["GET"])
@require_permission("helm:view")
def helm_search_charts():
    cluster_id = _cluster_id()
    repo_name = request.args.get("repo") or request.args.get("repoName") or ""
    query = request.args.get("q") or request.args.get("chart") or ""
    if not cluster_id or not repo_name:
        return error_response("cluster and repo query parameters are required", 400)
    try:
        access = _resolve_access(cluster_id)
        charts = search_charts(access, repo_name, query)
        return success_response(charts)
    except Exception as exc:
        return error_response(str(exc), 400)


@helm_bp.route("/chart-templates", methods=["GET"])
@require_permission("helm:view")
def helm_chart_templates():
    return success_response(list_chart_templates())


@helm_bp.route("/chart-templates/<slug>", methods=["GET"])
@require_permission("helm:view")
def helm_chart_template_detail(slug: str):
    version = request.args.get("version") or ""
    try:
        template = get_chart_template(slug, version)
    except ChartTemplateError as exc:
        return error_response(str(exc), 404)
    if not template:
        return error_response("Helm chart template not found.", 404)
    return success_response(template)


@helm_bp.route("/chart-templates/<slug>/versions", methods=["POST"])
@require_permission("inventory:update")
def helm_chart_template_add_version(slug: str):
    user = get_current_user()
    data, err, status = add_chart_version(
        slug, _body(), actor_user_id=user.id if user else None
    )
    if err:
        return error_response(err, status)
    return success_response(data, status_code=status)


@helm_bp.route("/chart-templates/<slug>/versions/<version>/current", methods=["POST"])
@require_permission("inventory:update")
def helm_chart_template_select_version(slug: str, version: str):
    user = get_current_user()
    data, err, status = set_current_chart_version(
        slug, version, actor_user_id=user.id if user else None
    )
    if err:
        return error_response(err, status)
    return success_response(data)


@helm_bp.route("/chart-templates/<slug>/versions/<version>", methods=["DELETE"])
@require_permission("inventory:update")
def helm_chart_template_delete_version(slug: str, version: str):
    user = get_current_user()
    data, err, status = delete_chart_version(
        slug, version, actor_user_id=user.id if user else None
    )
    if err:
        return error_response(err, status)
    return success_response(data)


@helm_bp.route("/chart-templates/import/yaml", methods=["POST"])
@require_permission("inventory:update")
def helm_chart_template_import_yaml():
    user = get_current_user()
    data, err, status = import_yaml_chart(
        _body(), actor_user_id=user.id if user else None
    )
    if err:
        return error_response(err, status)
    return success_response(data, status_code=status)


@helm_bp.route("/chart-templates/import/git", methods=["POST"])
@require_permission("inventory:update")
def helm_chart_template_import_git():
    user = get_current_user()
    data, err, status = import_git_chart(
        _body(), actor_user_id=user.id if user else None
    )
    if err:
        return error_response(err, status)
    return success_response(data, status_code=status)


@helm_bp.route("/chart-templates/git/refs", methods=["POST"])
@require_permission("inventory:update")
def helm_chart_template_git_refs():
    data, err, status = discover_git_refs(_body())
    if err:
        return error_response(err, status)
    return success_response(data)


@helm_bp.route("/chart-templates/git/paths", methods=["POST"])
@require_permission("inventory:update")
def helm_chart_template_git_paths():
    data, err, status = discover_git_paths(_body())
    if err:
        return error_response(err, status)
    return success_response(data)


@helm_bp.route("/chart-templates/import/archive", methods=["POST"])
@require_permission("inventory:update")
def helm_chart_template_import_archive():
    user = get_current_user()
    data, err, status = import_archive_chart(
        _body(), actor_user_id=user.id if user else None
    )
    if err:
        return error_response(err, status)
    return success_response(data, status_code=status)


@helm_bp.route("/chart-templates/<slug>", methods=["DELETE"])
@require_permission("inventory:update")
def helm_chart_template_delete(slug: str):
    user = get_current_user()
    data, err, status = delete_chart_template(
        slug, actor_user_id=user.id if user else None
    )
    if err:
        return error_response(err, status)
    return success_response(data)


@helm_bp.route("/template", methods=["POST"])
@require_permission("helm:view")
def helm_template():
    data, err, status = render_template(_body())
    if err:
        return error_response(err, status)
    return success_response(data)


@helm_bp.route("/dry-run", methods=["POST"])
@require_permission("helm:view")
def helm_dry_run():
    user = get_current_user()
    data, err, status = dry_run_release(user, _body())
    if err:
        return error_response(err, status)
    return success_response(data)


@helm_bp.route("/install", methods=["POST"])
@require_permission("helm:install")
def helm_install():
    user = get_current_user()
    body = _body()
    confirmation = body.get("confirmation") or ""
    data, err, status = install_or_upgrade_release(user, body, confirmation)
    if err:
        return error_response(err, status)

    chart_name, chart_version = _catalog_chart_metadata(body)
    create_or_update_from_helm(
        user,
        cluster_id=body.get("clusterId") or body.get("cluster"),
        namespace=body.get("namespace"),
        release_name=body.get("releaseName") or body.get("release_name"),
        chart_name=chart_name,
        chart_version=chart_version,
        owner_team=body.get("ownerTeam") or body.get("owner_team"),
        environment=body.get("environment"),
        criticality=body.get("criticality"),
        description=body.get("description"),
    )
    return success_response(data)


@helm_bp.route("/upgrade", methods=["POST"])
@require_permission("helm:upgrade")
def helm_upgrade():
    user = get_current_user()
    body = _body()
    body["isUpgrade"] = True
    confirmation = body.get("confirmation") or ""
    data, err, status = install_or_upgrade_release(user, body, confirmation)
    if err:
        return error_response(err, status)

    chart_name, chart_version = _catalog_chart_metadata(body)
    create_or_update_from_helm(
        user,
        cluster_id=body.get("clusterId") or body.get("cluster"),
        namespace=body.get("namespace"),
        release_name=body.get("releaseName") or body.get("release_name"),
        chart_name=chart_name,
        chart_version=chart_version,
        owner_team=body.get("ownerTeam") or body.get("owner_team"),
        environment=body.get("environment"),
        criticality=body.get("criticality"),
        description=body.get("description"),
    )
    return success_response(data)


@helm_bp.route("/rollback", methods=["POST"])
@require_permission("helm:rollback")
def helm_rollback():
    user = get_current_user()
    body = _body()
    cluster_id = body.get("clusterId") or body.get("cluster") or ""
    namespace = body.get("namespace") or ""
    release_name = body.get("releaseName") or body.get("release_name") or ""
    revision = body.get("revision")
    data, err, status = rollback_release(user, cluster_id, namespace, release_name, revision)
    if err:
        return error_response(err, status)
    return success_response(data)


@helm_bp.route("/uninstall", methods=["POST"])
@require_permission("helm:uninstall")
def helm_uninstall():
    user = get_current_user()
    body = _body()
    cluster_id = body.get("clusterId") or body.get("cluster") or ""
    namespace = body.get("namespace") or ""
    release_name = body.get("releaseName") or body.get("release_name") or ""
    data, err, status = uninstall_release(user, cluster_id, namespace, release_name)
    if err:
        return error_response(err, status)
    return success_response(data)


@helm_bp.route("/confirmation-phrase", methods=["POST"])
@require_permission("helm:install")
def helm_confirmation_phrase():
    body = _body()
    release_name = (body.get("releaseName") or body.get("release_name") or "").strip().lower()
    namespace = (body.get("namespace") or "").strip()
    is_upgrade = body.get("isUpgrade") or release_exists_from_payload(body)
    return success_response({
        "confirmation": expected_confirmation(release_name, namespace, is_upgrade),
        "isUpgrade": is_upgrade,
    })
