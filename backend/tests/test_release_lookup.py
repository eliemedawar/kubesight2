"""Kubernetes release lookups (dl.k8s.io) never hold a page load hostage."""

import threading
import time
from unittest.mock import patch

import api.upgrade_provider as up


def _reset():
    with up._version_cache_lock:
        up._version_cache.clear()
        up._version_lookups.clear()


def test_unreachable_release_site_costs_at_most_the_wait_budget():
    _reset()
    release = threading.Event()

    def hanging_fetch(url):
        release.wait(10)
        return None

    with patch.object(up, "_VERSION_LOOKUP_WAIT_SECONDS", 0.3), patch.object(up, "_fetch_release_text", side_effect=hanging_fetch):
        started = time.perf_counter()
        assert up._fetch_latest_k8s_version() == "unknown"
        assert time.perf_counter() - started < 1.5
        # A second caller does not start a second lookup or wait again forever.
        started = time.perf_counter()
        up._fetch_latest_k8s_version()
        assert time.perf_counter() - started < 1.5
        assert len(up._version_lookups) == 1
    release.set()
    _reset()


def test_answer_arrives_for_later_callers_and_stale_answer_is_kept():
    _reset()
    with patch.object(up, "_fetch_release_text", return_value="v1.34.1"):
        assert up._fetch_latest_k8s_version() == "v1.34.1"
    # Expired, and the site is now down: the last good answer is still served.
    with up._version_cache_lock:
        up._version_cache["latest_k8s_stable"]["ts"] -= up._VERSION_CACHE_TTL + 1
    with patch.object(up, "_fetch_release_text", return_value=None):
        assert up._fetch_latest_k8s_version() == "v1.34.1"
        deadline = time.time() + 3
        while up._version_lookups and time.time() < deadline:
            time.sleep(0.02)
        assert up._fetch_latest_k8s_version() == "v1.34.1"
    _reset()
