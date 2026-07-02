"""TOTP MFA helpers — secret generation, provisioning URIs, QR codes, verification.

Uses :mod:`pyotp` for RFC-6238 TOTP (compatible with Google Authenticator,
Microsoft Authenticator, Authy, 1Password, …) and :mod:`segno` to render the
provisioning URI as an inline PNG data URI the frontend can drop straight into an
``<img>`` tag — no client-side QR library or external asset fetch required.
"""

from __future__ import annotations

import os

import pyotp
import segno

# Displayed as the account's issuer in the authenticator app.
_ISSUER = os.getenv("TOTP_ISSUER", "KubeSight").strip() or "KubeSight"


def generate_totp_secret() -> str:
    """Return a fresh base32 TOTP secret."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, account_name: str) -> str:
    """Build the ``otpauth://`` URI encoded into the QR code."""
    return pyotp.TOTP(secret).provisioning_uri(
        name=account_name or "user",
        issuer_name=_ISSUER,
    )


def qr_data_uri(uri: str) -> str:
    """Render an otpauth URI as a self-contained PNG ``data:`` URI.

    Returns an empty string if QR rendering fails for any reason — enrolment must
    still succeed, since the user can always type the base32 secret into their
    authenticator app manually.
    """
    try:
        qr = segno.make(uri, error="m")
        return qr.png_data_uri(scale=6, border=2)
    except Exception:
        return ""


def build_enrollment(secret: str, account_name: str) -> dict:
    """Return everything the setup screen needs: secret, URI and inline QR."""
    uri = provisioning_uri(secret, account_name)
    return {
        "secret": secret,
        "otpauthUri": uri,
        "qrDataUri": qr_data_uri(uri),
        "issuer": _ISSUER,
        "accountName": account_name,
    }


def verify_totp(secret: str, code: str) -> bool:
    """Validate a 6-digit TOTP code, tolerating ±1 time step for clock drift."""
    if not secret or not code:
        return False
    normalized = str(code).strip().replace(" ", "")
    if not normalized.isdigit():
        return False
    try:
        return bool(pyotp.TOTP(secret).verify(normalized, valid_window=1))
    except Exception:
        return False
