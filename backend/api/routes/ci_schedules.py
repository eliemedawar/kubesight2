"""Scheduled builds API: a service's schedules, a cron preview, and Run now.

Kept beside ``routes/ci.py`` rather than inside it, the way merge checks are:
the routes share the ``/api/ci`` prefix and the service lookup, and nothing
else.

Permissions compose by stacking ``@require_permission``: each decorator checks
its own key, so a route under two of them needs both. Saving a schedule needs
``ci_pipelines:edit`` (it is standing configuration of how the service builds)
AND ``ci_builds:run`` (it starts builds, every night, as the person who saved
it). Running one now is a build like any other and needs only ``ci_builds:run``.
"""

from __future__ import annotations

from flask import Blueprint, request

from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services.ci import catalog as catalog_service
from ..services.ci import schedules as schedules_service

ci_schedules_bp = Blueprint("ci_schedules", __name__, url_prefix="/api/ci")


def _payload() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@ci_schedules_bp.errorhandler(LookupError)
def _not_found(exc: LookupError):
    return error_response(str(exc) or "Not found.", 404)


@ci_schedules_bp.route("/services/<int:service_id>/schedules", methods=["GET"])
@require_permission("ci_builds:view")
def list_schedules(service_id: int):
    service = catalog_service.get_service(service_id)
    items = schedules_service.list_schedules(service)
    return success_response(
        {
            "items": items,
            "count": len(items),
            # What the form needs to render build inputs for any pipeline the
            # schedule may name, so it never has to ask twice.
            "pipelines": schedules_service.pipeline_choices(service),
            "defaultBranch": service.default_branch,
            "limits": {
                "maxSchedules": schedules_service.MAX_SCHEDULES_PER_SERVICE,
                "minIntervalMinutes": schedules_service.MIN_INTERVAL_MINUTES,
            },
        }
    )


@ci_schedules_bp.route("/schedules/preview", methods=["POST"])
@require_permission("ci_builds:view")
def preview_schedule():
    """The words and the next runs for a cron + timezone the form is holding.

    Always 200 with ``valid`` saying whether it parsed: an expression being
    typed is invalid most of the time, and that is not a request error.
    """
    payload = _payload()
    return success_response(
        schedules_service.preview(
            payload.get("cron"),
            payload.get("timezone"),
            count=payload.get("count") or schedules_service.PREVIEW_RUNS,
        )
    )


@ci_schedules_bp.route("/services/<int:service_id>/schedules", methods=["POST"])
@require_permission("ci_pipelines:edit")
@require_permission("ci_builds:run")
def create_schedule(service_id: int):
    service = catalog_service.get_service(service_id)
    try:
        data = schedules_service.create_schedule(service, _payload(), actor=get_current_user())
    except schedules_service.ScheduleError as exc:
        return error_response(str(exc), 400)
    return success_response(data, status_code=201)


@ci_schedules_bp.route("/services/<int:service_id>/schedules/<int:schedule_id>", methods=["PUT"])
@require_permission("ci_pipelines:edit")
@require_permission("ci_builds:run")
def update_schedule(service_id: int, schedule_id: int):
    service = catalog_service.get_service(service_id)
    row = schedules_service.get_schedule(service, schedule_id)
    try:
        data = schedules_service.update_schedule(row, _payload(), actor=get_current_user())
    except schedules_service.ScheduleError as exc:
        return error_response(str(exc), 400)
    return success_response(data)


@ci_schedules_bp.route("/services/<int:service_id>/schedules/<int:schedule_id>", methods=["DELETE"])
@require_permission("ci_pipelines:edit")
@require_permission("ci_builds:run")
def delete_schedule(service_id: int, schedule_id: int):
    service = catalog_service.get_service(service_id)
    row = schedules_service.get_schedule(service, schedule_id)
    schedules_service.delete_schedule(row, actor=get_current_user())
    return success_response({"deleted": True, "id": schedule_id})


@ci_schedules_bp.route("/services/<int:service_id>/schedules/<int:schedule_id>/run", methods=["POST"])
@require_permission("ci_builds:run")
def run_schedule_now(service_id: int, schedule_id: int):
    service = catalog_service.get_service(service_id)
    row = schedules_service.get_schedule(service, schedule_id)
    try:
        build = schedules_service.run_now(row, actor=get_current_user())
    except schedules_service.ScheduleError as exc:
        return error_response(str(exc), 409)
    return success_response(build, status_code=201)
