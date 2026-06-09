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
    expected_confirmation,
    _resolve_access,
)

helm_bp = Blueprint("helm", __name__, url_prefix="/api/helm")


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _cluster_id() -> str:
    return request.args.get("cluster") or request.args.get("clusterId") or ""


def _namespace() -> str:
    return request.args.get("namespace") or ""


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

    create_or_update_from_helm(
        user,
        cluster_id=body.get("clusterId") or body.get("cluster"),
        namespace=body.get("namespace"),
        release_name=body.get("releaseName") or body.get("release_name"),
        chart_name=body.get("chartName") or body.get("chart_name"),
        chart_version=body.get("chartVersion") or body.get("chart_version"),
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

    create_or_update_from_helm(
        user,
        cluster_id=body.get("clusterId") or body.get("cluster"),
        namespace=body.get("namespace"),
        release_name=body.get("releaseName") or body.get("release_name"),
        chart_name=body.get("chartName") or body.get("chart_name"),
        chart_version=body.get("chartVersion") or body.get("chart_version"),
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
