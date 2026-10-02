"""The code scan quality gate and its PDF report.

What is locked here:

* the gate's configuration rules (command stages only, a whole-number
  allowance, known severities, real email addresses);
* the count: every Semgrep finding blocks unless Semgrep says otherwise, and
  the severity floor decides which ones are counted;
* the wrapped stage script, run for real with a stand-in ``semgrep``: the
  stage's own command is unchanged, the results land in the report file, and
  the stage fails exactly when the blocking count passes the allowance;
* the report is collected on Kubernetes whether the gate passed or failed;
* the PDF is built from the stored results and says what the build decided;
* sending is a person's choice of recipients, audited, with the PDF attached.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api.db import db
from api.models_ci import CiBuild, CiBuildStage, CiService
from api.services.ci import code_scan, code_scan_gate as gate, code_scan_report
from api.services.ci.pipelines import PipelineError, normalize_stage
from api.services.ci.runners import kubernetes as k8s
from api.services.ci.runners.base import StageExecution
from tests.conftest import auth_headers


def _finding(rule="js.xss.innerhtml", severity="ERROR", path="src/app.ts", line=3, **extra):
    return {
        "check_id": f"javascript.lang.security.{rule}",
        "path": path,
        "start": {"line": line, "col": 1},
        "end": {"line": line, "col": 20},
        "extra": {
            "severity": severity,
            "message": f"Finding from {rule}.",
            "lines": "requires login",
            "metadata": {"cwe": ["CWE-79: Cross-site Scripting"], "references": ["https://owasp.org/x"]},
            **extra,
        },
    }


def _report(*results):
    return {
        "version": "1.99.0",
        "results": list(results),
        "errors": [],
        "paths": {"scanned": ["src/app.ts", "src/b.ts"]},
    }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_no_gate_is_none_and_a_gate_is_normalized():
    assert code_scan.normalize(None, "command", "Scan") is None
    assert code_scan.normalize({}, "command", "Scan") is None
    config = code_scan.normalize(
        {"maxBlocking": "5", "countFrom": "WARNING", "recipients": "a@x.com, b@y.org; a@x.com"},
        "command",
        "Scan",
    )
    assert config == {
        "enabled": True,
        "tool": "semgrep",
        "maxBlocking": 5,
        "countFrom": "warning",
        "recipients": ["a@x.com", "b@y.org"],
    }


def test_gate_is_refused_off_a_command_stage():
    with pytest.raises(code_scan.CodeScanConfigError, match="command stage that runs the scanner"):
        code_scan.normalize({"maxBlocking": 0}, "container_image", "Build image")


@pytest.mark.parametrize(
    "value, message",
    [
        ({"maxBlocking": -1}, "between 0 and"),
        ({"maxBlocking": "lots"}, "whole number"),
        ({"countFrom": "critical"}, "count findings from"),
        ({"tool": "sonar"}, "supports semgrep"),
        ({"recipients": ["not-an-email"]}, "is not an email address"),
    ],
)
def test_gate_rejects_bad_values(value, message):
    with pytest.raises(code_scan.CodeScanConfigError, match=message):
        code_scan.normalize(value, "command", "Scan")


def test_pipeline_save_carries_the_gate_and_reports_its_errors():
    stage = normalize_stage(
        {"name": "Scan", "stageType": "command", "commands": ["semgrep scan"], "codeScan": {"maxBlocking": 3}},
        0,
        set(),
    )
    assert stage["code_scan"]["maxBlocking"] == 3
    with pytest.raises(PipelineError) as excinfo:
        normalize_stage(
            {"name": "Scan", "stageType": "command", "commands": ["x"], "codeScan": {"maxBlocking": -4}},
            0,
            set(),
        )
    assert excinfo.value.code == "invalid_code_scan"


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------

def test_every_finding_blocks_unless_semgrep_says_otherwise():
    report = _report(
        _finding(),
        _finding(severity="WARNING"),
        _finding(severity="INFO"),
        _finding(is_ignored=True),
        _finding(is_blocking=False),
    )
    summary = gate.summarize(report, "info")
    assert summary["total"] == 5
    assert summary["blocking"] == 3
    assert summary["notBlocking"] == 2
    assert summary["blockingBySeverity"] == {"error": 1, "warning": 1, "info": 1}


def test_severity_floor_decides_what_is_counted():
    report = _report(_finding(), _finding(severity="WARNING"), _finding(severity="INFO"), _finding(severity="HIGH"))
    assert gate.summarize(report, "info")["blocking"] == 4
    assert gate.summarize(report, "warning")["blocking"] == 3
    assert gate.summarize(report, "error")["blocking"] == 2  # ERROR + HIGH


def test_verdict_fails_only_above_the_allowance():
    summary = gate.summarize(_report(_finding(), _finding(), _finding()), "info")
    assert gate.verdict(summary, 3) == "passed"
    assert gate.verdict(summary, 2) == "failed"
    assert gate.verdict(summary, 0) == "failed"


def test_gate_stamps_the_report_and_copies_code_excerpts(tmp_path, monkeypatch, capsys):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("a\nb\nel.innerHTML = x;\nd\ne\n", encoding="utf-8")
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(_report(_finding(), _finding(rule="hardcoded-secret", line=2))), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    assert gate.main([str(report_path), "0", "info"]) == 1
    out = capsys.readouterr().out
    assert "Quality gate FAILED: 2 blocking findings, more than the 0 allowed." in out

    stamped = json.loads(report_path.read_text(encoding="utf-8"))
    assert stamped["kubesight"]["gate"] == {"maxBlocking": 0, "countFrom": "info", "verdict": "failed"}
    snippet = stamped["results"][0]["extra"]["kubesightSnippet"]
    assert snippet["firstLine"] == 1 and "el.innerHTML = x;" in snippet["lines"]
    # A finding about a secret never has its line copied into a file that gets emailed.
    assert stamped["results"][1]["extra"]["kubesightSnippet"] == {"withheld": True}

    assert gate.main([str(report_path), "5", "info"]) == 0


# ---------------------------------------------------------------------------
# The wrapped script, run for real
# ---------------------------------------------------------------------------

_FAKE_SEMGREP = r"""#!/bin/sh
# Stand-in for semgrep: print a summary, and write the JSON wherever
# --json-output says, with as many ERROR findings as $FAKE_FINDINGS.
out=""
for arg in "$@"; do
  case "$arg" in --json-output=*) out="${arg#--json-output=}" ;; esac
done
echo "Scan completed successfully."
if [ -n "$out" ]; then
  python3 - "$out" "${FAKE_FINDINGS:-0}" <<'PY'
import json, sys
n = int(sys.argv[2])
results = [{"check_id": "r.rule%d" % i, "path": "x.js", "start": {"line": 1}, "end": {"line": 1},
            "extra": {"severity": "ERROR", "message": "m"}} for i in range(n)]
json.dump({"results": results, "errors": [], "paths": {"scanned": ["x.js"]}}, open(sys.argv[1], "w"))
PY
fi
exit "${FAKE_EXIT:-0}"
"""


def _shell_available():
    if not shutil.which("sh"):
        return False
    probe = subprocess.run(
        ["sh", "-c", "command -v python3 >/dev/null && python3 -c 'import json'"],
        capture_output=True,
    )
    return probe.returncode == 0


def _run_script(tmp_path, script, *, cwd, env):
    """Run the stage body the way a runner does: under ``set -e``. From a file,
    because a long ``sh -c`` argument does not survive Windows' command line."""
    path = tmp_path / "stage.sh"
    path.write_text("set -e\n" + script, encoding="utf-8", newline="\n")
    return subprocess.run(["sh", str(path)], cwd=cwd, env=env, capture_output=True, text=True)


@pytest.mark.skipif(not _shell_available(), reason="needs sh and python3")
@pytest.mark.parametrize(
    "findings, allowed, fake_exit, expect_rc",
    [
        (3, 5, 0, 0),   # within the allowance
        (3, 2, 0, 1),   # over it
        (0, 0, 0, 0),   # a clean scan with a zero allowance
        (3, 5, 1, 0),   # semgrep --error exits 1 on findings: the gate decides, not it
        (0, 5, 2, 2),   # semgrep itself broke: the stage fails with its code
    ],
)
def test_wrapped_stage_fails_exactly_when_the_gate_says(tmp_path, findings, allowed, fake_exit, expect_rc):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "semgrep"
    fake.write_text(_FAKE_SEMGREP, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "x.js").write_text("eval(x)\n", encoding="utf-8")

    config = {"enabled": True, "maxBlocking": allowed, "countFrom": "info"}
    # The stage's command, unchanged from what somebody would write by hand.
    [script] = code_scan.wrap_commands(["semgrep scan --config p/javascript ."], config, 3)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "KUBESIGHT_WORKSPACE": str(workspace).replace("\\", "/"),
        "FAKE_FINDINGS": str(findings),
        "FAKE_EXIT": str(fake_exit),
    }
    result = _run_script(tmp_path, script, cwd=workspace, env=env)
    assert result.returncode == expect_rc, result.stdout + result.stderr
    report = workspace / ".kubesight" / "code-scan-3.json"
    if expect_rc != 2:
        assert report.exists()
        assert json.loads(report.read_text())["kubesight"]["gate"]["maxBlocking"] == allowed
        assert "== quality gate ==" in result.stdout


@pytest.mark.skipif(not _shell_available(), reason="needs sh and python3")
def test_a_stage_that_never_ran_semgrep_fails_the_gate(tmp_path):
    [script] = code_scan.wrap_commands(["echo not scanning"], {"maxBlocking": 5}, 0)
    env = {**os.environ, "KUBESIGHT_WORKSPACE": str(tmp_path).replace("\\", "/")}
    result = _run_script(tmp_path, script, cwd=tmp_path, env=env)
    assert result.returncode == 1
    assert "Semgrep saved no results" in result.stderr


# ---------------------------------------------------------------------------
# Runner wiring
# ---------------------------------------------------------------------------

def _execution(position, stage_type="command", **kw):
    return StageExecution(
        build_id=7, build_number=3, stage_id=100 + position, service_slug="acquiring-ui",
        stage_name=f"Stage {position}", stage_type=stage_type, image="semgrep/semgrep",
        working_directory=None, commands=kw.get("commands", ["semgrep scan ."]), env={},
        position=position, workspace_ref="acquiring-ui-3",
        repository_url="https://bitbucket.org/areeba/acquiring-ui.git", branch="develop",
        code_scan=kw.get("code_scan"), callback_url="http://backend:5000/api/ci/worker",
        callback_token="t",
    )


def _collector_specs(job):
    collector = next(c for c in job["spec"]["template"]["spec"]["containers"] if c["name"] == "collector")
    env = {entry["name"]: entry.get("value") for entry in collector["env"]}
    return json.loads(env["KUBESIGHT_ARTIFACTS"])


def test_kubernetes_keeps_the_report_of_a_gated_stage_only():
    gated = _execution(1, code_scan={"enabled": True, "maxBlocking": 0})
    plain = _execution(2)
    gated.plan = [gated, plain]
    _, _, job = k8s.build_job_resources(gated)
    specs = _collector_specs(job)
    assert specs == [
        {
            "path": "/workspace/.kubesight/code-scan-1.json",
            "type": "scan-report",
            "name": "code-scan-stage-2.json",
            "workdir": "",
            "stagePosition": 1,
        }
    ]


# ---------------------------------------------------------------------------
# Report, download and send
# ---------------------------------------------------------------------------

@pytest.fixture()
def gated_build(app, tmp_path, monkeypatch):
    """A finished build whose stage 2 ran under a gate and saved its results."""
    from api.services.ci import artifacts as artifacts_service
    from api.services.ci.runners.base import ArtifactRef

    monkeypatch.setenv("CI_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    report = _report(*[_finding(line=i + 1) for i in range(8)], _finding(severity="WARNING", path="src/b.ts"))
    report["kubesight"] = {"gate": {"maxBlocking": 5, "countFrom": "info", "verdict": "failed"}}
    report_file = tmp_path / "code-scan-1.json"
    report_file.write_text(json.dumps(report), encoding="utf-8")

    with app.app_context():
        service = CiService(name="Acquiring-UI", slug="acquiring-ui")
        db.session.add(service)
        db.session.flush()
        gate_config = {"enabled": True, "tool": "semgrep", "maxBlocking": 5, "countFrom": "info",
                       "recipients": ["lead@areeba.com"]}
        build = CiBuild(
            service_id=service.id, number=25, status="failed", branch="2.13.15",
            commit_sha="b62fa09d" * 5,
            pipeline_snapshot={"stages": [
                {"name": "Checkout", "stageType": "checkout"},
                {"name": "Scan Source Code", "stageType": "command", "codeScan": gate_config},
            ]},
        )
        db.session.add(build)
        db.session.flush()
        db.session.add(CiBuildStage(build_id=build.id, position=0, name="Checkout", status="success"))
        stage = CiBuildStage(build_id=build.id, position=1, name="Scan Source Code", status="failed")
        db.session.add(stage)
        db.session.flush()
        artifacts_service.record_artifact(
            service_id=service.id, build_id=build.id, build_stage_id=stage.id,
            ref=ArtifactRef(name="code-scan-stage-2.json", artifact_type="scan-report",
                            local_path=str(report_file)),
            commit=True,
        )
        return SimpleNamespace(build_id=build.id, stage_id=stage.id)


def test_build_says_which_stage_ran_under_a_gate(client, admin_token, gated_build):
    data = client.get(f"/api/ci/builds/{gated_build.build_id}", headers=auth_headers(admin_token)).get_json()["data"]
    assert data["stages"][0]["codeScan"] is None
    assert data["stages"][1]["codeScan"] == {"tool": "semgrep", "maxBlocking": 5, "countFrom": "info"}


def test_overview_reports_what_the_build_decided(client, admin_token, gated_build):
    response = client.get(
        f"/api/ci/builds/{gated_build.build_id}/stages/{gated_build.stage_id}/code-scan",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["reportAvailable"] is True
    assert data["verdict"] == "failed"
    assert data["blocking"] == 9 and data["maxBlocking"] == 5
    assert data["recipients"] == ["lead@areeba.com"]


def test_pdf_is_a_real_document(app, gated_build):
    with app.app_context():
        build = db.session.get(CiBuild, gated_build.build_id)
        stage = db.session.get(CiBuildStage, gated_build.stage_id)
        pdf = code_scan_report.render_for_stage(build, stage)
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 3000


def test_pdf_downloads_with_a_ticket(client, admin_token, gated_build):
    base = f"/api/ci/builds/{gated_build.build_id}/stages/{gated_build.stage_id}/code-scan"
    ticket = client.post(f"{base}/report-ticket", headers=auth_headers(admin_token)).get_json()["data"]["ticket"]
    response = client.get(f"{base}/report?ticket={ticket}")
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert "acquiring-ui-build-25-code-scan.pdf" in response.headers["Content-Disposition"]
    assert response.data.startswith(b"%PDF-")


def test_send_emails_the_pdf_to_the_people_chosen(client, admin_token, gated_build):
    base = f"/api/ci/builds/{gated_build.build_id}/stages/{gated_build.stage_id}/code-scan"
    with patch("api.email_delivery.smtp_is_configured", return_value=True), patch(
        "api.email_delivery.send_email"
    ) as sent:
        response = client.post(
            f"{base}/send",
            json={"recipients": ["dev@areeba.com", "lead@areeba.com"], "note": "Please fix before Friday."},
            headers=auth_headers(admin_token),
        )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["sentTo"] == ["dev@areeba.com", "lead@areeba.com"]
    to, subject, body = sent.call_args.args
    assert to == "dev@areeba.com, lead@areeba.com"
    assert "Code scan FAILED: Acquiring-UI build #25 (9 blocking, 5 allowed)" in subject
    assert "Please fix before Friday." in body
    [(filename, content, mimetype)] = sent.call_args.kwargs["attachments"]
    assert filename == "acquiring-ui-build-25-code-scan.pdf" and mimetype == "application/pdf"
    assert content.startswith(b"%PDF-")


def test_send_needs_recipients_and_email(client, admin_token, gated_build):
    base = f"/api/ci/builds/{gated_build.build_id}/stages/{gated_build.stage_id}/code-scan"
    with patch("api.email_delivery.smtp_is_configured", return_value=True):
        empty = client.post(f"{base}/send", json={"recipients": []}, headers=auth_headers(admin_token))
        bad = client.post(f"{base}/send", json={"recipients": ["nope"]}, headers=auth_headers(admin_token))
    assert empty.status_code == 400 and "at least one" in empty.get_json()["error"]
    assert bad.status_code == 400 and "not an email address" in bad.get_json()["error"]
    with patch("api.email_delivery.smtp_is_configured", return_value=False):
        off = client.post(f"{base}/send", json={"recipients": ["a@b.com"]}, headers=auth_headers(admin_token))
    assert off.status_code == 409 and "download the PDF" in off.get_json()["error"]


def test_a_stage_without_a_gate_has_no_report(client, admin_token, gated_build, app):
    with app.app_context():
        checkout = CiBuildStage.query.filter_by(build_id=gated_build.build_id, position=0).one()
        checkout_id = checkout.id
    response = client.get(
        f"/api/ci/builds/{gated_build.build_id}/stages/{checkout_id}/code-scan",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 404
