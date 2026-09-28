"""Encrypt sensitive secrets (credentials, tokens, TOTP seeds) at rest.

The key is ``KUBESIGHT_SECRET_KEY`` — or its older name
``ALERT_ROUTING_SECRET_KEY``, still read so existing installations are
unchanged. Before a dedicated key existed, secrets were encrypted with
``JWT_SECRET_KEY`` or, with nothing configured, the public development default.
New ciphertext is always written with the primary key; reads also try those
legacy keys (and any ``KUBESIGHT_PREVIOUS_SECRET_KEYS``, comma-separated, for a
rotation), so introducing or rotating the key never orphans stored rows.

In production the primary key must be a dedicated, operator-chosen one — the
boot checks in ``runtime_config`` refuse to start otherwise, and ``_primary_key``
refuses too, in case something encrypts before ``create_app`` ran.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import List, Optional

from cryptography.fernet import Fernet, InvalidToken

from .runtime_config import (
    INSECURE_DEVELOPMENT_KEY as _INSECURE_DEVELOPMENT_KEY,
    InsecureConfigurationError,
    configured_encryption_key,
    is_production_env,
    is_real_key,
)

logger = logging.getLogger(__name__)

_legacy_key_warned = False


def secret_encryption_key_configured() -> bool:
    """Whether secrets are protected by an operator-provided key."""
    if configured_encryption_key():
        return True
    return is_real_key(os.getenv("JWT_SECRET_KEY", ""))


def _primary_key() -> str:
    dedicated = configured_encryption_key()
    if dedicated:
        return dedicated
    if is_production_env():
        raise InsecureConfigurationError(
            "KUBESIGHT_SECRET_KEY must be set in production to encrypt stored secrets."
        )
    # Development: keep the historical fallback chain so a laptop install with
    # nothing configured still encrypts and decrypts consistently.
    jwt_secret = os.getenv("JWT_SECRET_KEY", "").strip()
    return jwt_secret or _INSECURE_DEVELOPMENT_KEY


def _decryption_keys() -> List[str]:
    """Primary key first, then every key older rows may have been written with."""
    candidates = [_primary_key()]
    candidates.extend(
        part.strip()
        for part in os.getenv("KUBESIGHT_PREVIOUS_SECRET_KEYS", "").split(",")
        if part.strip()
    )
    candidates.extend(
        os.getenv(name, "").strip()
        for name in ("KUBESIGHT_SECRET_KEY", "ALERT_ROUTING_SECRET_KEY", "JWT_SECRET_KEY")
    )
    candidates.append(_INSECURE_DEVELOPMENT_KEY)
    ordered: List[str] = []
    for raw in candidates:
        if raw and raw not in ordered:
            ordered.append(raw)
    return ordered


def _fernet_for(raw: str) -> Fernet:
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _fernet() -> Fernet:
    return _fernet_for(_primary_key())


def encrypt_secret(plain: str) -> str:
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def _decrypt_with_any_key(cipher: str) -> Optional[str]:
    global _legacy_key_warned
    try:
        token = cipher.encode("ascii")
    except UnicodeEncodeError:
        return None
    for index, raw in enumerate(_decryption_keys()):
        try:
            plain = _fernet_for(raw).decrypt(token).decode("utf-8")
        except InvalidToken:
            continue
        if index and not _legacy_key_warned:
            _legacy_key_warned = True
            logger.warning(
                "A stored secret was encrypted with a legacy key (not KUBESIGHT_SECRET_KEY). "
                "It still decrypts; saving it again re-encrypts it with the current key."
            )
        return plain
    return None


def decrypt_secret(cipher: str) -> str:
    if not cipher:
        return ""
    plain = _decrypt_with_any_key(cipher)
    return plain if plain is not None else ""


def decrypt_secret_or_none(cipher: str) -> Optional[str]:
    """Like ``decrypt_secret`` but tells "not a valid ciphertext" apart from empty.

    For columns being migrated from plaintext, where a value that does not
    decrypt may be a legacy plaintext row rather than garbage.
    """
    if not cipher:
        return None
    return _decrypt_with_any_key(cipher)


def looks_encrypted(value: str) -> bool:
    """Whether ``value`` has the shape of a Fernet token (version byte 0x80)."""
    return bool(value) and value.startswith("gAAAAA")
