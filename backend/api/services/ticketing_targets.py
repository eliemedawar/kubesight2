"""The deploy surface — one selection per ticketing provider.

A ticket only ever says *what* to deploy; *where it can go* is KubeSight's own
answer — a source cluster, the namespaces picked out of it, which deployments in
those namespaces to publish, the custom non-cluster environments, and the Jenkins
job overrides that route a build.

That answer used to be a single shared row on the theory that "a cluster is a
cluster whichever system raised the ticket". In practice it made the two tabs
fight: choosing namespaces on the Jira tab silently rewrote what Zoho published,
and because :func:`set_source` prunes ``selected_deployments`` and
``job_overrides`` down to the namespaces still chosen, each save also dropped the
other provider's per-deployment selection and routing rules. Every provider now
owns its own :class:`~api.models.TicketingDeployConfig` row, keyed by
``provider``.

The split is all-or-nothing on purpose. Keeping the namespaces per-provider but
the Jenkins routing shared would reintroduce exactly that pruning bug, since a
rule for a namespace only Jira selected would vanish the next time Zoho saved.

Two consumers are provider-agnostic — deploy automation resolving a run whose
originating ticket is gone, and Mobile Applications listing environment names.
They pass ``provider=None`` and read across every provider's row; each such
function documents how it breaks a tie.

Value sanitization stays in :mod:`zoho_sync_service`: custom environment names
and applications are stored already reduced to picklist-safe characters, because
what is stored is exactly what gets published *and* matched against on inbound.
That rule is Zoho's, but it is the stricter of the two — a value Zoho accepts is
always valid in Jira — so applying it to every provider's row keeps one spelling.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..db import db
from ..models import TicketingDeployConfig


def _key(provider: Any) -> str:
    """Normalize a provider key. Raises ValueError on a blank one.

    Deliberately strict rather than defaulting to Zoho: a silent default is how a
    caller ends up reading — or worse, writing — the wrong provider's estate.
    """
    key = str(provider or "").strip().lower()
    if not key:
        raise ValueError("A ticketing provider key is required to read the deploy source.")
    return key[:16]


def get_or_create_config(provider: str) -> TicketingDeployConfig:
    """This provider's source row, created empty on first use."""
    key = _key(provider)
    row = TicketingDeployConfig.query.filter_by(provider=key).first()
    if row is None:
        row = TicketingDeployConfig(provider=key)
        db.session.add(row)
        db.session.commit()
    return row


def _staged_config(provider: str) -> TicketingDeployConfig:
    """This provider's row for a caller that owns the transaction.

    :func:`get_or_create_config` COMMITS when it has to create the row, which is
    fine on a read path but wrong inside a provider's ``update_config``: that
    commit would flush the provider's own half-applied, not-yet-validated edits
    before the validation below could roll them back. Here the new row is only
    staged, so it lives or dies with the caller's transaction.
    """
    key = _key(provider)
    row = TicketingDeployConfig.query.filter_by(provider=key).first()
    if row is None:
        row = TicketingDeployConfig(provider=key)
        db.session.add(row)
    return row


def all_configs() -> List[TicketingDeployConfig]:
    """Every provider's row, in a stable order — for the cross-provider readers."""
    return TicketingDeployConfig.query.order_by(TicketingDeployConfig.provider).all()


def _resolve(provider: Optional[str], row: Optional[TicketingDeployConfig]):
    """The row to read: an explicit one wins, else the provider's own.

    A row already in hand is provider-scoped by construction, so a caller that
    has one does not need to name the provider again.
    """
    if row is not None:
        return row
    return get_or_create_config(provider)


# ---------------------------------------------------------------------------
# Readers — each tolerates a legacy/garbled encoding by falling back to empty
# ---------------------------------------------------------------------------

def source_cluster_id(provider: Optional[str] = None, row: Optional[TicketingDeployConfig] = None) -> Optional[str]:
    return _resolve(provider, row).source_cluster_id or None


def namespace_list(
    provider: Optional[str] = None, row: Optional[TicketingDeployConfig] = None
) -> List[str]:
    """The operator's chosen namespaces (stored JSON-encoded, order-stable)."""
    raw = _resolve(provider, row).selected_namespaces
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        # Tolerate a legacy comma-separated value.
        parsed = [p.strip() for p in str(raw).split(",")]
    return [str(n).strip() for n in parsed if str(n).strip()] if isinstance(parsed, list) else []


def deployment_selection(
    provider: Optional[str] = None, row: Optional[TicketingDeployConfig] = None
) -> Dict[str, Any]:
    """Per-namespace deployment selection: ``{namespace: {"all": bool, "names": [str]}}``.

    A namespace absent from the map publishes ALL its live deployments — the
    dynamic default, so a newly created deployment shows up without a config edit.
    """
    raw = _resolve(provider, row).selected_deployments
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def custom_environment_list(
    provider: Optional[str] = None, row: Optional[TicketingDeployConfig] = None
) -> List[Dict[str, Any]]:
    """Custom (non-cluster) environments: ``[{name, applications, jenkinsJobPath, jenkinsParams}]``."""
    raw = _resolve(provider, row).custom_environments
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def custom_environment_names(provider: Optional[str] = None) -> List[str]:
    """Names of the custom environments, in stored order.

    ``provider=None`` returns the union across every provider, de-duped
    casefolded (the comparison both providers' dropdowns use). Mobile
    Applications reads it that way: it offers environment names as a dropdown
    instead of free text and has no ticketing provider of its own.
    """
    if provider is not None:
        entries = custom_environment_list(provider)
    else:
        entries = [e for row in all_configs() for e in custom_environment_list(row=row)]
    out: List[str] = []
    seen = set()
    for entry in entries:
        name = str(entry.get("name", "")).strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out


def custom_environment_by_name(
    name: str, provider: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """The custom-environment entry matching ``name``, casefolded.

    Both providers compare dropdown values case-insensitively, so the lookup does
    too. Used by deploy automation to find a run's Jenkins routing.

    ``provider=None`` searches every provider's list and returns the first match
    in provider order — the fallback for a run whose originating ticket row is
    gone (the FK is ``ON DELETE SET NULL``). Two providers defining the same
    custom environment name is an operator choice, not a conflict this can
    resolve; naming the provider avoids the guess.
    """
    target = str(name or "").strip().casefold()
    if not target:
        return None
    if provider is not None:
        candidates = custom_environment_list(provider)
    else:
        candidates = [e for row in all_configs() for e in custom_environment_list(row=row)]
    for entry in candidates:
        if str(entry.get("name", "")).casefold() == target:
            return entry
    return None


def job_override_list(
    provider: Optional[str] = None, row: Optional[TicketingDeployConfig] = None
) -> List[Dict[str, Any]]:
    """Jenkins job overrides for cluster targets.

    ``[{"namespace", "deployments", "jenkinsJobPath", "jenkinsParams"}]`` — an
    empty ``deployments`` list means the whole namespace.
    """
    raw = _resolve(provider, row).job_overrides
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def job_override_for(
    namespace: str, deployment: str, provider: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """The override rule for a cluster target, or None (= the global router job).

    A rule naming the deployment beats a whole-namespace rule. Matched casefolded
    — the values round-trip through a ticket dropdown, which compares values
    case-insensitively. Consulted by deploy automation only when a run actually
    needs a build (the ticket's image tag is not already in the registry).

    ``provider=None`` scans every provider's rules, keeping that same specificity
    order globally: a deployment-specific rule from any provider wins over a
    namespace-wide one from any provider.
    """
    ns = str(namespace or "").strip().casefold()
    dep = str(deployment or "").strip().casefold()
    if not ns:
        return None
    if provider is not None:
        rules = job_override_list(provider)
    else:
        rules = [r for row in all_configs() for r in job_override_list(row=row)]
    ns_wide: Optional[Dict[str, Any]] = None
    for entry in rules:
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


def serialize(
    provider: Optional[str] = None, row: Optional[TicketingDeployConfig] = None
) -> Dict[str, Any]:
    """One provider's deploy surface as the config API exposes it.

    Merged into that provider's config payload under the same keys the Zoho tab
    already used, so the settings form is unchanged on the wire — each tab simply
    now round-trips its own selection.
    """
    row = _resolve(provider, row)
    return {
        "sourceClusterId": row.source_cluster_id or "",
        "selectedNamespaces": namespace_list(row=row),
        "selectedDeployments": deployment_selection(row=row),
        "customEnvironments": custom_environment_list(row=row),
        "jobOverrides": job_override_list(row=row),
    }


# ---------------------------------------------------------------------------
# Writers — the normalizers live in zoho_sync_service (imported lazily to keep
# this module free of a circular import at module load).
# ---------------------------------------------------------------------------

def _normalizers():
    from . import zoho_sync_service

    return zoho_sync_service


def set_source(
    provider: str,
    cluster_id: Optional[str],
    namespaces: Optional[List[str]],
    deployments: Optional[Dict[str, Any]] = None,
    custom_environments: Optional[List[Dict[str, Any]]] = None,
    job_overrides: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Persist one provider's dropdown source.

    That provider's Environment dropdown becomes exactly these namespaces plus
    its custom environment names; its Application dropdown becomes the live
    deployments running in them (resolved at sync time) plus the custom
    applications — a namespace left unspecified publishes all of its deployments
    dynamically. The other provider's selection is untouched.

    Raises ValueError on a custom name colliding with a namespace, or on two
    job overrides targeting the same thing.
    """
    svc = _normalizers()
    row = get_or_create_config(provider)
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
    return serialize(row=row)


def apply_config_payload(provider: str, payload: Dict[str, Any]) -> List[str]:
    """Apply the source keys out of one provider's config payload.

    The settings form posts the whole config in one PUT, so each provider's
    ``update_config`` hands the payload here for the source half. Stages changes
    on the session WITHOUT committing — the caller owns the transaction, so a
    validation failure further down rolls this edit back too. Returns the
    validation errors it collected.
    """
    svc = _normalizers()
    errors: List[str] = []
    row = _staged_config(provider)

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
                    payload.get("customEnvironments"), reserved=set(namespace_list(row=row))
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
