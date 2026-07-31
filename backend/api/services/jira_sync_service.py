"""Jira ticketing integration — the Jira counterpart of :mod:`zoho_sync_service`.

Same three jobs, same contract:

* **Outbound** — publish the live-cluster option lists into the DevOps Request
  issue's custom fields, then wire the environment → application cascade.
* **Inbound** — resolve an issue Jira pushed to the webhook down to an exact
  deployment, and record it in :class:`~api.models.ZohoInboundTicket` (the
  provider-neutral intake log deploy automation reads).
* **Write-back** — report a finished run's outcome onto the issue.

Everything *below* the provider boundary is deliberately shared rather than
reimplemented: the option lists come from the same builders in
:mod:`zoho_sync_service` (they read Kubernetes, not Zoho), the binding engine in
:mod:`zoho_option_sources` synthesizes the same three locked bindings from this
row's identically-named columns, and the deploy surface comes from
:mod:`ticketing_targets`. A binding on the Jira Application field therefore
resolves to byte-identical values to the Zoho one — which is the point: both
providers are describing the same clusters.

Three places where Jira genuinely differs, and what is done about each:

``Options are a diff, not a replace``
    Zoho PATCHes the whole ``allowedValues`` array. Jira addresses options by id
    and refuses to delete one that is set on an existing issue, so
    ``jira_client.set_options`` reconciles and *disables* what fell out. Old
    issues keep rendering; new ones cannot pick a retired value.

``Cascade is one field, not a mapping``
    Jira has no dependency-mapping resource. A dependent dropdown is a single
    *cascading select* whose parent options carry children, so the cascade
    publishes the environment → application tree into ``cascade_field_id``. With
    no such field configured the two flat fields are published independently and
    the cascade is recorded as skipped — the honest outcome, not an error.

``Status is a transition``
    There is no "set status" in Jira. :func:`report_ticket_outcome` looks up the
    named transition on the issue's *current* available list; a transition that
    is not offered (someone already closed the issue by hand) is recorded, not
    raised.
"""

from __future__ import annotations

import hmac
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from ..db import db
from ..models import JiraIntegration, ZohoDeploymentSnapshot, ZohoInboundTicket
from ..secret_encryption import decrypt_secret, encrypt_secret
from . import jira_client
from . import ticketing_targets as targets
from . import zoho_sync_service as shared
from .jira_client import JiraConfig, JiraError

PROVIDER = "jira"

# The placeholder first entry Zoho requires. Jira has no such concept — an
# unselected single-select is simply empty — so the option lists the shared
# builders produce are stripped of it before publishing.
NONE_VALUE = shared.NONE_VALUE

# Outbound writes are serialized for the same reason as Zoho's: two concurrent
# syncs reconciling the same option list would race each other's diffs.
_sync_lock = threading.Lock()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _publishable(values: List[str]) -> List[str]:
    """Drop the ``-None-`` placeholder — Jira represents "unset" as no value."""
    return [v for v in values if v != NONE_VALUE]


# ---------------------------------------------------------------------------
# Single-row config: load / serialize / update
# ---------------------------------------------------------------------------

def get_or_create_config() -> JiraIntegration:
    row = JiraIntegration.query.get(1)
    if row is None:
        row = JiraIntegration(id=1)
        db.session.add(row)
        db.session.commit()
    return row


def serialize(row: JiraIntegration) -> Dict[str, Any]:
    """Public config view. Secrets are never returned — only whether they're set.

    Keys the Zoho config also has keep the Zoho spelling (``layoutId``,
    ``inboundSecretConfigured``, ``lastSync*``, …) so the shared settings form and
    the shared flow strip read one shape for both providers. ``layoutId`` is
    Jira's screen id, which is what a screen *is* here.
    """
    return {
        "provider": PROVIDER,
        "enabled": bool(row.enabled),
        "baseUrl": row.base_url or "",
        "deploymentType": row.deployment_type or "cloud",
        "email": row.email or "",
        "apiTokenConfigured": bool(row.api_token_encrypted),
        "projectKey": row.project_key or "",
        "issueTypeId": row.issue_type_id or "",
        # The screen is Jira's layout; the shared UI reads it under both names.
        "screenId": row.screen_id or "",
        "layoutId": row.screen_id or "",
        "appFieldId": row.app_field_id or "",
        "appFieldApiName": row.app_field_api_name or row.app_field_id or "",
        "environmentFieldId": row.environment_field_id or "",
        "environmentFieldApiName": (
            row.environment_field_api_name or row.environment_field_id or ""
        ),
        "tagFieldApiName": row.tag_field_api_name or "",
        "variableFieldId": row.variable_field_id or "",
        "variableFieldApiName": row.variable_field_api_name or row.variable_field_id or "",
        "valueFieldApiName": row.value_field_api_name or "",
        "inboundSecretConfigured": bool(row.inbound_secret_encrypted),
        "syncApplication": bool(row.sync_application),
        "syncEnvironment": bool(row.sync_environment),
        "syncVariables": bool(row.sync_variables),
        "syncIntervalMinutes": int(row.sync_interval_minutes or 30),
        # Live-cluster dropdown source — this provider's own selection.
        **targets.serialize(PROVIDER),
        "cascadeEnabled": bool(row.cascade_enabled),
        "cascadeFieldId": row.cascade_field_id or "",
        "cascadeFieldApiName": row.cascade_field_api_name or row.cascade_field_id or "",
        "lastDependencyStatus": row.last_dependency_status,
        "lastDependencyMessage": row.last_dependency_message,
        # Issue write-back. Named "transition*" because that is what they are —
        # the shared UI labels them differently per provider.
        "ticketWritebackEnabled": bool(row.ticket_writeback_enabled),
        "transitionStarted": row.transition_started or "",
        "transitionDeployed": row.transition_deployed or "",
        "transitionFailed": row.transition_failed or "",
        "transitionCancelled": row.transition_cancelled or "",
        "ticketOwnerEmail": row.ticket_owner_email or "",
        "lastSyncAt": _iso(row.last_sync_at),
        "lastSyncStatus": row.last_sync_status,
        "lastSyncMessage": row.last_sync_message,
        "lastSyncedCount": row.last_synced_count,
        "lastTestAt": _iso(row.last_test_at),
        "lastTestStatus": row.last_test_status,
        "lastTestMessage": row.last_test_message,
        "updatedAt": _iso(row.updated_at),
    }


def get_config_dict() -> Dict[str, Any]:
    return serialize(get_or_create_config())


def _normalize_base_url(value: Any) -> str:
    """Strip a pasted ``/rest/api/3`` suffix and any trailing slash.

    Operators copy the URL out of an API doc as often as out of the browser bar,
    and ``jira_client`` appends the REST root itself — a doubled suffix 404s with
    an error that says nothing about the real cause.
    """
    url = str(value or "").strip().rstrip("/")
    for suffix in ("/rest/api/3", "/rest/api/2", "/rest/api/latest", "/rest"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    return url


def update_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate + apply a config payload onto the single row. Raises ValueError."""
    row = get_or_create_config()
    errors: List[str] = []

    if "enabled" in payload:
        row.enabled = bool(payload.get("enabled"))

    if "baseUrl" in payload and payload.get("baseUrl") is not None:
        row.base_url = _normalize_base_url(payload.get("baseUrl"))

    if "deploymentType" in payload:
        wanted = str(payload.get("deploymentType") or "cloud").strip().lower()
        if wanted not in ("cloud", "server"):
            errors.append("Deployment type must be either 'cloud' or 'server'.")
        else:
            row.deployment_type = wanted

    for key, attr in (
        ("email", "email"),
        ("projectKey", "project_key"),
        ("issueTypeId", "issue_type_id"),
        ("screenId", "screen_id"),
        ("appFieldId", "app_field_id"),
        ("appFieldApiName", "app_field_api_name"),
        ("environmentFieldId", "environment_field_id"),
        ("environmentFieldApiName", "environment_field_api_name"),
        ("tagFieldApiName", "tag_field_api_name"),
        ("variableFieldId", "variable_field_id"),
        ("variableFieldApiName", "variable_field_api_name"),
        ("valueFieldApiName", "value_field_api_name"),
        ("cascadeFieldId", "cascade_field_id"),
        ("cascadeFieldApiName", "cascade_field_api_name"),
        ("transitionStarted", "transition_started"),
        ("transitionDeployed", "transition_deployed"),
        ("transitionFailed", "transition_failed"),
        ("transitionCancelled", "transition_cancelled"),
        ("ticketOwnerEmail", "ticket_owner_email"),
    ):
        if key in payload and payload.get(key) is not None:
            setattr(row, attr, str(payload.get(key)).strip())

    # A Jira custom field's id IS its webhook key, so an operator who filled only
    # the id has already given us the api name. Mirroring keeps the inbound
    # resolver reading one shape for both providers.
    for id_attr, name_attr in (
        ("app_field_id", "app_field_api_name"),
        ("environment_field_id", "environment_field_api_name"),
        ("variable_field_id", "variable_field_api_name"),
        ("cascade_field_id", "cascade_field_api_name"),
    ):
        if getattr(row, id_attr, "") and not str(getattr(row, name_attr, "") or "").strip():
            setattr(row, name_attr, getattr(row, id_attr))

    if "ticketWritebackEnabled" in payload:
        row.ticket_writeback_enabled = bool(payload.get("ticketWritebackEnabled"))
    if "syncApplication" in payload:
        row.sync_application = bool(payload.get("syncApplication"))
    if "syncEnvironment" in payload:
        row.sync_environment = bool(payload.get("syncEnvironment"))
    if "syncVariables" in payload:
        row.sync_variables = bool(payload.get("syncVariables"))
    if "cascadeEnabled" in payload:
        row.cascade_enabled = bool(payload.get("cascadeEnabled"))

    if "syncIntervalMinutes" in payload:
        try:
            row.sync_interval_minutes = max(1, int(payload.get("syncIntervalMinutes")))
        except (TypeError, ValueError):
            errors.append("Sync interval must be a whole number of minutes.")

    # The dropdown source is this provider's own row; staged, not committed, so
    # the rollback below undoes it too if validation fails.
    errors.extend(targets.apply_config_payload(PROVIDER, payload))

    _apply_secret(payload, "apiToken", "clearApiToken", row, "api_token_encrypted")
    _apply_secret(payload, "inboundSecret", "clearInboundSecret", row, "inbound_secret_encrypted")

    # If the integration is being turned on, the essentials must be present.
    if row.enabled:
        for label, value in (
            ("Base URL", row.base_url),
            ("Project key", row.project_key),
            ("Screen ID", row.screen_id),
            ("Application field ID", row.app_field_id),
        ):
            if not value:
                errors.append(f"{label} is required to enable the integration.")
        if not row.api_token_encrypted:
            errors.append("An API token is required to enable the integration.")
        if row.deployment_type != "server" and not row.email:
            errors.append("Jira Cloud needs the account email the API token belongs to.")

    if errors:
        db.session.rollback()
        raise ValueError(" ".join(errors))

    db.session.add(row)
    db.session.commit()
    return serialize(row)


def _apply_secret(
    payload: Dict[str, Any], value_key: str, clear_key: str, row: JiraIntegration, attr: str
) -> None:
    if payload.get(clear_key):
        setattr(row, attr, None)
        return
    value = payload.get(value_key)
    if value is not None and str(value).strip():
        setattr(row, attr, encrypt_secret(str(value).strip()))


def _to_client_config(row: JiraIntegration) -> JiraConfig:
    return JiraConfig(
        base_url=row.base_url or "",
        deployment_type=row.deployment_type or "cloud",
        email=row.email or "",
        api_token=decrypt_secret(row.api_token_encrypted or ""),
        project_key=row.project_key or "",
        screen_id=row.screen_id or "",
        app_field_id=row.app_field_id or "",
        environment_field_id=row.environment_field_id or "",
    )


def set_source(
    cluster_id: Optional[str],
    namespaces: Optional[List[str]],
    deployments: Optional[Dict[str, Any]] = None,
    custom_environments: Optional[List[Dict[str, Any]]] = None,
    job_overrides: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Persist this provider's dropdown source, then return its config view."""
    targets.set_source(
        PROVIDER, cluster_id, namespaces, deployments, custom_environments, job_overrides
    )
    return serialize(get_or_create_config())


# ---------------------------------------------------------------------------
# Preview — what a sync would publish, without touching Jira
# ---------------------------------------------------------------------------

def build_preview(fresh: bool = False) -> Dict[str, Any]:
    """The same live-source preview Zoho shows, read through the shared builders.

    The *builders* are shared — they read Kubernetes, not the ticketing system —
    but the source they read is this provider's own row, so both the config row
    and the provider key are handed over rather than defaulted to Zoho's.
    """
    row = get_or_create_config()
    preview = shared.build_preview(fresh=fresh, row=row, provider=PROVIDER)
    preview["provider"] = PROVIDER
    preview["appFieldId"] = row.app_field_id or ""
    preview["environmentFieldId"] = row.environment_field_id or ""
    preview["variableFieldId"] = row.variable_field_id or ""
    return preview


# ---------------------------------------------------------------------------
# Outbound sync + connection test
# ---------------------------------------------------------------------------

def _record_sync(row: JiraIntegration, status: str, message: str, count: Optional[int]) -> None:
    row.last_sync_at = datetime.now(timezone.utc)
    row.last_sync_status = status
    row.last_sync_message = message
    if count is not None:
        row.last_synced_count = count
    db.session.add(row)
    db.session.commit()


def _record_dependency(row: JiraIntegration, status: str, message: str) -> None:
    row.last_dependency_status = status
    row.last_dependency_message = message
    db.session.add(row)
    db.session.commit()


def _cascade_tree(
    resolved_parent, resolved_child
) -> List[Tuple[str, List[str]]]:
    """``[(environment, [application, ...])]`` in published order.

    Built through the same ``align_by_parent`` funnel Zoho's mapping uses, so a
    child list can never name a spelling that was not published — the tree is
    then just that mapping re-expressed as the nesting Jira wants.
    """
    from . import zoho_option_sources as sources

    mapping = sources.align_by_parent(
        resolved_child.by_parent, resolved_parent.canon, resolved_child.canon
    )
    return [
        (parent, list(children))
        for parent in _publishable(resolved_parent.values)
        for children in [mapping.get(parent) or []]
        if children
    ]


def _maybe_sync_cascade(
    row: JiraIntegration, cfg: JiraConfig, bindings: List[Any], resolved: Dict[str, Any]
) -> Dict[str, Any]:
    """Best-effort environment → application cascade. Never raises.

    Jira cannot make one field filter another, so the cascade is a *third* field:
    a cascading select carrying the whole tree. With no ``cascade_field_id``
    configured there is nothing to write and the outcome is "skipped" — the two
    flat dropdowns still published fine, the operator just does not get filtering.
    """
    if not row.cascade_enabled:
        _record_dependency(row, "skipped", "Cascade disabled in configuration.")
        return {"status": "skipped", "message": "Cascade disabled."}

    cascade_field = str(row.cascade_field_id or "").strip()
    if not cascade_field:
        msg = (
            "No cascade field configured. Jira cannot filter one field by another — "
            "point 'Cascade field' at a cascading-select custom field to publish the "
            "Environment → Application tree into it."
        )
        _record_dependency(row, "skipped", msg)
        return {"status": "skipped", "message": msg}

    parent_id = str(row.environment_field_id or "")
    child_id = str(row.app_field_id or "")
    if parent_id not in resolved or child_id not in resolved:
        msg = "Cascade needs both the Application and Environment fields published."
        _record_dependency(row, "skipped", msg)
        return {"status": "skipped", "message": msg}

    try:
        tree = _cascade_tree(resolved[parent_id], resolved[child_id])
        if not tree:
            msg = "Cascade not written — no environment has any application to offer."
            _record_dependency(row, "skipped", msg)
            return {"status": "skipped", "message": msg}
        result = jira_client.set_cascading_options(cfg, cascade_field, tree)
        message = (
            f"Cascade configured for {len(tree)} environment(s) "
            f"({result.created} added, {result.disabled} retired)."
        )
        _record_dependency(row, "ok", message)
        return {"status": "ok", "message": message}
    except JiraError as exc:
        hint = ""
        if getattr(exc, "status", None) in (401, 403):
            hint = " The API token's account needs Jira administrator rights to edit field options."
        msg = f"Cascade not applied: {exc}.{hint}"
        _record_dependency(row, "error", msg)
        return {"status": "error", "message": msg}
    except Exception as exc:  # a broken cascade must not fail an otherwise-good sync
        msg = f"Cascade not applied: {exc}."
        _record_dependency(row, "error", msg)
        return {"status": "error", "message": msg}


def sync_now() -> Dict[str, Any]:
    """Publish every bound field's option list, then wire the cascade.

    Structurally identical to :func:`zoho_sync_service.sync_now`: resolve the live
    source first (a bad cluster read is an error recorded *without* touching Jira,
    rather than one that empties the dropdowns), resolve each binding, publish,
    then layer the cascade on top. A stored binding that fails degrades to a
    warning — one operator-added dropdown must not stop the fields that drive
    production deploys — while a failure on the original three is fatal to the run.
    """
    from . import zoho_option_sources as sources

    with _sync_lock:
        row = get_or_create_config()
        if not row.enabled:
            raise ValueError("The Jira integration is disabled. Enable it before syncing.")
        cfg = _to_client_config(row)
        try:
            entries = shared._source_entries(row, provider=PROVIDER)
        except ValueError as exc:
            _record_sync(row, "error", str(exc), None)
            return {"status": "error", "message": str(exc), **serialize(row)}

        ctx = sources.SourceContext(row, entries, PROVIDER)
        bindings = sources.all_bindings(row, PROVIDER)
        publishing = [b for b in bindings if b.enabled]
        resolved: Dict[str, Any] = {}
        warnings: List[str] = []

        def _degrade(binding, action: str, exc: Exception) -> None:
            """Fatal for the original three, a warning for a stored binding."""
            message = f"{action} the {binding.label} field: {exc}"
            if binding.locked:
                raise ValueError(message)
            warnings.append(f"{message}.")
            sources.record_result(binding, "error", message)

        try:
            for binding in list(publishing):
                try:
                    resolved[binding.field_id] = sources.resolve(binding, ctx)
                except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                    _degrade(binding, "Resolving", exc)
                    publishing.remove(binding)

            parts: List[str] = []
            counts: Dict[str, int] = {}
            for binding in list(publishing):
                options = resolved[binding.field_id]
                try:
                    # Name the failing field — an option-context rejection is
                    # meaningless without knowing which list Jira refused.
                    jira_client.set_options(
                        cfg, binding.field_id, _publishable(options.values)
                    )
                except JiraError as exc:
                    _degrade(binding, "Publishing", exc)
                    publishing.remove(binding)
                    resolved.pop(binding.field_id, None)
                    continue
                counts[binding.field_id] = options.count
                parts.append(binding.publish_note.format(n=options.count, label=binding.label))
                sources.record_result(
                    binding, "ok", f"Published {options.count} value(s).", options.count
                )

            if parts:
                message = "Published " + "; ".join(parts) + " to Jira."
            else:
                message = "Nothing published — all field syncs are turned off."
            if warnings:
                message = f"{message} {' '.join(warnings)}"
            deployments = counts.get(str(row.app_field_id or ""), 0)
            _record_sync(row, "ok", message, deployments)
        except (JiraError, ValueError) as exc:
            _record_sync(row, "error", str(exc), None)
            return {"status": "error", "message": str(exc), **serialize(row)}

        cascade = _maybe_sync_cascade(row, cfg, bindings, resolved)
        return {
            "status": "ok",
            "message": message,
            "deployments": deployments,
            "namespaces": counts.get(str(row.environment_field_id or ""), 0),
            "variables": counts.get(str(row.variable_field_id or ""), 0),
            "count": deployments,
            "cascade": cascade,
            "warnings": warnings,
            **serialize(row),
        }


def test_connection() -> Dict[str, Any]:
    """Verify the credential and the configured scope, without writing.

    Two probes, because they fail for different reasons an operator fixes
    differently: ``/myself`` proves the token + site URL, and reading the project
    proves the project key. A failing project read after a working ``/myself`` is
    reported as a caveat rather than a hard failure — the credential is fine and
    the sync itself only needs the field-option permissions.
    """
    row = get_or_create_config()
    cfg = _to_client_config(row)
    try:
        me = jira_client.get_myself(cfg)
    except JiraError as exc:
        row.last_test_at = datetime.now(timezone.utc)
        row.last_test_status = "error"
        row.last_test_message = str(exc)
        db.session.add(row)
        db.session.commit()
        return {"status": "error", "message": str(exc), **serialize(row)}

    who = me.get("displayName") or me.get("emailAddress") or me.get("name") or "the API token"
    try:
        project = jira_client.get_project(cfg)
        message = f"Connected as {who}. Project '{project.get('name') or row.project_key}' is reachable."
    except JiraError as exc:
        message = (
            f"Connected as {who}. Could not read project '{row.project_key}' ({exc}); "
            "check the project key and the token account's project permissions."
        )
    row.last_test_at = datetime.now(timezone.utc)
    row.last_test_status = "ok"
    row.last_test_message = message
    db.session.add(row)
    db.session.commit()
    return {"status": "ok", "message": message, **serialize(row)}


def run_due_sync() -> bool:
    """Scheduler hook: sync if enabled and the interval has elapsed. True if it ran."""
    row = JiraIntegration.query.get(1)
    if not row or not row.enabled:
        return False
    interval = max(1, int(row.sync_interval_minutes or 30))
    if row.last_sync_at is not None:
        last = row.last_sync_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed_min = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
        if elapsed_min < interval:
            return False
    sync_now()
    return True


# ---------------------------------------------------------------------------
# Inbound webhook — verify, parse, resolve, record
# ---------------------------------------------------------------------------

def verify_inbound_secret(provided: Optional[str]) -> bool:
    """Constant-time compare of the caller's shared secret against the stored one.

    With no inbound secret configured the webhook is treated as open (dev/test);
    configure one for production.
    """
    row = JiraIntegration.query.get(1)
    stored = decrypt_secret(row.inbound_secret_encrypted or "") if row else ""
    if not stored:
        return True
    return bool(provided) and hmac.compare_digest(str(provided), stored)


def _field_value(raw: Any) -> Optional[str]:
    """Flatten one Jira field value to the plain string the resolver wants.

    Jira does not send scalars for structured fields: a single-select arrives as
    ``{"value": "..."}``, a cascading select as ``{"value": parent, "child":
    {"value": child}}``, a user as an object, and a plain text field as a bare
    string. Only the *own* value is returned here — see :func:`_cascade_parts`
    for the two halves of a cascading select.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        for key in ("value", "name", "key", "displayName"):
            if raw.get(key):
                return str(raw[key])
        return None
    if isinstance(raw, (list, tuple)):
        for item in raw:
            flattened = _field_value(item)
            if flattened:
                return flattened
        return None
    text = str(raw).strip()
    return text or None


def _cascade_parts(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(parent, child)`` out of a cascading-select value, else ``(None, None)``."""
    if not isinstance(raw, dict):
        return None, None
    parent = _field_value({"value": raw.get("value")}) if raw.get("value") else None
    child = _field_value(raw.get("child")) if raw.get("child") else None
    return parent, child


def _extract(payload: Dict[str, Any], *keys: str) -> Optional[Any]:
    """First present, non-empty value among ``keys``, across a Jira webhook body.

    Jira's webhook nests everything under ``issue.fields``, but the same handler
    also has to accept a hand-rolled Automation-rule body that posts the fields
    flat — so both shapes (and the raw issue object) are searched.
    """
    containers: List[Dict[str, Any]] = [payload]
    issue = payload.get("issue")
    if isinstance(issue, dict):
        containers.append(issue)
        fields = issue.get("fields")
        if isinstance(fields, dict):
            containers.append(fields)
    fields = payload.get("fields")
    if isinstance(fields, dict):
        containers.append(fields)
    for container in containers:
        for key in keys:
            if key in container and container[key] not in (None, "", {}, []):
                return container[key]
    return None


def resolve_inbound(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Parse the target deployment from a Jira issue, resolve, and record.

    Mirrors :func:`zoho_sync_service.resolve_inbound` — same resolution ladder
    (explicit id → namespace+deployment → unambiguous name-only), same
    request-shape validation, same intake table, so deploy automation cannot tell
    the two providers apart downstream. Idempotent per issue.

    The one Jira-specific step is up front: when a cascading select is configured
    it carries BOTH the environment and the application in one value, so it is
    read first and the flat fields only fill in what it did not supply.
    """
    row = get_or_create_config()
    app_field = row.app_field_api_name or row.app_field_id or ""
    env_field = row.environment_field_api_name or row.environment_field_id or ""
    tag_field = row.tag_field_api_name or ""
    variable_field = row.variable_field_api_name or row.variable_field_id or ""
    value_field = row.value_field_api_name or ""
    cascade_field = row.cascade_field_api_name or row.cascade_field_id or ""
    source_cluster = targets.source_cluster_id(PROVIDER) or ""

    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    ticket_id = _extract(payload, "key", "issueKey", "id", "issue_id") or issue.get("key")
    # Jira's human handle IS the key (KUB-123); the numeric id is the stable one.
    ticket_number = issue.get("key") or _extract(payload, "key", "issueKey")
    subject = _extract(payload, "summary", "subject", "title")

    dep_name: Optional[str] = None
    ns_name: Optional[str] = None
    raw_app_value: Optional[str] = None

    if cascade_field:
        parent, child = _cascade_parts(_extract(payload, cascade_field))
        if parent:
            ns_name = parent
        if child:
            dep_name = child
            raw_app_value = child

    if dep_name is None and app_field:
        raw_app_value = _field_value(_extract(payload, app_field, "application", "deployment"))
        dep_name = raw_app_value
    if ns_name is None and env_field:
        ns_name = _field_value(_extract(payload, env_field, "environment", "namespace"))

    def _read(configured: str, *fallbacks: str) -> Optional[str]:
        """Read one configured field, ignoring a blank config key."""
        keys = tuple(k for k in (configured,) + fallbacks if k)
        return _field_value(_extract(payload, *keys)) if keys else None

    tag = _read(tag_field, "tag", "version")
    variable = _read(variable_field, "variable")
    variable_value = _read(value_field, "variableValue")
    if variable is not None and str(variable).strip() in ("", NONE_VALUE):
        variable = None

    error: Optional[str] = None
    snapshot: Optional[ZohoDeploymentSnapshot] = None

    def _cluster_scoped(query):
        if source_cluster:
            # Custom (non-cluster) environments snapshot under the sentinel.
            return query.filter(
                ZohoDeploymentSnapshot.cluster_id.in_(
                    (source_cluster, shared.CUSTOM_SOURCE_CLUSTER)
                )
            )
        return query

    # 1) An explicit id field wins if present.
    explicit_id = _extract(payload, "snapshot_id", "workload_id", "deployment_id", "target_id")
    if explicit_id is not None:
        try:
            snapshot = ZohoDeploymentSnapshot.query.get(int(str(_field_value(explicit_id)).strip()))
        except (TypeError, ValueError):
            snapshot = None

    # 2) Resolve on (namespace, deployment) — the normal path.
    if snapshot is None and ns_name and dep_name:
        snapshot = (
            _cluster_scoped(
                ZohoDeploymentSnapshot.query.filter_by(
                    namespace=ns_name, deployment_name=dep_name
                )
            )
            .order_by(ZohoDeploymentSnapshot.id.desc())
            .first()
        )

    # 3) Name-only, but only when it's unambiguous across namespaces.
    if snapshot is None and dep_name and not ns_name:
        matches = _cluster_scoped(
            ZohoDeploymentSnapshot.query.filter_by(deployment_name=dep_name)
        ).all()
        if len(matches) == 1:
            snapshot = matches[0]
        elif len(matches) > 1:
            error = (
                f"'{dep_name}' exists in multiple namespaces — include the Environment "
                "field on the issue to resolve it."
            )

    snapshot_id: Optional[int] = snapshot.id if snapshot else None
    if snapshot is None and error is None:
        error = "Could not resolve the issue's Application value to a known deployment."

    existing = None
    if ticket_id is not None:
        existing = ZohoInboundTicket.query.filter_by(ticket_id=str(ticket_id)).first()
    record = existing or ZohoInboundTicket(provider=PROVIDER)
    record.provider = PROVIDER
    record.ticket_id = str(ticket_id) if ticket_id is not None else record.ticket_id
    record.ticket_number = str(ticket_number) if ticket_number is not None else None
    record.subject = str(subject) if subject is not None else None
    record.raw_app_value = str(raw_app_value) if raw_app_value is not None else None
    # app_service_id carries the resolved snapshot id; app_service_name its human
    # path "namespace / deployment" (schema shared with the Zoho intake).
    record.app_service_id = snapshot_id
    record.app_service_name = shared._snapshot_display(snapshot) if snapshot else None
    record.tag = str(tag) if tag is not None else None
    record.variable_name = str(variable).strip() if variable is not None else None
    record.variable_value = str(variable_value) if variable_value is not None else None
    record.resolved = snapshot is not None

    # An issue asks for exactly ONE change: an image tag OR a variable+value.
    has_tag = bool((record.tag or "").strip())
    has_variable = bool((record.variable_name or "").strip())
    has_value = bool((record.variable_value or "").strip())
    if error is None:
        if has_tag and has_variable:
            error = "The issue has both a Tag and a Variable — automation needs exactly one change."
        elif has_variable and not has_value:
            error = "The issue picks a Variable but has no Value to set it to."
        elif not has_tag and not has_variable:
            error = (
                "The issue carries no change — neither a Tag nor a Variable/Value arrived. "
                "If they were filled on the issue, the Jira webhook is not sending those "
                "custom fields; add them to the webhook's issue fields."
            )

    record.error = error
    record.payload = payload if isinstance(payload, dict) else {"raw": payload}
    record.received_at = datetime.now(timezone.utc)
    db.session.add(record)
    db.session.commit()

    # Deploy automation: a freshly-resolved issue carrying one valid change starts
    # its run immediately when auto-run is on. Never lets automation break the
    # webhook response (maybe_auto_run swallows every error).
    if record.resolved and error is None and (has_tag or (has_variable and has_value)):
        from .deploy_automation_service import maybe_auto_run

        maybe_auto_run(record.id)

    # The intake is the only place rows are created, so prune here — the log is
    # shared across providers and bounded as a whole.
    try:
        shared._prune_inbound_tickets()
    except Exception:  # pruning must never break the webhook response
        db.session.rollback()

    return {
        "resolved": record.resolved,
        "targetId": snapshot_id,
        "targetName": record.app_service_name,
        "deploymentName": snapshot.deployment_name if snapshot else None,
        "namespace": snapshot.namespace if snapshot else None,
        "clusterId": snapshot.cluster_id if snapshot else None,
        "tag": record.tag,
        "variableName": record.variable_name,
        "variableValue": record.variable_value,
        "error": error,
        "recordId": record.id,
    }


# ---------------------------------------------------------------------------
# Issue write-back — transition + assignee + comment
# ---------------------------------------------------------------------------

# Automation outcome → which configured transition name drives the move.
_OUTCOME_TRANSITION_ATTR = {
    "started": "transition_started",
    "deployed": "transition_deployed",
    "failed": "transition_failed",
    "cancelled": "transition_cancelled",
}


def report_ticket_outcome(
    ticket_id: Optional[str],
    outcome: str,
    *,
    comment: Optional[str] = None,
    resolution: Optional[str] = None,
) -> None:
    """Write a finished automation run's result back to its Jira issue.

    Runs the mapped transition, reassigns to the configured owner and posts a
    comment. No-op when write-back is disabled or the credential is missing;
    never raises.

    Jira has no separate "resolution" text field to PATCH the way Desk does, so
    the resolution sentence is appended to the comment instead of dropped — the
    operator still gets the full account on the issue.
    """
    if not ticket_id:
        return
    row = get_or_create_config()
    if not row.ticket_writeback_enabled:
        return
    if not (row.api_token_encrypted and row.base_url):
        return

    transition_name = getattr(row, _OUTCOME_TRANSITION_ATTR.get(outcome, ""), "") or None
    owner_email = (row.ticket_owner_email or "").strip()
    cfg = _to_client_config(row)
    issue_key = str(ticket_id)
    body = "\n\n".join(part for part in (comment, resolution) if part)

    def _work() -> None:
        if transition_name:
            try:
                moved = jira_client.transition_issue(cfg, issue_key, transition_name)
                if not moved:
                    logger.info(
                        "Jira transition %r is not available on %s right now; skipped.",
                        transition_name,
                        issue_key,
                    )
            except Exception:
                logger.warning("Jira issue transition failed (%s)", issue_key, exc_info=True)
        if owner_email:
            try:
                account_id = jira_client.resolve_account_id(cfg, owner_email)
                if account_id:
                    jira_client.assign_issue(cfg, issue_key, account_id)
            except Exception:
                logger.warning("Jira issue assignee update failed (%s)", issue_key, exc_info=True)
        if body:
            try:
                jira_client.add_comment(cfg, issue_key, body)
            except Exception:
                logger.warning("Jira issue comment failed (%s)", issue_key, exc_info=True)

    shared._dispatch_ticket_work(_work)


# --- Provider port: the parts that are genuinely shared -------------------
# The cluster/namespace/deployment pickers read Kubernetes, not a ticketing
# system, so they are re-exported rather than reimplemented — the routes call one
# name whichever provider is selected.
list_source_clusters = shared.list_source_clusters
list_source_namespaces = shared.list_source_namespaces
list_source_deployments = shared.list_source_deployments
preview_source_deployments = shared.preview_source_deployments
delete_inbound_ticket = shared.delete_inbound_ticket


def list_inbound_tickets(limit: int = 50) -> List[Dict[str, Any]]:
    """This provider's slice of the shared inbound-ticket log."""
    return shared.list_inbound_tickets(limit, provider=PROVIDER)


def post_ticket_comment(ticket_id: Optional[str], comment: str) -> None:
    """Post a standalone comment on an issue (used by the rollout watcher)."""
    if not (ticket_id and comment):
        return
    row = get_or_create_config()
    if not (row.ticket_writeback_enabled and row.api_token_encrypted and row.base_url):
        return
    cfg = _to_client_config(row)
    issue_key = str(ticket_id)

    def _work() -> None:
        try:
            jira_client.add_comment(cfg, issue_key, comment)
        except Exception:
            logger.warning("Jira issue comment failed (%s)", issue_key, exc_info=True)

    shared._dispatch_ticket_work(_work)
