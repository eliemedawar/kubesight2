"""Change bundle lifecycle outcomes: requester notification emails and the
post-execution pod-health watch (rollback + notify) for manually-authored
bundles.

Bundles run against the mock cluster ("prod-us-east"/"payments"), so
execute_bundle applies items without kubectl. Module-level email bindings in
change_bundle_service are patched directly; the executor's failure mail imports
from api.email_delivery at call time, so that module is patched there.
"""

from datetime import datetime, timedelta, timezone

from api.db import db
from api.models import (
    BundleRolloutWatch,
    ChangeBundle,
    ChangeBundleItem,
    DeployAutomationRun,
    User,
)

CLUSTER = "prod-us-east"
NAMESPACE = "payments"
DEPLOYMENT = "payments-api"

DEPLOYMENT_YAML = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: payments-api
  namespace: payments
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: payments-api
          image: ghcr.io/mock/payments:v3.0.0
"""


def _admin_with_email(email="requester@areeba.com"):
    admin = User.query.filter_by(username="admin").first()
    admin.email = email
    db.session.add(admin)
    db.session.commit()
    return admin


def _make_bundle(requester, status="pending_approval", with_item=True):
    bundle = ChangeBundle(
        requester_user_id=requester.id if requester else None,
        status=status,
        required_approvals=1,
        total_recipients=1,
        note="test bundle",
    )
    db.session.add(bundle)
    db.session.flush()
    if with_item:
        db.session.add(
            ChangeBundleItem(
                bundle_id=bundle.id,
                position=0,
                action_type="edit_deployment",
                cluster_id=CLUSTER,
                namespace=NAMESPACE,
                resource_kind="Deployment",
                resource_name=DEPLOYMENT,
                yaml_preview=DEPLOYMENT_YAML,
                new_payload_json={"execution": {"mode": "apply"}},
            )
        )
    db.session.commit()
    return bundle


def _capture_service_email(monkeypatch):
    """Patch the module-level email bindings inside change_bundle_service."""
    sent = []
    monkeypatch.setattr("api.services.change_bundle_service.smtp_is_configured", lambda: True)
    monkeypatch.setattr(
        "api.services.change_bundle_service.send_email",
        lambda to, subject, body, **kw: sent.append((to, subject, body)),
    )
    return sent


def test_requester_emailed_on_approve_and_reject(app, monkeypatch):
    from api.services.change_bundle_service import decide_bundle

    admin = _admin_with_email()
    sent = _capture_service_email(monkeypatch)

    approved = _make_bundle(admin)
    decide_bundle(approved.id, "approve", actor=admin)
    assert approved.status == "approved"

    rejected = _make_bundle(admin)
    decide_bundle(rejected.id, "decline", actor=admin, reason="wrong window")
    assert rejected.status == "rejected"

    # One outcome email per finalization, addressed to the requester.
    requester_mail = [m for m in sent if m[0] == admin.email]
    assert len(requester_mail) == 2
    assert "approved" in requester_mail[0][1]
    assert "rejected" in requester_mail[1][1]
    assert "wrong window" in requester_mail[1][2]


def test_automation_bundles_send_no_requester_email(app, monkeypatch):
    """requester_user_id=None (deploy automation) → no outcome email."""
    from api.services.change_bundle_service import decide_bundle

    _admin_with_email()
    sent = _capture_service_email(monkeypatch)
    bundle = _make_bundle(None)
    decide_bundle(bundle.id, "decline", actor=None, reason="nope")
    assert bundle.status == "rejected"
    assert sent == []


def test_execution_emails_requester_and_starts_watch(app, monkeypatch):
    """Window execution → 'deployed' email + a pod-health watch per applied
    Deployment; the watch resolves healthy on the (mock) next tick."""
    from api.services.change_bundle_executor import process_due_bundles, watch_bundle_rollouts

    admin = _admin_with_email()
    sent = _capture_service_email(monkeypatch)
    bundle = _make_bundle(admin, status="approved")

    process_due_bundles()

    assert bundle.status == "completed"
    assert any(m[0] == admin.email and "deployed" in m[1] for m in sent)

    watch = BundleRolloutWatch.query.filter_by(bundle_id=bundle.id).first()
    assert watch is not None and watch.status == "watching"
    assert watch.deployment_name == DEPLOYMENT

    watch_bundle_rollouts()
    assert watch.status == "healthy"


def test_automation_bundles_skip_watch_creation(app):
    """A bundle owned by a deploy-automation run must not get a second watcher
    — the run already does pod-health + rollback itself."""
    from api.services.change_bundle_executor import _start_rollout_watches

    bundle = _make_bundle(None, status="completed")
    for item in bundle.items:
        item.status = "succeeded"
    db.session.add(
        DeployAutomationRun(
            cluster_id=CLUSTER,
            namespace=NAMESPACE,
            deployment_name=DEPLOYMENT,
            image_tag="v1.0.0",
            status="verifying_rollout",
            bundle_id=bundle.id,
        )
    )
    db.session.commit()

    _start_rollout_watches(bundle)
    assert BundleRolloutWatch.query.filter_by(bundle_id=bundle.id).count() == 0


def test_watch_failure_rolls_back_and_notifies(app, monkeypatch):
    """Pods never come up ⇒ rollout undo + failed watch + email to the
    requester AND every admin."""
    admin = _admin_with_email("requester@areeba.com")
    bundle = _make_bundle(admin, status="completed")
    watch = BundleRolloutWatch(
        bundle_id=bundle.id,
        cluster_id=CLUSTER,
        namespace=NAMESPACE,
        deployment_name=DEPLOYMENT,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=60),
    )
    db.session.add(watch)
    db.session.commit()

    monkeypatch.setattr(
        "api.services.change_bundle_executor.should_use_real_k8s", lambda cid=None: True
    )
    monkeypatch.setattr(
        "api.services.deployment_service.rollout_health",
        lambda cluster_id, namespace, name: {
            "observedCurrent": True, "desired": 2, "ready": 0, "updated": 2,
        },
    )
    undo_calls = []
    monkeypatch.setattr(
        "api.services.change_bundle_executor._run_kubectl_for_cluster",
        lambda cid, args: undo_calls.append((cid, args)) or "deployment.apps rolled back",
    )
    sent = []
    monkeypatch.setattr("api.email_delivery.smtp_is_configured", lambda: True)
    monkeypatch.setattr(
        "api.email_delivery.send_email",
        lambda to, subject, body, **kw: sent.append((to, subject, body)),
    )

    from api.services.change_bundle_executor import watch_bundle_rollouts

    watch_bundle_rollouts()

    row = db.session.get(BundleRolloutWatch, watch.id)
    assert row.status == "failed"
    assert row.rolled_back is True
    assert "Rolled back" in row.detail
    assert undo_calls and undo_calls[0][1][:2] == ["rollout", "undo"]
    assert undo_calls[0][1][2] == f"deployment/{DEPLOYMENT}"
    recipients = {m[0] for m in sent}
    assert "requester@areeba.com" in recipients
    assert all("rollout failed" in m[1] for m in sent)


def test_watch_waits_on_stale_generation(app, monkeypatch):
    """Fully-ready counters with a stale observedGeneration must not mark the
    watch healthy — same guard as the automation rollout watch."""
    bundle = _make_bundle(None, status="completed", with_item=False)
    watch = BundleRolloutWatch(
        bundle_id=bundle.id,
        cluster_id=CLUSTER,
        namespace=NAMESPACE,
        deployment_name=DEPLOYMENT,
    )
    db.session.add(watch)
    db.session.commit()

    monkeypatch.setattr(
        "api.services.change_bundle_executor.should_use_real_k8s", lambda cid=None: True
    )
    monkeypatch.setattr(
        "api.services.deployment_service.rollout_health",
        lambda cluster_id, namespace, name: {
            "observedCurrent": False, "desired": 2, "ready": 2, "updated": 2,
        },
    )

    from api.services.change_bundle_executor import watch_bundle_rollouts

    watch_bundle_rollouts()
    row = db.session.get(BundleRolloutWatch, watch.id)
    assert row.status == "watching"
    assert "observe" in (row.detail or "")
