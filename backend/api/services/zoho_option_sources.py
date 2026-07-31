"""Where a Zoho picklist's options come from — providers + field bindings.

The Application / Environment / Variable dropdowns were always fed by live
cluster reads, through three copy-pasted blocks inside ``sync_now``. This module
turns that into data: a **source kind** resolves to ``ResolvedOptions(values,
by_parent)``, and a :class:`~api.models.ZohoFieldBinding` says which picklist
gets which kind (and, optionally, which field cascades into it).

Two rules keep the production integration safe:

* **Providers reuse the existing builders** in :mod:`zoho_sync_service` rather
  than reimplementing them. A binding on the Application field would resolve to
  byte-identical values.
* **The legacy three are never rows.** :func:`all_bindings` synthesizes them in
  memory from ``ZohoIntegration``'s own columns and toggles, marked ``locked``,
  so the live integration keeps its exact semantics (and its exact log wording)
  while the sync loop stops caring which binding is which.

There is deliberately **no ``static`` kind**: a hand-typed option list is already
owned by ``zoho_fields_service.set_field_options``, and a static binding would be
a second writer racing it on every sync.

Every provider's output goes through :func:`canonical_values`, which is the
generalization of the env-var collision map: Zoho compares picklist values
case-insensitively and rejects the WHOLE patch with "The allowed values has
duplicate value" if two differ only by case. Cascade child lists and parent keys
go through the same funnel (:func:`align_by_parent`), so a mapping can never
reference a spelling that was not published.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ..db import db
from ..models import ZohoFieldBinding, ZohoIntegration
from . import zoho_sync_service as sync_svc
from .zoho_sync_service import NONE_VALUE

# ---------------------------------------------------------------------------
# Canonicalization — one funnel for every source
# ---------------------------------------------------------------------------

def _canon_key(value: Any) -> str:
    """The collision key Zoho compares picklist values by (sanitized, casefolded)."""
    return _clean(value).casefold()


def _clean(value: Any) -> str:
    """Sanitize + truncate one value the way the picklist builders do. Idempotent."""
    return sync_svc._sanitize_value(value)[: sync_svc._MAX_VALUE_LEN].rstrip()


def canonical_values(raw: Iterable[Any]) -> Tuple[List[str], Dict[str, str]]:
    """``([-None-, …], {collision key: published spelling})`` for one option list.

    First spelling wins, order is preserved (providers decide their own sort),
    and ``-None-`` is always first — Zoho's blank entry.
    """
    values = [NONE_VALUE]
    canon: Dict[str, str] = {}
    for item in raw:
        cleaned = _clean(item)
        if not cleaned or cleaned == NONE_VALUE:
            continue
        key = cleaned.casefold()
        if key in canon:
            continue
        canon[key] = cleaned
        values.append(cleaned)
    return values, canon


def align_by_parent(
    by_parent: Dict[str, List[str]], parent_canon: Dict[str, str], child_canon: Dict[str, str]
) -> Dict[str, List[str]]:
    """Re-key a cascade map onto the values both fields actually published.

    Zoho 422s a mapping that names a parent or child value it never saw. Parent
    keys that collide case-insensitively (two namespaces sanitizing to the same
    string publish ONE Environment value) merge into a single bucket instead of
    producing a phantom parent.
    """
    out: Dict[str, List[str]] = {}
    for parent, children in (by_parent or {}).items():
        published_parent = parent_canon.get(_canon_key(parent))
        if not published_parent:
            continue
        bucket = out.setdefault(published_parent, [])
        for child in children or []:
            published_child = child_canon.get(_canon_key(child))
            if published_child and published_child not in bucket:
                bucket.append(published_child)
    return {parent: children for parent, children in out.items() if children}


# ---------------------------------------------------------------------------
# Shared per-sync context — the cluster is read once for every binding
# ---------------------------------------------------------------------------

class SourceContext:
    """One sync's live reads, shared by every binding that needs them.

    ``entries`` (the published deployments) is resolved by the caller before any
    Zoho write, because a failed cluster read must abort the sync rather than
    publish an empty list. Env-var names are read lazily: it is a full spec read
    per deployment and only the ``env_vars`` kind wants it.

    ``provider`` is required: the deploy source is per-provider, so a resolver
    that reads it (``namespaces``, and the cluster the env-var specs come from)
    has to know whose sync it is running inside.
    """

    def __init__(self, row, entries: List[Dict[str, Any]], provider: str, fresh: bool = False):
        self.row = row
        self.entries = entries
        self.provider = provider
        self.fresh = fresh
        self._vars_by_ns: Optional[Dict[str, Dict[str, List[str]]]] = None

    @property
    def vars_by_ns(self) -> Dict[str, Dict[str, List[str]]]:
        if self._vars_by_ns is None:
            self._vars_by_ns = sync_svc._variables_for_entries(
                self.row, self.entries, fresh=self.fresh, provider=self.provider
            )
        return self._vars_by_ns

    @property
    def variables_read(self) -> bool:
        """Whether anything actually asked for env vars (drives the legacy cascade)."""
        return self._vars_by_ns is not None


@dataclass
class ResolvedOptions:
    """What a provider publishes: the option list + its cascade map (unaligned)."""

    values: List[str]
    canon: Dict[str, str]
    by_parent: Dict[str, List[str]] = dc_field(default_factory=dict)

    @property
    def count(self) -> int:
        """Options excluding Zoho's ``-None-`` placeholder."""
        return max(0, len(self.values) - 1)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

def _namespaces(ctx: SourceContext, params: Dict[str, Any]) -> ResolvedOptions:
    values, canon = canonical_values(
        sync_svc.build_environment_values(ctx.row, ctx.provider)
    )
    return ResolvedOptions(values, canon)


def _deployments(ctx: SourceContext, params: Dict[str, Any]) -> ResolvedOptions:
    values, canon = canonical_values(sync_svc._application_values(ctx.entries))
    return ResolvedOptions(values, canon, sync_svc._namespace_to_labels(ctx.entries))


def _env_vars(ctx: SourceContext, params: Dict[str, Any]) -> ResolvedOptions:
    vars_by_ns = ctx.vars_by_ns
    values, canon = canonical_values(sync_svc.build_variable_values(ctx.entries, vars_by_ns))
    return ResolvedOptions(values, canon, sync_svc._app_to_variables(ctx.entries, vars_by_ns))


def _clusters(ctx: SourceContext, params: Dict[str, Any]) -> ResolvedOptions:
    values, canon = canonical_values(c.get("name") for c in sync_svc.list_source_clusters())
    return ResolvedOptions(values, canon)


@dataclass(frozen=True)
class SourceKind:
    key: str
    label: str
    description: str
    resolve: Callable[[SourceContext, Dict[str, Any]], ResolvedOptions]
    # Which kind a parent binding must have for the cascade to make sense — the
    # provider's ``by_parent`` keys are values of THAT kind. None = no cascade.
    parent_kind: Optional[str] = None
    # Provider parameters (name/label/type). Every kind shipped so far is
    # parameter-free; the plumbing exists so adding one is not an API change.
    params: Tuple[Dict[str, str], ...] = ()


SOURCE_KINDS: Dict[str, SourceKind] = {
    kind.key: kind
    for kind in (
        SourceKind(
            "namespaces",
            "Namespaces / environments",
            "The namespaces selected on the Source tab, plus any custom environments.",
            _namespaces,
        ),
        SourceKind(
            "deployments",
            "Deployments / applications",
            "Live deployments running in the selected namespaces.",
            _deployments,
            parent_kind="namespaces",
        ),
        SourceKind(
            "env_vars",
            "Environment variable names",
            "Literal env-var names read from the published deployments' specs.",
            _env_vars,
            parent_kind="deployments",
        ),
        SourceKind(
            "clusters",
            "Clusters",
            "Every cluster KubeSight can read.",
            _clusters,
        ),
    )
}


def describe_sources() -> List[Dict[str, Any]]:
    """The source catalogue for the UI's picker."""
    return [
        {
            "key": kind.key,
            "label": kind.label,
            "description": kind.description,
            "parentKind": kind.parent_kind,
            "params": [dict(p) for p in kind.params],
        }
        for kind in SOURCE_KINDS.values()
    ]


# ---------------------------------------------------------------------------
# Bindings — the synthesized legacy three + stored rows
# ---------------------------------------------------------------------------

@dataclass
class Binding:
    """One picklist's option source, whether stored or synthesized."""

    field_id: str
    label: str
    source_kind: str
    params: Dict[str, Any] = dc_field(default_factory=dict)
    parent_field_id: Optional[str] = None
    enabled: bool = True
    # Locked bindings are the legacy three: configured by the integration's own
    # toggles on the Source tab, not editable (or deletable) as binding rows.
    locked: bool = False
    api_name: str = ""
    row_id: Optional[int] = None
    # Message fragments so the legacy fields' log wording survives the refactor.
    publish_note: str = "{n} value(s) -> {label}"
    cascade_note: str = "{child} filtered by {parent} for {n} value(s)."
    # Last publish outcome (stored bindings only; the legacy three report through
    # the integration's own lastSync* fields).
    last_status: Optional[str] = None
    last_message: Optional[str] = None
    last_count: Optional[int] = None
    last_synced_at: Optional[datetime] = None

    @property
    def kind(self) -> Optional[SourceKind]:
        return SOURCE_KINDS.get(self.source_kind)


# Field ids owned by the integration config; a binding row may not target them.
def legacy_field_ids(row) -> Dict[str, str]:
    return {
        "application": str(row.app_field_id or ""),
        "environment": str(row.environment_field_id or ""),
        "variable": str(row.variable_field_id or ""),
    }


def _legacy_bindings(row) -> List[Binding]:
    """The original three, in their original publish order.

    Order is load-bearing: Application, then Environment, then Variable is what
    the sync has always sent, and the cascade rebuild that follows assumes both
    ends of each mapping are already published.
    """
    out: List[Binding] = []
    if row.app_field_id:
        out.append(
            Binding(
                field_id=str(row.app_field_id),
                label="Application",
                source_kind="deployments",
                parent_field_id=str(row.environment_field_id or "") or None,
                enabled=bool(row.sync_application),
                locked=True,
                api_name=row.app_field_api_name or "",
                publish_note="{n} deployment(s) -> Application",
                cascade_note="Cascade configured for {n} namespace(s).",
            )
        )
    if row.environment_field_id:
        out.append(
            Binding(
                field_id=str(row.environment_field_id),
                label="Environment",
                source_kind="namespaces",
                enabled=bool(row.sync_environment),
                locked=True,
                api_name=row.environment_field_api_name or "",
                publish_note="{n} namespace(s) -> Environment",
            )
        )
    if row.variable_field_id:
        out.append(
            Binding(
                field_id=str(row.variable_field_id),
                label="Variable",
                source_kind="env_vars",
                parent_field_id=str(row.app_field_id or "") or None,
                enabled=bool(row.sync_variables),
                locked=True,
                api_name=row.variable_field_api_name or "",
                publish_note="{n} variable name(s) -> Variable",
                cascade_note="Variable lists mapped for {n} application(s).",
            )
        )
    return out


def _params_of(row: ZohoFieldBinding) -> Dict[str, Any]:
    try:
        parsed = json.loads(row.params or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _from_row(row: ZohoFieldBinding) -> Binding:
    return Binding(
        field_id=str(row.field_id),
        label=row.label or row.api_name or f"Field {row.field_id}",
        source_kind=row.source_kind,
        params=_params_of(row),
        parent_field_id=str(row.parent_field_id or "") or None,
        enabled=bool(row.enabled),
        api_name=row.api_name or "",
        row_id=row.id,
        last_status=row.last_status,
        last_message=row.last_message,
        last_count=row.last_count,
        last_synced_at=row.last_synced_at,
    )


def all_bindings(row, provider: str = "zoho") -> List[Binding]:
    """Every binding this sync should consider — legacy three first, then rows.

    Disabled bindings are included on purpose: the cascade needs them to tear
    down a mapping whose child was switched off.

    ``row`` is the provider's integration config. It is duck-typed on purpose:
    :class:`~api.models.ZohoIntegration` and :class:`~api.models.JiraIntegration`
    both carry ``*_field_id``, ``*_field_api_name`` and the three ``sync_*``
    toggles under identical names, which is all :func:`_legacy_bindings` reads —
    so the same synthesis produces the same three locked bindings either way.
    """
    out = _legacy_bindings(row)
    taken = {b.field_id for b in out}
    stored_rows = (
        ZohoFieldBinding.query.filter_by(provider=provider)
        .order_by(ZohoFieldBinding.id)
        .all()
    )
    for stored in stored_rows:
        field_id = str(stored.field_id)
        if field_id in taken or stored.source_kind not in SOURCE_KINDS:
            continue
        taken.add(field_id)
        out.append(_from_row(stored))
    return out


def resolve(binding: Binding, ctx: SourceContext) -> ResolvedOptions:
    """Run a binding's provider. Raises ValueError on an unknown source kind."""
    kind = binding.kind
    if kind is None:
        raise ValueError(f"Unknown option source '{binding.source_kind}'.")
    return kind.resolve(ctx, binding.params or {})


# ---------------------------------------------------------------------------
# Cascade wiring
# ---------------------------------------------------------------------------

def cascade_pairs(bindings: List[Binding]) -> Tuple[List[Tuple[Binding, Binding]], List[Tuple[Binding, Binding]]]:
    """``(active, teardown)`` parent→child pairs, parents before children.

    *Active* pairs get their mapping rebuilt; *teardown* pairs are ones whose
    child was switched off — their stale mapping must still be deleted, both
    because it would keep filtering a ticket by a list nobody maintains and
    because Zoho refuses to create a mapping whose child already parents one.

    A pair whose PARENT is disabled is left completely alone: the parent's own
    option list is not being maintained either, so deleting the mapping would
    destroy operator configuration the sync no longer owns.
    """
    by_id = {b.field_id: b for b in bindings}
    active: List[Tuple[Binding, Binding]] = []
    teardown: List[Tuple[Binding, Binding]] = []
    for child in bindings:
        parent = by_id.get(str(child.parent_field_id or ""))
        if parent is None or parent.field_id == child.field_id or not parent.enabled:
            continue
        (active if child.enabled else teardown).append((parent, child))
    return _topological(active), teardown


def _topological(pairs: List[Tuple[Binding, Binding]]) -> List[Tuple[Binding, Binding]]:
    """Order pairs so a field is always created as a parent before it is a child.

    Zoho rejects ("invalid child Id") a mapping whose child already parents
    another, so the chain must be rebuilt from the top down. Raises ValueError on
    a cycle — silently truncating one would leave a half-wired cascade.
    """
    if len(pairs) < 2:
        return list(pairs)
    remaining = list(pairs)
    ordered: List[Tuple[Binding, Binding]] = []
    while remaining:
        # A pair is ready when nothing still-pending has to create its parent first.
        pending_children = {child.field_id for _, child in remaining}
        ready_at = [i for i, (parent, _) in enumerate(remaining) if parent.field_id not in pending_children]
        if not ready_at:
            names = ", ".join(f"{p.label} -> {c.label}" for p, c in remaining)
            raise ValueError(f"The cascade has a cycle: {names}.")
        ordered.extend(remaining[i] for i in ready_at)
        remaining = [p for i, p in enumerate(remaining) if i not in set(ready_at)]
    return ordered


# ---------------------------------------------------------------------------
# Binding CRUD (used by the routes)
# ---------------------------------------------------------------------------

def serialize(binding: Binding) -> Dict[str, Any]:
    """One binding for the UI — locked (synthesized) ones included."""
    kind = binding.kind
    return {
        "id": binding.row_id,
        "fieldId": binding.field_id,
        "apiName": binding.api_name or "",
        "label": binding.label,
        "sourceKind": binding.source_kind,
        "sourceLabel": kind.label if kind else binding.source_kind,
        "params": dict(binding.params or {}),
        "parentFieldId": binding.parent_field_id,
        "enabled": binding.enabled,
        # Locked = owned by the integration config, not editable as a binding.
        "locked": binding.locked,
        "lastStatus": binding.last_status,
        "lastMessage": binding.last_message,
        "lastCount": binding.last_count,
        "lastSyncedAt": sync_svc._iso(binding.last_synced_at),
    }


def get_binding_row(field_id: str, provider: str = "zoho") -> Optional[ZohoFieldBinding]:
    return ZohoFieldBinding.query.filter_by(
        field_id=str(field_id), provider=provider
    ).first()


def bindings_by_field(row, provider: str = "zoho") -> Dict[str, Dict[str, Any]]:
    """``{field id: serialized binding}`` — one query, no N+1 from the layout view."""
    return {b.field_id: serialize(b) for b in all_bindings(row, provider)}


def upsert_binding(
    row,
    field_id: str,
    source_kind: str,
    *,
    label: str = "",
    api_name: str = "",
    params: Optional[Dict[str, Any]] = None,
    parent_field_id: Optional[str] = None,
    enabled: bool = True,
    provider: str = "zoho",
) -> ZohoFieldBinding:
    """Create or replace one field's binding. Callers validate first (see
    ``zoho_fields_service.set_field_binding``) — this only writes."""
    stored = get_binding_row(field_id, provider) or ZohoFieldBinding(
        field_id=str(field_id), provider=provider
    )
    stored.provider = provider
    stored.source_kind = source_kind
    stored.label = label or stored.label
    stored.api_name = api_name or stored.api_name
    stored.params = json.dumps(params or {})
    stored.parent_field_id = str(parent_field_id or "") or None
    stored.enabled = bool(enabled)
    db.session.add(stored)
    db.session.commit()
    return stored


def delete_binding(field_id: str, provider: str = "zoho") -> bool:
    stored = get_binding_row(field_id, provider)
    if stored is None:
        return False
    db.session.delete(stored)
    db.session.commit()
    return True


def check_cascade(bindings: List[Binding]) -> None:
    """Raise ValueError if the declared parents form a cycle.

    Checked with every binding treated as enabled: a cycle that only bites once
    someone flips a toggle back on is still a cycle, and refusing it at save time
    is the only place the operator can see which pair to break.
    """
    cascade_pairs([replace(b, enabled=True) for b in bindings])


def record_result(
    binding: Binding, status: str, message: str, count: Optional[int] = None
) -> None:
    """Persist a stored binding's last outcome (locked ones have no row)."""
    if binding.row_id is None:
        return
    row = ZohoFieldBinding.query.get(binding.row_id)
    if row is None:
        return
    row.last_status = status
    row.last_message = message
    row.last_count = count
    row.last_synced_at = datetime.now(timezone.utc)
    db.session.add(row)
