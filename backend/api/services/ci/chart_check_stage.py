"""The Chart check stage: render the service's chart, at the commit being
built, with its values file, and check it against the platform rules.

KubeSight runs it itself (a server stage, no container). It is what makes a
chart change safe before it reaches a cluster: in a build it stops the
pipeline before the Deploy stage; in a pull request's merge check it reports
to the quality gate, through the same one-line contract every other check
uses (``##kubesight-metric tool=chart ...``, see merge_checks/metrics.py).

Its settings are the stage's own environment, so a stage needs no new column:

* ``KUBESIGHT_CHART_PATH``  the chart folder in the repository (required);
* ``KUBESIGHT_VALUES_PATH`` the values file layered over the chart's defaults;
* ``KUBESIGHT_CHART_POLICY`` the rule set (chart_policy.POLICIES), or empty to
  check only that the chart renders.

A problem is a rule broken (FAIL) or a chart that does not render. Advice
(WARN) is written to the log and never fails the stage.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

from ...db import db
from ...models_ci import CiBuild, CiBuildStage, CiService
from . import chart_policy
from . import logs as logs_service
from . import server_stage_base as base

logger = logging.getLogger(__name__)

PREFIX = "[chart check]"
TOOL = "chart"
ENV_CHART = "KUBESIGHT_CHART_PATH"
ENV_VALUES = "KUBESIGHT_VALUES_PATH"
ENV_POLICY = "KUBESIGHT_CHART_POLICY"
_RENDER_TIMEOUT = 120


def start(build: CiBuild, stage: CiBuildStage, definition: Dict[str, Any]) -> None:
    base.begin(stage, {"kind": "chart_check", "phase": "checking", "outcome": None, "message": ""})
    _check(build, stage, definition)


def advance(build: CiBuild, stage: CiBuildStage, definition: Dict[str, Any]) -> None:
    # The check runs to the end inside start(); a stage still "checking" here
    # was cut off by a restart, so it simply runs again.
    if base.state(stage).get("phase") == "checking":
        _check(build, stage, definition)


def cancel(build: CiBuild, stage: CiBuildStage) -> None:
    base.finish(stage, "cancelled", "Cancelled before the chart was checked.", outcome="cancelled")


def summarize(stage: CiBuildStage) -> str:
    return str(base.state(stage).get("message") or "")


def _settings(definition: Dict[str, Any]) -> Tuple[str, str, str]:
    env = definition.get("env") if isinstance(definition.get("env"), dict) else {}
    return (
        str(env.get(ENV_CHART) or "").strip(),
        str(env.get(ENV_VALUES) or "").strip(),
        str(env.get(ENV_POLICY) or "").strip().lower(),
    )


def _check(build: CiBuild, stage: CiBuildStage, definition: Dict[str, Any]) -> None:
    from .. import helm_chart_template_service as charts

    chart_path, values_path, policy = _settings(definition)
    if not chart_path:
        _error(stage, f"This Chart check stage names no chart folder ({ENV_CHART}).")
        return
    service = db.session.get(CiService, build.service_id)
    if service is None or not service.repository_url:
        _error(stage, "The service has no repository to take the chart from.")
        return
    commit = build.commit_sha or ""
    at = commit[:12] if commit else f"the tip of {build.branch or service.default_branch}"
    try:
        taken = charts.prepare_repository_chart(
            repository_url=service.repository_url,
            credential_profile_id=service.credential_profile_id,
            commit=commit,
            branch=build.branch or service.default_branch or "",
            chart_path=chart_path,
            values_path=values_path,
        )
    except charts.ChartTemplateError as exc:
        _problems(stage, 1, 0, f"The chart could not be read at {at}: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 — Git or the disk; the stage reports it
        logger.exception("chart check could not read the chart for build %s", build.id)
        _error(stage, f"The chart could not be read ({type(exc).__name__}).")
        return

    _log(stage, f"Rendering {chart_path} at {at}" + (f" with {values_path}" if values_path else "") + ".")
    rendered, problem = render(taken["prepared"]["files"], taken["values"], namespace=_namespace(definition))
    if problem:
        _problems(stage, 1, 0, f"The chart does not render: {problem}")
        return
    count = sum(1 for _ in chart_policy._documents(rendered or ""))
    if not policy:
        _log(stage, f"The chart renders: {count} objects. No rule set is checked.")
        _report(stage, 0, 0)
        _done(stage, "success", f"The chart renders ({count} objects).")
        return
    findings = chart_policy.check(policy, rendered or "")
    fails, warns = chart_policy.summary(findings)
    _log(stage, f"{count} objects checked against {chart_policy.label(policy)}: {fails} FAIL, {warns} WARN.")
    for finding in findings[:60]:
        _log(stage, "  " + chart_policy.describe(finding))
    if fails:
        _problems(stage, fails, warns, f"The chart breaks {fails} of {chart_policy.label(policy)}.")
        return
    _report(stage, 0, warns)
    _done(stage, "success", f"The chart keeps {chart_policy.label(policy)}" + (f" ({warns} advice)." if warns else "."))


def _namespace(definition: Dict[str, Any]) -> str:
    env = definition.get("env") if isinstance(definition.get("env"), dict) else {}
    return str(env.get("KUBESIGHT_CHART_NAMESPACE") or "default").strip() or "default"


def render(files: Dict[str, Any], values: Dict[str, Any], *, namespace: str = "default") -> Tuple[Optional[str], str]:
    """``helm template`` a chart held as {path: bytes} with ``values`` over its
    defaults, on this server (no cluster). Returns ``(manifests, "")`` or
    ``(None, why)``."""
    from ..helm_service import _helm_binary

    with tempfile.TemporaryDirectory(prefix="kubesight-chart-") as temp:
        chart_dir = Path(temp) / "chart"
        for name, content in (files or {}).items():
            relative = str(name).replace("\\", "/").lstrip("/")
            if not relative or ".." in relative.split("/"):
                continue
            target = chart_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            data = content if isinstance(content, bytes) else str(content).encode("utf-8")
            target.write_bytes(data)
        values_file = Path(temp) / "values.yaml"
        values_file.write_text(yaml.safe_dump(values or {}, sort_keys=False), encoding="utf-8")
        command = [_helm_binary(), "template", "chart-check", str(chart_dir),
                   "--namespace", namespace, "-f", str(values_file)]
        env = {key: value for key, value in os.environ.items() if key != "KUBECONFIG"}
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=False,
                                       timeout=_RENDER_TIMEOUT, env=env)
        except FileNotFoundError:
            return None, "helm is not installed on the KubeSight server."
        except subprocess.TimeoutExpired:
            return None, f"helm template took longer than {_RENDER_TIMEOUT} seconds."
    if completed.returncode != 0:
        return None, (completed.stderr or completed.stdout or "helm template failed").strip()[-1500:]
    return completed.stdout, ""


def _log(stage: CiBuildStage, message: str) -> None:
    base.log(stage, PREFIX, message)


def _report(stage: CiBuildStage, fails: int, warns: int, status: str = "ok", message: str = "") -> None:
    """The line the merge check gate reads (merge_checks/metrics.py)."""
    from .merge_checks.metrics import SENTINEL

    extra = f' message="{message[:200]}"' if message else ""
    logs_service.append_system(
        stage, f"{SENTINEL} tool={TOOL} status={status} problems={fails} errors={fails} warnings={warns}{extra}",
        commit=False,
    )


def _problems(stage: CiBuildStage, fails: int, warns: int, message: str) -> None:
    _log(stage, message)
    _report(stage, fails, warns)
    _done(stage, "failed", message)


def _error(stage: CiBuildStage, message: str) -> None:
    """The check could not run: the gate sees a broken tool, not a clean chart."""
    _log(stage, message)
    _report(stage, 0, 0, status="error", message=message.replace('"', "'"))
    _done(stage, "failed", message)


def _done(stage: CiBuildStage, status: str, message: str) -> None:
    base.save(stage, {"message": message})
    base.finish(stage, status, message, outcome="passed" if status == "success" else "failed")
