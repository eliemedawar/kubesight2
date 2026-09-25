"""Turning Hermes' decision into a route: execute, approval, or impediment.

Two kinds of objection, kept apart on purpose:

* **errors** — the decision cannot be executed as stated (target not in the
  catalog, a tag with shell characters, a restart on a Jenkins-only
  environment). The engine feeds these back to Hermes once; if they survive,
  the ticket is an impediment.
* **reasons** — the decision is executable, but a human should look first
  (confidence under the bar, disagreement with the ticket's own dropdowns,
  concerns Hermes raised). These route to a Telegram approval.

Confidence is Hermes' own claim and is never the only gate: a High decision
that disagrees with the structured fields still waits for a person.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ...models import ZohoDeploymentSnapshot, ZohoInboundTicket
from . import catalog, schema

_RANK = {"Low": 1, "Medium": 2, "High": 3}
# Docker's tag charset (the router gets it raw; the registry gets it templated).
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}$")
VARIABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,252}$")


@dataclass
class Plan:
    route: str  # execute | approval | impediment
    change_type: Optional[str] = None
    snapshot: Optional[ZohoDeploymentSnapshot] = None
    tag: Optional[str] = None
    variable: Optional[str] = None
    value: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    # The same objections, phrased for the requester (they end up on the ticket).
    problems: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def fail(self, for_hermes: str, for_requester: str) -> None:
        self.errors.append(for_hermes)
        self.problems.append(for_requester)


def check(
    decision: Dict[str, Any],
    targets: List[ZohoDeploymentSnapshot],
    ticket: ZohoInboundTicket,
    min_confidence: str = "High",
) -> Plan:
    if decision["decision"] == "clarify":
        return Plan(route="impediment", reasons=["Hermes needs clarification from the requester."])

    action = decision["action"]
    change_type = schema.ACTION_CHANGE_TYPE[action]
    target = decision["target"]
    change = decision["change"]
    plan = Plan(route="execute", change_type=change_type)

    snap = catalog.find_target(targets, target.get("environment"), target.get("application"))
    if snap is None:
        plan.fail(
            f"Application '{target.get('application') or '?'}' in environment "
            f"'{target.get('environment') or '?'}' is not in the catalog. Copy both from one "
            "catalog entry, or decide 'clarify'.",
            "I could not match the application and environment to anything KubeSight can deploy. "
            "Please pick the Application and Environment from the ticket's dropdowns.",
        )
    plan.snapshot = snap

    tag, variable, value = change.get("tag"), change.get("variable"), change.get("value")
    if change_type == "image":
        if not tag:
            plan.fail("deploy_image needs change.tag.", "The ticket does not say which version (tag) to deploy.")
        elif not TAG_RE.match(tag):
            plan.fail(f"change.tag '{tag}' is not a valid image tag.", f"'{tag}' is not a valid version tag.")
        if variable or value:
            plan.fail(
                "deploy_image must not carry a variable or value.",
                "The ticket asks for both a new version and a variable change; please raise one ticket per change.",
            )
        plan.tag = tag
    elif change_type == "env_var":
        if not variable:
            plan.fail("set_env_var needs change.variable.", "The ticket does not say which variable to change.")
        elif not VARIABLE_RE.match(variable):
            plan.fail(
                f"change.variable '{variable}' is not a valid variable name.",
                f"'{variable}' is not a valid environment variable name.",
            )
        if value is None or value == "":
            plan.fail("set_env_var needs change.value.", "The ticket does not say what value to set the variable to.")
        if tag:
            plan.fail(
                "set_env_var must not carry a tag.",
                "The ticket asks for both a variable change and a new version; please raise one ticket per change.",
            )
        plan.variable, plan.value = variable, value
    else:  # restart
        if tag or variable or value:
            plan.fail(
                "restart must not carry a tag, variable or value.",
                "The ticket asks for a restart together with another change; please raise one ticket per change.",
            )

    if snap is not None and catalog.is_custom(snap) and change_type != "image":
        plan.fail(
            f"'{snap.namespace}' is a custom (Jenkins-only) environment; only deploy_image is possible there.",
            f"'{snap.namespace}' only supports deploying a version; restarts and variable changes are not possible there.",
        )

    if plan.errors:
        plan.route = "impediment"
        return plan

    # --- Executable. Does a human need to look first? ---
    if _RANK.get(decision["confidence"], 0) < _RANK.get(min_confidence, 3):
        plan.reasons.append(f"Hermes' confidence is {decision['confidence']} (the bar is {min_confidence}).")
    for concern in decision.get("concerns") or []:
        plan.reasons.append(f"Hermes flagged: {concern}")
    plan.reasons.extend(_disagreements(plan, ticket))
    if plan.reasons:
        plan.route = "approval"
    return plan


def _disagreements(plan: Plan, ticket: ZohoInboundTicket) -> List[str]:
    """Where Hermes' reading departs from what the requester picked in dropdowns."""
    out: List[str] = []
    snap = plan.snapshot
    if ticket.resolved and ticket.app_service_id and snap is not None and ticket.app_service_id != snap.id:
        out.append(
            f"The ticket's dropdowns point at {ticket.app_service_name or 'another target'}, "
            f"Hermes chose {snap.namespace} / {snap.deployment_name}."
        )
    ticket_tag = (ticket.tag or "").strip()
    if plan.change_type == "image" and ticket_tag and ticket_tag != (plan.tag or ""):
        out.append(f"The ticket's Tag field says {ticket_tag}, Hermes chose {plan.tag}.")
    ticket_var = (ticket.variable_name or "").strip()
    if plan.change_type == "env_var":
        if ticket_var and ticket_var.casefold() != (plan.variable or "").casefold():
            out.append(f"The ticket's Variable field says {ticket_var}, Hermes chose {plan.variable}.")
        ticket_val = (ticket.variable_value or "").strip()
        if ticket_val and ticket_val != (plan.value or ""):
            out.append("The ticket's Value field differs from the value Hermes chose.")
    if plan.change_type != "image" and ticket_tag:
        out.append(f"The ticket carries a Tag ({ticket_tag}) but Hermes chose a {plan.change_type.replace('_', ' ')}.")
    return out
