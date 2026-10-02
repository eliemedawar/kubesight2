"""Which tools the MCP server will serve at all — the switches in Settings → MCP tools.

This is a ceiling over every token, never a grant. A tool switched on here still
needs the permission its route needs, so turning a write tool on cannot hand a
viewer's token a write. What it buys is the other direction: an administrator
can take a tool away from every agent at once — Hermes included — without
re-minting a token or editing a role that people also use in the UI.

Two layers, each only ever narrowing the one above it:

1. **Tools.** Each tool on or off, plus a master switch for the whole server.
2. **Clusters.** Per cluster, a mode: ``full`` (follow the tool switches),
   ``read`` (no tool that changes something may act on it), ``off`` (agents
   cannot see it or name it), or ``custom`` (its own list of switched-off
   tools). A tool off everywhere stays off on every cluster.

Decisions worth keeping:

* **Stored as what is OFF.** A missing row, a cluster with no rule, or a tool a
  later release adds all answer "on", which is how the server behaved before
  these switches existed. An installation that never opens the screen sees no
  change.
* **Off means absent.** A switched-off tool is dropped from ``tools/list`` as
  well as refused at ``tools/call``, and an ``off`` cluster is dropped from
  every cluster listing, so an agent does not plan around what it will be
  refused. The refusal still names the switch, for an agent holding a stale list.
* **The cluster check sits where a cluster is decided, not in each tool.**
  ``common.resolve_cluster`` / ``visible_clusters`` for the tools that name a
  cluster, and ``deploy_automation_service.start_run`` for a ticket's deploy
  target. They ask :func:`check_cluster`, which reads the tool being called from
  a context variable the MCP route sets — outside an MCP call it is a no-op, so
  the UI and the deploy automation are never affected by an agent's rules.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional

from ..db import db
from ..models import McpAccessSettings
from .protocol import ToolError
from . import tools

# Tools the Hermes ticket agent cannot work without. Switching one off while the
# agent is on leaves every inbound ticket stuck, so the screen says so first.
TICKET_AGENT_TOOLS = frozenset(
    name for name in tools.known_names() if name.startswith("kubesight_ticket_")
)

CLUSTER_MODES = ("full", "read", "off", "custom")


class AccessError(ValueError):
    """A save that names something that is not a tool, cluster mode, or rule."""


@dataclass(frozen=True)
class ClusterRule:
    mode: str = "full"
    disabled: FrozenSet[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Policy:
    enabled: bool = True
    disabled: FrozenSet[str] = field(default_factory=frozenset)
    clusters: Mapping[str, ClusterRule] = field(default_factory=dict)

    def allows(self, name: str) -> bool:
        return self.enabled and name not in self.disabled

    def refusal(self, name: str) -> str:
        if not self.enabled:
            return (
                "KubeSight's MCP server is switched off by an administrator "
                "(Settings → MCP tools). No tool can be called until it is switched back on."
            )
        return (
            f"'{name}' is switched off by an administrator (Settings → MCP tools). "
            "It cannot be called through MCP until it is switched back on."
        )

    def allows_on_cluster(self, name: str, cluster_id: Any) -> bool:
        """Whether ``name`` may touch ``cluster_id``. Assumes ``allows(name)``."""
        rule = self.clusters.get(str(cluster_id))
        if rule is None or rule.mode == "full":
            return True
        if rule.mode == "off":
            return False
        if rule.mode == "read":
            return not tools.is_write(name)
        return name not in rule.disabled  # custom

    def cluster_refusal(self, name: str, cluster_id: Any) -> str:
        rule = self.clusters.get(str(cluster_id)) or ClusterRule()
        if rule.mode == "off":
            return (
                f"Cluster '{cluster_id}' is closed to agents by an administrator "
                "(Settings → MCP tools → Clusters). No tool can read or change it."
            )
        if rule.mode == "read":
            return (
                f"Cluster '{cluster_id}' is read only for agents (Settings → MCP tools → "
                f"Clusters), so '{name}', which changes something, cannot act on it. "
                "A person can make this change from the KubeSight UI."
            )
        return (
            f"'{name}' is switched off for cluster '{cluster_id}' by an administrator "
            "(Settings → MCP tools → Clusters)."
        )


# ---------------------------------------------------------------------------
# Reading and saving
# ---------------------------------------------------------------------------

def _rules_from(raw: Any) -> Dict[str, ClusterRule]:
    out: Dict[str, ClusterRule] = {}
    if not isinstance(raw, dict):
        return out
    for cluster_id, rule in raw.items():
        if not isinstance(rule, dict):
            continue
        mode = str(rule.get("mode") or "full")
        if mode not in CLUSTER_MODES or mode == "full":
            continue
        disabled = frozenset(
            name for name in (rule.get("disabledTools") or []) if isinstance(name, str)
        )
        out[str(cluster_id)] = ClusterRule(mode=mode, disabled=disabled)
    return out


def current() -> Policy:
    """The policy in force. Read-only: a missing row is the default, not created."""
    row = db.session.get(McpAccessSettings, 1)
    if row is None:
        return Policy()
    return Policy(
        enabled=bool(row.enabled),
        disabled=frozenset(row.disabled_tools or []),
        clusters=_rules_from(row.cluster_rules),
    )


def _ticket_agent_on() -> bool:
    from ..models_ticket_agent import TicketAgentSettings

    row = db.session.get(TicketAgentSettings, 1)
    return bool(row and row.enabled)


def _known_clusters() -> List[Dict[str, Any]]:
    """Every cluster KubeSight knows, for the screen — unfiltered by user, like
    the tool list: the administrator is deciding for every token."""
    from .tools.common import cluster_items

    seen = set()
    out = []
    for item in cluster_items():
        cluster_id = str(item.get("id") or "").strip()
        if not cluster_id or cluster_id in seen:
            continue
        seen.add(cluster_id)
        out.append(
            {
                "id": cluster_id,
                "name": str(item.get("name") or cluster_id),
                "environment": item.get("environment") or item.get("env") or None,
            }
        )
    return out


def payload() -> Dict[str, Any]:
    row = db.session.get(McpAccessSettings, 1)
    policy = current()
    catalog = tools.catalog()
    for entry in catalog:
        entry["enabled"] = entry["name"] not in policy.disabled
        entry["usedByTicketAgent"] = entry["name"] in TICKET_AGENT_TOOLS

    clusters = _known_clusters()
    known = {item["id"] for item in clusters}
    # A rule for a cluster that is not reachable right now is still a rule; it
    # is listed so it can be seen and removed rather than silently kept.
    for cluster_id in sorted(set(policy.clusters) - known):
        clusters.append({"id": cluster_id, "name": cluster_id, "environment": None, "missing": True})
    for item in clusters:
        rule = policy.clusters.get(item["id"]) or ClusterRule()
        item["mode"] = rule.mode
        item["disabledTools"] = sorted(rule.disabled)

    return {
        "enabled": policy.enabled,
        "tools": catalog,
        "domains": list(tools.DOMAINS),
        "clusters": clusters,
        "clusterModes": list(CLUSTER_MODES),
        "ticketAgentEnabled": _ticket_agent_on(),
        "updatedAt": row.updated_at.isoformat() if row is not None and row.updated_at else None,
        "updatedBy": getattr(getattr(row, "updated_by", None), "username", None),
    }


def _clean_cluster_rules(requested: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(requested, dict):
        raise AccessError("clusterRules must be an object keyed by cluster id.")
    scoped = {name for name in tools.known_names() if tools.is_cluster_scoped(name)}
    out: Dict[str, Dict[str, Any]] = {}
    for cluster_id, rule in requested.items():
        cluster_id = str(cluster_id).strip()
        if not cluster_id or not isinstance(rule, dict):
            raise AccessError("Each cluster rule needs a cluster id and a mode.")
        mode = str(rule.get("mode") or "full")
        if mode not in CLUSTER_MODES:
            raise AccessError(f"'{mode}' is not a cluster mode ({', '.join(CLUSTER_MODES)}).")
        if mode == "full":
            continue  # the default; storing it would only be noise
        names = rule.get("disabledTools") or []
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise AccessError("disabledTools must be a list of tool names.")
        not_scoped = sorted(set(names) - scoped)
        if not_scoped:
            raise AccessError(
                "Only tools that work on a cluster can be switched per cluster: "
                + ", ".join(not_scoped)
            )
        out[cluster_id] = {
            "mode": mode,
            # Kept only for custom — a read/off rule carrying a stale list would
            # resurface the day somebody switches it back to custom.
            "disabledTools": sorted(set(names)) if mode == "custom" else [],
        }
    return out


def update(data: Dict[str, Any], *, actor: Any) -> Dict[str, Any]:
    """Apply a save and return what changed, for the audit row.

    Lists are whole, not diffs: the screen holds the full state, and a full list
    cannot be half-applied by two people saving at once.
    """
    row = db.session.get(McpAccessSettings, 1)
    if row is None:
        row = McpAccessSettings(id=1, enabled=True, disabled_tools=[], cluster_rules={})
        db.session.add(row)

    before = current()
    changes: Dict[str, Any] = {}

    if "enabled" in data:
        row.enabled = bool(data.get("enabled"))
        if row.enabled != before.enabled:
            changes["enabled"] = row.enabled

    if "disabledTools" in data:
        requested = data.get("disabledTools")
        if not isinstance(requested, list) or not all(isinstance(n, str) for n in requested):
            raise AccessError("disabledTools must be a list of tool names.")
        known = set(tools.known_names())
        unknown = sorted(set(requested) - known)
        if unknown:
            raise AccessError("Not a KubeSight tool: " + ", ".join(unknown))
        wanted = set(requested)
        row.disabled_tools = sorted(wanted)
        turned_off = sorted(wanted - before.disabled)
        turned_on = sorted(before.disabled - wanted)
        if turned_off:
            changes["turnedOff"] = turned_off
        if turned_on:
            changes["turnedOn"] = turned_on

    if "clusterRules" in data:
        cleaned = _clean_cluster_rules(data.get("clusterRules"))
        row.cluster_rules = cleaned
        after = _rules_from(cleaned)
        moved = {}
        for cluster_id in sorted(set(before.clusters) | set(after)):
            old = before.clusters.get(cluster_id) or ClusterRule()
            new = after.get(cluster_id) or ClusterRule()
            if old == new:
                continue
            entry: Dict[str, Any] = {"from": old.mode, "to": new.mode}
            if new.mode == "custom":
                entry["toolsOff"] = sorted(new.disabled)
            moved[cluster_id] = entry
        if moved:
            changes["clusters"] = moved

    row.updated_by_user_id = getattr(actor, "id", None)
    db.session.commit()
    return changes


def filter_definitions(definitions: List[Dict[str, Any]], policy: Optional[Policy] = None) -> List[Dict[str, Any]]:
    policy = policy or current()
    return [entry for entry in definitions if policy.allows(entry["name"])]


# ---------------------------------------------------------------------------
# The call in progress, for the cluster checks deeper down
# ---------------------------------------------------------------------------

_CALL: contextvars.ContextVar = contextvars.ContextVar("kubesight_mcp_call", default=None)


@contextmanager
def calling(policy: Policy, name: str):
    """Mark the duration of one MCP tool call, so cluster checks know the tool."""
    token = _CALL.set((policy, name))
    try:
        yield
    finally:
        _CALL.reset(token)


def check_cluster(cluster_id: Any) -> None:
    """Refuse if the tool being called through MCP may not touch this cluster.

    A no-op outside an MCP call: the UI, the ticket automation's own runs and
    the tests that call services directly are never subject to an agent's rules.
    """
    call = _CALL.get()
    if call is None or cluster_id in (None, ""):
        return
    policy, name = call
    if not policy.allows_on_cluster(name, cluster_id):
        raise ToolError(policy.cluster_refusal(name, cluster_id))


def allows_cluster(cluster_id: Any) -> bool:
    """Whether the tool being called may see this cluster. True outside MCP."""
    call = _CALL.get()
    if call is None:
        return True
    policy, name = call
    return policy.allows_on_cluster(name, cluster_id)


def keep_allowed(rows: Iterable[Any], key: str = "clusterId") -> List[Any]:
    """Rows from a cross-cluster listing, minus the clusters this call may not see."""
    rows = list(rows or [])
    if _CALL.get() is None:
        return rows
    return [
        row for row in rows
        if not isinstance(row, dict) or row.get(key) in (None, "") or allows_cluster(row.get(key))
    ]
