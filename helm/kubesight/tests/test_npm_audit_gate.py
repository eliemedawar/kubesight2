from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "tools" / "npm_audit_gate.py"
SPEC = importlib.util.spec_from_file_location("npm_audit_gate", MODULE_PATH)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

TODAY = dt.date(2026, 8, 2)


def _audit(advisory="GHSA-qwww-vcr4-c8h2", package="react-router"):
    return {
        "vulnerabilities": {
            package: {
                "nodes": [f"node_modules/{package}"],
                "via": [{"url": f"https://github.com/advisories/{advisory}", "title": "test"}],
            }
        }
    }


def _lock(version="7.18.2", package="react-router"):
    return {"packages": {f"node_modules/{package}": {"version": version}}}


def _policy(review="2026-09-02"):
    return {
        "schemaVersion": 1,
        "entries": [{
            "advisory": "GHSA-qwww-vcr4-c8h2",
            "package": "react-router",
            "versions": ["7.18.2"],
            "reason": "This test rationale is deliberately long enough to document a concrete mitigation.",
            "reviewBy": review,
            "owner": "security",
        }],
    }


def test_exact_advisory_and_version_is_accepted():
    errors, notices = gate.evaluate(_audit(), _lock(), _policy(), TODAY)
    assert errors == []
    assert any("accepted GHSA-QWWW-VCR4-C8H2" in item for item in notices)


def test_different_advisory_on_same_package_fails():
    errors, _ = gate.evaluate(_audit("GHSA-2345-6789-cfgh"), _lock(), _policy(), TODAY)
    assert any("not allowlisted" in item for item in errors)


def test_different_installed_version_fails():
    errors, _ = gate.evaluate(_audit(), _lock("7.18.3"), _policy(), TODAY)
    assert any("allows only" in item for item in errors)


def test_expired_entry_fails_even_when_advisory_is_absent():
    errors, _ = gate.evaluate({"vulnerabilities": {}}, _lock(), _policy("2026-08-02"), TODAY)
    assert any("expired" in item for item in errors)
