/**
 * The Scan stage's model: which scanners there are, what each one's options
 * default to, how a scan stage reads in one line, and what a save would refuse.
 *
 * Mirrors services/ci/scan_stage.py — the backend still validates; this only
 * says it before Save. A scan stage names a TOOL and KubeSight writes the
 * image and script, so there is no command or image to edit here.
 */

import { DEFAULT_CODE_SCAN } from "../ciShared.jsx";

export const SCAN_TOOLS = [
  {
    value: "trivy_fs",
    label: "Trivy filesystem",
    sentence: "Finds known-vulnerable dependencies in lockfiles and jars, secrets committed to the repository, and optionally IaC misconfigurations.",
  },
  {
    value: "semgrep",
    label: "Semgrep",
    sentence: "Finds insecure code patterns in the source itself — injection, XSS, unsafe crypto — with the rules for this service's language.",
  },
  {
    value: "dependency_check",
    label: "Dependency-Check",
    sentence: "Finds dependencies with published CVEs, scored by CVSS, from the NVD database shared by every service.",
  },
  {
    value: "syft",
    label: "SBOM with Syft",
    sentence: "Lists every package the source tree depends on as a software bill of materials. Records, never fails.",
  },
];

export const scanToolOf = (tool) => SCAN_TOOLS.find((item) => item.value === tool) || null;

export const TRIVY_SCANNERS = [
  { value: "vuln", label: "Vulnerable dependencies" },
  { value: "secret", label: "Committed secrets" },
  { value: "misconfig", label: "IaC misconfigurations" },
];

export const DEFAULT_SKIP_DIRS = ["**/node_modules", "**/.git"];

export const SBOM_FORMATS = [
  { value: "cyclonedx-json", label: "CycloneDX JSON" },
  { value: "spdx-json", label: "SPDX JSON" },
];

export const SCAN_ON_FAIL = [
  { value: "block", label: "Fail the stage" },
  { value: "warn", label: "Warn and continue" },
];

const RULE_RE = /^[A-Za-z0-9_./:@+=~-]+$/;
const GLOB_RE = /^[A-Za-z0-9_./*@+~-]+$/;
const URL_RE = /^https?:\/\/[^\s'"`$\\]+$/;

/** What a stage gets when a tool is picked. Mirrors scan_stage.default_config. */
export function defaultScan(tool) {
  switch (tool) {
    case "trivy_fs":
      return {
        tool,
        scanners: ["vuln", "secret"],
        threshold: "critical",
        onFail: "block",
        ignoreUnfixed: false,
        skipDirs: [...DEFAULT_SKIP_DIRS],
      };
    case "semgrep":
      return { tool, rules: [] };
    case "dependency_check":
      return { tool, failOnCvss: 7, onFail: "block", nvdApiKeySecret: "", nvdDatafeedUrl: "" };
    case "syft":
      return { tool, format: "cyclonedx-json", target: "source" };
    default:
      return null;
  }
}

/**
 * The patch that points a scan stage at a tool. Semgrep is gated by the code
 * scan quality gate (kept if the stage already had one); any other tool must
 * not carry it, or the save is refused.
 */
export function scanToolPatch(stage, tool) {
  if (stage.scan?.tool === tool) return {};
  return {
    scan: defaultScan(tool),
    codeScan: tool === "semgrep" ? { ...DEFAULT_CODE_SCAN, ...(stage.codeScan || {}), enabled: true } : null,
  };
}

const SBOM_SUFFIX = { "cyclonedx-json": "cdx.json", "spdx-json": "spdx.json" };

/** The artifact the stage leaves on a build. Mirrors scan_stage.produces. */
export function scanArtifact(scan, index) {
  const number = Number(index) + 1;
  switch (scan?.tool) {
    case "trivy_fs":
      return { name: `trivy-fs-stage-${number}.json`, type: "scan-report" };
    case "dependency_check":
      return { name: `dependency-check-stage-${number}.json`, type: "scan-report" };
    case "semgrep":
      return { name: `code-scan-stage-${number}.json`, type: "scan-report" };
    case "syft":
      return { name: `sbom-stage-${number}.${SBOM_SUFFIX[scan.format] || "cdx.json"}`, type: "sbom" };
    default:
      return null;
  }
}

const cvss = (value) => {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(1) : "7.0";
};

/** Whether a finding can fail the build — shown as a mark in the flow. */
export function scanBlocks(stage) {
  const scan = stage.scan;
  if (!scan?.tool || scan.tool === "syft") return false;
  if (scan.tool === "semgrep") return true;
  return (scan.onFail || "block") === "block";
}

/** One line: what the scan stage checks and what stops it. */
export function scanSummary(stage) {
  const scan = stage.scan;
  if (!scan?.tool) return "No scanner chosen yet";
  const verb = (scan.onFail || "block") === "block" ? "fails" : "warns";
  switch (scan.tool) {
    case "trivy_fs": {
      const what = (scan.scanners?.length ? scan.scanners : ["vuln", "secret"])
        .map((value) => ({ vuln: "dependencies", secret: "secrets", misconfig: "IaC" })[value] || value)
        .join(", ");
      return `Trivy: ${what} · ${verb} at ${String(scan.threshold || "critical")}`;
    }
    case "semgrep": {
      const allowed = Number(stage.codeScan?.maxBlocking) || 0;
      const rules = (scan.rules || []).length ? scan.rules.join(" ") : "rules for the app type";
      return `Semgrep: ${rules} · fails over ${allowed} finding${allowed === 1 ? "" : "s"}`;
    }
    case "dependency_check":
      return `Dependency-Check · ${verb} at CVSS ${cvss(scan.failOnCvss)}+`;
    case "syft":
      return `SBOM of the source · ${scan.format === "spdx-json" ? "SPDX" : "CycloneDX"}`;
    default:
      return "Unknown scanner";
  }
}

/** What a save would refuse about a scan stage. Mirrors scan_stage.normalize. */
export function scanProblems(stage) {
  const scan = stage.scan;
  if (!scan?.tool) {
    return [{ field: "scan", message: "Choose what this stage scans with — a scan stage with no scanner cannot be saved." }];
  }
  const problems = [];
  if (scan.tool === "trivy_fs") {
    const bad = (scan.skipDirs || []).find((item) => !GLOB_RE.test(String(item)) || String(item).length > 200);
    if (bad) problems.push({ field: "scan", message: `“${bad}” is not a valid skip pattern — use paths and * globs only.` });
  }
  if (scan.tool === "semgrep") {
    const bad = (scan.rules || []).find((item) => !RULE_RE.test(String(item)) || String(item).length > 200);
    if (bad) problems.push({ field: "scan", message: `“${bad}” is not a valid rule set — a registry pack (p/java), a rule id or a path.` });
  }
  if (scan.tool === "dependency_check") {
    const raw = scan.failOnCvss;
    const score = Number(raw);
    if (raw === "" || raw === null || !Number.isFinite(score) || score < 0 || score > 10) {
      problems.push({ field: "scan", message: "The CVSS score to fail on must be a number from 0 to 10." });
    }
    const mirror = String(scan.nvdDatafeedUrl || "").trim();
    if (mirror && !URL_RE.test(mirror)) {
      problems.push({ field: "scan", message: "The NVD data mirror must be an http(s) URL." });
    }
  }
  return problems;
}
