"""Test result and coverage parsing: every format, the build summary, the API.

Fixtures are shaped like what the tools actually write (namespaces, DOCTYPEs,
CDATA stack traces, captured output, attributes that disagree with the cases)
because the formats' edges are exactly where a parser built from a spec breaks.
"""

from __future__ import annotations

import io
import json
from hashlib import sha256

import pytest

from api.db import db
from api.models_ci import CiArtifact, CiBuild, CiBuildStage, CiService
from api.services.ci import test_reports
from api.services.ci.test_reports import ReportError
from tests.conftest import auth_headers

# ---------------------------------------------------------------------------
# Fixtures, as the tools write them
# ---------------------------------------------------------------------------

SUREFIRE = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuite xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="https://maven.apache.org/surefire/maven-surefire-plugin/xsd/surefire-test-report-3.0.xsd" version="3.0" name="com.areeba.payment.PaymentServiceTest" time="1.234" tests="5" errors="1" skipped="1" failures="1">
  <properties>
    <property name="java.version" value="21.0.2"/>
    <property name="user.dir" value="/workspace/source"/>
  </properties>
  <testcase name="chargesCard" classname="com.areeba.payment.PaymentServiceTest" time="0.120"/>
  <testcase name="rejectsExpiredCard" classname="com.areeba.payment.PaymentServiceTest" time="0.050">
    <failure message="expected: &lt;DECLINED&gt; but was: &lt;APPROVED&gt;" type="org.opentest4j.AssertionFailedError"><![CDATA[org.opentest4j.AssertionFailedError: expected: <DECLINED> but was: <APPROVED>
	at org.junit.jupiter.api.AssertionUtils.fail(AssertionUtils.java:55)
	at com.areeba.payment.PaymentServiceTest.rejectsExpiredCard(PaymentServiceTest.java:42)
]]></failure>
    <system-out><![CDATA[2026-10-01 12:00:00 INFO  charging card ****4242
]]></system-out>
  </testcase>
  <testcase name="refunds" classname="com.areeba.payment.PaymentServiceTest" time="0.300">
    <error message="Connection refused" type="java.net.ConnectException"><![CDATA[java.net.ConnectException: Connection refused
	at java.base/sun.nio.ch.Net.connect0(Native Method)
]]></error>
  </testcase>
  <testcase name="settles" classname="com.areeba.payment.PaymentServiceTest" time="0">
    <skipped message="Disabled until the acquirer sandbox is back"/>
  </testcase>
  <testcase name="retriesOnTimeout" classname="com.areeba.payment.PaymentServiceTest" time="0.400">
    <flakyFailure message="timed out" type="java.util.concurrent.TimeoutException"><stackTrace>java.util.concurrent.TimeoutException</stackTrace></flakyFailure>
  </testcase>
  <system-out><![CDATA[a great deal of captured output that nobody needs]]></system-out>
</testsuite>
"""

SUREFIRE_SECOND = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.areeba.payment.LedgerTest" time="0.500" tests="2" errors="0" skipped="0" failures="1">
  <testcase name="balances" classname="com.areeba.payment.LedgerTest" time="0.2"/>
  <testcase name="postsTwice" classname="com.areeba.payment.LedgerTest" time="0.3">
    <failure type="java.lang.AssertionError">java.lang.AssertionError: posted 2 entries
	at com.areeba.payment.LedgerTest.postsTwice(LedgerTest.java:18)</failure>
  </testcase>
</testsuite>
"""

GRADLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.areeba.wallet.WalletTest" tests="3" skipped="0" failures="0" errors="0" timestamp="2026-10-01T09:00:00" hostname="build-pod" time="0.812">
  <properties/>
  <testcase name="topUp()" classname="com.areeba.wallet.WalletTest" time="0.4"/>
  <testcase name="withdraw()" classname="com.areeba.wallet.WalletTest" time="0.2"/>
  <testcase name="balance()" classname="com.areeba.wallet.WalletTest" time="0.2"/>
  <system-out><![CDATA[]]></system-out>
  <system-err><![CDATA[]]></system-err>
</testsuite>
"""

JEST_JUNIT = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="jest tests" tests="5" failures="1" errors="0" time="2.345">
  <testsuite name="Cart" errors="0" failures="1" skipped="1" timestamp="2026-10-01T09:00:00" time="1.2" tests="3" file="src/cart.test.js">
    <testcase classname="Cart adds items" name="Cart adds items" time="0.004">
    </testcase>
    <testcase classname="Cart totals with tax" name="Cart totals with tax" time="0.01">
      <failure>Error: expect(received).toBe(expected) // Object.is equality

Expected: 30
Received: 25
    at Object.&lt;anonymous&gt; (src/cart.test.js:12:5)</failure>
    </testcase>
    <testcase classname="Cart applies coupons" name="Cart applies coupons" time="0">
      <skipped/>
    </testcase>
  </testsuite>
  <testsuite name="Checkout" errors="0" failures="0" skipped="0" timestamp="2026-10-01T09:00:01" time="1.1" tests="2">
    <testcase classname="Checkout pays" name="Checkout pays" time="0.5"/>
    <testcase classname="Checkout refunds" name="Checkout refunds" time="0.6"/>
  </testsuite>
</testsuites>
"""

# Attributes deliberately disagree with the cases (skipped="1" but two are
# skipped): the cases win.
PYTEST = b"""<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" errors="0" failures="1" skipped="1" tests="4" time="0.567" timestamp="2026-10-01T09:00:00.000000+00:00" hostname="runner"><testcase classname="tests.test_api" name="test_health" file="tests/test_api.py" line="10" time="0.001" /><testcase classname="tests.test_api" name="test_login" file="tests/test_api.py" line="20" time="0.002"><failure message="AssertionError: assert 401 == 200">def test_login(client):
&gt;       assert client.post("/login").status_code == 200
E       AssertionError: assert 401 == 200

tests/test_api.py:22: AssertionError</failure></testcase><testcase classname="tests.test_api" name="test_needs_db" time="0.000"><skipped type="pytest.skip" message="needs a database">/workspace/tests/test_api.py:30: needs a database</skipped></testcase><testcase classname="tests.test_api" name="test_known_bug" time="0.000"><skipped type="pytest.xfail" message="known bug" /></testcase></testsuite></testsuites>
"""

NESTED = b"""<?xml version="1.0"?>
<testsuites>
  <testsuite name="All" tests="99" failures="0">
    <testsuite name="Accounts">
      <testcase name="opens" classname="Accounts"/>
      <testcase name="closes" classname="Accounts"><failure message="still open"/></testcase>
    </testsuite>
    <testsuite name="Transfers">
      <testsuite name="Domestic"><testcase name="sends" classname="Transfers.Domestic"/></testsuite>
    </testsuite>
  </testsuite>
</testsuites>
"""

DOTNET_JUNIT = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="Areeba.Ledger.Tests.dll" tests="2" skipped="0" failures="1" errors="0" time="0.5" timestamp="2026-10-01T09:00:00" hostname="build" id="0" package="Areeba.Ledger.Tests.dll">
    <properties />
    <testcase classname="Areeba.Ledger.Tests.CalculatorTests" name="Adds(a: 1, b: 2)" time="0.0100000" />
    <testcase classname="Areeba.Ledger.Tests.CalculatorTests" name="Divides" time="0.0200000">
      <failure type="failure" message="Assert.Equal() Failure&#xA;Expected: 2&#xA;Actual:   3">   at Areeba.Ledger.Tests.CalculatorTests.Divides() in /src/CalculatorTests.cs:line 21</failure>
    </testcase>
    <system-out>Test run for Areeba.Ledger.Tests.dll</system-out>
    <system-err></system-err>
  </testsuite>
</testsuites>
"""

SUMMARY_ONLY = b'<testsuite name="legacy" tests="7" failures="2" errors="1" skipped="1" time="3.5"/>'

TRX = b"""<?xml version="1.0" encoding="utf-8"?>
<TestRun id="1" name="build@runner" xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">
  <Results>
    <UnitTestResult testId="a" testName="Areeba.Api.Tests.HealthTests.Ok" outcome="Passed" duration="00:00:00.0150000" />
    <UnitTestResult testId="b" testName="Areeba.Api.Tests.HealthTests.Fails" outcome="Failed" duration="00:00:01.5000000">
      <Output><ErrorInfo><Message>Assert.True() Failure</Message><StackTrace>   at Areeba.Api.Tests.HealthTests.Fails()</StackTrace></ErrorInfo></Output>
    </UnitTestResult>
    <UnitTestResult testId="c" testName="Areeba.Api.Tests.HealthTests.Later" outcome="NotExecuted" duration="00:00:00" />
  </Results>
  <ResultSummary outcome="Failed"><Counters total="3" executed="2" passed="1" failed="1" notExecuted="1" /></ResultSummary>
</TestRun>
"""

XUNIT = b"""<?xml version="1.0" encoding="utf-8"?>
<assemblies timestamp="10/01/2026 09:00:00">
  <assembly name="/src/Tests.dll" total="3" passed="1" failed="1" skipped="1" time="0.4" errors="0">
    <errors />
    <collection total="3" passed="1" failed="1" skipped="1" name="Test collection for Tests.MathTests" time="0.2">
      <test name="Tests.MathTests.Adds" type="Tests.MathTests" method="Adds" time="0.01" result="Pass" />
      <test name="Tests.MathTests.Divides" type="Tests.MathTests" method="Divides" time="0.02" result="Fail">
        <failure exception-type="Xunit.Sdk.EqualException"><message><![CDATA[Assert.Equal() Failure]]></message><stack-trace><![CDATA[at Tests.MathTests.Divides()]]></stack-trace></failure>
      </test>
      <test name="Tests.MathTests.Later" type="Tests.MathTests" method="Later" time="0" result="Skip"><reason><![CDATA[not yet]]></reason></test>
    </collection>
  </assembly>
</assemblies>
"""

NUNIT3 = b"""<?xml version="1.0" encoding="utf-8"?>
<test-run id="0" testcasecount="3" result="Failed" total="3" passed="1" failed="1" skipped="1" duration="0.5">
  <test-suite type="Assembly" name="Tests.dll" fullname="/src/Tests.dll">
    <test-suite type="TestFixture" name="BankTests" fullname="Tests.BankTests">
      <test-case name="Deposits" fullname="Tests.BankTests.Deposits" classname="Tests.BankTests" result="Passed" duration="0.01" />
      <test-case name="Withdraws" fullname="Tests.BankTests.Withdraws" classname="Tests.BankTests" result="Failed" duration="0.02">
        <failure><message><![CDATA[Expected: 10  But was: 5]]></message><stack-trace><![CDATA[at Tests.BankTests.Withdraws()]]></stack-trace></failure>
      </test-case>
      <test-case name="Later" fullname="Tests.BankTests.Later" classname="Tests.BankTests" result="Skipped" label="Ignored" duration="0" />
    </test-suite>
  </test-suite>
</test-run>
"""

COBERTURA_PY = b"""<?xml version="1.0" ?>
<coverage version="7.4.1" timestamp="1727773200000" lines-valid="200" lines-covered="162" line-rate="0.81" branches-covered="30" branches-valid="40" branch-rate="0.75" complexity="0">
	<!-- Generated by coverage.py: https://coverage.readthedocs.io/en/7.4.1 -->
	<sources><source>/workspace/source/app</source></sources>
	<packages>
		<package name="." line-rate="0.81" branch-rate="0.75" complexity="0">
			<classes><class name="api.py" filename="api.py" complexity="0" line-rate="0.81" branch-rate="0.75"><methods/><lines><line number="1" hits="1"/></lines></class></classes>
		</package>
	</packages>
</coverage>
"""

# The original Cobertura: a DOCTYPE, rates only, and every line twice (under
# its method and under its class). Counting both would double the figure.
COBERTURA_LEGACY = b"""<?xml version="1.0"?>
<!DOCTYPE coverage SYSTEM "http://cobertura.sourceforge.net/xml/coverage-04.dtd">
<coverage line-rate="0.5" branch-rate="0.5" version="2.1.1" timestamp="1727773200000">
  <packages>
    <package name="com.areeba" line-rate="0.5" branch-rate="0.5" complexity="1">
      <classes>
        <class name="com.areeba.Fee" filename="com/areeba/Fee.java" line-rate="0.5" branch-rate="0.5" complexity="1">
          <methods>
            <method name="apply" signature="()V" line-rate="0.5" branch-rate="0.5">
              <lines>
                <line number="3" hits="4" branch="false"/>
                <line number="4" hits="0" branch="true" condition-coverage="50% (1/2)"/>
              </lines>
            </method>
          </methods>
          <lines>
            <line number="3" hits="4" branch="false"/>
            <line number="4" hits="0" branch="true" condition-coverage="50% (1/2)"/>
            <line number="5" hits="3" branch="false"/>
            <line number="6" hits="0" branch="false"/>
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
"""

def _jacoco(line_missed, line_covered, branch_missed=10, branch_covered=30, name="payment-service"):
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?><!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd"><report name="{name}"><sessioninfo id="build-pod-1" start="1727773200000" dump="1727773260000"/><package name="com/areeba/payment"><class name="com/areeba/payment/PaymentService" sourcefilename="PaymentService.java"><method name="charge" desc="()V" line="12"><counter type="INSTRUCTION" missed="3" covered="40"/><counter type="LINE" missed="1" covered="9"/></method><counter type="LINE" missed="5" covered="50"/></class><sourcefile name="PaymentService.java"><line nr="12" mi="0" ci="4" mb="0" cb="0"/><counter type="LINE" missed="5" covered="50"/></sourcefile><counter type="LINE" missed="5" covered="50"/></package><counter type="INSTRUCTION" missed="120" covered="880"/><counter type="BRANCH" missed="{branch_missed}" covered="{branch_covered}"/><counter type="LINE" missed="{line_missed}" covered="{line_covered}"/><counter type="COMPLEXITY" missed="12" covered="40"/><counter type="METHOD" missed="4" covered="30"/><counter type="CLASS" missed="0" covered="8"/></report>""".encode()


JACOCO = _jacoco(40, 160)

LCOV = b"""TN:
SF:src/cart.js
FN:1,addItem
FNDA:3,addItem
FNF:1
FNH:1
DA:1,3
DA:2,3
DA:3,0
LF:3
LH:2
BRDA:2,0,0,1
BRDA:2,0,1,0
BRF:2
BRH:1
end_of_record
TN:
SF:src/checkout.js
DA:1,1
DA:2,0
DA:3,1
DA:4,1
BRDA:3,0,0,1
BRDA:3,0,1,-
end_of_record
"""

ISTANBUL_SUMMARY = json.dumps(
    {
        "total": {
            "lines": {"total": 400, "covered": 300, "skipped": 0, "pct": 75},
            "statements": {"total": 420, "covered": 310, "skipped": 0, "pct": 73.8},
            "functions": {"total": 50, "covered": 40, "skipped": 0, "pct": 80},
            "branches": {"total": 100, "covered": 60, "skipped": 0, "pct": 60},
        },
        "/workspace/source/src/cart.js": {
            "lines": {"total": 10, "covered": 9, "skipped": 0, "pct": 90},
        },
    }
).encode()

ISTANBUL_FINAL = json.dumps(
    {
        "/workspace/source/src/cart.js": {
            "path": "/workspace/source/src/cart.js",
            "statementMap": {
                "0": {"start": {"line": 1, "column": 0}, "end": {"line": 1, "column": 10}},
                "1": {"start": {"line": 2, "column": 0}, "end": {"line": 2, "column": 10}},
                "2": {"start": {"line": 2, "column": 12}, "end": {"line": 2, "column": 20}},
                "3": {"start": {"line": 3, "column": 0}, "end": {"line": 3, "column": 10}},
            },
            "s": {"0": 1, "1": 0, "2": 2, "3": 0},
            "b": {"0": [1, 0]},
            "branchMap": {},
            "fnMap": {},
            "f": {},
        }
    }
).encode()

CLOVER = b"""<?xml version="1.0" encoding="UTF-8"?>
<coverage generated="1727773200000" clover="3.2.0">
  <project timestamp="1727773200000" name="All files">
    <metrics statements="80" coveredstatements="60" conditionals="20" coveredconditionals="15" methods="10" coveredmethods="9" elements="110" coveredelements="84" complexity="0" loc="80" ncloc="80" packages="1" files="1" classes="1"/>
    <file name="cart.js" path="/workspace/source/src/cart.js">
      <metrics statements="80" coveredstatements="60" conditionals="20" coveredconditionals="15" methods="10" coveredmethods="9"/>
      <line num="1" count="3" type="stmt"/>
    </file>
  </project>
</coverage>
"""

XXE = b"""<?xml version="1.0"?>
<!DOCTYPE testsuite [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<testsuite name="evil" tests="1"><testcase name="&xxe;" classname="x"><failure message="&xxe;"/></testcase></testsuite>
"""

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<testsuite name="&lol3;"><testcase name="a"/></testsuite>
"""

TRUNCATED = b'<?xml version="1.0"?><testsuite name="cut" tests="3"><testcase name="a"/><testcase name="b"'

POM = b"""<?xml version="1.0"?><project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion></project>"""


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def _parse(tmp_path, data, name="report.xml"):
    return test_reports.parse_file(_write(tmp_path, name, data))


# ---------------------------------------------------------------------------
# JUnit and friends
# ---------------------------------------------------------------------------

def test_surefire_counts_every_outcome_and_keeps_the_failures(tmp_path):
    result = _parse(tmp_path, SUREFIRE, "TEST-com.areeba.payment.PaymentServiceTest.xml")
    assert result["kind"] == "tests" and result["format"] == "junit"
    assert (result["tests"], result["passed"], result["failed"], result["errors"], result["skipped"]) == (5, 2, 1, 1, 1)
    # Passed on a rerun: a pass, and flagged as flaky.
    assert result["flaky"] == 1
    assert result["durationSeconds"] == pytest.approx(1.234)
    failed = {case["name"]: case for case in result["failures"]}
    assert set(failed) == {"rejectsExpiredCard", "refunds"}
    assert failed["rejectsExpiredCard"]["message"] == "expected: <DECLINED> but was: <APPROVED>"
    assert failed["rejectsExpiredCard"]["type"] == "org.opentest4j.AssertionFailedError"
    assert "PaymentServiceTest.java:42" in failed["rejectsExpiredCard"]["details"]
    assert failed["rejectsExpiredCard"]["suite"] == "com.areeba.payment.PaymentServiceTest"
    assert failed["refunds"]["kind"] == "error"
    # Captured output is never kept.
    assert "4242" not in json.dumps(result)


def test_gradle_suite_with_empty_output_blocks(tmp_path):
    result = _parse(tmp_path, GRADLE)
    assert (result["tests"], result["passed"], result["failed"]) == (3, 3, 0)
    assert result["durationSeconds"] == pytest.approx(0.812)


def test_jest_junit_uses_the_body_when_there_is_no_message(tmp_path):
    result = _parse(tmp_path, JEST_JUNIT, "junit.xml")
    assert (result["tests"], result["passed"], result["failed"], result["skipped"]) == (5, 3, 1, 1)
    assert result["durationSeconds"] == pytest.approx(2.345)  # the root's time
    (case,) = result["failures"]
    assert case["message"].startswith("Error: expect(received).toBe(expected)")
    assert "Received: 25" in case["details"]
    assert case["file"] == "src/cart.test.js"  # inherited from the suite


def test_pytest_counts_cases_when_the_attributes_disagree(tmp_path):
    result = _parse(tmp_path, PYTEST, "report.xml")
    # The suite says skipped="1"; two cases are skipped (one is an xfail).
    assert (result["tests"], result["passed"], result["failed"], result["skipped"]) == (4, 1, 1, 2)
    (case,) = result["failures"]
    assert case["message"] == "AssertionError: assert 401 == 200"
    assert case["file"] == "tests/test_api.py"
    assert case["classname"] == "tests.test_api"


def test_nested_suites_count_only_the_cases(tmp_path):
    result = _parse(tmp_path, NESTED)
    # The outer suite's tests="99" is not added on top of its children.
    assert (result["tests"], result["failed"]) == (3, 1)
    assert result["failures"][0]["suite"] == "Accounts"
    assert result["failures"][0]["message"] == "still open"


def test_dotnet_junit_logger(tmp_path):
    result = _parse(tmp_path, DOTNET_JUNIT)
    assert (result["tests"], result["failed"]) == (2, 1)
    assert result["failures"][0]["message"].startswith("Assert.Equal() Failure")


def test_a_summary_only_suite_falls_back_to_its_attributes(tmp_path):
    result = _parse(tmp_path, SUMMARY_ONLY)
    assert (result["tests"], result["failed"], result["errors"], result["skipped"], result["passed"]) == (7, 2, 1, 1, 3)


def test_trx_xunit_and_nunit(tmp_path):
    trx = _parse(tmp_path, TRX, "results.trx")
    assert trx["format"] == "trx"
    assert (trx["tests"], trx["passed"], trx["failed"], trx["skipped"]) == (3, 1, 1, 1)
    assert trx["failures"][0]["message"] == "Assert.True() Failure"
    assert trx["failures"][0]["classname"] == "Areeba.Api.Tests.HealthTests"
    assert trx["durationSeconds"] == pytest.approx(1.515)

    xunit = _parse(tmp_path, XUNIT, "xunit.xml")
    assert xunit["format"] == "xunit"
    assert (xunit["tests"], xunit["passed"], xunit["failed"], xunit["skipped"]) == (3, 1, 1, 1)
    assert xunit["failures"][0]["type"] == "Xunit.Sdk.EqualException"

    nunit = _parse(tmp_path, NUNIT3, "TestResult.xml")
    assert nunit["format"] == "nunit"
    assert (nunit["tests"], nunit["passed"], nunit["failed"], nunit["skipped"]) == (3, 1, 1, 1)
    assert nunit["failures"][0]["suite"] == "Tests.BankTests"


def test_failures_are_capped_and_long_text_truncated(tmp_path):
    cases = "".join(
        f'<testcase name="t{i}" classname="Big"><failure message="{"x" * 2000}">{"y" * 9000}</failure></testcase>'
        for i in range(test_reports.MAX_FAILURES + 25)
    )
    result = _parse(tmp_path, f'<testsuite name="big">{cases}</testsuite>'.encode())
    assert result["failed"] == test_reports.MAX_FAILURES + 25
    assert len(result["failures"]) == test_reports.MAX_FAILURES
    assert result["failuresTruncated"] is True
    assert len(result["failures"][0]["message"]) <= test_reports.MAX_MESSAGE_CHARS
    assert len(result["failures"][0]["details"]) <= test_reports.MAX_DETAIL_CHARS


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

def test_cobertura_from_coverage_py(tmp_path):
    result = _parse(tmp_path, COBERTURA_PY, "coverage.xml")
    assert result["kind"] == "coverage" and result["format"] == "cobertura"
    assert result["lines"] == {"covered": 162, "total": 200, "pct": 81.0}
    assert result["branches"] == {"covered": 30, "total": 40, "pct": 75.0}


def test_legacy_cobertura_counts_class_lines_once(tmp_path):
    result = _parse(tmp_path, COBERTURA_LEGACY)
    assert result["lines"] == {"covered": 2, "total": 4, "pct": 50.0}
    assert result["branches"] == {"covered": 1, "total": 2, "pct": 50.0}


def test_jacoco_reads_only_the_report_level_counters(tmp_path):
    result = _parse(tmp_path, JACOCO, "jacoco.xml")
    assert result["format"] == "jacoco"
    assert result["lines"] == {"covered": 160, "total": 200, "pct": 80.0}
    assert result["branches"] == {"covered": 30, "total": 40, "pct": 75.0}


def test_jacoco_without_line_data_says_it_used_instructions(tmp_path):
    data = b'<report name="x"><counter type="INSTRUCTION" missed="25" covered="75"/></report>'
    result = _parse(tmp_path, data)
    assert result["lines"]["pct"] == 75.0
    assert "instruction" in result["note"].lower()


def test_lcov_sums_records_and_counts_da_lines_when_lf_is_missing(tmp_path):
    result = _parse(tmp_path, LCOV, "lcov.info")
    assert result["format"] == "lcov"
    # cart: LF 3 / LH 2; checkout has no LF/LH: 4 DA lines, 3 hit.
    assert result["lines"] == {"covered": 5, "total": 7, "pct": 71.43}
    # cart: BRF 2 / BRH 1; checkout: 2 BRDA, one taken, one "-".
    assert result["branches"] == {"covered": 2, "total": 4, "pct": 50.0}
    assert result["files"] == 2


def test_istanbul_summary_and_final_json(tmp_path):
    summary = _parse(tmp_path, ISTANBUL_SUMMARY, "coverage-summary.json")
    assert summary["format"] == "istanbul-summary"
    assert summary["lines"] == {"covered": 300, "total": 400, "pct": 75.0}
    assert summary["branches"]["pct"] == 60.0

    final = _parse(tmp_path, ISTANBUL_FINAL, "coverage-final.json")
    assert final["format"] == "istanbul-json"
    # Lines 1, 2, 3: line 2 has a hit statement, line 3 none.
    assert final["lines"] == {"covered": 2, "total": 3, "pct": 66.67}
    assert final["branches"] == {"covered": 1, "total": 2, "pct": 50.0}


def test_clover(tmp_path):
    result = _parse(tmp_path, CLOVER, "clover.xml")
    assert result["format"] == "clover"
    assert result["lines"] == {"covered": 60, "total": 80, "pct": 75.0}
    assert result["branches"] == {"covered": 15, "total": 20, "pct": 75.0}


def test_format_comes_from_content_not_the_file_name(tmp_path):
    # A Cobertura file called junit.xml is coverage; a JUnit file called
    # coverage.xml is tests.
    assert _parse(tmp_path, COBERTURA_PY, "junit.xml")["kind"] == "coverage"
    assert _parse(tmp_path, GRADLE, "coverage.xml")["kind"] == "tests"
    assert _parse(tmp_path, LCOV, "report.txt")["format"] == "lcov"


# ---------------------------------------------------------------------------
# Hostile and broken files
# ---------------------------------------------------------------------------

def test_xxe_is_refused_and_nothing_is_read(tmp_path):
    with pytest.raises(ReportError) as excinfo:
        _parse(tmp_path, XXE)
    assert "refused" in str(excinfo.value)
    assert "root:" not in str(excinfo.value)


def test_billion_laughs_is_refused(tmp_path):
    with pytest.raises(ReportError) as excinfo:
        _parse(tmp_path, BILLION_LAUGHS)
    assert "refused" in str(excinfo.value)


@pytest.mark.parametrize(
    "data, fragment",
    [
        (TRUNCATED, "well-formed"),
        (POM, "Not a test or coverage report"),
        (b"", "empty"),
        (b"\x00\x01\x02PK\x03\x04 not a report", "Not a test or coverage report"),
        (b'{"name": "app", "version": "1.0.0"}', "Istanbul"),
        (b"{not json", "not valid JSON"),
        (b'<report name="x"></report>', "no report-level counters"),
    ],
)
def test_broken_files_say_what_is_wrong(tmp_path, data, fragment):
    with pytest.raises(ReportError) as excinfo:
        _parse(tmp_path, data)
    assert fragment in str(excinfo.value)


def test_oversized_reports_are_not_read(tmp_path, monkeypatch):
    monkeypatch.setattr(test_reports, "MAX_REPORT_BYTES", 100)
    with pytest.raises(ReportError) as excinfo:
        _parse(tmp_path, SUREFIRE)
    assert "MB" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Folding files into a build
# ---------------------------------------------------------------------------

def _merged(*results):
    summary = None
    for index, result in enumerate(results):
        summary = test_reports.merge(summary, artifact_id=index + 1, name=f"file-{index}", result=result)
    return summary


def test_test_reports_add_up_across_files(tmp_path):
    first = _parse(tmp_path, SUREFIRE)
    second = _parse(tmp_path, SUREFIRE_SECOND)
    summary = _merged(first, second)
    totals = summary["totals"]
    assert (totals["tests"], totals["passed"], totals["failed"], totals["errors"], totals["skipped"]) == (7, 3, 2, 1, 1)
    assert summary["failureCount"] == 3
    assert [case["name"] for case in summary["failures"]] == ["rejectsExpiredCard", "refunds", "postsTwice"]
    compact = test_reports.compact(summary)
    assert compact["total"] == 7 and compact["failed"] == 2 and compact["linesPct"] is None


def test_separate_modules_coverage_is_summed():
    module_a = {"kind": "coverage", "format": "jacoco", "lines": {"covered": 80, "total": 100, "pct": 80.0},
                "branches": {"covered": 8, "total": 10, "pct": 80.0}}
    module_b = {"kind": "coverage", "format": "jacoco", "lines": {"covered": 20, "total": 100, "pct": 20.0},
                "branches": None}
    coverage = _merged(module_a, module_b)["coverage"]
    assert coverage["lines"] == {"covered": 100, "total": 200, "pct": 50.0}
    assert coverage["branches"] == {"covered": 8, "total": 10, "pct": 80.0}
    assert "Combined from 2" in coverage["note"]


def test_an_aggregate_report_is_not_added_to_its_modules(tmp_path):
    module_a = _parse(tmp_path, _jacoco(20, 80, name="a"), "a.xml")       # 100 lines
    module_b = _parse(tmp_path, _jacoco(50, 50, name="b"), "b.xml")       # 100 lines
    aggregate = _parse(tmp_path, _jacoco(70, 130, name="all"), "all.xml")  # 200 = a + b
    coverage = _merged(module_a, module_b, aggregate)["coverage"]
    assert coverage["lines"] == {"covered": 130, "total": 200, "pct": 65.0}
    assert "aggregate" in coverage["note"]


def test_the_same_run_in_two_formats_is_not_counted_twice(tmp_path):
    lcov = _parse(tmp_path, LCOV, "lcov.info")
    cobertura = _parse(tmp_path, COBERTURA_PY, "cobertura-coverage.xml")
    coverage = _merged(lcov, cobertura)["coverage"]
    # Cobertura covers more lines, so it is used and lcov is named as left out.
    assert coverage["format"] == "cobertura"
    assert coverage["lines"]["total"] == 200
    assert "lcov" in coverage["note"]


def test_identical_copies_count_once(tmp_path):
    first = _parse(tmp_path, COBERTURA_PY, "coverage.xml")
    coverage = _merged(first, dict(first))["coverage"]
    assert coverage["lines"]["total"] == 200


def test_rate_only_reports_use_the_most_complete_one(tmp_path):
    rate_only = {"kind": "coverage", "format": "cobertura", "lines": {"covered": None, "total": None, "pct": 40.0},
                 "branches": None}
    counted = _parse(tmp_path, COBERTURA_PY)
    coverage = _merged(rate_only, counted)["coverage"]
    assert coverage["lines"]["pct"] == 81.0
    assert "most complete" in coverage["note"]


def test_errors_are_kept_beside_the_counts(tmp_path):
    summary = test_reports.merge(None, artifact_id=1, name="broken.xml", error="Not well-formed XML.")
    summary = test_reports.merge(summary, artifact_id=2, name="ok.xml", result=_parse(tmp_path, GRADLE))
    compact = test_reports.compact(summary)
    assert compact["parseErrors"] == 1 and compact["total"] == 3
    assert summary["errors"][0]["name"] == "broken.xml"


def test_merge_never_mutates_the_summary_it_was_given(tmp_path):
    summary = _merged(_parse(tmp_path, GRADLE))
    before = json.dumps(summary, sort_keys=True)
    test_reports.merge(summary, artifact_id=9, name="x", result=_parse(tmp_path, SUREFIRE))
    assert json.dumps(summary, sort_keys=True) == before


# ---------------------------------------------------------------------------
# Ingest through the real upload routes
# ---------------------------------------------------------------------------

@pytest.fixture()
def running_build(app, tmp_path, monkeypatch):
    monkeypatch.setenv("CI_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    with app.app_context():
        service = CiService(name="Payment Service", slug="payment-service")
        db.session.add(service)
        db.session.flush()
        build = CiBuild(
            service_id=service.id,
            number=1,
            status="running",
            branch="develop",
            pipeline_snapshot={
                "stages": [
                    {"name": "Checkout", "stageType": "checkout"},
                    {
                        "name": "Test",
                        "stageType": "command",
                        "artifacts": [
                            {"path": "**/target/surefire-reports/TEST-*.xml", "type": "test-report"},
                            {"path": "**/jacoco*.xml", "type": "coverage-report"},
                        ],
                    },
                ]
            },
            worker_callback_token_hash=sha256(b"worker-token").hexdigest(),
        )
        db.session.add(build)
        db.session.flush()
        db.session.add(CiBuildStage(build_id=build.id, position=1, name="Test", status="running"))
        db.session.commit()
        return build.id


def _upload(client, build_id, data, *, name, kind):
    return client.post(
        f"/api/ci/worker/builds/{build_id}/artifacts",
        data={
            "name": name,
            "type": kind,
            "stagePosition": "1",
            "declaredPath": "**/target/surefire-reports/TEST-*.xml",
            "sourcePath": f"target/surefire-reports/{name}",
            "file": (io.BytesIO(data), name),
        },
        headers={"Authorization": "Bearer worker-token"},
        content_type="multipart/form-data",
    )


def test_collector_uploads_become_a_build_summary(app, client, admin_token, running_build):
    assert _upload(client, running_build, SUREFIRE, name="TEST-PaymentServiceTest.xml", kind="test-report").status_code == 201
    assert _upload(client, running_build, SUREFIRE_SECOND, name="TEST-LedgerTest.xml", kind="test-report").status_code == 201
    assert _upload(client, running_build, JACOCO, name="jacoco.xml", kind="coverage-report").status_code == 201

    with app.app_context():
        rows = CiArtifact.query.filter_by(build_id=running_build).order_by(CiArtifact.id).all()
        assert rows[0].artifact_metadata["testReport"]["tests"] == 5
        # The artifact carries counts only; the cases live on the build.
        assert "failures" not in rows[0].artifact_metadata["testReport"]
        assert rows[2].artifact_metadata["testReport"]["lines"]["pct"] == 80.0
        summary = db.session.get(CiBuild, running_build).test_summary
        assert summary["totals"]["tests"] == 7
        assert summary["reports"][0]["stage"] == "Test"
        assert summary["reports"][0]["name"] == "target/surefire-reports/TEST-PaymentServiceTest.xml"

    headers = auth_headers(admin_token)
    build = client.get(f"/api/ci/builds/{running_build}", headers=headers).get_json()["data"]
    assert build["testSummary"]["total"] == 7
    assert build["testSummary"]["failed"] == 2
    assert build["testSummary"]["errors"] == 1
    assert build["testSummary"]["linesPct"] == 80.0
    assert "failures" not in build["testSummary"]

    listed = client.get("/api/ci/services/1/builds", headers=headers).get_json()["data"]["items"]
    assert listed[0]["testSummary"]["passed"] == 3

    detail = client.get(f"/api/ci/builds/{running_build}/tests", headers=headers).get_json()["data"]
    assert detail["summary"]["total"] == 7
    assert [case["name"] for case in detail["failures"]] == ["rejectsExpiredCard", "refunds", "postsTwice"]
    assert detail["failures"][0]["artifactId"] is not None
    assert detail["coverage"]["lines"]["pct"] == 80.0
    assert len(detail["reports"]) == 3
    assert {item["type"] for item in detail["declared"]} == {"test-report", "coverage-report"}


def test_a_broken_report_never_fails_the_upload(app, client, admin_token, running_build):
    response = _upload(client, running_build, TRUNCATED, name="TEST-Broken.xml", kind="test-report")
    assert response.status_code == 201
    response = _upload(client, running_build, XXE, name="TEST-Evil.xml", kind="test-report")
    assert response.status_code == 201
    with app.app_context():
        rows = CiArtifact.query.filter_by(build_id=running_build).order_by(CiArtifact.id).all()
        assert "well-formed" in rows[0].artifact_metadata["testReport"]["error"]
        assert "refused" in rows[1].artifact_metadata["testReport"]["error"]
    detail = client.get(
        f"/api/ci/builds/{running_build}/tests", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert detail["summary"]["parseErrors"] == 2
    assert detail["summary"]["total"] is None  # no test file was readable
    assert len(detail["errors"]) == 2


def test_report_aliases_and_sniffed_binaries(app, client, running_build):
    # "coverage" is what the stage editor used to offer.
    _upload(client, running_build, LCOV, name="lcov.info", kind="coverage")
    # Kept as a plain binary, but unmistakably JUnit: read.
    _upload(client, running_build, GRADLE, name="TEST-WalletTest.xml", kind="binary")
    # A binary XML that is not a report: left alone, no error recorded.
    _upload(client, running_build, POM, name="pom.xml", kind="binary")
    # Not a name a report has: not even opened.
    _upload(client, running_build, GRADLE, name="app.jar", kind="binary")
    with app.app_context():
        rows = {row.name: row for row in CiArtifact.query.filter_by(build_id=running_build).all()}
        assert rows["lcov.info"].artifact_type == "coverage-report"
        assert rows["TEST-WalletTest.xml"].artifact_type == "binary"
        assert rows["TEST-WalletTest.xml"].artifact_metadata["testReport"]["detected"] is True
        assert "testReport" not in rows["pom.xml"].artifact_metadata
        assert "testReport" not in rows["app.jar"].artifact_metadata
        summary = db.session.get(CiBuild, running_build).test_summary
        assert summary["totals"]["tests"] == 3
        assert summary["errorCount"] == 0
        assert summary["coverage"]["lines"]["total"] == 7


def test_testng_results_beside_surefire_are_not_counted_twice(app, client, running_build):
    testng = b'<?xml version="1.0"?><testng-results skipped="0" failed="1" total="5" passed="4"><suite name="Suite"/></testng-results>'
    _upload(client, running_build, SUREFIRE, name="TEST-TestSuite.xml", kind="test-report")
    _upload(client, running_build, testng, name="testng-results.xml", kind="test-report")
    with app.app_context():
        rows = {row.name: row for row in CiArtifact.query.filter_by(build_id=running_build).all()}
        assert "not counted" in rows["testng-results.xml"].artifact_metadata["testReport"]["ignored"]
        summary = db.session.get(CiBuild, running_build).test_summary
        assert summary["totals"]["tests"] == 5 and summary["errorCount"] == 0


def test_a_build_with_no_reports_says_what_was_declared(app, client, admin_token, running_build):
    headers = auth_headers(admin_token)
    build = client.get(f"/api/ci/builds/{running_build}", headers=headers).get_json()["data"]
    assert build["testSummary"] is None
    detail = client.get(f"/api/ci/builds/{running_build}/tests", headers=headers).get_json()["data"]
    assert detail["summary"] is None and detail["failures"] == []
    assert detail["declared"][0]["path"] == "**/target/surefire-reports/TEST-*.xml"
    assert client.get("/api/ci/builds/999999/tests", headers=headers).status_code == 404


def test_trend_lists_builds_with_reports_oldest_first(app, client, admin_token, running_build):
    _upload(client, running_build, SUREFIRE, name="TEST-A.xml", kind="test-report")
    with app.app_context():
        first = db.session.get(CiBuild, running_build)
        service_id = first.service_id
        # A build with no reports in between is skipped, not drawn as zero.
        db.session.add(CiBuild(service_id=service_id, number=2, status="success", pipeline_snapshot={}))
        third = CiBuild(service_id=service_id, number=3, status="success", pipeline_snapshot={})
        third.test_summary = test_reports.merge(
            None, artifact_id=None, name="x", result={"kind": "coverage", "format": "lcov",
                                                      "lines": {"covered": 9, "total": 10, "pct": 90.0},
                                                      "branches": None}
        )
        db.session.add(third)
        db.session.commit()

    data = client.get(
        f"/api/ci/services/{service_id}/test-trend?limit=5", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert [point["number"] for point in data["points"]] == [1, 3]
    assert data["points"][0]["failed"] == 2  # failures + errors
    assert data["points"][1]["total"] is None and data["points"][1]["linesPct"] == 90.0


def test_agent_uploads_are_read_too(app, client, admin_token, tmp_path, monkeypatch):
    """The agent path goes through the same record_artifact, so it is parsed
    the same way — proven end to end through its own route."""
    from api.models_application_intelligence import BitbucketCredentialProfile
    from api.models_ci import CiRunner
    from api.secret_encryption import encrypt_secret
    from api.services.ci import agents as agents_service
    from api.services.ci import engine

    monkeypatch.setenv("CI_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    headers = auth_headers(admin_token)
    with app.app_context():
        credential = BitbucketCredentialProfile(
            name="ci-token", provider="bitbucket", credential_type="repository_access_token",
            secret_cipher=encrypt_secret("clone-token-value"), read_only=True, enabled=True,
        )
        db.session.add(credential)
        db.session.commit()
        credential_id = credential.id
    service_id = client.post(
        "/api/ci/services", json={"name": "Wallet", "applicationType": "java"}, headers=headers
    ).get_json()["data"]["id"]
    client.put(
        f"/api/ci/services/{service_id}/source",
        json={"repositoryUrl": "https://bitbucket.org/areeba/wallet", "defaultBranch": "develop",
              "credentialProfileId": credential_id},
        headers=headers,
    )
    pipeline_id = client.get(
        f"/api/ci/services/{service_id}/pipelines", headers=headers
    ).get_json()["data"]["items"][0]["id"]
    client.put(
        f"/api/ci/pipelines/{pipeline_id}",
        json={"parameters": [], "stages": [{
            "name": "Test", "stageType": "command", "commands": ["mvn -B test"],
            "artifacts": [{"path": "**/target/surefire-reports/TEST-*.xml", "type": "test-report"}],
        }]},
        headers=headers,
    )
    with app.app_context():
        for runner in CiRunner.query.all():
            runner.enabled = False
            db.session.add(runner)
        runner, token = agents_service.create_agent(
            {"name": "linux-1", "runnerType": "agent_linux", "maxConcurrent": 1}
        )
        agents_service.heartbeat(runner, {"capabilities": ["linux", "java"]})
        db.session.commit()

    build_id = client.post(
        f"/api/ci/services/{service_id}/builds", json={}, headers=headers
    ).get_json()["data"]["id"]
    with app.app_context():
        engine.advance_ci_builds()
    agent_headers = {"Authorization": f"Bearer {token}"}
    task = client.post("/api/ci/agent/claim", json={}, headers=agent_headers).get_json()["data"]

    response = client.post(
        f"/api/ci/agent/tasks/{task['taskId']}/artifacts",
        data={
            "claimToken": task["claimToken"],
            "name": "TEST-PaymentServiceTest.xml",
            "type": "test-report",
            "file": (io.BytesIO(SUREFIRE), "TEST-PaymentServiceTest.xml"),
        },
        headers=agent_headers,
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    build = client.get(f"/api/ci/builds/{build_id}", headers=headers).get_json()["data"]
    assert build["testSummary"]["total"] == 5
    assert build["testSummary"]["failed"] == 1


def test_mcp_build_failure_names_the_failed_tests(app, client, admin_token, running_build):
    _upload(client, running_build, SUREFIRE, name="TEST-PaymentServiceTest.xml", kind="test-report")
    with app.app_context():
        build = db.session.get(CiBuild, running_build)
        build.status = "failed"
        build.stages[0].status = "failed"
        db.session.commit()

    response = client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "kubesight_build_failure", "arguments": {"buildId": running_build}},
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    found = response.get_json()["result"]["structuredContent"]
    tests = found["tests"]
    assert tests["total"] == 5 and tests["failed"] == 1 and tests["errors"] == 1
    assert [case["name"] for case in tests["failedTests"]] == ["rejectsExpiredCard", "refunds"]
    # The assertion, not the stack trace.
    assert "details" not in tests["failedTests"][0]
