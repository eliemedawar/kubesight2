"""CI secrets: encrypted storage and build-time resolution.

Two invariants this module exists to hold:

1. A value is written once and read only by the build path. Every list/get
   route serializes through :func:`~.serializers.secret_to_dict`, which has no
   branch that can emit ``value_cipher``.
2. A pipeline stores *references*. The plaintext is joined in at dispatch, put
   into the runner's environment, and handed to the log masker so the same
   value cannot reappear in output.

Global secrets (``scope='global'``) are visible to every service; service
secrets shadow a global of the same key, so a service can override a shared
default without renaming anything.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ...audit import log_audit
from ...db import db
from ...models_ci import CiSecret
from ...secret_encryption import decrypt_secret, encrypt_secret
from .serializers import secret_to_dict

MAX_VALUE_CHARS = 100_000


class SecretError(ValueError):
    """A secret payload was rejected. Message is user-facing."""


def _clean_key(value: Any) -> str:
    key = " ".join(str(value or "").split())[:120]
    if not key:
        raise SecretError("A secret name is required.")
    if not all(char.isalnum() or char in "_-." for char in key):
        raise SecretError(
            "Secret names may contain letters, digits, '_', '-', and '.' only."
        )
    return key


def list_secrets(service_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Service secrets plus the global ones that apply to it."""
    if service_id is None:
        rows = (
            CiSecret.query.filter(CiSecret.scope == "global")
            .order_by(CiSecret.key.asc())
            .all()
        )
    else:
        rows = (
            CiSecret.query.filter(
                db.or_(CiSecret.service_id == service_id, CiSecret.scope == "global")
            )
            .order_by(CiSecret.scope.desc(), CiSecret.key.asc())
            .all()
        )
    return [secret_to_dict(row) for row in rows]


def get_secret(secret_id: int) -> CiSecret:
    row = db.session.get(CiSecret, int(secret_id))
    if row is None:
        raise LookupError("Secret not found.")
    return row


def create_secret(
    payload: Dict[str, Any], *, service_id: Optional[int] = None, actor=None
) -> Dict[str, Any]:
    key = _clean_key(payload.get("key"))
    value = str(payload.get("value") or "")
    if not value:
        raise SecretError("A secret value is required.")
    if len(value) > MAX_VALUE_CHARS:
        raise SecretError("That secret value is too large.")

    scope = "service" if service_id is not None else "global"
    # PostgreSQL treats NULL service_id as distinct, so the table's unique
    # constraint cannot police the global scope. Check both scopes here.
    existing = CiSecret.query.filter_by(scope=scope, service_id=service_id, key=key).first()
    if existing:
        raise SecretError(f"A {scope} secret named '{key}' already exists.")

    row = CiSecret(
        scope=scope,
        service_id=service_id,
        key=key,
        value_cipher=encrypt_secret(value),
        description=" ".join(str(payload.get("description") or "").split())[:255] or None,
        created_by_user_id=getattr(actor, "id", None),
    )
    db.session.add(row)
    db.session.commit()
    log_audit(
        "ci_secret_created",
        actor=actor,
        target_type="ci_secret",
        target_id=str(row.id),
        # The key is recorded; the value never is.
        details={"scope": scope, "serviceId": service_id, "key": key},
    )
    return secret_to_dict(row)


def update_secret(row: CiSecret, payload: Dict[str, Any], *, actor=None) -> Dict[str, Any]:
    if "description" in payload:
        row.description = (
            " ".join(str(payload.get("description") or "").split())[:255] or None
        )
    rotated = False
    if payload.get("value"):
        value = str(payload["value"])
        if len(value) > MAX_VALUE_CHARS:
            raise SecretError("That secret value is too large.")
        row.value_cipher = encrypt_secret(value)
        rotated = True
    row.updated_at = datetime.now(timezone.utc)
    db.session.add(row)
    db.session.commit()
    log_audit(
        "ci_secret_updated",
        actor=actor,
        target_type="ci_secret",
        target_id=str(row.id),
        details={"key": row.key, "scope": row.scope, "valueRotated": rotated},
    )
    return secret_to_dict(row)


def delete_secret(row: CiSecret, *, actor=None) -> None:
    key, scope, service_id = row.key, row.scope, row.service_id
    secret_id = row.id
    db.session.delete(row)
    db.session.commit()
    log_audit(
        "ci_secret_deleted",
        actor=actor,
        target_type="ci_secret",
        target_id=str(secret_id),
        details={"key": key, "scope": scope, "serviceId": service_id},
    )


# ---------------------------------------------------------------------------
# Build-time resolution
# ---------------------------------------------------------------------------

def resolve_for_service(service_id: int) -> Dict[str, str]:
    """``{key: plaintext}`` for one service — service scope shadows global.

    The result is held in memory for the duration of a dispatch and is never
    written to the database, an API response, or a log.
    """
    resolved: Dict[str, str] = {}
    rows = (
        CiSecret.query.filter(
            db.or_(CiSecret.service_id == service_id, CiSecret.scope == "global")
        )
        # Global first so a same-named service secret overwrites it.
        .order_by(CiSecret.scope.asc())
        .all()
    )
    for row in rows:
        if row.scope == "global" or row.service_id == service_id:
            resolved[row.key] = decrypt_secret(row.value_cipher or "")
    return resolved


def env_for_stage(
    stage_definition: Dict[str, Any], resolved: Dict[str, str]
) -> Dict[str, str]:
    """Map a stage's ``secretRefs`` onto environment variables."""
    env: Dict[str, str] = {}
    for ref in stage_definition.get("secretRefs") or []:
        if not isinstance(ref, dict):
            continue
        name = ref.get("name")
        if name and name in resolved:
            env[ref.get("envVar") or name] = resolved[name]
    return env


def mark_used(service_id: int, keys: List[str]) -> None:
    """Record that a build consumed these secrets — feeds an unused-secret audit."""
    if not keys:
        return
    now = datetime.now(timezone.utc)
    CiSecret.query.filter(
        CiSecret.key.in_(list(keys)),
        db.or_(CiSecret.service_id == service_id, CiSecret.scope == "global"),
    ).update({"last_used_at": now}, synchronize_session=False)
