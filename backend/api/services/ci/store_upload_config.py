"""What an App store upload stage is configured to publish — normalized, checked.

Pure functions, no database: the pipeline validator calls them on save (and
then checks the app exists, see pipelines._store_upload) and the executor
(``store_upload_stage.py``) reads the same shape back from the build's snapshot.

A store upload stage names ONE registered Mobile Application, ONE store and
ONE target on it — a Google Play track or App Store Connect's TestFlight — and
which of this build's binaries goes there. It publishes through the Mobile Apps
publish machinery, never around it: the same signature gate, the same steps,
the same release record.

Publishing is admin-only in KubeSight (``routes/mobile_apps.publish_build`` is
``@require_admin``). A build is often started by a webhook with no person
behind it, so — like a Deploy stage — the stage carries ``authorizedBy``: the
administrator who last saved its target, re-checked whenever it runs.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Optional

STORES = ("google_play", "app_store")
# Kept in step with mobile_app_service.PLAY_TRACKS / APP_STORE_TARGETS, which
# are what start_publish accepts. Duplicated (not imported) to keep this module
# free of the database layer; a test locks the two together.
PLAY_TRACKS = ("internal", "alpha", "beta", "production")
APP_STORE_TARGETS = ("testflight", "review")
ARTIFACT_TYPES = {"google_play": ("aab", "apk"), "app_store": ("ipa",)}
DEFAULT_TARGET = {"google_play": "internal", "app_store": "testflight"}
DEFAULT_ARTIFACT_TYPE = {"google_play": "aab", "app_store": "ipa"}
STORE_LABELS = {"google_play": "Google Play", "app_store": "App Store Connect"}

_PATTERN_RE = re.compile(r"^[A-Za-z0-9._*?/\[\]-]{1,200}$")


class StoreUploadConfigError(ValueError):
    """A store upload stage's configuration was rejected. Message is user-facing."""


def normalize(value: Any, stage_type: str, stage_name: str) -> Optional[Dict[str, Any]]:
    """A store upload stage's target, or None for any other stage.

    ``appId`` may be empty here: the pipeline validator fills it with the app
    linked to the service when there is exactly one. ``authorizedBy`` is never
    taken from the payload.
    """
    if stage_type != "store_upload":
        if value not in (None, "", {}):
            raise StoreUploadConfigError(
                f"Stage '{stage_name}' is a {stage_type} stage, so it publishes nothing. "
                "Store targets are set on an App store upload stage."
            )
        return None
    if not isinstance(value, dict):
        value = {}

    store = str(value.get("store") or "google_play").strip()
    if store not in STORES:
        raise StoreUploadConfigError(
            f"Stage '{stage_name}': the store must be Google Play (google_play) or "
            "App Store Connect (app_store)."
        )

    raw_app = value.get("appId")
    app_id: Optional[int] = None
    if raw_app not in (None, ""):
        try:
            app_id = int(raw_app)
        except (TypeError, ValueError):
            raise StoreUploadConfigError(f"Stage '{stage_name}': '{raw_app}' is not a mobile application.")
        if app_id <= 0:
            raise StoreUploadConfigError(f"Stage '{stage_name}': '{raw_app}' is not a mobile application.")

    target = str(value.get("target") or DEFAULT_TARGET[store]).strip().lower()
    allowed = PLAY_TRACKS if store == "google_play" else APP_STORE_TARGETS
    if target not in allowed:
        what = "track" if store == "google_play" else "target"
        raise StoreUploadConfigError(
            f"Stage '{stage_name}': the {STORE_LABELS[store]} {what} must be one of {', '.join(allowed)}."
        )

    artifact_type = str(value.get("artifactType") or DEFAULT_ARTIFACT_TYPE[store]).strip().lower()
    if artifact_type not in ARTIFACT_TYPES[store]:
        raise StoreUploadConfigError(
            f"Stage '{stage_name}': {STORE_LABELS[store]} takes "
            f"{' or '.join(t.upper() for t in ARTIFACT_TYPES[store])} files, not {artifact_type.upper() or 'nothing'}."
        )

    pattern = str(value.get("artifactPattern") or "").strip()
    if pattern and not _PATTERN_RE.match(pattern):
        raise StoreUploadConfigError(
            f"Stage '{stage_name}': '{pattern}' is not a file name pattern (letters, digits, "
            "'.', '-', '_', '/', and the wildcards * ? [ ])."
        )

    return {
        "appId": app_id,
        "store": store,
        "target": target,
        "artifactType": artifact_type,
        "artifactPattern": pattern,
    }


def signature(config: Optional[Dict[str, Any]]) -> str:
    """Everything an administrator authorizes by saving the target.

    Change any of it and the stage would publish a different app, to a
    different place, or a different file — so whoever saves it next must be
    able to publish themselves.
    """
    if not isinstance(config, dict):
        return ""
    material = {
        "appId": config.get("appId") or 0,
        "store": config.get("store") or "",
        "target": config.get("target") or "",
        "artifactType": config.get("artifactType") or "",
        "artifactPattern": config.get("artifactPattern") or "",
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def target_label(config: Dict[str, Any]) -> str:
    """'Google Play (internal track)' / 'App Store Connect (TestFlight)'."""
    store = config.get("store") or ""
    target = config.get("target") or ""
    if store == "google_play":
        return f"Google Play ({target} track)"
    if target == "testflight":
        return "App Store Connect (TestFlight)"
    return "App Store Connect (submitted for App Review)"
