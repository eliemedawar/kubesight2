"""What an application *is*, as opposed to what it is doing.

Two different registries live here and they are easy to confuse, so the tool
names keep them apart:

* **Application Intelligence** (``kubesight_applications_*``) is the analysis
  side: a repository read by Hermes and turned into findings, dependencies,
  endpoints, a runtime topology and a security posture. It answers "what does
  this code do, and what is wrong with it".
* **Application services** (``kubesight_app_services_*``) is the operational
  side: a named service mapped onto real deployments and pods, with health
  rolled up per component. It answers "is this application up, and which
  component is not".

Where they touch is the topology: a service's components, the clients connected
to it and the transports between them. That graph is the thing worth reaching
for when a question spans more than one workload — "what talks to payments" is
not a question the cluster tools can answer, because Kubernetes does not record
it and KubeSight does.

Everything in this module reads. Creating an application, registering a service
or starting an analysis are one-click actions in the UI that cost real work
downstream — an analysis spawns a worker and reads a repository — and none of
them are questions, so none of them are here.
"""

from __future__ import annotations

from typing import Any, Dict

from ..protocol import ToolError
from .common import pick, take, unwrap
from .registry import MAX_ROWS, _limit, tool as _register


def tool(name, **kwargs):
    kwargs.setdefault("domain", "apps")
    return _register(name, **kwargs)


def _user():
    from ...auth_utils import get_current_user

    try:
        return get_current_user()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Application Intelligence
# ---------------------------------------------------------------------------

@tool(
    "kubesight_applications_list",
    permission="applications:view",
    description=(
        "Applications registered for source analysis, with their repository and "
        "the state of their latest analysis. This is the Application "
        "Intelligence catalog, not the CI catalog — for buildable services use "
        "kubesight_services_list."
    ),
    schema={
        "type": "object",
        "properties": {
            "page": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
    },
)
def _applications_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.application_intelligence_service import list_applications

    page = max(1, int(arguments.get("page") or 1))
    payload = list_applications(page, _limit(arguments, 25))
    return {
        "page": page,
        "total": payload.get("total"),
        "count": len(payload.get("items") or []),
        "applications": payload.get("items") or [],
    }


@tool(
    "kubesight_application_get",
    permission="applications:view",
    description=(
        "One analysed application in full, with its analysis history: what the "
        "code is built from, what it talks to, and what the last analysis found."
    ),
    schema={
        "type": "object",
        "properties": {"applicationId": {"type": "integer"}},
        "required": ["applicationId"],
    },
)
def _application_get(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.application_intelligence_service import application_to_dict, get_application

    try:
        row = get_application(int(arguments.get("applicationId") or 0))
    except (LookupError, TypeError, ValueError) as exc:
        raise ToolError(str(exc) or "Name the application by its id.")
    return application_to_dict(row, include_history=True)


@tool(
    "kubesight_application_analysis",
    permission="applications:view",
    description=(
        "One analysis run in full: findings with their evidence, the discovered "
        "endpoints and dependencies, the topology, and the security posture. "
        "Every finding carries the file and line it came from — quote those "
        "rather than the summary, because the summary is a count and the "
        "evidence is the argument."
    ),
    schema={
        "type": "object",
        "properties": {"analysisId": {"type": "integer"}},
        "required": ["analysisId"],
    },
)
def _application_analysis(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.application_intelligence_service import analysis_to_dict, get_analysis

    try:
        row = get_analysis(int(arguments.get("analysisId") or 0))
    except (LookupError, TypeError, ValueError) as exc:
        raise ToolError(str(exc) or "Name the analysis by its id.")
    return analysis_to_dict(row) or {}


@tool(
    "kubesight_application_analyses",
    permission="applications:view",
    description=(
        "An application's analysis runs, newest first, with status and when each "
        "finished. Take an analysisId from here for kubesight_application_analysis."
    ),
    schema={
        "type": "object",
        "properties": {
            "applicationId": {"type": "integer"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
        "required": ["applicationId"],
    },
)
def _application_analyses(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.application_intelligence_service import list_analyses

    try:
        payload = list_analyses(int(arguments.get("applicationId") or 0), 1, _limit(arguments, 25))
    except (LookupError, TypeError, ValueError) as exc:
        raise ToolError(str(exc) or "Name the application by its id.")
    return {
        "total": payload.get("total"),
        "count": len(payload.get("items") or []),
        "items": payload.get("items") or [],
    }


# ---------------------------------------------------------------------------
# Application services and who connects to them
# ---------------------------------------------------------------------------

_APP_SERVICE_FIELDS = (
    "id", "name", "description", "health", "clusterId", "namespace",
    "componentCount", "environment", "ownerTeam", "updatedAt",
)


@tool(
    "kubesight_app_services_list",
    permission="app_services:view",
    description=(
        "Application services and their rolled-up health — the operational view, "
        "where one service is several deployments and its health is the worst of "
        "them. Start here for 'is X up'."
    ),
    schema={
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS}},
    },
)
def _app_services_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.application_service_service import list_services

    payload = list_services(user=_user()) or {}
    rows = payload.get("items") or []
    total = len(rows)
    rows = [pick(row, _APP_SERVICE_FIELDS) for row in take(rows, _limit(arguments, MAX_ROWS))]
    return {"totalMatching": total, "count": len(rows), "services": rows}


@tool(
    "kubesight_app_service_get",
    permission="app_services:view",
    description=(
        "One application service with every component, each component's live "
        "deployment and pods, and its health. This is the tool that says WHICH "
        "part of a service is unhealthy rather than that the service is."
    ),
    schema={
        "type": "object",
        "properties": {"serviceId": {"type": "integer"}},
        "required": ["serviceId"],
    },
)
def _app_service_get(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.application_service_service import get_service

    service_id = arguments.get("serviceId")
    if service_id is None:
        raise ToolError("A serviceId is required — kubesight_app_services_list has them.")
    return unwrap(get_service(int(service_id), user=_user()), what="service") or {}


@tool(
    "kubesight_clients_list",
    permission="clients:view",
    description=(
        "The external clients registered against this platform and, for each, "
        "which application services they connect to and over what transport. "
        "The answer to 'who would notice if this service went down'."
    ),
    schema={
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS}},
    },
)
def _clients_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.client_service import list_clients

    payload = list_clients()
    rows = payload.get("items") if isinstance(payload, dict) else payload
    rows = rows or []
    total = len(rows)
    rows = take(rows, _limit(arguments, MAX_ROWS))
    return {"totalMatching": total, "count": len(rows), "clients": rows}


@tool(
    "kubesight_components_list",
    permission="components:view",
    description=(
        "Topology components — the pieces of infrastructure KubeSight tracks that "
        "are not Kubernetes workloads: databases, brokers, gateways, external "
        "endpoints. Each carries its last health check."
    ),
    schema={
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS}},
    },
)
def _components_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.topology_component_service import list_components

    payload = list_components()
    rows = payload.get("items") if isinstance(payload, dict) else payload
    rows = rows or []
    total = len(rows)
    rows = take(rows, _limit(arguments, MAX_ROWS))
    return {"totalMatching": total, "count": len(rows), "components": rows}
