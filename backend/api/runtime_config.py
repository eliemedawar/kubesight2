"""Runtime environment detection and the production boot checks.

KubeSight runs in three shapes: a developer laptop (``python app.py``, mock
Kubernetes, SQLite), the test suite, and a real installation. The first two must
keep working with no configuration at all; the third must never quietly run on
a development default — a hard-coded signing key, an auth kill switch or a
throwaway SQLite file — because every one of those fails *open*.

``is_production_env`` is the one place that decides which shape this process is
in, and ``enforce_production_config`` is the gate ``create_app`` runs before it
serves anything. It lives in its own module (not ``api/__init__``) so the
low-level helpers — ``auth_utils``, ``secret_encryption`` — can ask the same
question without an import cycle.
"""

from __future__ import annotations

import logging
import os
from typing import List

logger = logging.getLogger("kubesight.config")

# Any one of these naming production is enough. KUBESIGHT_ENV is the product's
# own; APP_ENV is what the Kubernetes manifests set; FLASK_ENV is honoured for
# older deployments that followed Flask's convention.
PRODUCTION_ENV_VARS = ("KUBESIGHT_ENV", "APP_ENV", "FLASK_ENV")
_PRODUCTION_VALUES = {"production", "prod"}

# The literal every key used to fall back to. Treated as "no key" everywhere:
# it is in the public source, so anything signed or encrypted with it is not.
INSECURE_DEVELOPMENT_KEY = "kubesight-dev-secret-change-me"

# Below this a signing key is guessable offline; warned about, not refused, so an
# upgrade does not take down an installation whose key is merely short.
_MIN_RECOMMENDED_KEY_LENGTH = 32

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


class InsecureConfigurationError(RuntimeError):
    """Raised when a production process is missing a configuration it must have."""


def is_production_env() -> bool:
    """Whether this process is a real installation.

    Decided by the environment name alone. ``FLASK_DEBUG`` is deliberately NOT
    consulted: it defaults to on for the local dev server, and a deployment that
    simply never set it must not be mistaken for a laptop. (A production
    process with ``FLASK_DEBUG`` on is warned about in the boot checks.)
    """
    for key in PRODUCTION_ENV_VARS:
        if os.getenv(key, "").strip().lower() in _PRODUCTION_VALUES:
            return True
    return False


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean env var; anything unrecognised means ``default``."""
    value = os.getenv(name, "").strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return default


def is_real_key(value: str) -> bool:
    """A key an operator actually chose — present, and not the public default."""
    value = (value or "").strip()
    return bool(value) and value != INSECURE_DEVELOPMENT_KEY


def configured_jwt_secret() -> str:
    """The operator-provided JWT signing key, or ``""`` if there is none."""
    for key in ("JWT_SECRET_KEY", "FLASK_SECRET_KEY"):
        value = os.getenv(key, "").strip()
        if is_real_key(value):
            return value
    return ""


def configured_encryption_key() -> str:
    """The operator-provided at-rest encryption key, or ``""`` if there is none.

    ``KUBESIGHT_SECRET_KEY`` is the dedicated key. ``ALERT_ROUTING_SECRET_KEY``
    is its older name and is still read, so an installation that set it keeps
    decrypting (and encrypting) exactly as before.
    """
    for key in ("KUBESIGHT_SECRET_KEY", "ALERT_ROUTING_SECRET_KEY"):
        value = os.getenv(key, "").strip()
        if is_real_key(value):
            return value
    return ""


def production_config_problems() -> List[str]:
    """Every reason a production process must refuse to start. Empty is good."""
    problems: List[str] = []
    if not configured_jwt_secret():
        problems.append(
            "JWT_SECRET_KEY is missing or set to the development default. "
            "Generate one with `openssl rand -hex 32`."
        )
    if not configured_encryption_key():
        problems.append(
            "KUBESIGHT_SECRET_KEY (or the older ALERT_ROUTING_SECRET_KEY) is missing "
            "or set to the development default. It encrypts stored credentials; "
            "generate one with `openssl rand -hex 32` and keep it stable — rows "
            "encrypted with the previous key (JWT_SECRET_KEY or the default) still decrypt."
        )
    if not env_flag("AUTH_REQUIRED", default=True):
        problems.append(
            "AUTH_REQUIRED is off. Disabling authentication is a local-debugging "
            "switch and is refused in production."
        )
    return problems


def enforce_production_config() -> None:
    """Refuse to boot a production process that would run on a dev default.

    Outside production this only logs what would have been refused, so a dev
    or mock install keeps starting with no configuration.
    """
    production = is_production_env()
    problems = production_config_problems()
    if production:
        if problems:
            message = "Refusing to start KubeSight in production:\n  - " + "\n  - ".join(problems)
            logger.critical(message)
            raise InsecureConfigurationError(message)
        jwt_secret = configured_jwt_secret()
        if len(jwt_secret) < _MIN_RECOMMENDED_KEY_LENGTH:
            logger.warning(
                "JWT_SECRET_KEY is shorter than %s characters; use `openssl rand -hex 32`.",
                _MIN_RECOMMENDED_KEY_LENGTH,
            )
        if jwt_secret == configured_encryption_key():
            logger.warning(
                "KUBESIGHT_SECRET_KEY equals JWT_SECRET_KEY. Use two different keys so "
                "rotating the session key does not orphan every stored credential."
            )
        if env_flag("FLASK_DEBUG", default=False):
            logger.warning("FLASK_DEBUG is on in production; turn it off.")
        return
    for problem in problems:
        logger.warning("Development mode (would be refused in production): %s", problem)
