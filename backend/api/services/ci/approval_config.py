"""What an Approval stage is configured to ask for — normalized and checked.

Pure functions, no database: the pipeline validator calls them on save (after
it has resolved user ids to names, see pipelines._approval) and the executor
(``approval_stage.py``) reads the same shape back from the build's snapshot.

An Approval stage holds the build until enough of the right people say yes:

* **who** — named KubeSight users, and/or anyone holding ``ci_builds:approve``;
* **how many** — ``minApprovals`` distinct people (default 1);
* **not yourself** — the person who started the build may not approve it
  unless the stage says so, the same rule as the cluster approval gate;
* **how long** — the stage's own timeout. When it runs out the stage fails
  "not approved in time". Waiting never turns into a silent pass.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

MAX_INSTRUCTIONS_CHARS = 2000
MAX_NAMED_APPROVERS = 25
MAX_MIN_APPROVALS = 10
APPROVE_PERMISSION = "ci_builds:approve"


class ApprovalConfigError(ValueError):
    """An Approval stage's configuration was rejected. Message is user-facing."""


def _user_ids(value: Any, stage_name: str) -> List[int]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ApprovalConfigError(f"Stage '{stage_name}': approvers must be a list of users.")
    ids: List[int] = []
    for item in value:
        raw = item.get("id") if isinstance(item, dict) else item
        try:
            user_id = int(raw)
        except (TypeError, ValueError):
            raise ApprovalConfigError(f"Stage '{stage_name}': '{raw}' is not a user.")
        if user_id <= 0:
            raise ApprovalConfigError(f"Stage '{stage_name}': '{raw}' is not a user.")
        if user_id not in ids:
            ids.append(user_id)
    if len(ids) > MAX_NAMED_APPROVERS:
        raise ApprovalConfigError(
            f"Stage '{stage_name}' names {len(ids)} approvers; the limit is {MAX_NAMED_APPROVERS}. "
            f"Give the group the {APPROVE_PERMISSION} permission instead."
        )
    return ids


def normalize(
    value: Any, stage_type: str, stage_name: str, *, known_users: Optional[Dict[int, str]] = None
) -> Optional[Dict[str, Any]]:
    """An Approval stage's settings, or None for any other stage.

    ``known_users`` maps the ids of active users to their usernames; a named
    approver who is not in it is refused, so a stage can never wait on
    somebody who cannot log in to answer it.
    """
    if stage_type != "approval":
        if value not in (None, "", {}):
            raise ApprovalConfigError(
                f"Stage '{stage_name}' is a {stage_type} stage, so it asks nobody for approval. "
                "Approvers are set on an Approval stage."
            )
        return None
    value = value if isinstance(value, dict) else {}

    instructions = str(value.get("instructions") or "").strip()
    if len(instructions) > MAX_INSTRUCTIONS_CHARS:
        raise ApprovalConfigError(
            f"Stage '{stage_name}': the message to approvers is {len(instructions)} characters; "
            f"the limit is {MAX_INSTRUCTIONS_CHARS}."
        )

    ids = _user_ids(value.get("users"), stage_name)
    users: List[Dict[str, Any]] = []
    known = known_users if known_users is not None else {}
    for user_id in ids:
        if known_users is not None and user_id not in known:
            raise ApprovalConfigError(
                f"Stage '{stage_name}': user #{user_id} is not an active KubeSight user, so they "
                "could never answer this approval. Pick someone else."
            )
        users.append({"id": user_id, "username": known.get(user_id) or f"user #{user_id}"})

    anyone = bool(value.get("anyoneWithPermission"))
    if not users and not anyone:
        raise ApprovalConfigError(
            f"Stage '{stage_name}' has nobody who may approve it. Name the approvers, or let "
            f"anyone with the {APPROVE_PERMISSION} permission approve."
        )

    raw_min = value.get("minApprovals")
    try:
        minimum = int(raw_min) if raw_min not in (None, "") else 1
    except (TypeError, ValueError):
        raise ApprovalConfigError(f"Stage '{stage_name}': the number of approvals must be a whole number.")
    if not 1 <= minimum <= MAX_MIN_APPROVALS:
        raise ApprovalConfigError(
            f"Stage '{stage_name}': the number of approvals must be between 1 and {MAX_MIN_APPROVALS}."
        )
    if not anyone and minimum > len(users):
        raise ApprovalConfigError(
            f"Stage '{stage_name}' needs {minimum} approvals but names only {len(users)} "
            f"approver{'s' if len(users) != 1 else ''}, so it could never pass. Name more people, "
            "lower the number, or let permission holders approve too."
        )

    return {
        "instructions": instructions,
        "users": users,
        "anyoneWithPermission": anyone,
        "minApprovals": minimum,
        # Off unless asked for: the person who started a build approving it
        # themselves is a second pair of eyes that is the first pair again.
        "allowSelfApproval": bool(value.get("allowSelfApproval")),
        "notify": bool(value.get("notify")),
    }


def describe_approvers(config: Dict[str, Any]) -> str:
    """'alice, bob or anyone with ci_builds:approve' — who may answer, in words."""
    names = [str(user.get("username") or f"user #{user.get('id')}") for user in config.get("users") or []]
    parts = []
    if names:
        parts.append(", ".join(names))
    if config.get("anyoneWithPermission"):
        parts.append(f"anyone with the {APPROVE_PERMISSION} permission")
    return " or ".join(parts) or "nobody"
