"""Encrypt sensitive alert-routing secrets at rest."""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

_INSECURE_DEVELOPMENT_KEY = "kubesight-dev-secret-change-me"


def secret_encryption_key_configured() -> bool:
    """Whether secrets are protected by an operator-provided key."""
    raw = (
        os.getenv("ALERT_ROUTING_SECRET_KEY")
        or os.getenv("JWT_SECRET_KEY")
        or ""
    ).strip()
    return bool(raw and raw != _INSECURE_DEVELOPMENT_KEY)


def _fernet() -> Fernet:
    raw = (
        os.getenv("ALERT_ROUTING_SECRET_KEY")
        or os.getenv("JWT_SECRET_KEY")
        or _INSECURE_DEVELOPMENT_KEY
    )
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(cipher: str) -> str:
    if not cipher:
        return ""
    try:
        return _fernet().decrypt(cipher.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return ""
