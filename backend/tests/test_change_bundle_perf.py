"""Change bundles off the scheduler's critical path.

Locked here: the tick claims due bundles and hands them to a bounded worker
pool (never more than the free workers), a bundle orphaned in ``deploying`` by
a restart is closed rather than re-run, rollout watches read one namespace
with one kubectl call and skip a cluster that stopped answering, a bulk queue
asks the registry once per image, and the MCP request list filters in SQL.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from api.db import db
from api.models import (
    BundleRolloutWatch,
    ChangeBundle,
    ChangeBundleItem,
    DeploymentRequest,
    User,
)
from api.services import change_bundle_executor as executor

CLUSTER = "prod-us-east"
NAMESPACE = "payments"


def _approved_bundle(n_items=1, status="approved"):
    bundle = ChangeBundle(status=status, required_approvals=0, total_recipients=0,
                          requested_start_time=datetime.now(timezone.utc) - timedelta(minutes=1),
                          requested_end_time=datetime.now(timezone.utc) + timedelta(hours=1))
    db.session.add(bundle)
    db.session.flush()
    for i in range(n_items):
        db.session.add(ChangeBundleItem(
            bundle_id=bundle.id, position=i, action_type="scale_replicas", cluster_id=CLUSTER,
            namespace=NAMESPACE, resource_kind="Deployment", resource_name=f"app-{i}",
            new_payload_json={"execution": {"mode": "scale", "replicas": 2}}, yaml_preview="#",
        ))
    db.session.commit()
    return bundle


def test_the_tick_claims_only_free_workers_and_runs_them_off_thread(app, monkeypatch):
    ids = [_approved_bundle().id for _ in range(4)]
    submitted = []

    class Pool:
        def submit(self, fn, app_obj, bundle_id):
            submitted.append(bundle_id)

    monkeypatch.setattr(executor, "_testing", lambda: False)
    monkeypatch.setattr(executor, "_executor", lambda: Pool())
    monkeypatch.setattr(executor, "_workers", lambda: 2)
    executor._running.clear()
    try:
        result = executor.process_due_bundles()
        assert result["executed"] == 2 and submitted == ids[:2]
        statuses = [db.session.get(ChangeBundle, i).status for i in ids]
        assert statuses == ["deploying", "deploying", "approved", "approved"]
        # Both workers still busy: the next tick claims nothing more.
        assert executor.process_due_bundles()["executed"] == 0
        # A worker finishes: one more is claimed.
        executor._running.discard(ids[0])
        assert executor.process_due_bundles()["executed"] == 1 and submitted[-1] == ids[2]
    finally:
        executor._running.clear()

    # The worker body applies a claimed bundle in its own app context.
    executor._run_in_app(app, ids[0])
    db.session.expire_all()
    assert db.session.get(ChangeBundle, ids[0]).status == "completed"


def test_a_bundle_orphaned_by_a_restart_is_closed_not_rerun(app):
    bundle = _approved_bundle(n_items=3, status="deploying")
    items = sorted(bundle.items, key=lambda i: i.position)
    items[0].status, items[1].status = "succeeded", "applying"
    db.session.commit()
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    db.session.execute(db.text("UPDATE change_bundles SET updated_at = :t WHERE id = :i"),
                       {"t": old.replace(tzinfo=None), "i": bundle.id})
    db.session.commit()
    fresh = _approved_bundle(status="deploying")  # recently advanced: left alone

    executor.process_due_bundles()
    db.session.expire_all()
    row = db.session.get(ChangeBundle, bundle.id)
    assert row.status == "partially_failed"
    assert [i.status for i in sorted(row.items, key=lambda i: i.position)] == ["succeeded", "failed", "skipped"]
    assert "not re-run" in sorted(row.items, key=lambda i: i.position)[1].execution_result["error"]
    assert db.session.get(ChangeBundle, fresh.id).status == "deploying"


def test_due_and_expired_are_found_in_sql(app):
    past = _approved_bundle()
    past.requested_end_time = datetime.now(timezone.utc) - timedelta(minutes=1)
    later = _approved_bundle()
    later.requested_start_time = datetime.now(timezone.utc) + timedelta(hours=1)
    pending = _approved_bundle(status="pending_approval")
    pending.requested_end_time = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.session.commit()
    result = executor.process_due_bundles()
    assert result == {"executed": 0, "expired": 2}
    assert db.session.get(ChangeBundle, later.id).status == "approved"


def _watch(namespace, name):
    watch = BundleRolloutWatch(bundle_id=1, cluster_id=CLUSTER, namespace=namespace, deployment_name=name)
    db.session.add(watch)
    return watch


def _deployment(name, ready=True):
    return {
        "metadata": {"name": name, "generation": 2},
        "spec": {"replicas": 1},
        "status": {"observedGeneration": 2, "replicas": 1, "updatedReplicas": 1,
                   "availableReplicas": 1 if ready else 0, "readyReplicas": 1 if ready else 0},
    }


def test_watches_read_a_namespace_once_and_skip_a_dead_cluster(app, monkeypatch):
    _approved_bundle(status="completed")
    a, b, c = _watch("ns-a", "web"), _watch("ns-a", "api"), _watch("ns-a", "gone")
    d, e = _watch("ns-b", "web"), _watch("ns-b", "api")
    db.session.commit()
    calls = []

    def run(access, args, timeout=None):
        calls.append(args)
        if "ns-a" in args:
            return json.dumps({"items": [_deployment("web"), _deployment("api", ready=False)]})
        raise executor.K8sCommandError("connection refused")

    monkeypatch.setattr(executor, "should_use_real_k8s", lambda cid=None: True)
    monkeypatch.setattr(executor, "resolve_cluster_access", lambda cid: object())
    monkeypatch.setattr("api.k8s_provider._run_for_access", run)
    settings_reads = []
    real_settings = executor._watch_settings
    monkeypatch.setattr(executor, "_watch_settings", lambda: settings_reads.append(1) or real_settings())

    assert executor.watch_bundle_rollouts() == 5
    assert len(calls) == 2 and len(settings_reads) == 1
    assert calls[0] == ["get", "deployments", "-n", "ns-a", "-o", "json"]
    db.session.expire_all()
    assert db.session.get(BundleRolloutWatch, a.id).status == "healthy"
    assert db.session.get(BundleRolloutWatch, b.id).status == "watching"
    assert "could not read" in db.session.get(BundleRolloutWatch, c.id).detail
    assert "connection refused" in db.session.get(BundleRolloutWatch, d.id).detail

    # A third namespace on the same dead cluster is not even asked this pass.
    calls.clear()
    _watch("ns-c", "x"), _watch("ns-c", "y")
    db.session.commit()
    executor.watch_bundle_rollouts()
    assert [args[3] for args in calls] == ["ns-a", "ns-b"]


def test_a_bulk_queue_asks_the_registry_once_per_image(app, monkeypatch):
    from api.services.change_bundle_service import queue_many_for_approval

    asked = []
    monkeypatch.setattr(
        "api.services.registry_service.check_image",
        lambda image, cluster_id=None: asked.append(image) or {"image": image, "status": "found", "blocking": False},
    )
    manifest = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: app-{n}\nspec:\n  template:\n"
        "    spec:\n      containers:\n        - name: web\n          image: ghcr.io/x/app:1.0\n"
    )
    admin = User.query.filter_by(username="admin").first()
    payloads = [
        {"actionType": "apply_yaml", "clusterId": CLUSTER, "namespace": NAMESPACE, "yaml": manifest.format(n=n)}
        for n in range(5)
    ]
    result = queue_many_for_approval(admin, payloads, source="test")
    assert result["itemCount"] == 5
    assert asked == ["ghcr.io/x/app:1.0"]
    bundle = db.session.get(ChangeBundle, result["id"])
    assert [i.position for i in sorted(bundle.items, key=lambda i: i.position)] == [0, 1, 2, 3, 4]


def test_mcp_request_list_finds_older_matches(client, admin_token):
    from tests.test_mcp_server import call_tool

    admin = User.query.filter_by(username="admin").first()
    base = datetime.now(timezone.utc)
    db.session.add(DeploymentRequest(requester_id=admin.id, cluster_id=CLUSTER, cluster_name=CLUSTER,
                                     message="old one", status="approved",
                                     created_at=base - timedelta(days=30)))
    for i in range(205):
        db.session.add(DeploymentRequest(requester_id=admin.id, cluster_id=CLUSTER, cluster_name=CLUSTER,
                                         message=f"r{i}", status="declined",
                                         created_at=base - timedelta(minutes=i)))
    db.session.commit()
    result = call_tool(client, admin_token, "kubesight_deployment_requests_list", {"status": "approved"})
    data = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    assert data["totalMatching"] == 1 and data["requests"][0]["message"] == "old one"
