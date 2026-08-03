#!/usr/bin/env python3
"""Fail on every npm advisory except a narrow, reviewed exception.

The allowlist is not a package suppression list. An entry binds one GHSA to
exact installed versions and expires on its review date. A different advisory
on the same package still fails the build.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

GHSA_RE = re.compile(
    r"^GHSA-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}$",
    re.IGNORECASE,
)


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _advisory_id(url: object) -> str:
    if not isinstance(url, str):
        return ""
    return url.rstrip("/").rsplit("/", 1)[-1].upper()


def _validate_policy(policy: dict[str, Any], today: dt.date) -> tuple[dict[tuple[str, str], dict[str, Any]], list[str]]:
    errors: list[str] = []
    entries = policy.get("entries")
    if policy.get("schemaVersion") != 1 or not isinstance(entries, list):
        return {}, ["allowlist must have schemaVersion 1 and an entries array"]

    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for number, entry in enumerate(entries, 1):
        prefix = f"allowlist entry {number}"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} must be an object")
            continue
        advisory = str(entry.get("advisory", "")).upper()
        package = str(entry.get("package", ""))
        versions = entry.get("versions")
        reason = str(entry.get("reason", "")).strip()
        owner = str(entry.get("owner", "")).strip()
        try:
            review_by = dt.date.fromisoformat(str(entry.get("reviewBy", "")))
        except ValueError:
            review_by = None

        if not GHSA_RE.fullmatch(advisory):
            errors.append(f"{prefix} advisory must be a GHSA identifier")
        if not package:
            errors.append(f"{prefix} package is required")
        if not isinstance(versions, list) or not versions or any(not isinstance(v, str) or not v for v in versions):
            errors.append(f"{prefix} versions must be a non-empty exact-version array")
        if len(reason) < 40:
            errors.append(f"{prefix} reason must document the concrete mitigation")
        if not owner:
            errors.append(f"{prefix} owner is required")
        if review_by is None:
            errors.append(f"{prefix} reviewBy must be an ISO date")
        elif review_by <= today:
            errors.append(f"{prefix} expired on {review_by}; review or remove it")

        key = (advisory, package)
        if key in indexed:
            errors.append(f"{prefix} duplicates {advisory} for {package}")
        indexed[key] = entry
    return indexed, errors


def _node_versions(lock: dict[str, Any], nodes: list[object]) -> set[str]:
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        return set()
    versions: set[str] = set()
    for node in nodes:
        if not isinstance(node, str):
            continue
        item = packages.get(node.replace("node_modules/", "node_modules/", 1))
        if isinstance(item, dict) and isinstance(item.get("version"), str):
            versions.add(item["version"])
    return versions


def evaluate(audit: dict[str, Any], lock: dict[str, Any], policy: dict[str, Any], today: dt.date) -> tuple[list[str], list[str]]:
    allowlist, errors = _validate_policy(policy, today)
    notices: list[str] = []
    used: set[tuple[str, str]] = set()
    vulnerabilities = audit.get("vulnerabilities", {})
    if not isinstance(vulnerabilities, dict):
        return errors + ["npm audit JSON has no vulnerabilities object"], notices

    for package, vulnerability in vulnerabilities.items():
        if not isinstance(vulnerability, dict):
            errors.append(f"malformed npm audit record for {package}")
            continue
        nodes = vulnerability.get("nodes") if isinstance(vulnerability.get("nodes"), list) else []
        versions = _node_versions(lock, nodes)
        via = vulnerability.get("via") if isinstance(vulnerability.get("via"), list) else []
        for advisory in via:
            if not isinstance(advisory, dict):
                errors.append(f"{package}: transitive advisory {advisory!r} is not allowlisted")
                continue
            advisory_id = _advisory_id(advisory.get("url"))
            key = (advisory_id, str(package))
            entry = allowlist.get(key)
            title = str(advisory.get("title", "unknown advisory"))
            if entry is None:
                errors.append(f"{package} {sorted(versions)}: {advisory_id or title} is not allowlisted")
                continue
            allowed_versions = set(entry["versions"])
            if not versions or not versions.issubset(allowed_versions):
                errors.append(
                    f"{package}: {advisory_id} allows only {sorted(allowed_versions)}, installed {sorted(versions) or ['unknown']}"
                )
                continue
            used.add(key)
            notices.append(
                f"accepted {advisory_id} for {package} {sorted(versions)} until {entry['reviewBy']}: {entry['reason']}"
            )

    for key, entry in sorted(allowlist.items()):
        if key not in used:
            notices.append(f"unused allowlist entry {key[0]} for {key[1]} (review by {entry['reviewBy']})")
    return errors, notices


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--allowlist", type=Path, required=True)
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args(argv)
    try:
        errors, notices = evaluate(_load(args.audit), _load(args.lock), _load(args.allowlist), args.today)
    except ValueError as exc:
        print(f"npm audit gate: {exc}", file=sys.stderr)
        return 2
    for notice in notices:
        print(f"NOTICE: {notice}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        print(f"npm audit gate rejected {len(errors)} item(s)", file=sys.stderr)
        return 1
    print("npm audit gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
