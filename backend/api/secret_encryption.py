"""Encrypt sensitive alert-routing secrets at rest."""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    raw = (
        os.getenv("ALERT_ROUTING_SECRET_KEY")
        or os.getenv("JWT_SECRET_KEY")
        or "kubesight-dev-secret-change-me"
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
