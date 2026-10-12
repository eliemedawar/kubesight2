"""The Promotions overview, cheap to poll.

Locked here: polling the overview writes nothing once what runs is recorded,
the payload is built once per TTL however many callers ask (and a ladder
change rebuilds it), a cluster bound by a few exact namespaces is scanned per
namespace in parallel with the others, and the ledger is read only for the
images that run now.
"""

from __future__ import annotations

import json

from sqlalchemy import event

from api.db import db
from api.models_promotion import PromotionRecord
from api.services import promotion_service as svc
from tests.test_promotion_rules import PROD, STAGING, _admin, _fresh_mock_ladder, _ladder  # noqa: F401


def _statements(fn):
    seen = []

    def hook(conn, cursor, statement, *args):
        seen.append(statement)

    event.listen(db.engine, "before_cursor_execute", hook)
    try:
        fn()
    finally:
        event.remove(db.engine, "before_cursor_execute", hook)
    return seen


def test_polling_the_overview_writes_nothing_once_recorded(app):
    _ladder()
    svc.overview()  # first sight: what runs healthy is recorded
    recorded = PromotionRecord.query.count()
    assert recorded > 0
    writes = [s for s in _statements(svc.overview) if s.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))]
    assert writes == []
    assert PromotionRecord.query.count() == recorded


def test_the_overview_is_built_once_per_ttl(app, monkeypatch):
    from api.services import promotion_overview

    _ladder()
    builds = []
    real = promotion_overview.build_overview
    monkeypatch.setattr(promotion_overview, "build_overview", lambda: builds.append(1) or real())
    app.config["TESTING"] = False
    try:
        svc.invalidate_overview()
        first = svc.overview()
        assert svc.overview() is first and len(builds) == 1
        # A ladder change rebuilds it.
        env = svc.ladder()[1]
        svc.update_environment(_admin(), env.id, {"mode": "warn"})
        svc.overview()
        assert len(builds) == 2
    finally:
        app.config["TESTING"] = True
        svc.invalidate_scan()


def test_a_few_exact_namespaces_are_scanned_one_by_one(app, monkeypatch):
    _ladder()
    resolver = svc.Resolver()
    assert resolver.scan_namespaces(STAGING) == ["payments-dev", "payments-sit", "payments-uat"]
    assert resolver.scan_namespaces(PROD) == ["payments-preprod"]

    calls = []

    def run(access, args, timeout=None):
        calls.append(args)
        return json.dumps({"items": []})

    monkeypatch.setattr("api.k8s_provider.should_use_real_k8s", lambda cid=None: True)
    monkeypatch.setattr("api.k8s_provider.resolve_cluster_access", lambda cid: object())
    monkeypatch.setattr("api.k8s_provider._run_for_access", run)
    app.config["TESTING"] = False  # the parallel path
    try:
        svc.invalidate_scan()
        by_env, errors = svc.scan_ladder()
    finally:
        app.config["TESTING"] = True
        svc.invalidate_scan()
    assert errors == []
    scopes = sorted(tuple(args[2:4]) for args in calls)
    assert scopes == [("-n", "payments-dev"), ("-n", "payments-preprod"), ("-n", "payments-sit"), ("-n", "payments-uat")]


def test_the_ledger_reads_only_the_images_asked_for(app):
    envs = _ladder()
    svc.overview()
    dev = envs["dev"].id
    everything = svc.ledger_by_env([dev])[dev]
    assert len(everything) > 1
    one = sorted(everything)[0]
    assert list(svc.ledger_by_env([dev], [one])[dev]) == [one]
    assert svc.ledger_by_env([dev], [])[dev] == {}
