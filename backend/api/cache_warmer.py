"""Proactive cache warming.

First clicks on the Namespaces / Clients / Inventory tabs previously paid the
full kubectl cost (multiple seconds). Warming runs the same cached reads in a
background thread — at app startup and again whenever the cluster list is
requested (throttled) — so by the time a user opens a tab, the data is served
from cache and kept fresh by stale-while-revalidate.
"""

from __future__ import annotations

import logging
import os
import threading

from .ttl_cache import TTLCache

logger = logging.getLogger(__name__)

_warm_flags = TTLCache("cache-warmer")
_WARM_INTERVAL_SECONDS = int(os.getenv("CACHE_WARM_INTERVAL_SECONDS", "120"))


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


def _warm(app) -> None:
    with app.app_context():
        try:
            from .k8s_provider import (
                list_clusters_from_k8s,
                list_namespaces_from_k8s,
                resolve_cluster_access,
                should_use_real_k8s,
            )

            if not should_use_real_k8s():
                return

            cluster_items = list_clusters_from_k8s().get("items", [])
            for item in cluster_items:
                cluster_id = item.get("id")
                if not cluster_id or not should_use_real_k8s(cluster_id):
                    continue
                try:
                    access = resolve_cluster_access(cluster_id)
                    if access:
                        list_namespaces_from_k8s(access)
                except Exception:
                    logger.debug("warm namespaces failed for %s", cluster_id, exc_info=True)
                try:
                    from .services.inventory_service import _discover_cluster_inventory_real

                    _discover_cluster_inventory_real(cluster_id)
                except Exception:
                    logger.debug("warm inventory failed for %s", cluster_id, exc_info=True)

            # Service health map — feeds the Clients and Application Services
            # tabs. user=None warms every linked (cluster, namespace) pair; RBAC
            # decisions still happen per request before the cache is consulted.
            try:
                from .services.application_service_service import list_services

                list_services(user=None)
            except Exception:
                logger.debug("warm service health failed", exc_info=True)

            logger.info("cache warm pass completed (%d clusters)", len(cluster_items))
        except Exception:
            logger.debug("cache warm pass failed", exc_info=True)
