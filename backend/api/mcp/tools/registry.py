"""The one table every tool is registered in, and the dispatch over it.

A tool is a function plus four facts about it: the permission it answers under,
whether it writes, which domain it belongs to, and the schema of its arguments.
Those facts live in the decorator next to the function rather than in a list
somewhere else, because a list somewhere else drifts — and each of them is load
bearing:

**Permission.** Checked against the calling token's user through the same access
engine every HTTP route uses. There is no MCP identity and no service bypass, so
an agent is exactly as privileged as the person whose token it holds. A token
minted without ``apps:deploy`` can read every workload here and change none.

**Write.** Becomes the ``readOnlyHint`` a client reads when it decides whether to
ask a person before calling. A tool that mutated while claiming to be read-only
would take that decision away from them.

**Domain.** What makes a surface this size usable. Tools are grouped the way the
product is — ci, clusters, workloads, deploys, observability, apps, platform —
and the skill that teaches an agent to use them is split the same way, so
answering a question about a build never costs the reader a page about Helm.

**Permission-filtered listing.** ``definitions(user=…)`` advertises only the
tools that user could actually call. This is not the security boundary — ``call``
re-checks, and that is the boundary — it is an economy one: a viewer's agent
should not spend its context reading about tools that will refuse it, and should
not plan around them either.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from ...access_engine import user_has_permission
from ..protocol import ToolError

logger = logging.getLogger(__name__)

# name -> {"permission", "description", "schema", "run", "write", "destructive",
#          "domain", "approval"}
_REGISTRY: Dict[str, Dict[str, Any]] = {}

# The domains, in the order an agent should think about them: what is being
# built, where it runs, what is running, how it gets there, what it is doing,
# what it is, and the machinery underneath.
DOMAINS = (
    "ci",
    "clusters",
    "workloads",
    "deploys",
    "observability",
    "apps",
    "platform",
)

MAX_ROWS = 100
MAX_LOG_LINES = 400
MAX_FILE_LINES = 1200
MAX_FILE_CHARS = 120_000
MAX_TREE_PATHS = 2_000


def tool(
    name: str,
    *,
    permission: str,
    description: str,
    domain: str = "ci",
    schema: Optional[Dict[str, Any]] = None,
    write: bool = False,
    destructive: bool = False,
    approval: str = "",
) -> Callable:
    """Register one tool, the permission it answers under, and what it does.

    ``approval`` names an approval gate the tool passes through when the
    installation has one configured — an empty string means there is none. It is
    documentation for the agent, not enforcement: enforcement lives in the
    service the tool calls, which is the same service the UI calls, so a gate
    cannot be sidestepped by coming in through here.
    """

    def decorate(func: Callable) -> Callable:
        if domain not in DOMAINS:
            raise ValueError(f"'{name}' declares unknown domain '{domain}'.")
        _REGISTRY[name] = {
            "permission": permission,
            "description": description,
            "schema": schema or {"type": "object", "properties": {}},
            "run": func,
            "write": bool(write),
            "destructive": bool(destructive),
            "domain": domain,
            "approval": approval,
        }
        return func

    return decorate


def _limit(arguments: Dict[str, Any], default: int = 25) -> int:
    try:
        return max(1, min(int(arguments.get("limit", default)), MAX_ROWS))
    except (TypeError, ValueError):
        return default


def definitions(user: Any = None) -> List[Dict[str, Any]]:
    """The tool list, in MCP's shape, narrowed to what ``user`` may call.

    Passing no user lists everything — which is what the tests want, and what an
    installation running with authentication off gets.
    """
    out = []
    for name, entry in sorted(_REGISTRY.items()):
        if user is not None and not user_has_permission(user, entry["permission"]):
            continue
        description = entry["description"]
        if entry["approval"]:
            description = f"{description} {entry['approval']}"
        out.append(
            {
                "name": name,
                "description": description,
                "inputSchema": entry["schema"],
                # Declared per tool rather than assumed for the surface: a client
                # that asks a person before a write can only do that if the tools
                # that write say so.
                "annotations": {
                    "readOnlyHint": not entry["write"],
                    "destructiveHint": bool(entry["destructive"]),
                    # Not part of the spec's annotation set, and harmless to a
                    # client that ignores it. A client that reads it can group a
                    # list this long by the same seven words the skill uses.
                    "kubesightDomain": entry["domain"],
                },
            }
        )
    return out


def domain_of(name: str) -> str:
    entry = _REGISTRY.get(name)
    return entry["domain"] if entry else ""


def is_write(name: str) -> bool:
    """Whether a tool changes anything. Read by the route, for the audit row."""
    entry = _REGISTRY.get(name)
    return bool(entry and entry["write"])


def known_names() -> List[str]:
    return sorted(_REGISTRY)


# Keys whose list length is worth saying out loud in the one-line summary. Order
# matters only in that the first match wins, and a payload carrying two of these
# leads with the one that is the answer rather than the one that is context.
_COUNTABLE = (
    "paths", "revisions", "services", "builds", "runners", "artifacts", "stages",
    "environments", "clusters", "namespaces", "nodes", "pods", "events", "lines",
    "alerts", "policies", "workloads", "releases", "applications", "requests",
    "bundles", "entries", "tickets", "runs", "apps", "registries", "users",
    "roles", "components", "clients", "resources", "items",
)


def _summarise(name: str, payload: Any) -> str:
    """One line for a model deciding what to ask next."""
    if not isinstance(payload, dict):
        return f"{name}: done."
    # A write says what it did before it says how big the result is: a model
    # that reads "12 stages" after an edit cannot tell whether the edit landed.
    if payload.get("changed"):
        return f"{name}: saved — {payload['changed']}."
    for key in _COUNTABLE:
        if isinstance(payload.get(key), list):
            return f"{name}: {len(payload[key])} {key}."
    if payload.get("content") is not None and payload.get("path"):
        return f"{name}: {payload['path']} ({payload.get('totalLines', 0)} lines)."
    if "service" in payload and isinstance(payload["service"], dict):
        return f"{name}: {payload['service'].get('slug', 'service')}."
    return f"{name}: ok."


def call(name: str, arguments: Dict[str, Any], *, user) -> Dict[str, Any]:
    """Run one tool as ``user``, or refuse with a reason they can act on."""
    entry = _REGISTRY.get(name)
    if entry is None:
        raise ToolError(
            f"KubeSight has no tool '{name}'. Available: " + ", ".join(sorted(_REGISTRY))
        )

    # The same permission the equivalent HTTP route requires. An agent never
    # sees more than the person whose token it is holding.
    if user is not None and not user_has_permission(user, entry["permission"]):
        raise ToolError(
            f"'{name}' needs the '{entry['permission']}' permission, which this "
            "token does not have."
        )

    # A write is attributed: the service it calls records who did it, and "who"
    # is the token holder, never KubeSight. Read tools do not receive the user
    # at all, so one cannot start acting on their behalf by accident.
    if entry["write"]:
        payload = entry["run"](arguments or {}, user=user)
    else:
        payload = entry["run"](arguments or {})
    return {
        "content": [
            {"type": "text", "text": _summarise(name, payload)},
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)},
        ],
        "structuredContent": payload if isinstance(payload, dict) else {"result": payload},
    }
