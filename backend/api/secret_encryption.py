"""Encrypt stored credential secrets at rest with a dedicated key."""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import and_, select

_INSECURE_DEVELOPMENT_KEY = "kubesight-dev-secret-change-me"
_ENVELOPE_VERSION = "ks1"


def secret_encryption_key_configured() -> bool:
    """Whether secrets are protected by an operator-provided key."""
    raw = (os.getenv("ALERT_ROUTING_SECRET_KEY") or "").strip()
    return bool(raw and raw != _INSECURE_DEVELOPMENT_KEY)


def _primary_key() -> str:
    return (
        (os.getenv("ALERT_ROUTING_SECRET_KEY") or "").strip()
        or _INSECURE_DEVELOPMENT_KEY
    )


def _keyring() -> list[str]:
    keys = [_primary_key()]
    previous = os.getenv("ALERT_ROUTING_SECRET_KEY_PREVIOUS", "")
    for candidate in previous.split(","):
        normalized = candidate.strip()
        if normalized and normalized not in keys:
            keys.append(normalized)
    return keys


def _key_id(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _fernet(raw: str) -> Fernet:
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    if not plain:
        return ""
    primary = _primary_key()
    token = _fernet(primary).encrypt(plain.encode("utf-8")).decode("ascii")
    return f"{_ENVELOPE_VERSION}:{_key_id(primary)}:{token}"


def decrypt_secret(cipher: str) -> str:
    if not cipher:
        return ""
    candidates = _keyring()
    token = cipher
    if cipher.startswith(f"{_ENVELOPE_VERSION}:"):
        try:
            _version, expected_key_id, token = cipher.split(":", 2)
        except ValueError:
            return ""
        candidates = [
            key for key in candidates if _key_id(key) == expected_key_id
        ]
    for raw in candidates:
        try:
            return _fernet(raw).decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            continue
    return ""


def secret_needs_rotation(cipher: str) -> bool:
    """Whether a ciphertext is legacy or was written by a non-primary key."""
    if not cipher:
        return False
    expected_prefix = f"{_ENVELOPE_VERSION}:{_key_id(_primary_key())}:"
    return not cipher.startswith(expected_prefix)


def rotate_encrypted_secret(cipher: str) -> str:
    """Re-encrypt one stored value with the primary key, preserving blanks."""
    if not cipher or not secret_needs_rotation(cipher):
        return cipher
    plain = decrypt_secret(cipher)
    if not plain:
        raise ValueError("Stored secret cannot be decrypted with the configured keyring.")
    return encrypt_secret(plain)


def rotate_database_secrets(*, dry_run: bool = False) -> dict[str, Any]:
    """Validate and rotate every conventionally named encrypted DB column.

    The operation is one transaction: an unreadable value rolls the entire
    rotation back, so an operator never ends up needing two partially applied
    keyrings. No plaintext or ciphertext is returned in the report.
    """
    from .db import db

    suffixes = ("_encrypted", "_cipher")
    scanned = 0
    rotated = 0
    per_table: dict[str, int] = {}
    try:
        for table in db.metadata.sorted_tables:
            secret_columns = [
                column
                for column in table.columns
                if column.name.endswith(suffixes)
            ]
            primary_keys = list(table.primary_key.columns)
            if not secret_columns or not primary_keys:
                continue
            rows = db.session.execute(
                select(*primary_keys, *secret_columns)
            ).mappings()
            for row in rows:
                updates: dict[str, str] = {}
                for column in secret_columns:
                    cipher = row[column.name]
                    if not cipher:
                        continue
                    scanned += 1
                    if secret_needs_rotation(cipher):
                        updates[column.name] = rotate_encrypted_secret(cipher)
                if not updates:
                    continue
                rotated += len(updates)
                per_table[table.name] = per_table.get(table.name, 0) + len(updates)
                if dry_run:
                    continue
                predicate = and_(
                    *(
                        column == row[column.name]
                        for column in primary_keys
                    )
                )
                db.session.execute(
                    table.update().where(predicate).values(**updates)
                )
        if dry_run:
            db.session.rollback()
        else:
            db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return {
        "dryRun": dry_run,
        "scanned": scanned,
        "rotated": rotated,
        "tables": per_table,
    }
