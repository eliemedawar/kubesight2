"""The worker's department, built once on the host.

Workflow code runs in Temporal's sandbox, which re-imports workflow modules
for every run. The agents must not be rebuilt there (that reads files and
environment), so workflows import this module passed through and find the one
department the worker installed before it started polling.
"""

from __future__ import annotations

from typing import Optional

from .agents import Department

_department: Optional[Department] = None


def install(department: Department) -> None:
    global _department
    _department = department


def department() -> Department:
    if _department is None:
        raise RuntimeError("No department is installed on this worker.")
    return _department
