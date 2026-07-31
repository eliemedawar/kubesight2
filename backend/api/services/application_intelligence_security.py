"""Trust-boundary helpers for untrusted repositories and AI evidence."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

ALLOWED_BITBUCKET_HOSTS = {"bitbucket.org", "www.bitbucket.org"}
EXCLUDED_DIRECTORIES = {
    ".git",
    "node_modules",
    "vendor",
    "target",
    "build",
    "dist",
    "bin",
    "obj",
    "coverage",
    ".cache",
}
SECRET_KEY_PATTERN = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|private[_-]?key|client[_-]?secret)",
    re.IGNORECASE,
)
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)^(\s*[A-Z][A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|PRIVATE_KEY)"
    r"[A-Z0-9_]*\s*[:=]\s*)([^\r\n#]+)"
)
GENERIC_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"""(?ix)
    (
      (?:
        ["']?[A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|
        private[_-]?key|client[_-]?secret)[A-Za-z0-9_.-]*["']?
      )
      \s*[:=]\s*
      ["']
    )
    ([^"'\r\n]+)
    (["'])
    """
)
PEM_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
URL_CREDENTIAL_PATTERN = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)([^/@:\s]+):([^/@\s]+)@"
)


def validate_repository_url(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Repository URL is required.")
    if len(raw) > 1024:
        raise ValueError("Repository URL is too long.")
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_BITBUCKET_HOSTS:
        raise ValueError("Use an HTTPS Bitbucket Cloud repository URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Repository URLs must not contain credentials, query strings, or fragments.")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise ValueError("Repository URL must identify a Bitbucket workspace and repository.")
    workspace, repository = parts
    repository = repository[:-4] if repository.endswith(".git") else repository
    allowed = re.compile(r"^[A-Za-z0-9._-]+$")
    if not allowed.fullmatch(workspace) or not allowed.fullmatch(repository):
        raise ValueError("Repository workspace or name contains unsupported characters.")
    normalized = f"https://bitbucket.org/{workspace}/{repository}.git"
    return normalized, f"{workspace}/{repository}"


def validate_relative_path(value: str | None, label: str) -> str | None:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return None
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or "\x00" in raw:
        raise ValueError(f"{label} must be a repository-relative path.")
    return str(path)


def redact_text(value: str, *, max_chars: int = 200_000) -> str:
    text = str(value or "")[:max_chars]
    text = URL_CREDENTIAL_PATTERN.sub(r"\1***:***@", text)
    text = SECRET_ASSIGNMENT_PATTERN.sub(r"\1[REDACTED]", text)
    text = GENERIC_SECRET_ASSIGNMENT_PATTERN.sub(r"\1[REDACTED]\3", text)
    text = PEM_PRIVATE_KEY_PATTERN.sub("[REDACTED PRIVATE KEY]", text)
    # Common bearer/basic token forms in scanner error output.
    text = re.sub(
        r"(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]+",
        r"\1[REDACTED]",
        text,
    )
    return text


def redact_structure(value: Any, *, depth: int = 0) -> Any:
    if depth > 20:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result = {}
        object_is_secret = bool(
            value.get("secret") is True
            or str(value.get("classification") or "").lower() == "secret"
            or str(value.get("type") or "").lower() == "secret"
        )
        for key, item in value.items():
            key_text = str(key)
            sensitive_leaf = SECRET_KEY_PATTERN.search(key_text) or (
                object_is_secret and key_text.lower() in {"value", "default", "example"}
            )
            if sensitive_leaf and not isinstance(item, (dict, list)):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = redact_structure(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [redact_structure(item, depth=depth + 1) for item in value[:5000]]
    if isinstance(value, str):
        return redact_text(value)
    return value


def bounded_json_bytes(value: Any, max_bytes: int) -> bytes:
    safe = redact_structure(value)
    encoded = json.dumps(safe, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError("The bounded analysis package exceeds the configured size limit.")
    return encoded


def safe_error(exc: BaseException, fallback: str = "Analysis failed safely.") -> str:
    message = redact_text(str(exc), max_chars=1000).strip()
    return message or fallback
