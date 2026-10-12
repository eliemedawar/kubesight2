"""Ticketing dropdown sync: batched snapshot ids, the cluster-wide read,
skipping unchanged publishes on scheduled syncs, and last_sync_at on failure."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from api.db import db
from api.models import ZohoDeploymentSnapshot, ZohoIntegration
from api.services import zoho_sync_service as svc
from tests.test_zoho_sync_parity import zoho  # noqa: F401 — fixture


@pytest.fixture(autouse=True)
def _fresh_publish_memory():
    svc.clear_published()
    yield
    svc.clear_published()


def _count_statements(table: str):
    seen = []

    def _listener(conn, cursor, statement, params, context, executemany):
        if table in statement:
            seen.append(statement.split()[0].upper())

    return seen, _listener


def test_source_entries_batch_snapshot_ids_and_only_sync_touches(zoho):
    seen, listener = _count_statements("zoho_deployment_snapshots")
    engine = db.engine
    event.listen(engine, "before_cursor_execute", listener)
    try:
        first = svc._source_entries(None)
        inserts = seen.count("INSERT")
        assert inserts >= 1  # four new deployments, inserted in one flush
        ids = {(e["namespace"], e["name"]): e["id"] for e in first}
        assert len(ids) == 4

        seen.clear()
        again = svc._source_entries(None)
        assert {(e["namespace"], e["name"]): e["id"] for e in again} == ids
        # A preview / catalog build: reads only, one SELECT for the cluster.
        assert seen == ["SELECT"]

        stamp = datetime.now(timezone.utc) - timedelta(days=3)
        ZohoDeploymentSnapshot.query.update({ZohoDeploymentSnapshot.last_seen_at: stamp})
        db.session.commit()
        seen.clear()
        svc._source_entries(None, touch=True)
        assert seen == ["SELECT", "UPDATE"]
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    db.session.expire_all()
    for snap in ZohoDeploymentSnapshot.query.all():
        last = snap.last_seen_at.replace(tzinfo=timezone.utc) if snap.last_seen_at.tzinfo is None else snap.last_seen_at
        assert last > stamp + timedelta(days=2)


def test_many_namespaces_use_one_cluster_wide_read(app, monkeypatch):
    from api import k8s_provider
    from api.k8s_provider import K8sCommandError

    namespaces = ["a", "b", "c", "d", "e"]
    monkeypatch.setattr(k8s_provider, "should_use_real_k8s", lambda *_a, **_k: True)
    monkeypatch.setattr(k8s_provider, "resolve_cluster_access", lambda cid: SimpleNamespace(cluster_id=cid))
    per_ns_calls = []
    monkeypatch.setattr(
        k8s_provider,
        "namespace_resource_list_from_k8s",
        lambda access, ns, kind, fresh=False: per_ns_calls.append(ns) or {"deployments": [{"name": f"{ns}-app"}]},
    )
    items = [
        {"metadata": {"namespace": "a", "name": "api"}},
        {"metadata": {"namespace": "c", "name": "web"}},
        {"metadata": {"namespace": "zzz", "name": "ignored"}},
    ]
    monkeypatch.setattr(k8s_provider, "cluster_list_items", lambda access, kind: items)
    out = svc._list_deployments_by_namespace("prod", namespaces)
    assert out == {"a": ["api"], "b": [], "c": ["web"], "d": [], "e": []}
    assert per_ns_calls == []

    # No cluster-wide rights: falls back to the per-namespace reads.
    def _refuse(access, kind):
        raise K8sCommandError("forbidden")

    monkeypatch.setattr(k8s_provider, "cluster_list_items", _refuse)
    out = svc._list_deployments_by_namespace("prod", namespaces)
    assert out["e"] == ["e-app"] and sorted(per_ns_calls) == namespaces

    # Three or fewer namespaces keep the (cached) per-namespace reads.
    per_ns_calls.clear()
    monkeypatch.setattr(k8s_provider, "cluster_list_items", lambda *a: pytest.fail("not used"))
    svc._list_deployments_by_namespace("prod", ["a", "b"])
    assert sorted(per_ns_calls) == ["a", "b"]


def test_scheduled_sync_skips_unchanged_fields_and_cascade(zoho):
    first = svc.sync_now(force=False)
    assert first["status"] == "ok"
    assert len(zoho["publishes"]) == 3
    creates = len(zoho["mapping_creates"])
    assert creates >= 1

    zoho["publishes"].clear()
    second = svc.sync_now(force=False)
    assert second["status"] == "ok"
    assert zoho["publishes"] == []  # nothing changed: no PATCH
    assert len(zoho["mapping_creates"]) == creates  # cascade not torn down
    assert second["cascade"].get("unchanged") is True
    assert "(unchanged)" in second["message"]

    # A manual Sync forces the full republish.
    third = svc.sync_now()
    assert len(zoho["publishes"]) == 3
    assert len(zoho["mapping_creates"]) == 2 * creates
    assert third["status"] == "ok"


def test_scheduled_sync_republishes_a_changed_field(zoho, monkeypatch):
    svc.sync_now(force=False)
    zoho["publishes"].clear()
    original = svc._list_deployments_by_namespace

    def _more(cluster_id, namespaces, fresh=False):
        out = original(cluster_id, namespaces, fresh=fresh)
        out["payments"] = out["payments"] + ["new-svc"]
        return out

    monkeypatch.setattr(svc, "_list_deployments_by_namespace", _more)
    svc.sync_now(force=False)
    # Only the Application list changed (the env/variable lists did not).
    assert [p["fieldId"] for p in zoho["publishes"]] == ["9001"]


def test_unexpected_sync_failure_still_stamps_last_sync_at(zoho, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("socket exploded")

    monkeypatch.setattr(svc, "_source_entries", _boom)
    row = ZohoIntegration.query.get(1)
    row.last_sync_at = None
    db.session.commit()
    with pytest.raises(RuntimeError):
        svc.run_due_sync()
    row = ZohoIntegration.query.get(1)
    assert row.last_sync_at is not None
    assert row.last_sync_status == "error"
    # The next tick waits for the interval instead of retrying immediately.
    assert svc.run_due_sync() is False


def test_ticket_details_are_cached_and_dropped_on_comment(app, monkeypatch):
    from api import ttl_cache
    from api.services import zoho_client

    monkeypatch.setattr(ttl_cache, "caching_disabled", lambda: False)  # on in tests
    row = svc.get_or_create_config()
    row.refresh_token_encrypted = "x"
    row.client_id = "c"
    row.org_id = "o"
    db.session.commit()
    calls = []
    monkeypatch.setattr(svc, "_to_client_config", lambda r: SimpleNamespace(org_id="o"))
    monkeypatch.setattr(
        zoho_client, "get_ticket", lambda cfg, tid: calls.append("t") or {"description": "d"}
    )
    monkeypatch.setattr(
        zoho_client,
        "list_ticket_comments",
        lambda cfg, tid: calls.append("c") or [{"content": "hello"}],
    )
    svc.invalidate_ticket_details()
    first = svc.fetch_ticket_details("42")
    assert first == {"description": "d", "comments": ["hello"]}
    assert svc.fetch_ticket_details("42") == first
    assert sorted(calls) == ["c", "t"]
    svc.invalidate_ticket_details("42")
    svc.fetch_ticket_details("42")
    assert len(calls) == 4
