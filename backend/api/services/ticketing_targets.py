"""The deploy surface every ticketing provider shares.

A ticket only ever says *what* to deploy; *where it can go* is KubeSight's own
answer — a source cluster, the namespaces picked out of it, which deployments in
those namespaces to publish, the custom non-cluster environments, and the Jenkins
job overrides that route a build. None of that is Zoho- or Jira-shaped, so it
lives on the single :class:`~api.models.TicketingDeployConfig` row rather than
being configured once per provider.

These columns used to sit on :class:`~api.models.ZohoIntegration`. They moved
verbatim (same names, same JSON encodings) and
``migrate_rbac._migrate_ticketing_tables`` seeds the shared row from the Zoho one
on first start, so an existing deployment keeps its exact source selection.

Value sanitization stays in :mod:`zoho_sync_service`: custom environment names
and applications are stored already reduced to picklist-safe characters, because
what is stored is exactly what gets published *and* matched against on inbound.
That rule is Zoho's, but it is the stricter of the two — a value Zoho accepts is
always valid in Jira — so applying it to the shared store keeps one spelling for
both providers.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..db import db
from ..models import TicketingDeployConfig


def get_or_create_config() -> TicketingDeployConfig:
    row = TicketingDeployConfig.query.get(1)
    if row is None:
        row = TicketingDeployConfig(id=1)
        db.session.add(row)
        db.session.commit()
    return row


def _staged_config() -> TicketingDeployConfig:
    """The shared row for a caller that owns the transaction.

    :func:`get_or_create_config` COMMITS when it has to create the row, which is
    fine on a read path but wrong inside a provider's ``update_config``: that
    commit would flush the provider's own half-applied, not-yet-validated edits
    before the validation below could roll them back. Here the new row is only
    staged, so it lives or dies with the caller's transaction.
    """
    row = TicketingDeployConfig.query.get(1)
    if row is None:
        row = TicketingDeployConfig(id=1)
        db.session.add(row)
    return row


# ---------------------------------------------------------------------------
# Readers — each tolerates a legacy/garbled encoding by falling back to empty
# ---------------------------------------------------------------------------

def source_cluster_id() -> Optional[str]:
    return get_or_create_config().source_cluster_id or None


def namespace_list(row: Optional[TicketingDeployConfig] = None) -> List[str]:
    """The operator's chosen namespaces (stored JSON-encoded, order-stable)."""
    raw = (row or get_or_create_config()).selected_namespaces
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        # Tolerate a legacy comma-separated value.
        parsed = [p.strip() for p in str(raw).split(",")]
    return [str(n).strip() for n in parsed if str(n).strip()] if isinstance(parsed, list) else []


def deployment_selection(row: Optional[TicketingDeployConfig] = None) -> Dict[str, Any]:
    """Per-namespace deployment selection: ``{namespace: {"all": bool, "names": [str]}}``.

    A namespace absent from the map publishes ALL its live deployments — the
    dynamic default, so a newly created deployment shows up without a config edit.
    """
    raw = (row or get_or_create_config()).selected_deployments
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def custom_environment_list(row: Optional[TicketingDeployConfig] = None) -> List[Dict[str, Any]]:
    """Custom (non-cluster) environments: ``[{name, applications, jenkinsJobPath, jenkinsParams}]``."""
    raw = (row or get_or_create_config()).custom_environments
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def custom_environment_names() -> List[str]:
    """Names of the custom environments, in stored order.

    Used by Mobile Applications to offer the environment binding as a dropdown
    instead of free text.
    """
    return [
        str(entry.get("name", "")).strip()
        for entry in custom_environment_list()
        if str(entry.get("name", "")).strip()
    ]


def custom_environment_by_name(name: str) -> Optional[Dict[str, Any]]:
    """The custom-environment entry matching ``name``, casefolded.

    Both providers compare dropdown values case-insensitively, so the lookup does
    too. Used by deploy automation to find a run's Jenkins routing.
    """
    target = str(name or "").strip().casefold()
    if not target:
        return None
    for entry in custom_environment_list():
        if str(entry.get("name", "")).casefold() == target:
            return entry
    return None


def job_override_list(row: Optional[TicketingDeployConfig] = None) -> List[Dict[str, Any]]:
    """Jenkins job overrides for cluster targets.

    ``[{"namespace", "deployments", "jenkinsJobPath", "jenkinsParams"}]`` — an
    empty ``deployments`` list means the whole namespace.
    """
    raw = (row or get_or_create_config()).job_overrides
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def job_override_for(namespace: str, deployment: str) -> Optional[Dict[str, Any]]:
    """The override rule for a cluster target, or None (= the global router job).

    A rule naming the deployment beats a whole-namespace rule. Matched casefolded
    — the values round-trip through a ticket dropdown, which compares values
    case-insensitively. Consulted by deploy automation only when a run actually
    needs a build (the ticket's image tag is not already in the registry).
    """
    ns = str(namespace or "").strip().casefold()
    dep = str(deployment or "").strip().casefold()
    if not ns:
        return None
    ns_wide: Optional[Dict[str, Any]] = None
    for entry in job_override_list():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("namespace", "")).strip().casefold() != ns:
            continue
        deps = [str(d).strip().casefold() for d in entry.get("deployments") or []]
        if deps:
            if dep and dep in deps:
                return entry
        elif ns_wide is None:
            ns_wide = entry
    return ns_wide


def serialize(row: Optional[TicketingDeployConfig] = None) -> Dict[str, Any]:
    """The shared deploy surface as the config API exposes it.

    Merged into every provider's config payload under the same keys the Zoho tab
    already used, so the settings form is unchanged on the wire.
    """
    row = row or get_or_create_config()
    return {
        "sourceClusterId": row.source_cluster_id or "",
        "selectedNamespaces": namespace_list(row),
        "selectedDeployments": deployment_selection(row),
        "customEnvironments": custom_environment_list(row),
        "jobOverrides": job_override_list(row),
    }


# ---------------------------------------------------------------------------
# Writers — the normalizers live in zoho_sync_service (imported lazily to keep
# this module free of a circular import at module load).
# ---------------------------------------------------------------------------

def _normalizers():
    from . import zoho_sync_service

    return zoho_sync_service


def set_source(
    cluster_id: Optional[str],
    namespaces: Optional[List[str]],
    deployments: Optional[Dict[str, Any]] = None,
    custom_environments: Optional[List[Dict[str, Any]]] = None,
    job_overrides: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Persist the dropdown source shared by every provider.

    The Environment dropdown becomes exactly these namespaces plus the custom
    environment names; the Application dropdown becomes the live deployments
    running in them (resolved at sync time) plus the custom applications — a
    namespace left unspecified publishes all of its deployments dynamically.

    Raises ValueError on a custom name colliding with a namespace, or on two
    job overrides targeting the same thing.
    """
    svc = _normalizers()
    row = get_or_create_config()
    row.source_cluster_id = (str(cluster_id).strip() if cluster_id else None) or None
    clean: List[str] = []
    seen = set()
    for ns in namespaces or []:
        name = str(ns).strip()
        if name and name not in seen:
            seen.add(name)
            clean.append(name)
    if custom_environments is not None:
        row.custom_environments = json.dumps(
            svc._normalize_custom_environments(custom_environments, reserved=seen)
        )
    if job_overrides is not None:
        # Keep only rules for namespaces still chosen (same as deployments).
        rules = [r for r in svc._normalize_job_overrides(job_overrides) if r["namespace"] in seen]
        row.job_overrides = json.dumps(rules)
    row.selected_namespaces = json.dumps(clean)
    if deployments is not None:
        # Keep only selections for namespaces still chosen.
        normalized = {
            ns: sel
            for ns, sel in svc._normalize_deployment_selection(deployments).items()
            if ns in seen
        }
        row.selected_deployments = json.dumps(normalized)
    db.session.add(row)
    db.session.commit()
    return serialize(row)


def apply_config_payload(payload: Dict[str, Any]) -> List[str]:
    """Apply the shared source keys out of a provider's config payload.

    The settings form posts the whole config in one PUT, so each provider's
    ``update_config`` hands the payload here for the shared half. Stages changes
    on the session WITHOUT committing — the caller owns the transaction, so a
    validation failure further down rolls the shared edit back too. Returns the
    validation errors it collected.
    """
    svc = _normalizers()
    errors: List[str] = []
    row = _staged_config()

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
            svc._normalize_deployment_selection(payload.get("selectedDeployments"))
        )
    if "customEnvironments" in payload:
        try:
            row.custom_environments = json.dumps(
                svc._normalize_custom_environments(
                    payload.get("customEnvironments"), reserved=set(namespace_list(row))
                )
            )
        except ValueError as exc:
            errors.append(str(exc))
    if "jobOverrides" in payload:
        try:
            row.job_overrides = json.dumps(
                svc._normalize_job_overrides(payload.get("jobOverrides"))
            )
        except ValueError as exc:
            errors.append(str(exc))

    db.session.add(row)
    return errors
