"""Proactive cache warming.

First clicks on the Namespaces / Clients / Inventory tabs previously paid the
full kubectl cost (multiple seconds). Warming runs the same cached reads in a
background thread — at app startup and again whenever the cluster list is
requested (throttled) — so by the time a user opens a tab, the data is served
from cache and kept fresh by stale-while-revalidate.

The first pass warms everything. Later passes only fill entries that are
MISSING (never built, or aged out of their stale window): an entry that is
still there is kept fresh by the readers' own stale-while-revalidate, and
re-warming it every couple of minutes only re-ran every cluster's kubectl
reads whether or not anyone was looking. One pass runs at a time.
"""

from __future__ import annotations

import logging
import os
import threading

from .ttl_cache import TTLCache

logger = logging.getLogger(__name__)

_warm_flags = TTLCache("cache-warmer")
_WARM_INTERVAL_SECONDS = int(os.getenv("CACHE_WARM_INTERVAL_SECONDS", "120"))
_pass_lock = threading.Lock()
_state = {"booted": False}


def warm_caches_async(app) -> None:
    """Kick off a background warm pass, at most once per interval."""
    if app.config.get("TESTING"):
        return
    if os.getenv("CACHE_WARM_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}:
        return
    if _warm_flags.get("scheduled"):
        return
    _warm_flags.set("scheduled", True, _WARM_INTERVAL_SECONDS)
    threading.Thread(
        target=_warm, args=(app,), name="kubesight-cache-warmer", daemon=True
    ).start()


def _missing(cache: TTLCache, key: str, only_missing: bool) -> bool:
    return not only_missing or cache.age(key) is None


def _warm(app) -> None:
    if not _pass_lock.acquire(blocking=False):
        return  # the previous pass is still running
    try:
        only_missing = _state["booted"]
        _warm_pass(app, only_missing)
        _state["booted"] = True
    finally:
        _pass_lock.release()


def _warm_pass(app, only_missing: bool) -> None:
    with app.app_context():
        try:
            from .k8s_provider import (
                _K8S_READ_CACHE,
                list_clusters_from_k8s,
                list_namespaces_from_k8s,
                resolve_cluster_access,
                should_use_real_k8s,
            )
            from .services.inventory_service import (
                _INVENTORY_DISCOVERY_CACHE,
                _discover_cluster_inventory_real,
            )

            if not should_use_real_k8s():
                return

            cluster_items = list_clusters_from_k8s().get("items", [])
            warmed = 0
            for item in cluster_items:
                cluster_id = item.get("id")
                if not cluster_id or not should_use_real_k8s(cluster_id):
                    continue
                if _missing(_K8S_READ_CACHE, f"nslist:{cluster_id}", only_missing):
                    try:
                        access = resolve_cluster_access(cluster_id)
                        if access:
                            list_namespaces_from_k8s(access)
                            warmed += 1
                    except Exception:
                        logger.debug("warm namespaces failed for %s", cluster_id, exc_info=True)
                if _missing(_INVENTORY_DISCOVERY_CACHE, f"inv:{cluster_id}", only_missing):
                    try:
                        _discover_cluster_inventory_real(cluster_id)
                        warmed += 1
                    except Exception:
                        logger.debug("warm inventory failed for %s", cluster_id, exc_info=True)

            # Service health map — feeds the Clients and Application Services
            # tabs. user=None warms every linked (cluster, namespace) pair; RBAC
            # decisions still happen per request before the cache is consulted.
            try:
                from .services.application_service_service import (
                    list_services,
                    service_list_cached,
                )

                if not only_missing or not service_list_cached(None):
                    list_services(user=None)
                    warmed += 1
            except Exception:
                logger.debug("warm service health failed", exc_info=True)

            logger.info(
                "cache warm pass completed (%d clusters, %d reads warmed%s)",
                len(cluster_items),
                warmed,
                ", missing only" if only_missing else "",
            )
        except Exception:
            logger.debug("cache warm pass failed", exc_info=True)
