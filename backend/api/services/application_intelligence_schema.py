"""Strict Hermes response validation.

Unknown top-level keys are rejected so repository-controlled prompt injection
cannot smuggle arbitrary content into persisted results.
"""

from __future__ import annotations

from typing import Any

TOP_LEVEL_KEYS = {
    "application_profile",
    "architecture_summary",
    "findings",
    "communications",
    "api_inventory",
    "configuration_inventory",
    "secret_requirements",
    "docker_analysis",
    "operational_readiness",
    "kubernetes_recommendations",
    "risk_summary",
    "limitations",
}
REQUIRED_KEYS = TOP_LEVEL_KEYS
LIST_KEYS = {
    "findings",
    "communications",
    "api_inventory",
    "configuration_inventory",
    "secret_requirements",
    "limitations",
}
OBJECT_KEYS = TOP_LEVEL_KEYS - LIST_KEYS
SEVERITIES = {"Critical", "High", "Medium", "Low", "Informational"}
CONFIDENCES = {"Confirmed", "High", "Medium", "Low", "Informational"}
EVIDENCE_STATES = {
    "Source Inferred",
    "Configuration Declared",
    "Kubernetes Declared",
    "Runtime Observed",
    "Trace Confirmed",
}


def validate_hermes_output(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Hermes returned an invalid response.")
    keys = set(payload)
    if keys != REQUIRED_KEYS:
        missing = sorted(REQUIRED_KEYS - keys)
        unknown = sorted(keys - TOP_LEVEL_KEYS)
        detail = []
        if missing:
            detail.append(f"missing fields: {', '.join(missing)}")
        if unknown:
            detail.append(f"unknown fields: {', '.join(unknown)}")
        raise ValueError("Hermes response schema mismatch (" + "; ".join(detail) + ").")
    for key in LIST_KEYS:
        if not isinstance(payload[key], list):
            raise ValueError(f"Hermes field '{key}' must be an array.")
    for key in OBJECT_KEYS:
        if not isinstance(payload[key], dict):
            raise ValueError(f"Hermes field '{key}' must be an object.")
    if len(payload["findings"]) > 5000 or len(payload["communications"]) > 5000:
        raise ValueError("Hermes response exceeds the item limit.")
    for index, finding in enumerate(payload["findings"]):
        if not isinstance(finding, dict):
            raise ValueError(f"Hermes finding {index} is invalid.")
        for field in ("title", "category", "severity", "confidence", "description"):
            if not isinstance(finding.get(field), str) or not finding[field].strip():
                raise ValueError(f"Hermes finding {index} is missing '{field}'.")
        if finding["severity"] not in SEVERITIES:
            raise ValueError(f"Hermes finding {index} has an invalid severity.")
        if finding["confidence"] not in CONFIDENCES:
            raise ValueError(f"Hermes finding {index} has an invalid confidence.")
        for line_field in ("line", "start_line", "end_line"):
            if finding.get(line_field) is not None and not isinstance(
                finding[line_field], int
            ):
                raise ValueError(
                    f"Hermes finding {index} has an invalid '{line_field}'."
                )
    for index, communication in enumerate(payload["communications"]):
        if not isinstance(communication, dict):
            raise ValueError(f"Hermes communication {index} is invalid.")
        for field in ("source", "destination", "destination_type", "confidence"):
            if not isinstance(communication.get(field), str) or not communication[field].strip():
                raise ValueError(
                    f"Hermes communication {index} is missing '{field}'."
                )
        if communication["confidence"] not in CONFIDENCES:
            raise ValueError(f"Hermes communication {index} has an invalid confidence.")
        if communication.get("port") is not None and not isinstance(
            communication["port"], int
        ):
            raise ValueError(f"Hermes communication {index} has an invalid port.")
        if communication.get("line") is not None and not isinstance(
            communication["line"], int
        ):
            raise ValueError(f"Hermes communication {index} has an invalid line.")
        if communication.get("required") is not None and not isinstance(
            communication["required"], bool
        ):
            raise ValueError(
                f"Hermes communication {index} has an invalid required flag."
            )
        evidence_state = communication.get("evidence_state", "Source Inferred")
        if evidence_state not in EVIDENCE_STATES:
            raise ValueError(
                f"Hermes communication {index} has an invalid evidence state."
            )
    for key in ("api_inventory", "configuration_inventory", "secret_requirements"):
        if any(not isinstance(item, dict) for item in payload[key]):
            raise ValueError(f"Hermes field '{key}' contains an invalid item.")
    return payload


def empty_result() -> dict:
    """Valid empty evidence shape for deterministic tests, never a fake result."""
    return {
        "application_profile": {},
        "architecture_summary": {},
        "findings": [],
        "communications": [],
        "api_inventory": [],
        "configuration_inventory": [],
        "secret_requirements": [],
        "docker_analysis": {},
        "operational_readiness": {},
        "kubernetes_recommendations": {},
        "risk_summary": {},
        "limitations": [],
    }
