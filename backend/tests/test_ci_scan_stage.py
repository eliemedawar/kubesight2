"""The Scan stage: a stage whose script KubeSight generates around one scanner.

What is locked here:

* configuration — four tools, each with its own options and refusals; a scan
  stage without a tool, with commands/image/artifacts, or with the code scan
  gate on a non-Semgrep tool is refused on save, with a message that says what
  to do; the SBOM of the pushed image is refused with the reason;
* compatibility — a scan stage saved while the kind was retired (no config)
  still loads, is skipped with a reason, and never fails the build;
* the generated scripts, run for real under ``sh`` with stand-in scanners on
  PATH: the report lands where the collector looks, and the stage fails
  exactly when the gate says (and not when it is set to warn);
* the engine folds generation into the execution (catalog image, secrets),
  the Kubernetes runner runs it like a command stage and collects the report or
  SBOM, and an agent or the mock runner skips it with a reason;
* a Semgrep scan stage's report works with the existing PDF report unchanged;
* the MCP schema describes the kind.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from api.db import db
from api.models_ci import CiBuild, CiBuildStage, CiService
from api.services.ci import build_environments, code_scan, scan_stage
from api.services.ci.pipelines import PipelineError, normalize_stage
from api.services.ci.runners import kubernetes as k8s
from api.services.ci.runners.base import StageExecution
from tests.conftest import auth_headers
from tests.test_ci_code_scan import _FAKE_SEMGREP, _finding, _report, _shell_available
from tests.test_ci_engine import _drain, _store_legacy_stage, runnable_service  # noqa: F401


def _scan(tool, **options):
    return {"name": "Scan", "stageType": "scan", "scan": {"tool": tool, **options}}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_each_tool_normalizes_to_its_defaults():
    trivy = normalize_stage(_scan("trivy_fs"), 0, set())
    assert trivy["stage_type"] == "scan"
    assert trivy["scan"] == scan_stage.default_config("trivy_fs")
    assert trivy["code_scan"] is None and trivy["commands"] == [] and trivy["image"] is None

    semgrep = normalize_stage(_scan("semgrep"), 0, set())
    assert semgrep["scan"] == {"tool": "semgrep", "rules": []}
    # A Semgrep scan stage always carries the quality gate: the default one.
    assert semgrep["code_scan"] == code_scan.default_config()

    dc = normalize_stage(_scan("dependency_check"), 0, set())
    assert dc["scan"] == scan_stage.default_config("dependency_check")

    syft = normalize_stage(_scan("syft"), 0, set())
    assert syft["scan"] == {"tool": "syft", "format": "cyclonedx-json", "target": "source"}


def test_options_are_cleaned_and_held_to_their_sets():
    config = normalize_stage(
        _scan(
            "trivy_fs",
            scanners=["misconfig", "vuln", "vuln"],
            threshold="HIGH",
            onFail="warn",
            ignoreUnfixed=1,
            skipDirs="**/vendor, docs",
        ),
        0,
        set(),
    )["scan"]
    # Canonical order, no repeats, so one choice always writes one line.
    assert config["scanners"] == ["vuln", "misconfig"]
    assert config["threshold"] == "high" and config["onFail"] == "warn"
    assert config["ignoreUnfixed"] is True
    assert config["skipDirs"] == ["**/vendor", "docs"]
    # An explicit empty list means "skip nothing", not "the defaults".
    assert normalize_stage(_scan("trivy_fs", skipDirs=[]), 0, set())["scan"]["skipDirs"] == []

    dc = normalize_stage(
        _scan("dependency_check", failOnCvss="8.26", nvdApiKeySecret="MY_NVD",
              nvdDatafeedUrl="https://nexus.areeba.com/nvd/"),
        0,
        {"MY_NVD"},
    )["scan"]
    assert dc["failOnCvss"] == 8.3  # one decimal, as CVSS scores are written
    assert dc["nvdApiKeySecret"] == "MY_NVD"
    assert dc["nvdDatafeedUrl"] == "https://nexus.areeba.com/nvd/"

    rules = normalize_stage(_scan("semgrep", rules="p/java  p/secrets"), 0, set())["scan"]["rules"]
    assert rules == ["p/java", "p/secrets"]


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"name": "Scan", "stageType": "scan"}, "names no scanner"),
        (_scan(""), "names no scanner"),
        (_scan("sonar"), "not a scanner KubeSight runs"),
        (_scan("trivy_fs", scanners=["licence"]), "Trivy scans for"),
        (_scan("trivy_fs", threshold="severe"), "severity threshold"),
        (_scan("trivy_fs", onFail="ignore"), "what a finding does"),
        (_scan("trivy_fs", skipDirs=["$(rm -rf /)"]), "not a valid skip pattern"),
        (_scan("semgrep", rules=["p/java; curl evil"]), "not a valid rule set"),
        (_scan("dependency_check", failOnCvss=11), "from 0 to 10"),
        (_scan("dependency_check", failOnCvss="high"), "must be a number"),
        (_scan("dependency_check", nvdApiKeySecret="MISSING"), "not defined for this service"),
        (_scan("dependency_check", nvdDatafeedUrl="ftp://x"), "http(s) URL"),
        (_scan("syft", format="xml"), "SBOM format"),
        (_scan("syft", target="image"), "push credential"),
    ],
)
def test_bad_scan_configuration_is_refused_with_a_reason(payload, message):
    with pytest.raises(PipelineError) as excinfo:
        normalize_stage(payload, 0, set())
    assert message in str(excinfo.value)
    assert excinfo.value.code == "invalid_scan"


@pytest.mark.parametrize(
    "extra, message",
    [
        ({"commands": ["trivy fs ."]}, "writes its script"),
        ({"image": "aquasec/trivy:0.50"}, "approved image"),
        ({"artifacts": [{"path": "report.json", "type": "scan-report"}]}, "kept on the build automatically"),
    ],
)
def test_a_scan_stage_refuses_what_it_would_ignore(extra, message):
    with pytest.raises(PipelineError, match=message):
        normalize_stage({**_scan("trivy_fs"), **extra}, 0, set())


def test_the_quality_gate_goes_only_on_a_semgrep_scan_stage():
    with pytest.raises(PipelineError, match="only goes on a Semgrep scan stage"):
        normalize_stage({**_scan("trivy_fs"), "codeScan": {"maxBlocking": 1}}, 0, set())
    # A parked gate on a Semgrep scan stage is switched back on: a scan stage
    # that never gates is a log, not a scan.
    stage = normalize_stage(
        {**_scan("semgrep"), "codeScan": {"enabled": False, "maxBlocking": 4, "countFrom": "error"}},
        0,
        set(),
    )
    assert stage["code_scan"]["enabled"] is True and stage["code_scan"]["maxBlocking"] == 4
    # ...and is still refused on stage kinds that run no scanner at all.
    with pytest.raises(PipelineError, match="command stage that runs the scanner"):
        normalize_stage({"name": "Image", "stageType": "container_image", "codeScan": {"maxBlocking": 0}}, 0, set())


def test_scanner_settings_are_refused_off_a_scan_stage():
    with pytest.raises(PipelineError, match="Scanner settings go on a scan stage"):
        normalize_stage(
            {"name": "Build", "stageType": "command", "commands": ["make"], "scan": {"tool": "syft"}}, 0, set()
        )
    plain = normalize_stage({"name": "Build", "stageType": "command", "commands": ["make"]}, 0, set())
    assert plain["scan"] is None


def test_publish_artifact_stays_retired():
    with pytest.raises(PipelineError, match="no executor"):
        normalize_stage({"name": "Publish", "stageType": "publish_artifact"}, 0, set())


def test_the_pipeline_api_saves_and_returns_a_scan_stage(client, admin_token, runnable_service):  # noqa: F811
    base = f"/api/ci/services/{runnable_service}/pipelines"
    listing = client.get(base, headers=auth_headers(admin_token)).get_json()["data"]
    pipeline_id = listing["items"][0]["id"]
    # The editor's tool cards come with the listing, Semgrep's rules for THIS
    # service's application type included.
    tools = {item["tool"]: item for item in listing["scanTools"]}
    assert set(tools) == set(scan_stage.TOOLS)
    assert tools["syft"]["image"].endswith("anchore/syft:debug")
    assert tools["semgrep"]["defaultRules"] == ["p/default", "p/java"]
    assert tools["syft"]["produces"] == [{"name": "sbom-stage-1.cdx.json", "type": "sbom"}]

    saved = client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={
            "parameters": [],
            "stages": [
                {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
                {"name": "Dependencies", "stageType": "scan", "scan": {"tool": "trivy_fs", "threshold": "high"}},
            ],
        },
        headers=auth_headers(admin_token),
    )
    assert saved.status_code == 200, saved.get_json()
    stage = saved.get_json()["data"]["stages"][1]
    assert stage["stageType"] == "scan"
    assert stage["scan"]["tool"] == "trivy_fs" and stage["scan"]["threshold"] == "high"

    refused = client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={"stages": [{"name": "Scan", "stageType": "scan"}]},
        headers=auth_headers(admin_token),
    )
    assert refused.status_code == 400
    assert "names no scanner" in refused.get_json()["error"]


# ---------------------------------------------------------------------------
# The generated scripts
# ---------------------------------------------------------------------------

def test_scripts_are_one_line_and_write_outside_the_checkout():
    for tool in scan_stage.TOOLS:
        config = scan_stage.default_config(tool)
        [script] = scan_stage.commands(config, position=2, application_type="node",
                                       gate=code_scan.default_config())
        assert "${KUBESIGHT_WORKSPACE:-/workspace}/.kubesight/" in script
        assert scan_stage.report_file(config, 2) in script


def test_trivy_script_scans_once_and_gates_the_saved_report(monkeypatch):
    monkeypatch.setenv("CI_TRIVY_DB_REPOSITORY", "nexus.areeba.com/aquasecurity/trivy-db")
    monkeypatch.setenv("CI_TRIVY_SKIP_DB_UPDATE", "true")
    config = {**scan_stage.default_config("trivy_fs"), "threshold": "high", "ignoreUnfixed": True,
              "scanners": ["vuln", "secret", "misconfig"]}
    [script] = scan_stage.commands(config, position=1)
    assert script.count("trivy fs ") == 1
    assert "--scanners vuln,secret,misconfig" in script
    assert "--severity CRITICAL,HIGH,MEDIUM,LOW" in script
    assert "--skip-dirs '**/node_modules' --skip-dirs '**/.git'" in script
    assert "--ignore-unfixed" in script
    assert "--db-repository nexus.areeba.com/aquasecurity/trivy-db" in script
    assert "export TRIVY_SKIP_DB_UPDATE=true" in script
    # The gate reads the SAVED report, at the chosen severities only.
    assert 'trivy convert --format table --severity CRITICAL,HIGH --exit-code 3 "$KS_SCAN_REPORT"' in script
    # Shares the image scan gate's database directory.
    assert ': "${TRIVY_CACHE_DIR:=$KUBESIGHT_CACHE_DIR/trivy}"' in script


def test_semgrep_script_is_the_code_scan_gate_around_a_generated_scan():
    gate = {**code_scan.default_config(), "maxBlocking": 3, "countFrom": "warning"}
    [automatic] = scan_stage.commands({"tool": "semgrep", "rules": []}, position=4,
                                      application_type="java_gradle", gate=gate)
    assert 'DEFAULT_RULES="p/default p/java"' in automatic
    # Automatic rules follow the merge check: rules committed to the repo win.
    assert "if [ -f .semgrep.yml ]" in automatic
    assert "semgrep scan $CONFIG_ARGS --metrics=off --disable-version-check ." in automatic
    assert '.kubesight/code-scan-4.json' in automatic
    assert 'python3 - "$KS_CODE_SCAN_REPORT" 3 warning' in automatic

    [chosen] = scan_stage.commands({"tool": "semgrep", "rules": ["p/owasp-top-ten"]}, position=4, gate=gate)
    assert 'DEFAULT_RULES="p/owasp-top-ten"' in chosen
    # Rules somebody chose are not silently replaced by a .semgrep.yml.
    assert ".semgrep.yml" not in chosen


def test_dependency_check_script_shares_the_merge_check_database_and_lock():
    from api.services.ci.merge_checks import stages as merge_check_stages

    config = {**scan_stage.default_config("dependency_check"),
              "nvdDatafeedUrl": "https://nexus.areeba.com/nvd/"}
    [script] = scan_stage.commands(config, position=2, application_type="python")
    for line in merge_check_stages.dependency_check_lock_lines():
        assert line in script
    assert 'DATA_DIR="${DC_DATA_DIR:-/tmp/dependency-check-data}"' in script
    assert "--enableExperimental" in script
    assert "--failOnCVSS 11" in script  # the verdict is read from the report, not the exit code
    assert "if [ -z \"${NVD_DATAFEED_URL:-}\" ]; then NVD_DATAFEED_URL=https://nexus.areeba.com/nvd/; fi" in script
    [java] = scan_stage.commands(config, position=2, application_type="java_gradle")
    assert "no jars here yet" in java and "--enableExperimental" not in java


def test_syft_script_looks_for_the_binary_where_the_debug_image_keeps_it():
    [script] = scan_stage.commands({"tool": "syft", "format": "spdx-json", "target": "source"}, position=0)
    assert "KS_SYFT=/syft" in script
    assert '"$KS_SYFT" dir:. -o spdx-json="$KS_SBOM"' in script
    assert "sbom-0.spdx.json" in script and "sbom-stage-1.spdx.json" in script


_FAKE_TRIVY = r"""#!/bin/sh
# Stand-in for trivy. `fs` writes a report with $FAKE_CRITICAL CRITICAL and
# $FAKE_LOW LOW findings (or fails with $FAKE_FAIL); `convert` exits with the
# --exit-code it was given when $FAKE_GATED says something is at the threshold.
cmd="$1"; shift
out=""; code=1; prev=""
for arg in "$@"; do
  [ "$prev" = "--output" ] && out="$arg"
  [ "$prev" = "--exit-code" ] && code="$arg"
  prev="$arg"
done
case "$cmd" in
  fs)
    echo "$@" > "$FAKE_ARGS_FILE"
    if [ "${FAKE_FAIL:-0}" != 0 ]; then echo "FATAL failed to download vulnerability DB" >&2; exit 1; fi
    {
      echo '{'
      echo '  "Results": ['
      echo '    {"Vulnerabilities": ['
      i=0; while [ "$i" -lt "${FAKE_CRITICAL:-0}" ]; do echo '      {"VulnerabilityID": "CVE-1", "Severity": "CRITICAL"},'; i=$((i + 1)); done
      i=0; while [ "$i" -lt "${FAKE_LOW:-0}" ]; do echo '      {"VulnerabilityID": "CVE-2", "Severity": "LOW"},'; i=$((i + 1)); done
      echo '      {"VulnerabilityID": "end", "VendorSeverity": {"nvd": 1}}'
      echo '    ]}'
      echo '  ]'
      echo '}'
    } > "$out"
    ;;
  convert)
    echo "Total: ${FAKE_GATED:-0}"
    if [ "${FAKE_GATED:-0}" -gt 0 ]; then exit "$code"; fi
    exit 0
    ;;
esac
"""

_FAKE_DC = r"""#!/bin/sh
# Stand-in for dependency-check.sh: writes a report with one vulnerability per
# score in $FAKE_SCORES (CVSS v3 baseScore), into the --out directory.
out=""; prev=""
for arg in "$@"; do
  [ "$prev" = "--out" ] && out="$arg"
  prev="$arg"
done
echo "$@" > "$FAKE_ARGS_FILE"
if [ "${FAKE_FAIL:-0}" != 0 ]; then echo "No documents exist"; exit 1; fi
{
  echo '{ "dependencies" : [ { "fileName" : "app.jar", "vulnerabilities" : ['
  n=0
  for score in ${FAKE_SCORES:-}; do
    [ "$n" -gt 0 ] && echo ','
    echo "{ \"source\" : \"NVD\", \"name\" : \"CVE-2024-$n\", \"cvssv3\" : { \"baseScore\" : $score } }"
    n=$((n + 1))
  done
  echo '] } ] }'
} > "$out/dependency-check-report.json"
exit 0
"""

_FAKE_SYFT = r"""#!/bin/sh
# Stand-in for syft: `dir:. -o <format>=<file>` writes $FAKE_PACKAGES packages.
out=""; prev=""
for arg in "$@"; do
  [ "$prev" = "-o" ] && out="${arg#*=}"
  prev="$arg"
done
[ "${FAKE_FAIL:-0}" != 0 ] && exit 0
{
  echo '{"bomFormat": "CycloneDX", "components": ['
  i=0; while [ "$i" -lt "${FAKE_PACKAGES:-0}" ]; do
    [ "$i" -gt 0 ] && echo ','
    echo "{\"name\": \"p$i\", \"purl\": \"pkg:npm/p$i@1.0.0\"}"
    i=$((i + 1))
  done
  echo ']}'
} > "$out"
"""


def _install(tmp_path, name, body):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / name
    fake.write_text(body, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    return bin_dir


def _workspace(tmp_path):
    workspace = tmp_path / "ws"
    source = workspace / "source"
    source.mkdir(parents=True)
    (source / "package-lock.json").write_text("{}", encoding="utf-8")
    return workspace, source


def _posix(path):
    return str(path).replace("\\", "/")


def _run(tmp_path, script, *, cwd, env):
    """Under ``set -e``, like a runner, and from a file: a long ``sh -c``
    argument does not survive Windows' command line."""
    path = tmp_path / "stage.sh"
    path.write_text("set -e\n" + script, encoding="utf-8", newline="\n")
    return subprocess.run(["sh", str(path)], cwd=cwd, env=env, capture_output=True, text=True)


def _env(tmp_path, bin_dir, workspace, **extra):
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "KUBESIGHT_WORKSPACE": _posix(workspace),
        "KUBESIGHT_CACHE_DIR": "",
        "FAKE_ARGS_FILE": _posix(tmp_path / "args.txt"),
        **{key: str(value) for key, value in extra.items()},
    }


def _needs_sh():
    return pytest.mark.skipif(not shutil.which("sh"), reason="needs sh")


@_needs_sh()
@pytest.mark.parametrize(
    "on_fail, gated, fake_fail, expect_rc, says",
    [
        ("block", 0, 0, 0, "Scan passed"),
        ("block", 2, 0, 1, "Scan FAILED: findings at or above HIGH"),
        ("warn", 2, 0, 0, "set to warn, so the stage passes"),
        ("block", 0, 1, 1, "Trivy did not finish"),
    ],
)
def test_trivy_stage_fails_exactly_when_the_gate_says(tmp_path, on_fail, gated, fake_fail, expect_rc, says):
    bin_dir = _install(tmp_path, "trivy", _FAKE_TRIVY)
    workspace, source = _workspace(tmp_path)
    config = {**scan_stage.default_config("trivy_fs"), "threshold": "high", "onFail": on_fail}
    [script] = scan_stage.commands(config, position=1)
    result = _run(tmp_path, script, cwd=source, env=_env(
        tmp_path, bin_dir, workspace, FAKE_CRITICAL=gated, FAKE_LOW=3, FAKE_GATED=gated, FAKE_FAIL=fake_fail,
    ))
    output = result.stdout + result.stderr
    assert result.returncode == expect_rc, output
    assert says in output
    if not fake_fail:
        report = workspace / ".kubesight" / "scan-1-trivy.json"
        assert report.exists()
        # The readable summary counts every finding, not just the gated ones,
        # and is not fooled by VendorSeverity.
        assert f"Findings: CRITICAL {gated}, HIGH 0, MEDIUM 0, LOW 3." in output
    assert "--scanners vuln,secret" in (tmp_path / "args.txt").read_text()


@_needs_sh()
def test_trivy_stage_says_when_the_image_has_no_trivy(tmp_path):
    workspace, source = _workspace(tmp_path)
    [script] = scan_stage.commands(scan_stage.default_config("trivy_fs"), position=1)
    env = _env(tmp_path, tmp_path, workspace)
    env["PATH"] = os.path.dirname(shutil.which("sh"))  # sh and coreutils, no trivy
    result = _run(tmp_path, script, cwd=source, env=env)
    assert result.returncode == 1
    assert "trivy is not in this stage's image" in result.stderr
    assert "CI_TEMPLATE_TRIVY_IMAGE" in result.stderr


@_needs_sh()
@pytest.mark.parametrize(
    "scores, threshold, on_fail, expect_rc, says",
    [
        ("", 7.0, "block", 0, "nothing scores CVSS 7.0 or higher"),
        ("5.3 6.9", 7.0, "block", 0, "highest CVSS 6.9"),
        ("5.3 9.8", 7.0, "block", 1, "scores CVSS 9.8, at or above 7.0"),
        ("5.3 9.8", 7.0, "warn", 0, "set to warn, so the stage passes"),
        ("7.0", 7.0, "block", 1, "at or above 7.0"),
        ("", 0.0, "block", 0, "nothing scores CVSS 0.0 or higher"),
    ],
)
def test_dependency_check_stage_fails_exactly_when_a_score_reaches_the_threshold(
    tmp_path, scores, threshold, on_fail, expect_rc, says
):
    bin_dir = _install(tmp_path, "dependency-check.sh", _FAKE_DC)
    workspace, source = _workspace(tmp_path)
    data_dir = tmp_path / "nvd"
    config = {**scan_stage.default_config("dependency_check"), "failOnCvss": threshold, "onFail": on_fail}
    [script] = scan_stage.commands(config, position=2, application_type="node")
    result = _run(tmp_path, script, cwd=source, env=_env(
        tmp_path, bin_dir, workspace, FAKE_SCORES=scores, DC_DATA_DIR=_posix(data_dir), NVD_API_KEY="k",
    ))
    output = result.stdout + result.stderr
    assert result.returncode == expect_rc, output
    assert says in output
    report = workspace / ".kubesight" / "scan-2-dependency-check.json"
    assert report.exists()
    # The lock was taken and released, so the next scan is not left waiting.
    assert not (data_dir / ".kubesight-scan.lock").exists()
    args = (tmp_path / "args.txt").read_text()
    assert "--nvdApiKey k" in args and "--failOnCVSS 11" in args


@_needs_sh()
def test_dependency_check_without_a_report_explains_why(tmp_path):
    bin_dir = _install(tmp_path, "dependency-check.sh", _FAKE_DC)
    workspace, source = _workspace(tmp_path)
    [script] = scan_stage.commands(scan_stage.default_config("dependency_check"), position=2)
    result = _run(tmp_path, script, cwd=source, env=_env(
        tmp_path, bin_dir, workspace, FAKE_FAIL=1, DC_DATA_DIR=_posix(tmp_path / "nvd"),
    ))
    assert result.returncode == 1
    assert "Dependency-Check produced no report" in result.stderr
    assert "vulnerability database" in result.stdout and "NVD_API_KEY" in result.stdout


@_needs_sh()
@pytest.mark.parametrize("packages, fake_fail, expect_rc", [(3, 0, 0), (0, 0, 0), (0, 1, 1)])
def test_syft_stage_writes_the_sbom_and_counts_its_packages(tmp_path, packages, fake_fail, expect_rc):
    bin_dir = _install(tmp_path, "syft", _FAKE_SYFT)
    workspace, source = _workspace(tmp_path)
    [script] = scan_stage.commands(scan_stage.default_config("syft"), position=3)
    result = _run(tmp_path, script, cwd=source, env=_env(
        tmp_path, bin_dir, workspace, FAKE_PACKAGES=packages, FAKE_FAIL=fake_fail,
    ))
    output = result.stdout + result.stderr
    assert result.returncode == expect_rc, output
    if expect_rc == 0:
        sbom = workspace / ".kubesight" / "sbom-3.cdx.json"
        assert json.loads(sbom.read_text())["bomFormat"] == "CycloneDX"
        assert f"SBOM written: {packages} packages. Kept on this build as sbom-stage-4.cdx.json." in output
    else:
        assert "Syft wrote nothing" in output


@pytest.mark.skipif(not _shell_available(), reason="needs sh and python3")
@pytest.mark.parametrize("findings, allowed, expect_rc", [(2, 5, 0), (6, 5, 1)])
def test_semgrep_stage_runs_the_existing_gate(tmp_path, findings, allowed, expect_rc):
    bin_dir = _install(tmp_path, "semgrep", _FAKE_SEMGREP)
    workspace, source = _workspace(tmp_path)
    gate = {**code_scan.default_config(), "maxBlocking": allowed}
    [script] = scan_stage.commands({"tool": "semgrep", "rules": []}, position=1,
                                   application_type="node", gate=gate)
    result = _run(tmp_path, script, cwd=source, env=_env(tmp_path, bin_dir, workspace, FAKE_FINDINGS=findings))
    output = result.stdout + result.stderr
    assert result.returncode == expect_rc, output
    assert "Scanning with rules: p/default p/javascript p/typescript" in output
    report = json.loads((workspace / ".kubesight" / "code-scan-1.json").read_text())
    assert report["kubesight"]["gate"]["maxBlocking"] == allowed
    assert "== quality gate ==" in output


@_needs_sh()
def test_semgrep_stage_says_when_the_image_has_no_semgrep(tmp_path):
    """The gate's shim defines a shell function called semgrep, so the check
    must look on PATH, not ask `command -v`."""
    workspace, source = _workspace(tmp_path)
    [script] = scan_stage.commands({"tool": "semgrep", "rules": []}, position=1, gate=code_scan.default_config())
    env = _env(tmp_path, tmp_path, workspace)
    env["PATH"] = os.path.dirname(shutil.which("sh"))
    result = _run(tmp_path, script, cwd=source, env=env)
    assert result.returncode == 1
    assert "semgrep is not in this stage's image" in result.stderr


# ---------------------------------------------------------------------------
# Kubernetes runner
# ---------------------------------------------------------------------------

def _execution(position, stage_type="command", **kw):
    return StageExecution(
        build_id=7, build_number=3, stage_id=100 + position, service_slug="payment-service",
        stage_name=f"Stage {position}", stage_type=stage_type, image=kw.get("image"),
        working_directory=kw.get("workdir"), commands=kw.get("commands", ["echo hello"]),
        env={}, secrets=kw.get("secrets", {}), position=position, workspace_ref="payment-service-3",
        repository_url="https://bitbucket.org/areeba/payment-service.git", branch="develop",
        code_scan=kw.get("code_scan"), scan=kw.get("scan"),
        callback_url="http://backend:5000/api/ci/worker", callback_token="t",
    )


def _scan_execution(position, config, **kw):
    gate = scan_stage.gate_for(config, None, "Scan")
    return _execution(
        position,
        "scan",
        image=scan_stage.image_for(config),
        commands=scan_stage.commands(config, position=position, gate=gate),
        code_scan=gate,
        scan=config,
        **kw,
    )


def _collector_specs(job):
    collector = next(c for c in job["spec"]["template"]["spec"]["containers"] if c["name"] == "collector")
    env = {entry["name"]: entry.get("value") for entry in collector["env"]}
    return json.loads(env["KUBESIGHT_ARTIFACTS"])


def test_kubernetes_runs_a_scan_stage_like_a_command_stage(monkeypatch):
    monkeypatch.delenv("CI_CACHE_STORAGE_CLASS", raising=False)
    monkeypatch.setenv("CI_CACHE_CLAIM_NAME", "ci-cache")
    monkeypatch.setenv("CI_CACHE_SHARED", "none")
    trivy = _scan_execution(1, scan_stage.default_config("trivy_fs"), workdir="services/api")
    dc = _scan_execution(2, scan_stage.default_config("dependency_check"), secrets={"NVD_API_KEY": "k"})
    syft = _scan_execution(3, {"tool": "syft", "format": "spdx-json", "target": "source"})
    semgrep = _scan_execution(4, {"tool": "semgrep", "rules": []})
    first = _execution(0, "checkout", secrets={"KUBESIGHT_GIT_TOKEN": "t", "KUBESIGHT_GIT_CREDENTIAL_TYPE": "oauth"})
    first.plan = [first, trivy, dc, syft, semgrep]
    secret, _, job = k8s.build_job_resources(first)
    containers = job["spec"]["template"]["spec"]["initContainers"]

    assert [c["image"] for c in containers[1:]] == [
        build_environments.image("trivy"),
        build_environments.image("dependency-check"),
        build_environments.image("syft"),
        build_environments.image("semgrep"),
    ]
    for container in containers[1:]:
        # Found on PATH: the Syft debug image has no /bin/sh.
        assert container["command"][:2] == ["sh", "-c"]
        assert {"name": "cache", "mountPath": "/kubesight-cache"} in container["volumeMounts"]
        # The same stage wrapper as a command stage: fail flag, exit marker.
        assert "/workspace/.kubesight/failed" in container["command"][2]
    assert "cd /workspace/source/services/api" in containers[1]["command"][2]

    trivy_env = {e["name"]: e.get("value") for e in containers[1]["env"]}
    assert trivy_env["TRIVY_CACHE_DIR"] == "/kubesight-cache/payment-service/trivy"
    dc_env = {e["name"]: e for e in containers[2]["env"]}
    assert dc_env["DC_DATA_DIR"]["value"] == "/kubesight-cache/payment-service/dependency-check-data"
    # The NVD key reaches the stage from the per-build Secret, never as a value.
    assert "secretKeyRef" in dc_env["NVD_API_KEY"]["valueFrom"]
    assert "value" not in dc_env["NVD_API_KEY"]

    specs = _collector_specs(job)
    assert specs == [
        {"path": "/workspace/.kubesight/scan-1-trivy.json", "type": "scan-report",
         "name": "trivy-fs-stage-2.json", "workdir": "", "stagePosition": 1},
        {"path": "/workspace/.kubesight/scan-2-dependency-check.json", "type": "scan-report",
         "name": "dependency-check-stage-3.json", "workdir": "", "stagePosition": 2},
        {"path": "/workspace/.kubesight/sbom-3.spdx.json", "type": "sbom",
         "name": "sbom-stage-4.spdx.json", "workdir": "", "stagePosition": 3},
        # A Semgrep scan stage's report is the code scan gate's own, by name.
        code_scan.artifact_spec(4),
    ]
    # No registry credential is mounted into a scan stage.
    for container in containers[1:]:
        assert not any(m["name"] == "docker-config" for m in container["volumeMounts"])


def test_runners_say_where_scan_stages_run(app):
    from api.services.ci.runners.agent import ExternalAgentRunnerAdapter
    from api.services.ci.runners.mock import MockRunnerAdapter

    assert "scan" in k8s.KubernetesJobRunnerAdapter().supported_stage_types()
    agent = ExternalAgentRunnerAdapter("agent_linux")
    assert "scan" not in agent.supported_stage_types()
    assert "Kubernetes runner" in agent.skip_reason("scan")
    assert "scan" not in MockRunnerAdapter().supported_stage_types()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def _pipeline_id(client, admin_token, service_id):
    return client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=auth_headers(admin_token)
    ).get_json()["data"]["items"][0]["id"]


def _stage_log(client, admin_token, build_id, stage_id):
    logs = client.get(
        f"/api/ci/builds/{build_id}/stages/{stage_id}/logs", headers=auth_headers(admin_token)
    ).get_json()["data"]
    return " ".join(line["content"] for line in logs["lines"])


def test_an_old_scan_stage_without_a_scanner_still_loads_and_is_skipped(
    app, client, admin_token, runnable_service  # noqa: F811
):
    pipeline_id = _pipeline_id(client, admin_token, runnable_service)
    _store_legacy_stage(app, pipeline_id, name="Old scan", stage_type="scan")

    pipeline = client.get(f"/api/ci/pipelines/{pipeline_id}", headers=auth_headers(admin_token))
    assert pipeline.status_code == 200
    old = next(s for s in pipeline.get_json()["data"]["stages"] if s["name"] == "Old scan")
    assert old["stageType"] == "scan" and old["scan"] is None

    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    _drain(app)
    data = client.get(f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)).get_json()["data"]
    stage = next(s for s in data["stages"] if s["name"] == "Old scan")
    assert stage["status"] == "skipped" and stage["scan"] is None
    assert data["status"] == "success"
    text = _stage_log(client, admin_token, build_id, stage["id"])
    assert "names no scanner" in text and "choose Trivy, Semgrep, Dependency-Check or Syft" in text


def test_a_scan_stage_on_a_runner_that_cannot_run_it_is_skipped_with_the_reason(
    app, client, admin_token, runnable_service  # noqa: F811
):
    pipeline_id = _pipeline_id(client, admin_token, runnable_service)
    client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={"parameters": [], "stages": [
            {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
            {"name": "SBOM", "stageType": "scan", "runnerLabels": ["mock"], "scan": {"tool": "syft"}},
        ]},
        headers=auth_headers(admin_token),
    )
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]
    _drain(app)
    data = client.get(f"/api/ci/builds/{build_id}", headers=auth_headers(admin_token)).get_json()["data"]
    stage = next(s for s in data["stages"] if s["name"] == "SBOM")
    assert stage["status"] == "skipped"
    # The build drawer still says what the stage was for.
    assert stage["scan"]["tool"] == "syft"
    assert stage["scan"]["produces"] == [{"name": "sbom-stage-2.cdx.json", "type": "sbom"}]
    assert "Scan stages run on the Kubernetes runner" in _stage_log(client, admin_token, build_id, stage["id"])


def test_the_engine_generates_the_scan_into_the_execution(app, client, admin_token, runnable_service):  # noqa: F811
    from api.services.ci import engine

    assert client.post(
        f"/api/ci/services/{runnable_service}/secrets",
        json={"key": "MY_NVD", "value": "nvd-secret-value"},
        headers=auth_headers(admin_token),
    ).status_code == 201
    pipeline_id = _pipeline_id(client, admin_token, runnable_service)
    saved = client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={"parameters": [], "stages": [
            {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
            {"name": "CVEs", "stageType": "scan",
             "scan": {"tool": "dependency_check", "failOnCvss": 9, "nvdApiKeySecret": "MY_NVD"}},
            {"name": "Code", "stageType": "scan", "scan": {"tool": "semgrep"},
             "codeScan": {"maxBlocking": 2}},
        ]},
        headers=auth_headers(admin_token),
    )
    assert saved.status_code == 200, saved.get_json()
    build_id = client.post(
        f"/api/ci/services/{runnable_service}/builds", json={}, headers=auth_headers(admin_token)
    ).get_json()["data"]["id"]

    with app.app_context():
        build = db.session.get(CiBuild, build_id)
        rows = sorted(build.stages, key=lambda s: s.position)
        cves = engine._build_execution(build, rows[1], engine._definition_for(build, rows[1]))
        code = engine._build_execution(build, rows[2], engine._definition_for(build, rows[2]))

    assert cves.stage_type == "scan"
    assert cves.image == build_environments.image("dependency-check")
    assert "--failOnCVSS 11" in cves.commands[0] and "fails on CVSS 9.0 or higher" in cves.commands[0]
    assert cves.secrets["NVD_API_KEY"] == "nvd-secret-value"
    assert cves.scan["tool"] == "dependency_check"
    assert cves.code_scan is None

    # A Semgrep scan stage runs under the code scan gate, rules for a java service.
    assert code.image == build_environments.image("semgrep")
    assert code.code_scan["maxBlocking"] == 2 and code.code_scan["enabled"] is True
    assert 'DEFAULT_RULES="p/default p/java"' in code.commands[0]
    assert 'python3 - "$KS_CODE_SCAN_REPORT" 2 info' in code.commands[0]


# ---------------------------------------------------------------------------
# The PDF report works for a Semgrep scan stage, unchanged
# ---------------------------------------------------------------------------

@pytest.fixture()
def scanned_build(app, tmp_path, monkeypatch):
    from api.services.ci import artifacts as artifacts_service
    from api.services.ci.runners.base import ArtifactRef

    monkeypatch.setenv("CI_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    report = _report(*[_finding(line=i + 1) for i in range(3)])
    report["kubesight"] = {"gate": {"maxBlocking": 5, "countFrom": "info", "verdict": "passed"}}
    report_file = tmp_path / "code-scan-1.json"
    report_file.write_text(json.dumps(report), encoding="utf-8")
    with app.app_context():
        service = CiService(name="Acquiring-UI", slug="acquiring-ui")
        db.session.add(service)
        db.session.flush()
        gate = {**code_scan.default_config(), "maxBlocking": 5}
        build = CiBuild(
            service_id=service.id, number=4, status="success", branch="develop",
            pipeline_snapshot={"stages": [
                {"name": "Checkout", "stageType": "checkout"},
                {"name": "Code scan", "stageType": "scan", "scan": {"tool": "semgrep", "rules": []},
                 "codeScan": gate},
            ]},
        )
        db.session.add(build)
        db.session.flush()
        db.session.add(CiBuildStage(build_id=build.id, position=0, name="Checkout", status="success"))
        stage = CiBuildStage(build_id=build.id, position=1, name="Code scan", stage_type="scan", status="success")
        db.session.add(stage)
        db.session.flush()
        artifacts_service.record_artifact(
            service_id=service.id, build_id=build.id, build_stage_id=stage.id,
            ref=ArtifactRef(name=code_scan.report_artifact_name(1), artifact_type="scan-report",
                            local_path=str(report_file)),
            commit=True,
        )
        return SimpleNamespace(build_id=build.id, stage_id=stage.id)


def test_a_semgrep_scan_stage_gets_the_code_scan_report(app, client, admin_token, scanned_build):
    from api.services.ci import code_scan_report

    data = client.get(f"/api/ci/builds/{scanned_build.build_id}", headers=auth_headers(admin_token)).get_json()["data"]
    assert data["stages"][1]["codeScan"] == {"tool": "semgrep", "maxBlocking": 5, "countFrom": "info"}
    assert data["stages"][1]["scan"]["tool"] == "semgrep"

    overview = client.get(
        f"/api/ci/builds/{scanned_build.build_id}/stages/{scanned_build.stage_id}/code-scan",
        headers=auth_headers(admin_token),
    )
    assert overview.status_code == 200
    payload = overview.get_json()["data"]
    assert payload["reportAvailable"] is True
    assert payload["verdict"] == "passed" and payload["blocking"] == 3

    with app.app_context():
        pdf = code_scan_report.render_for_stage(
            db.session.get(CiBuild, scanned_build.build_id),
            db.session.get(CiBuildStage, scanned_build.stage_id),
        )
    assert pdf.startswith(b"%PDF-")


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------

def test_the_mcp_stage_schema_describes_scan_stages():
    from api.mcp.tools import ci as mcp_ci

    assert "scan" in mcp_ci.STAGE_FIELDS
    schema = mcp_ci._STAGE_SCHEMA
    assert "scan" in schema["properties"]["stageType"]["enum"]
    assert schema["properties"]["scan"]["properties"]["tool"]["enum"] == list(scan_stage.TOOLS)
    assert schema["properties"]["scan"]["properties"]["format"]["enum"] == list(scan_stage.SBOM_FORMATS)
    assert "scan {tool:" in schema["description"]
