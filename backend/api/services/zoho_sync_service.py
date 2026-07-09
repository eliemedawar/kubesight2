"""Zoho Desk DevOps Request field-sync — the integration's brain.

The dropdowns are driven by a live-cluster source the operator picks: a cluster
plus a chosen set of namespaces. This service:

* **Outbound** — publishes the selected namespaces into the Zoho Environment
  picklist, and the LIVE deployments running in those namespaces (read via
  kubectl through ``k8s_provider``) into the Application picklist as
  ``"{deployment} - {namespace}"``. It's a whole-list PATCH per field (field-sync
  spec §4), serialized behind a lock (§5.1). It then wires an
  Environment→Application dependency mapping (cascade) best-effort — degrading to
  the flat lists if the token can't write it. Each published deployment is also
  recorded in :class:`ZohoDeploymentSnapshot` (stable id per cluster/namespace/
  deployment) for the deploy-automation phase.

* **Inbound** — resolves a ticket Zoho pushed to the webhook. The Application
  value ``"{deployment} - {namespace}"`` resolves against the configured source
  cluster to the exact :class:`ZohoDeploymentSnapshot` — unique in Kubernetes, no
  fuzzy matching (an explicit id or a legacy trailing `` - id`` also resolves).
  Every delivery is recorded in :class:`ZohoInboundTicket`.

Secrets (client secret, refresh token, inbound webhook secret) are Fernet
encrypted at rest via ``secret_encryption`` — the same treatment registry/SMTP
secrets already get.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from ..db import db
from ..models import ZohoDeploymentSnapshot, ZohoInboundTicket, ZohoIntegration
from ..secret_encryption import decrypt_secret, encrypt_secret
from . import zoho_client
from .zoho_client import ZohoConfig, ZohoError

# The placeholder Zoho keeps as the default/blank picklist entry — always first.
NONE_VALUE = "-None-"
# How the snapshot id is embedded in / parsed out of a picklist value. We anchor
# to the END of the string so digits inside a deployment/namespace name can't be
# mistaken for the id marker.
_ID_SUFFIX_RE = re.compile(r"(\d+)\s*$")
# Zoho picklist values reject special characters (spec §5.4 — "same restriction
# as status names"; the verified-working values were plain: processing-ms, UAT).
# A '/' or '#' triggers a 422, so keep only letters/digits/space/underscore/dot/
# hyphen and collapse anything else to a space.
_UNSAFE_VALUE_RE = re.compile(r"[^A-Za-z0-9 _.\-]+")
# Zoho picklist value-name limit (spec §5.4: keep names <= ~120 chars).
_MAX_VALUE_LEN = 120

# Serialize outbound writes: every PATCH replaces the whole array, so two
# concurrent syncs would clobber each other (spec guardrail §5.1).
_sync_lock = threading.Lock()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# Single-row config: load / serialize / update
# ---------------------------------------------------------------------------

def get_or_create_config() -> ZohoIntegration:
    row = ZohoIntegration.query.get(1)
    if row is None:
        row = ZohoIntegration(id=1)
        db.session.add(row)
        db.session.commit()
    return row


def serialize(row: ZohoIntegration) -> Dict[str, Any]:
    """Public config view. Secrets are never returned — only whether they're set."""
    return {
        "enabled": bool(row.enabled),
        "apiBase": row.api_base or "",
        "accountsBase": row.accounts_base or "",
        "tokenEndpoint": row.token_endpoint or "",
        "orgId": row.org_id or "",
        "departmentId": row.department_id or "",
        "layoutId": row.layout_id or "",
        "appFieldId": row.app_field_id or "",
        "appFieldApiName": row.app_field_api_name or "cf_application",
        "environmentFieldId": row.environment_field_id or "",
        "environmentFieldApiName": row.environment_field_api_name or "cf_environment",
        "tagFieldApiName": row.tag_field_api_name or "cf_tag",
        "clientId": row.client_id or "",
        "clientSecretConfigured": bool(row.client_secret_encrypted),
        "refreshTokenConfigured": bool(row.refresh_token_encrypted),
        "inboundSecretConfigured": bool(row.inbound_secret_encrypted),
        "syncApplication": bool(row.sync_application),
        "syncEnvironment": bool(row.sync_environment),
        "statusFilter": _status_list(row),
        "syncIntervalMinutes": int(row.sync_interval_minutes or 30),
        # Live-cluster dropdown source + cascade.
        "sourceClusterId": row.source_cluster_id or "",
        "selectedNamespaces": _namespace_list(row),
        "selectedDeployments": _deployment_selection(row),
        "cascadeEnabled": bool(row.cascade_enabled),
        "dependencyMappingId": row.dependency_mapping_id or "",
        "lastDependencyStatus": row.last_dependency_status,
        "lastDependencyMessage": row.last_dependency_message,
        # Ticket write-back.
        "ticketWritebackEnabled": bool(row.ticket_writeback_enabled),
        "ticketStatusStarted": row.ticket_status_started or "Open",
        "ticketStatusDeployed": row.ticket_status_deployed or "Closed",
        "ticketStatusFailed": row.ticket_status_failed or "Failed",
        "ticketStatusCancelled": row.ticket_status_cancelled or "Canceled",
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


def _status_list(row: ZohoIntegration) -> List[str]:
    return [s.strip() for s in (row.status_filter or "").split(",") if s.strip()]


def _namespace_list(row: ZohoIntegration) -> List[str]:
    """Decode the operator's chosen namespaces (stored JSON-encoded, order-stable)."""
    raw = row.selected_namespaces
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        # Tolerate a legacy comma-separated value.
        parsed = [p.strip() for p in str(raw).split(",")]
    return [str(n).strip() for n in parsed if str(n).strip()] if isinstance(parsed, list) else []


def _deployment_selection(row: ZohoIntegration) -> Dict[str, Any]:
    """Decode the per-namespace deployment selection.

    Shape: ``{namespace: {"all": bool, "names": [str]}}``. A namespace absent from
    the map publishes ALL its live deployments (dynamic default).
    """
    raw = row.selected_deployments
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_deployment_selection(value: Any) -> Dict[str, Any]:
    """Coerce an incoming selection map into ``{ns: {"all": bool, "names": [str]}}``."""
    out: Dict[str, Any] = {}
    if not isinstance(value, dict):
        return out
    for ns, sel in value.items():
        ns_name = str(ns).strip()
        if not ns_name:
            continue
        # Tolerant of a few shapes: a bare list = explicit names; a dict = full form.
        if isinstance(sel, list):
            names = [str(n).strip() for n in sel if str(n).strip()]
            out[ns_name] = {"all": False, "names": names}
        elif isinstance(sel, dict):
            is_all = bool(sel.get("all", True))
            names = [str(n).strip() for n in (sel.get("names") or []) if str(n).strip()]
            out[ns_name] = {"all": is_all} if is_all else {"all": False, "names": names}
        elif sel in (None, "all", "*"):
            out[ns_name] = {"all": True}
    return out


def set_source(
    cluster_id: Optional[str],
    namespaces: Optional[List[str]],
    deployments: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist the dropdown source: a cluster, the namespaces, and (optionally) the
    specific deployments to publish per namespace.

    The Environment picklist becomes exactly these namespaces; the Application
    picklist becomes the live deployments running in them (resolved at sync time),
    filtered by ``deployments`` — a namespace left unspecified publishes all of its
    deployments dynamically.
    """
    row = get_or_create_config()
    row.source_cluster_id = (str(cluster_id).strip() if cluster_id else None) or None
    clean: List[str] = []
    seen = set()
    for ns in namespaces or []:
        name = str(ns).strip()
        if name and name not in seen:
            seen.add(name)
            clean.append(name)
    row.selected_namespaces = json.dumps(clean)
    if deployments is not None:
        # Keep only selections for namespaces still chosen.
        normalized = {
            ns: sel for ns, sel in _normalize_deployment_selection(deployments).items() if ns in seen
        }
        row.selected_deployments = json.dumps(normalized)
    db.session.add(row)
    db.session.commit()
    return serialize(row)


def update_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate + apply a config payload onto the single row. Raises ValueError."""
    row = get_or_create_config()
    errors: List[str] = []

    if "enabled" in payload:
        row.enabled = bool(payload.get("enabled"))

    # Plain (non-secret) fields — only overwrite when the key is present.
    for key, attr in (
        ("apiBase", "api_base"),
        ("accountsBase", "accounts_base"),
        ("tokenEndpoint", "token_endpoint"),
        ("orgId", "org_id"),
        ("departmentId", "department_id"),
        ("layoutId", "layout_id"),
        ("appFieldId", "app_field_id"),
        ("appFieldApiName", "app_field_api_name"),
        ("environmentFieldId", "environment_field_id"),
        ("environmentFieldApiName", "environment_field_api_name"),
        ("tagFieldApiName", "tag_field_api_name"),
        ("clientId", "client_id"),
        ("ticketStatusStarted", "ticket_status_started"),
        ("ticketStatusDeployed", "ticket_status_deployed"),
        ("ticketStatusFailed", "ticket_status_failed"),
        ("ticketStatusCancelled", "ticket_status_cancelled"),
        ("ticketOwnerEmail", "ticket_owner_email"),
    ):
        if key in payload and payload.get(key) is not None:
            setattr(row, attr, str(payload.get(key)).strip())

    if "ticketWritebackEnabled" in payload:
        row.ticket_writeback_enabled = bool(payload.get("ticketWritebackEnabled"))

    if "statusFilter" in payload:
        value = payload.get("statusFilter")
        if isinstance(value, (list, tuple)):
            parts = [str(v).strip().lower() for v in value]
        else:
            parts = [p.strip().lower() for p in str(value or "").split(",")]
        row.status_filter = ",".join([p for p in parts if p]) or "active,degraded"

    if "syncIntervalMinutes" in payload:
        try:
            row.sync_interval_minutes = max(1, int(payload.get("syncIntervalMinutes")))
        except (TypeError, ValueError):
            errors.append("Sync interval must be a whole number of minutes.")

    if "syncApplication" in payload:
        row.sync_application = bool(payload.get("syncApplication"))
    if "syncEnvironment" in payload:
        row.sync_environment = bool(payload.get("syncEnvironment"))
    if "cascadeEnabled" in payload:
        row.cascade_enabled = bool(payload.get("cascadeEnabled"))

    # Dropdown source may also be edited from the main config form.
    if "sourceClusterId" in payload:
        value = payload.get("sourceClusterId")
        row.source_cluster_id = (str(value).strip() if value else None) or None
    if "selectedNamespaces" in payload:
        value = payload.get("selectedNamespaces")
        if isinstance(value, (list, tuple)):
            names = [str(v).strip() for v in value if str(v).strip()]
        else:
            names = [p.strip() for p in str(value or "").split(",") if p.strip()]
        row.selected_namespaces = json.dumps(names)
    if "selectedDeployments" in payload:
        row.selected_deployments = json.dumps(
            _normalize_deployment_selection(payload.get("selectedDeployments"))
        )

    # Secrets: set only when a non-empty value is supplied; explicit clear flags wipe.
    _apply_secret(payload, "clientSecret", "clearClientSecret", row, "client_secret_encrypted")
    _apply_secret(payload, "refreshToken", "clearRefreshToken", row, "refresh_token_encrypted")
    _apply_secret(payload, "inboundSecret", "clearInboundSecret", row, "inbound_secret_encrypted")

    # If the integration is being turned on, the essentials must be present.
    if row.enabled:
        for label, value in (
            ("Org ID", row.org_id),
            ("Layout ID", row.layout_id),
            ("Application field ID", row.app_field_id),
            ("Client ID", row.client_id),
        ):
            if not value:
                errors.append(f"{label} is required to enable the integration.")
        if not row.client_secret_encrypted:
            errors.append("Client secret is required to enable the integration.")
        if not row.refresh_token_encrypted:
            errors.append("Refresh token is required to enable the integration.")

    if errors:
        db.session.rollback()
        raise ValueError(" ".join(errors))

    db.session.add(row)
    db.session.commit()
    return serialize(row)


def _apply_secret(
    payload: Dict[str, Any], value_key: str, clear_key: str, row: ZohoIntegration, attr: str
) -> None:
    if payload.get(clear_key):
        setattr(row, attr, None)
        return
    value = payload.get(value_key)
    if value is not None and str(value).strip():
        setattr(row, attr, encrypt_secret(str(value).strip()))


def _to_client_config(row: ZohoIntegration) -> ZohoConfig:
    return ZohoConfig(
        api_base=row.api_base or "https://desk.zoho.com/api/v1",
        accounts_base=row.accounts_base or "https://accounts.zoho.com",
        token_endpoint=row.token_endpoint or "https://accounts.zoho.com/oauth/v2/token",
        org_id=row.org_id or "",
        layout_id=row.layout_id or "",
        app_field_id=row.app_field_id or "",
        environment_field_id=row.environment_field_id or "",
        client_id=row.client_id or "",
        client_secret=decrypt_secret(row.client_secret_encrypted or ""),
        refresh_token=decrypt_secret(row.refresh_token_encrypted or ""),
    )


# ---------------------------------------------------------------------------
# Dropdown source — a live cluster + a chosen set of namespaces:
#   * Environment field -> the operator's selected namespaces.
#   * Application field  -> the LIVE deployments running in those namespaces, shown
#     as the bare deployment name (e.g. "aims-ui"). Values are DE-DUPLICATED across
#     namespaces (Zoho picklists require unique values, and the same app usually
#     runs in several namespaces). An inbound ticket therefore resolves via the
#     Environment field (namespace) + the Application name -> (cluster, namespace,
#     deployment). The namespace is the parent value the Application<-Environment
#     cascade maps on, so picking an Environment narrows the Application list.
# ---------------------------------------------------------------------------

# Legacy separator: older published values were "{deployment} - {namespace}".
# K8s names are DNS-1123 (no spaces), so " - " never occurs inside a name — used
# only to parse those older values on inbound.
_LABEL_SEP = " - "


def _sanitize_value(text: str) -> str:
    """Reduce a fragment to Zoho-safe picklist characters (letters/digits/space/-/_/.)."""
    cleaned = _UNSAFE_VALUE_RE.sub(" ", str(text or ""))
    return re.sub(r"\s+", " ", cleaned).strip()


def compose_deployment_label(namespace: str, name: str, snapshot_id: Optional[int] = None) -> str:
    """Application-dropdown value: just the bare deployment name (e.g. ``aims-ui``).

    Zoho-safe chars only, truncated to Zoho's value limit. The namespace is NOT
    included (the operator wants clean names); uniqueness across namespaces is
    handled by de-duplication when the list is built, and inbound resolution uses
    the ticket's Environment (namespace) field alongside this name. ``namespace`` /
    ``snapshot_id`` are accepted for signature stability but not embedded.
    """
    dep = _sanitize_value(name) or (f"workload-{snapshot_id}" if snapshot_id else "workload")
    return dep[:_MAX_VALUE_LEN].rstrip()


def parse_label(value: str) -> tuple:
    """Recover ``(deployment, namespace)`` from a picklist value (rsplit on `` - ``)."""
    if not value:
        return None, None
    s = str(value).strip()
    if _LABEL_SEP not in s:
        return None, None
    dep, ns = s.rsplit(_LABEL_SEP, 1)
    return (dep.strip() or None), (ns.strip() or None)


def parse_workload_id(value: str) -> Optional[int]:
    """Extract the snapshot id embedded in a picklist value (trailing `` - id``)."""
    if not value:
        return None
    match = _ID_SUFFIX_RE.search(str(value).strip())
    return int(match.group(1)) if match else None


# --- Live cluster reads (real kubectl, with a mock-data fallback for dev) -----

def list_source_clusters() -> List[Dict[str, str]]:
    """Clusters the operator may pick as the dropdown source (``[{id, name}]``)."""
    from ..k8s_provider import (
        K8sCommandError,
        list_clusters_from_k8s,
        should_use_real_k8s,
    )

    items: List[Dict[str, Any]] = []
    if should_use_real_k8s():
        try:
            items = list(list_clusters_from_k8s().get("items", []))
        except K8sCommandError:
            items = []
    else:
        from ..mock_data import CLUSTERS

        items = list(CLUSTERS)

    # Always fold in custom (DB-registered) clusters, even if discovery failed.
    try:
        from ..k8s_provider import _custom_clusters_as_items

        seen = {str(c.get("id")) for c in items}
        for c in _custom_clusters_as_items():
            if str(c.get("id")) not in seen:
                items.append(c)
    except Exception:
        pass

    return [
        {"id": str(c.get("id")), "name": c.get("name") or str(c.get("id"))}
        for c in items
        if c.get("id")
    ]


def preview_source_deployments(cluster_id: str, namespaces: List[str]) -> Dict[str, Any]:
    """Live deployments per namespace for the picker — read-only, no snapshot/DB write."""
    per_ns = _list_deployments_by_namespace(cluster_id, list(namespaces or []))
    groups = []
    total = 0
    for ns in namespaces or []:
        deps = per_ns.get(ns, [])
        total += len(deps)
        groups.append({"namespace": ns, "deployments": deps})
    return {"clusterId": cluster_id, "groups": groups, "count": total}


def list_source_namespaces(cluster_id: str) -> List[str]:
    """Namespace names available in the source cluster (live or, in dev, mock)."""
    cluster_id = (cluster_id or "").strip()
    if not cluster_id:
        return []
    from ..k8s_provider import (
        K8sCommandError,
        list_namespace_names_from_k8s,
        resolve_cluster_access,
        should_use_real_k8s,
    )

    if should_use_real_k8s(cluster_id):
        access = resolve_cluster_access(cluster_id)
        if not access:
            raise ValueError(f"Cluster '{cluster_id}' was not found.")
        try:
            data = list_namespace_names_from_k8s(access)
        except K8sCommandError as exc:
            raise ValueError(f"Could not read namespaces from cluster '{cluster_id}': {exc}")
        return [ns.get("name") for ns in data.get("items", []) if ns.get("name")]

    from ..mock_data import NAMESPACES

    return [ns.get("name") for ns in NAMESPACES.get(cluster_id, []) if ns.get("name")]


def list_source_deployments(cluster_id: str, namespace: str) -> List[str]:
    """Deployment names in one namespace of the source cluster (live or mock)."""
    if not namespace:
        return []
    return _list_deployments_by_namespace(cluster_id, [namespace]).get(namespace, [])


# Each namespace read is its own kubectl subprocess (~0.5-3s against a remote
# cluster on a cold cache), so a serial loop makes the preview/sync cost scale
# with the namespace count. Bounded fan-out keeps wall-clock at ~one call.
_SOURCE_READ_WORKERS = 6


def _list_deployments_by_namespace(
    cluster_id: str, namespaces: List[str], fresh: bool = False
) -> Dict[str, List[str]]:
    """``{namespace: [deployment names]}`` for the source cluster, read concurrently.

    Cluster access is resolved once on the request thread (it may hit the DB,
    which needs the Flask app context); only the raw per-namespace reads — pure
    kubectl subprocesses — fan out to worker threads. Raises ValueError on an
    unknown cluster or a failed read, matching the old per-namespace behavior.
    ``fresh=True`` (manual refresh) bypasses the k8s read cache.
    """
    cluster_id = (cluster_id or "").strip()
    if not cluster_id or not namespaces:
        return {}
    from ..k8s_provider import (
        K8sCommandError,
        namespace_resource_list_from_k8s,
        resolve_cluster_access,
        should_use_real_k8s,
    )

    if not should_use_real_k8s(cluster_id):
        from ..mock_data import NAMESPACE_RESOURCES

        out: Dict[str, List[str]] = {}
        for ns in namespaces:
            ns_res = (NAMESPACE_RESOURCES.get(cluster_id) or {}).get(ns) or {}
            out[ns] = [d.get("name") for d in ns_res.get("deployments", []) if d.get("name")]
        return out

    access = resolve_cluster_access(cluster_id)
    if not access:
        raise ValueError(f"Cluster '{cluster_id}' was not found.")

    def _fetch(ns: str) -> List[str]:
        try:
            data = namespace_resource_list_from_k8s(access, ns, "deployments", fresh=fresh)
        except K8sCommandError as exc:
            raise ValueError(f"Could not read deployments in '{ns}': {exc}")
        # The single-list reader keys items under the resource name, not "items".
        return [d.get("name") for d in data.get("deployments", []) if d.get("name")]

    if len(namespaces) == 1:
        return {namespaces[0]: _fetch(namespaces[0])}
    with ThreadPoolExecutor(max_workers=min(_SOURCE_READ_WORKERS, len(namespaces))) as pool:
        return dict(zip(namespaces, pool.map(_fetch, namespaces)))


def _get_or_create_snapshot(cluster_id: str, namespace: str, name: str) -> ZohoDeploymentSnapshot:
    """Stable id for a (cluster, namespace, deployment) tuple — reused across syncs."""
    snap = ZohoDeploymentSnapshot.query.filter_by(
        cluster_id=cluster_id, namespace=namespace, deployment_name=name
    ).first()
    now = datetime.now(timezone.utc)
    if snap is None:
        snap = ZohoDeploymentSnapshot(
            cluster_id=cluster_id,
            namespace=namespace,
            deployment_name=name,
            first_seen_at=now,
            last_seen_at=now,
        )
        db.session.add(snap)
        db.session.flush()  # assign the id we embed in the label
    else:
        snap.last_seen_at = now
    return snap


def _source_entries(row: ZohoIntegration, fresh: bool = False) -> List[Dict[str, Any]]:
    """The live deployments to publish: [{id, namespace, name, label}], snapshotted.

    Raises ValueError when no cluster is configured or a cluster read fails, so the
    caller (sync/preview) can surface a clear message instead of publishing nothing.
    """
    cluster_id = (row.source_cluster_id or "").strip()
    if not cluster_id:
        raise ValueError("No source cluster selected. Pick a cluster and namespaces first.")
    selection = _deployment_selection(row)
    namespaces = _namespace_list(row)
    # All cluster reads fan out up front; the DB snapshot work below stays on the
    # request thread (it needs the app-context session).
    per_ns = _list_deployments_by_namespace(cluster_id, namespaces, fresh=fresh)
    entries: List[Dict[str, Any]] = []
    # One entry per (namespace, deployment) — NOT de-duplicated here, because the
    # same app name can live in several namespaces and each pairing is needed for
    # the cascade + snapshot resolution. The Application picklist itself is
    # de-duplicated later (see _application_values).
    for ns in namespaces:
        ns_sel = selection.get(ns)
        # No entry for the namespace => publish all (dynamic default).
        explicit_names = None
        if isinstance(ns_sel, dict) and not ns_sel.get("all", True):
            explicit_names = set(ns_sel.get("names") or [])
        for name in per_ns.get(ns, []):
            if explicit_names is not None and name not in explicit_names:
                continue
            snap = _get_or_create_snapshot(cluster_id, ns, name)
            label = compose_deployment_label(ns, name, snap.id)
            entries.append({"id": snap.id, "namespace": ns, "name": name, "label": label})
    db.session.commit()
    return entries


def build_environment_values(row: ZohoIntegration) -> List[str]:
    """Environment dropdown: ``-None-`` + the operator's selected namespaces."""
    values = [NONE_VALUE]
    seen = {NONE_VALUE}
    for ns in _namespace_list(row):
        s = _sanitize_value(ns)
        if s and s not in seen:
            seen.add(s)
            values.append(s)
    return values


def _application_values(entries: List[Dict[str, Any]]) -> List[str]:
    """Application dropdown: ``-None-`` + unique deployment names (order-stable)."""
    values = [NONE_VALUE]
    seen = {NONE_VALUE}
    for e in entries:
        if e["label"] not in seen:
            seen.add(e["label"])
            values.append(e["label"])
    return values


def build_application_values(row: ZohoIntegration) -> List[str]:
    """Application dropdown: ``-None-`` + unique deployment names."""
    return _application_values(_source_entries(row))


def _namespace_to_labels(entries: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Group Application names by their (sanitized) namespace — the cascade map.

    De-dupes within each namespace so the child list per parent value is unique.
    """
    grouped: Dict[str, List[str]] = {}
    seen_per_ns: Dict[str, set] = {}
    for e in entries:
        key = _sanitize_value(e["namespace"])
        s = seen_per_ns.setdefault(key, set())
        if e["label"] not in s:
            s.add(e["label"])
            grouped.setdefault(key, []).append(e["label"])
    return grouped


def build_preview(fresh: bool = False) -> Dict[str, Any]:
    """What the next sync would publish (both dropdowns) — for the UI, no Zoho write."""
    row = get_or_create_config()
    env_values = build_environment_values(row)
    error: Optional[str] = None
    entries: List[Dict[str, Any]] = []
    try:
        entries = _source_entries(row, fresh=fresh)
    except ValueError as exc:
        error = str(exc)
    items = [
        {
            "id": e["id"],
            "label": e["label"],
            "deploymentName": e["name"],
            "namespace": e["namespace"],
            "clusterId": row.source_cluster_id or "",
        }
        for e in entries
    ]
    app_values = _application_values(entries)
    return {
        "sourceClusterId": row.source_cluster_id or "",
        "selectedNamespaces": _namespace_list(row),
        "selectedDeployments": _deployment_selection(row),
        "count": max(0, len(app_values) - 1),
        "manageApplication": bool(row.sync_application and row.app_field_id),
        "manageEnvironment": bool(row.sync_environment and row.environment_field_id),
        "cascadeEnabled": bool(row.cascade_enabled),
        "applicationValues": app_values,
        "environmentValues": env_values,
        "namespaces": [v for v in env_values if v != NONE_VALUE],
        "items": items,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Outbound sync (read-modify-write) + connection test
# ---------------------------------------------------------------------------

def _record_sync(row: ZohoIntegration, status: str, message: str, count: Optional[int]) -> None:
    row.last_sync_at = datetime.now(timezone.utc)
    row.last_sync_status = status
    row.last_sync_message = message
    if count is not None:
        row.last_synced_count = count
    db.session.add(row)
    db.session.commit()


def _record_dependency(row: ZohoIntegration, status: str, message: str) -> None:
    row.last_dependency_status = status
    row.last_dependency_message = message
    db.session.add(row)
    db.session.commit()


def _sync_dependency_mapping(
    cfg: ZohoConfig, row: ZohoIntegration, grouped: Dict[str, List[str]]
) -> Dict[str, Any]:
    """Create/replace the Environment(namespace) -> Application(deployments) mapping.

    Verified Zoho Desk schema (2026-07-07): ``POST /dependencyMappings`` with
    ``{layoutId, parentId, childId, mappings:{parentValue:[childValues]}}`` — the
    ``mappings`` value is an OBJECT keyed by namespace (an array yields HTTP 415).
    Create-or-replace: delete any existing mapping for this field pair (values go
    stale as deployments change), then POST fresh. May raise ZohoError (e.g. 403
    without Desk.settings.CREATE) — the caller degrades softly.
    """
    mappings = {ns: labels for ns, labels in grouped.items() if ns and labels}
    if not mappings:
        return {"message": "No namespace/deployment pairs to map."}

    # Remove any prior mapping(s) for this parent/child pair so we replace cleanly.
    try:
        existing = zoho_client.list_dependency_mappings(cfg)
        for m in existing.get("data") or []:
            if str(m.get("parentId")) == str(row.environment_field_id) and str(
                m.get("childId")
            ) == str(row.app_field_id):
                mid = m.get("id")
                if mid:
                    zoho_client.delete_dependency_mapping(cfg, str(mid))
    except ZohoError:
        pass  # best-effort cleanup; the create below is what matters

    body = {
        "layoutId": cfg.layout_id,
        "parentId": str(row.environment_field_id),
        "childId": str(row.app_field_id),
        "mappings": mappings,
    }
    created = zoho_client.create_dependency_mapping(cfg, body)
    new_id = created.get("id")
    if new_id:
        row.dependency_mapping_id = str(new_id)
    return {"message": f"Cascade configured for {len(mappings)} namespace(s)."}


def _maybe_sync_cascade(
    row: ZohoIntegration, cfg: ZohoConfig, entries: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Best-effort cascade sync. Never raises — records the outcome + returns a dict."""
    if not row.cascade_enabled:
        _record_dependency(row, "skipped", "Cascade disabled in configuration.")
        return {"status": "skipped", "message": "Cascade disabled."}
    if not (row.sync_application and row.app_field_id and row.sync_environment and row.environment_field_id):
        msg = "Cascade needs both the Application and Environment fields published."
        _record_dependency(row, "skipped", msg)
        return {"status": "skipped", "message": msg}
    try:
        result = _sync_dependency_mapping(cfg, row, _namespace_to_labels(entries))
        _record_dependency(row, "ok", result.get("message") or "Cascade configured.")
        return {"status": "ok", **result}
    except ZohoError as exc:
        hint = ""
        if getattr(exc, "status", None) == 403:
            hint = " Regenerate the Zoho refresh token with Desk.settings.CREATE to enable it."
        msg = f"Cascade not applied: {exc}.{hint}"
        _record_dependency(row, "error", msg)
        return {"status": "error", "message": msg}
    except Exception as exc:  # a broken cascade must not fail an otherwise-good sync
        msg = f"Cascade not applied: {exc}."
        _record_dependency(row, "error", msg)
        return {"status": "error", "message": msg}


def sync_now() -> Dict[str, Any]:
    """Publish deployments -> Application field and namespaces -> Environment field,
    then (best-effort) wire the Environment -> Application cascade.

    KubeSight is the source of truth and always PATCHes the full list (write-only),
    so no read scope is required. Serialized behind the lock.
    """
    with _sync_lock:
        row = get_or_create_config()
        if not row.enabled:
            raise ValueError("The Zoho integration is disabled. Enable it before syncing.")
        cfg = _to_client_config(row)
        # Resolve the live source first — a bad cluster/namespace read is an error
        # we record without touching Zoho (rather than clobbering the picklists).
        try:
            entries = _source_entries(row)
        except ValueError as exc:
            _record_sync(row, "error", str(exc), None)
            return {"status": "error", "message": str(exc), **serialize(row)}

        app_values = _application_values(entries)
        env_values = build_environment_values(row)
        try:
            parts = []
            # Application field <- live deployments (only when the toggle is on).
            deployments = 0
            if row.sync_application and cfg.app_field_id:
                zoho_client.set_allowed_values(cfg, app_values, field_id=cfg.app_field_id)
                deployments = max(0, len(app_values) - 1)  # exclude -None-
                parts.append(f"{deployments} deployment(s) -> Application")

            # Environment field <- selected namespaces (only when toggled on + configured).
            namespaces = 0
            if row.sync_environment and row.environment_field_id:
                zoho_client.set_allowed_values(cfg, env_values, field_id=row.environment_field_id)
                namespaces = max(0, len(env_values) - 1)
                parts.append(f"{namespaces} namespace(s) -> Environment")

            if parts:
                message = "Published " + "; ".join(parts) + " to Zoho."
            else:
                message = "Nothing published — both field syncs are turned off."
            _record_sync(row, "ok", message, deployments)
        except (ZohoError, ValueError) as exc:
            _record_sync(row, "error", str(exc), None)
            return {"status": "error", "message": str(exc), **serialize(row)}

        # Cascade is best-effort and layered on top of the (already published) lists.
        cascade = _maybe_sync_cascade(row, cfg, entries)
        return {
            "status": "ok",
            "message": message,
            "deployments": deployments,
            "namespaces": namespaces,
            "count": deployments,
            "cascade": cascade,
            **serialize(row),
        }


def test_connection() -> Dict[str, Any]:
    """Verify the integration can authenticate to Zoho, without writing.

    Success = the OAuth token mints (proves client id/secret/refresh token + data
    center). Reading the field is best-effort: reading the whole layout needs a
    scope Desk sometimes withholds even from Desk.settings.READ, but the sync only
    needs the WRITE scope — so a read failure is reported as a caveat, not a
    hard failure.
    """
    row = get_or_create_config()
    cfg = _to_client_config(row)
    try:
        zoho_client.get_access_token(cfg, force=True)
    except ZohoError as exc:
        # Token itself failed → real credential / data-center problem.
        row.last_test_at = datetime.now(timezone.utc)
        row.last_test_status = "error"
        row.last_test_message = str(exc)
        db.session.add(row)
        db.session.commit()
        return {"status": "error", "message": str(exc), **serialize(row)}

    try:
        field = zoho_client.get_field(cfg)
        count = len(field.get("allowedValues") or [])
        message = f"Connected. Field '{field.get('apiName') or row.app_field_api_name}' holds {count} value(s)."
        status = "ok"
    except ZohoError as exc:
        # OAuth works; only the layout read is unavailable — sync still publishes.
        message = (
            f"OAuth OK (token minted). Could not read the layout ({exc}); "
            "sync still publishes via the write path."
        )
        status = "ok"
    row.last_test_at = datetime.now(timezone.utc)
    row.last_test_status = status
    row.last_test_message = message
    db.session.add(row)
    db.session.commit()
    return {"status": status, "message": message, **serialize(row)}


def run_due_sync() -> bool:
    """Scheduler hook: sync if enabled and the interval has elapsed. Returns True if it ran."""
    row = ZohoIntegration.query.get(1)
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

    If no inbound secret is configured, the webhook is treated as open (dev/test);
    configure one for production.
    """
    row = ZohoIntegration.query.get(1)
    stored = decrypt_secret(row.inbound_secret_encrypted or "") if row else ""
    if not stored:
        return True
    return bool(provided) and hmac.compare_digest(str(provided), stored)


def _extract(payload: Dict[str, Any], *keys: str) -> Optional[Any]:
    """First present, non-empty value among ``keys`` (checks top level + ``cf``/``customFields``)."""
    containers = [payload]
    for nested_key in ("cf", "customFields", "customfields", "data"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            containers.append(nested)
    for container in containers:
        for key in keys:
            if key in container and container[key] not in (None, ""):
                return container[key]
    return None


def _snapshot_display(snap: ZohoDeploymentSnapshot) -> str:
    """Human path ``namespace / deployment`` for a resolved deployment snapshot."""
    parts = [snap.namespace, snap.deployment_name]
    return " / ".join(p for p in parts if p)


def resolve_inbound(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Parse the target deployment from a Zoho ticket, resolve, and record.

    The Application value is the bare deployment name (e.g. ``aims-ui``), which can
    repeat across namespaces, so resolution uses the ticket's Environment
    (namespace) field too: (source cluster, namespace, deployment) — unique in
    Kubernetes. Falls back to (a) an explicit id, (b) a legacy ``{deployment} -
    {namespace}`` value, and (c) name-only when it's unambiguous. Idempotent per
    ``ticketId``.
    """
    row = get_or_create_config()
    app_field = row.app_field_api_name or "cf_application"
    env_field = row.environment_field_api_name or "cf_environment"
    tag_field = row.tag_field_api_name or "cf_tag"
    source_cluster = (row.source_cluster_id or "").strip()

    ticket_id = _extract(payload, "ticketId", "id", "ticket_id")
    ticket_number = _extract(payload, "ticketNumber", "number", "ticket_number")
    subject = _extract(payload, "subject", "title")
    raw_app_value = _extract(payload, app_field, "deployment", "app_service", "application", "cf_application")
    raw_env_value = _extract(payload, env_field, "environment", "namespace", "cf_environment")
    tag = _extract(payload, tag_field, "tag", "cf_tag", "version")

    error: Optional[str] = None
    snapshot: Optional[ZohoDeploymentSnapshot] = None

    def _by_ns_name(ns: Optional[str], name: Optional[str]) -> Optional[ZohoDeploymentSnapshot]:
        if not (ns and name):
            return None
        q = ZohoDeploymentSnapshot.query.filter_by(namespace=ns, deployment_name=name)
        if source_cluster:
            q = q.filter_by(cluster_id=source_cluster)
        return q.order_by(ZohoDeploymentSnapshot.id.desc()).first()

    # 1) An explicit id field wins if present.
    explicit_id = _extract(
        payload, "snapshot_id", "workload_id", "deployment_id", "app_service_id", "appServiceId", "target_id"
    )
    if explicit_id is not None:
        try:
            snapshot = ZohoDeploymentSnapshot.query.get(int(str(explicit_id).strip()))
        except (TypeError, ValueError):
            snapshot = None

    # Work out the deployment name + namespace from the app value / env field.
    dep_name = ns_name = None
    if raw_app_value is not None:
        s = str(raw_app_value).strip()
        if _LABEL_SEP in s:  # legacy "{deployment} - {namespace}" value
            dep_name, ns_name = parse_label(s)
        else:  # current bare-name value
            dep_name = s or None
    if ns_name is None and raw_env_value is not None:
        ns_name = str(raw_env_value).strip() or None

    # 2) Resolve on (namespace, deployment) — the normal path.
    if snapshot is None:
        snapshot = _by_ns_name(ns_name, dep_name)

    # 3) Name-only, but only when it's unambiguous across namespaces.
    if snapshot is None and dep_name and not ns_name:
        q = ZohoDeploymentSnapshot.query.filter_by(deployment_name=dep_name)
        if source_cluster:
            q = q.filter_by(cluster_id=source_cluster)
        matches = q.all()
        if len(matches) == 1:
            snapshot = matches[0]
        elif len(matches) > 1:
            error = (
                f"'{dep_name}' exists in multiple namespaces — include the Environment "
                "(namespace) field on the ticket to resolve it."
            )

    # 4) Legacy fallback: an older value that still carried a trailing id.
    if snapshot is None and error is None and raw_app_value is not None:
        legacy_id = parse_workload_id(str(raw_app_value))
        if legacy_id is not None:
            snapshot = ZohoDeploymentSnapshot.query.get(legacy_id)

    snapshot_id: Optional[int] = snapshot.id if snapshot else None
    if snapshot is None and error is None:
        error = "Could not resolve the ticket's Application value to a known deployment."

    existing = None
    if ticket_id is not None:
        existing = ZohoInboundTicket.query.filter_by(ticket_id=str(ticket_id)).first()
    record = existing or ZohoInboundTicket()
    record.ticket_id = str(ticket_id) if ticket_id is not None else record.ticket_id
    record.ticket_number = str(ticket_number) if ticket_number is not None else None
    record.subject = str(subject) if subject is not None else None
    record.raw_app_value = str(raw_app_value) if raw_app_value is not None else None
    # app_service_id column carries the resolved snapshot id; app_service_name carries
    # its human path "namespace / deployment" (schema reused from the earlier design).
    record.app_service_id = snapshot_id
    record.app_service_name = _snapshot_display(snapshot) if snapshot else None
    record.tag = str(tag) if tag is not None else None
    record.resolved = snapshot is not None
    record.error = error
    record.payload = payload if isinstance(payload, dict) else {"raw": payload}
    record.received_at = datetime.now(timezone.utc)
    db.session.add(record)
    db.session.commit()

    # Deploy automation: when auto-run is enabled, a freshly-resolved ticket with
    # a tag starts its run immediately. Never lets automation break the webhook
    # response to Zoho (maybe_auto_run swallows every error).
    if record.resolved and (record.tag or "").strip():
        from .deploy_automation_service import maybe_auto_run

        maybe_auto_run(record.id)

    return {
        "resolved": record.resolved,
        "targetId": snapshot_id,
        "targetName": record.app_service_name,
        "deploymentName": snapshot.deployment_name if snapshot else None,
        "namespace": snapshot.namespace if snapshot else None,
        "clusterId": snapshot.cluster_id if snapshot else None,
        "tag": record.tag,
        "error": error,
        "recordId": record.id,
    }


def delete_inbound_ticket(record_id: int) -> Optional[Dict[str, Any]]:
    """Remove one inbound-ticket record from the log. Returns its summary, or
    None when the id is unknown. The log is KubeSight's own delivery record —
    Zoho never tells us about ticket deletions, so operators prune it here."""
    try:
        row = ZohoInboundTicket.query.get(int(record_id))
    except (TypeError, ValueError):
        return None
    if row is None:
        return None
    info = {
        "id": row.id,
        "ticketId": row.ticket_id,
        "ticketNumber": row.ticket_number,
        "resolved": bool(row.resolved),
    }
    db.session.delete(row)
    db.session.commit()
    return info


def list_inbound_tickets(limit: int = 50) -> List[Dict[str, Any]]:
    rows = (
        ZohoInboundTicket.query.order_by(ZohoInboundTicket.received_at.desc())
        .limit(max(1, min(int(limit or 50), 200)))
        .all()
    )
    # r.app_service_id holds the resolved snapshot id; re-resolve so the UI can
    # show the deployment/namespace even as records age. One IN query, not N.
    snapshot_ids = {r.app_service_id for r in rows if r.app_service_id}
    snapshots = (
        {
            s.id: s
            for s in ZohoDeploymentSnapshot.query.filter(
                ZohoDeploymentSnapshot.id.in_(snapshot_ids)
            ).all()
        }
        if snapshot_ids
        else {}
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        snapshot = snapshots.get(r.app_service_id) if r.app_service_id else None
        out.append(
            {
                "id": r.id,
                "ticketId": r.ticket_id,
                "ticketNumber": r.ticket_number,
                "subject": r.subject,
                "rawAppValue": r.raw_app_value,
                "targetId": r.app_service_id,
                "targetName": r.app_service_name,
                "deploymentName": snapshot.deployment_name if snapshot else None,
                "namespace": snapshot.namespace if snapshot else None,
                "clusterId": snapshot.cluster_id if snapshot else None,
                "tag": r.tag,
                "resolved": bool(r.resolved),
                "error": r.error,
                "receivedAt": _iso(r.received_at),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Ticket write-back — deploy automation reports its outcome back to the Desk
# ticket (status + comment + resolution + owner reassignment). Best-effort and
# dispatched off the caller's thread so it never blocks a webhook/manual Run or
# a scheduler tick, and never raises.
# ---------------------------------------------------------------------------

# Automation outcome → which configured status column drives the label.
_OUTCOME_STATUS_ATTR = {
    "started": "ticket_status_started",
    "deployed": "ticket_status_deployed",
    "failed": "ticket_status_failed",
    "cancelled": "ticket_status_cancelled",
}


def _dispatch_ticket_work(work) -> None:
    """Run ``work`` (pure HTTP against Zoho, no DB) off-thread — inline under
    TESTING so tests stay deterministic. Errors are swallowed by ``work``."""
    from flask import current_app

    try:
        testing = bool(current_app and current_app.config.get("TESTING"))
    except Exception:
        testing = False
    if testing:
        work()
        return
    threading.Thread(target=work, name="zoho-ticket-writeback", daemon=True).start()


def report_ticket_outcome(
    ticket_id: Optional[str],
    outcome: str,
    *,
    comment: Optional[str] = None,
    resolution: Optional[str] = None,
) -> None:
    """Write a finished automation run's result back to its Desk ticket.

    Sets the mapped status, reassigns the ticket to the configured owner
    (zagent), posts a comment, and (for terminal outcomes) a resolution. No-op
    when write-back is disabled or OAuth isn't configured. Never raises.
    """
    if not ticket_id:
        return
    row = get_or_create_config()
    if not row.ticket_writeback_enabled:
        return
    if not (row.refresh_token_encrypted and row.client_id and row.org_id):
        return

    status_label = getattr(row, _OUTCOME_STATUS_ATTR.get(outcome, ""), "") or None
    owner_email = (row.ticket_owner_email or "").strip()
    cfg = _to_client_config(row)
    ticket_id = str(ticket_id)

    def _work() -> None:
        # Status + owner in one PATCH (both are core, well-known ticket fields).
        core: Dict[str, Any] = {}
        if status_label:
            core["status"] = status_label
        if owner_email:
            try:
                agent_id = zoho_client.resolve_agent_id(cfg, owner_email)
                if agent_id:
                    core["assigneeId"] = agent_id
            except Exception:
                logger.warning("Zoho ticket owner resolve failed", exc_info=True)
        if core:
            try:
                zoho_client.update_ticket(cfg, ticket_id, core)
            except Exception:
                logger.warning("Zoho ticket status/owner update failed (%s)", ticket_id, exc_info=True)
        # Resolution goes in its OWN PATCH — if 'resolution' isn't writable in
        # this Desk it must not take the status/owner change down with it.
        if resolution:
            try:
                zoho_client.update_ticket(cfg, ticket_id, {"resolution": resolution})
            except Exception:
                logger.warning("Zoho ticket resolution update failed (%s)", ticket_id, exc_info=True)
        if comment:
            try:
                zoho_client.add_ticket_comment(cfg, ticket_id, comment)
            except Exception:
                logger.warning("Zoho ticket comment failed (%s)", ticket_id, exc_info=True)

    _dispatch_ticket_work(_work)
