"""Secret scrubbing for build logs.

``kubeadm init`` output contains the bootstrap token and the certificate key,
and step logs are rendered in the UI — so every log line passes through here
BEFORE persisting, without exception. The discovery CA-cert hash
(``sha256:<hex>``) is deliberately preserved: it is public verification data
and operators need it for debugging.
"""

from __future__ import annotations

import re

# kubeadm bootstrap token: 6 chars, dot, 16 chars ([a-z0-9]).
_TOKEN_RE = re.compile(r"\b[a-z0-9]{6}\.[a-z0-9]{16}\b")
# --token / --certificate-key argument values (belt and braces).
_TOKEN_ARG_RE = re.compile(r"(--token[=\s]+)\S+")
_CERT_KEY_ARG_RE = re.compile(r"(--certificate-key[=\s]+)[0-9a-fA-F]+")
# Bare 64-hex certificate key — but NOT the public sha256: CA hash.
_HEX64_RE = re.compile(r"(?<!sha256:)\b[0-9a-f]{64}\b")
# VRRP auth pass / generic "password: xxx" trailers in config echoes.
# keepalived's auth_pass is space-separated; generic forms use : or =.
_PASSWORD_LINE_RE = re.compile(
    r"((?:auth_pass|password|passwd)\s*[:=\s]\s*)\S+", re.IGNORECASE
)
# Shell/config assignments whose names imply a secret. Keep this broad because
# traced build inputs can include repository or registry setup scripts.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"^(\s*[A-Za-z_][A-Za-z0-9_]*(?:PASSWORD|PASSWD|TOKEN|SECRET|PRIVATE_KEY)"
    r"[A-Za-z0-9_]*\s*=\s*)(.*)$",
    re.IGNORECASE | re.MULTILINE,
)
_BEARER_RE = re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.IGNORECASE)
# Credentials embedded in an outbound proxy URL, e.g. http://user:pass@proxy.
_URI_PASSWORD_RE = re.compile(
    r"((?:https?://)[^/\s:@]+:)[^@\s/]+(@)", re.IGNORECASE
)
_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)
_KUBECONFIG_KEY_RE = re.compile(
    r"(^\s*client-key-data:\s*).+$", re.IGNORECASE | re.MULTILINE
)

_REDACTED = "[REDACTED]"


def scrub(text: str) -> str:
    if not text:
        return text
    text = _TOKEN_ARG_RE.sub(rf"\1{_REDACTED}", text)
    text = _CERT_KEY_ARG_RE.sub(rf"\1{_REDACTED}", text)
    text = _TOKEN_RE.sub(_REDACTED, text)
    text = _HEX64_RE.sub(_REDACTED, text)
    text = _PASSWORD_LINE_RE.sub(rf"\1{_REDACTED}", text)
    text = _SECRET_ASSIGNMENT_RE.sub(rf"\1{_REDACTED}", text)
    text = _BEARER_RE.sub(rf"\1{_REDACTED}", text)
    text = _URI_PASSWORD_RE.sub(rf"\1{_REDACTED}\2", text)
    text = _PRIVATE_KEY_BLOCK_RE.sub(_REDACTED, text)
    text = _KUBECONFIG_KEY_RE.sub(rf"\1{_REDACTED}", text)
    return text
