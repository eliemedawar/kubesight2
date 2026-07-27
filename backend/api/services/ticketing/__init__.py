"""The ticketing provider port — one interface over Zoho Desk and Jira.

KubeSight's ticketing feature is the same story either way: publish what the
clusters can deploy into a ticket form's dropdowns, take a ticket back through a
webhook, run the deploy, report the outcome onto the ticket. Only the vendor API
underneath changes, so this package is the seam: a :class:`Provider` names one
implementation's modules plus the handful of things the two genuinely cannot both
do, and everything above it (routes, deploy automation, the UI) reads the seam
instead of importing ``zoho_*`` directly.

Deliberately a *registry of modules*, not an abstract base class with two
subclasses. The two implementations already exist as module-level functions with
matching names, the call sites are all "one function, once", and an ABC would add
a layer of indirection whose only job is to forward. What the registry does add
is the part that actually needed a home: :attr:`Provider.capabilities`, the
honest list of what each side cannot do, so a route can answer "Jira has no
text→dropdown conversion" with a clear 501 instead of an AttributeError.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from types import ModuleType
from typing import Any, Dict, List, Optional, Type


@dataclass(frozen=True)
class Provider:
    """One ticketing implementation, plus what it can and cannot do."""

    key: str
    name: str
    # One line for the provider card on the Ticketing tab.
    tagline: str
    # The vendor's own name for the object a "layout"/form is, used verbatim in
    # the UI so the operator sees the word their admin console shows them.
    form_noun: str
    # Module implementing config/sync/inbound/write-back (``*_sync_service``).
    sync: ModuleType
    # Module implementing the form/field editor (``*_fields_service``).
    fields: ModuleType
    # The provider's transport exception, so routes can map it to one 502.
    error: Type[Exception]
    capabilities: Dict[str, Any] = dc_field(default_factory=dict)

    def describe(self) -> Dict[str, Any]:
        """Provider metadata + current status, for the two cards on the tab.

        Reads the config row (cheap, single-row) so a card can show whether the
        provider is set up and how its last sync went without the UI making one
        request per provider.
        """
        try:
            config = self.sync.get_config_dict()
        except Exception:  # noqa: BLE001 — a card must render even if config is broken
            config = {}
        return {
            "key": self.key,
            "name": self.name,
            "tagline": self.tagline,
            "formNoun": self.form_noun,
            "capabilities": dict(self.capabilities),
            "enabled": bool(config.get("enabled")),
            "configured": self.is_configured(config),
            "lastSyncAt": config.get("lastSyncAt"),
            "lastSyncStatus": config.get("lastSyncStatus"),
            "lastSyncMessage": config.get("lastSyncMessage"),
            "lastTestStatus": config.get("lastTestStatus"),
        }

    def is_configured(self, config: Optional[Dict[str, Any]] = None) -> bool:
        """Whether enough is filled in for the card to read "connected".

        Each provider names its own required keys in ``capabilities`` because the
        essentials genuinely differ — Zoho needs an org + OAuth grant, Jira needs
        a site URL + project.
        """
        if config is None:
            config = self.sync.get_config_dict()
        return all(config.get(key) for key in self.capabilities.get("requiredKeys", ()))


def _build_registry() -> Dict[str, Provider]:
    # Imported here rather than at module scope: the service modules import the
    # models, which import the db, and a top-level import chain would run before
    # the Flask app finishes wiring itself up.
    from .. import jira_fields_service, jira_sync_service, zoho_fields_service, zoho_sync_service
    from ..jira_client import JiraError
    from ..zoho_client import ZohoError

    return {
        "jira": Provider(
            key="jira",
            name="Jira",
            tagline="Atlassian Jira Cloud or Data Center",
            form_noun="screen",
            sync=jira_sync_service,
            fields=jira_fields_service,
            error=JiraError,
            capabilities={
                "requiredKeys": ("baseUrl", "projectKey", "appFieldId"),
                # Jira creates screen tabs directly; Zoho's API cannot add a
                # section to an existing layout at all.
                "createSections": True,
                # No whole-object rewrite to preview, so the dry-run is trivial.
                "layoutPlan": False,
                "layoutRecovery": False,
                # No "change this field's type" flow — see zoho's convertField.
                "convertField": False,
                # Deleting a custom field is possible (soft-delete to the trash).
                "deleteField": True,
                # A dependent dropdown is ONE cascading-select field, not a
                # mapping between two fields.
                "cascadeMode": "cascadingField",
                # Jira soft-deletes a custom field to the trash, so the warning
                # is about scope (site-wide, every issue) rather than finality.
                "deleteWarning": (
                    "This removes the field from the screen and sends the custom field to "
                    "the Jira trash, taking its value on every issue in the site with it. "
                    "An administrator can restore it from the trash."
                ),
                "secretFields": ("apiToken", "inboundSecret"),
                "docsHint": (
                    "The API token's account needs Jira administrator rights to edit "
                    "custom-field options and screens."
                ),
            },
        ),
        "zoho": Provider(
            key="zoho",
            name="Zoho Desk",
            tagline="Zoho Desk DevOps Request tickets",
            form_noun="layout",
            sync=zoho_sync_service,
            fields=zoho_fields_service,
            error=ZohoError,
            capabilities={
                "requiredKeys": ("orgId", "layoutId", "appFieldId"),
                "createSections": False,
                "layoutPlan": True,
                "layoutRecovery": True,
                "convertField": True,
                "deleteField": True,
                "cascadeMode": "dependencyMapping",
                # Desk's delete is permanent and metered — both facts belong in
                # front of the operator before they confirm.
                "deleteWarning": (
                    "This permanently deletes the custom field across the Zoho Desk "
                    "organization, including its stored values. This cannot be undone and "
                    "costs 500 Zoho API credits."
                ),
                "secretFields": ("clientSecret", "refreshToken", "inboundSecret"),
                "docsHint": (
                    "The refresh token must be minted with Desk.settings.ALL and "
                    "Desk.tickets.ALL for field writes and ticket write-back."
                ),
            },
        ),
    }


_REGISTRY: Optional[Dict[str, Provider]] = None


def registry() -> Dict[str, Provider]:
    """The provider registry, built once per process."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def get(key: str) -> Optional[Provider]:
    """One provider by key, or None when the key is unknown."""
    return registry().get(str(key or "").strip().lower())


def keys() -> List[str]:
    """Every provider key, in the order the cards are shown."""
    return list(registry().keys())


def describe_all() -> List[Dict[str, Any]]:
    """Every provider's card payload."""
    return [provider.describe() for provider in registry().values()]


def report_outcome(
    provider_key: str,
    ticket_id: Optional[str],
    outcome: str,
    *,
    comment: Optional[str] = None,
    resolution: Optional[str] = None,
) -> None:
    """Route a finished run's write-back to whichever provider owns the ticket.

    Deploy automation is provider-agnostic — it only knows the intake row — so
    this is where the ticket's ``provider`` column turns back into a vendor call.
    Never raises: a write-back failure must not affect the run that succeeded.
    """
    provider = get(provider_key)
    if provider is None:
        return
    try:
        provider.sync.report_ticket_outcome(
            ticket_id, outcome, comment=comment, resolution=resolution
        )
    except Exception:  # noqa: BLE001 — best-effort by contract
        pass


def post_comment(provider_key: str, ticket_id: Optional[str], comment: str) -> None:
    """Route a standalone comment (rollout watcher) to the owning provider."""
    provider = get(provider_key)
    if provider is None:
        return
    try:
        provider.sync.post_ticket_comment(ticket_id, comment)
    except Exception:  # noqa: BLE001 — best-effort by contract
        pass


def run_due_syncs() -> int:
    """Scheduler hook: run every provider whose interval has elapsed.

    Returns how many actually ran. One provider's failure must not stop the
    others, so each is guarded separately.
    """
    ran = 0
    for provider in registry().values():
        try:
            if provider.sync.run_due_sync():
                ran += 1
        except Exception:  # noqa: BLE001 — logged by the scheduler's own handler
            continue
    return ran
