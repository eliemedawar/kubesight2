"""Which module runs each stage kind the KubeSight server executes itself.

The engine knows one shape — ``start(build, stage, definition)``,
``advance(build, stage, definition)``, ``cancel(build, stage)`` — and asks this
table which module answers it for a stage's kind. Adding a server stage kind is
a module with those three functions and a line here.

The messages are what the stage says when its executor raised instead of
deciding — an unexpected error, not a refusal. They are per kind because "see
the cluster before retrying" is right for a deploy and wrong for an approval.
"""

from __future__ import annotations

from types import ModuleType
from typing import Dict

from . import approval_stage, deploy_stage, store_upload_stage

EXECUTORS: Dict[str, ModuleType] = {
    "deploy": deploy_stage,
    "approval": approval_stage,
    "store_upload": store_upload_stage,
}

START_FAILED = {
    "deploy": "The Deploy stage could not start; see the server log. Nothing was deployed.",
    "approval": "The Approval stage could not start; see the server log. Nothing after it ran.",
    "store_upload": "The App store upload stage could not start; see the server log. Nothing was published.",
}

ADVANCE_FAILED = {
    "deploy": (
        "The Deploy stage failed unexpectedly while it was running; see the server log. "
        "Check the deployment on the cluster before retrying."
    ),
    "approval": (
        "The Approval stage failed unexpectedly while it was waiting; see the server log. "
        "Nothing after it ran."
    ),
    "store_upload": (
        "The App store upload stage failed unexpectedly while it was running; see the server "
        "log. Check the release in Mobile Apps before retrying — an upload may have started."
    ),
}


def executor(stage_type: str) -> ModuleType:
    """The module that runs this kind. Unknown kinds fall back to Deploy, the
    only server kind that existed before this table — never reached for a
    kind the model does not list."""
    return EXECUTORS.get(stage_type or "", deploy_stage)
