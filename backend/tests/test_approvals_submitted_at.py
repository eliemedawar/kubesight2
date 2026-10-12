"""An approval's wait counts from when it was put in front of approvers.

A change bundle's draft (the cart) is created when it first opens, often long
before it is submitted, so the inbox read "waiting 108 days" for a bundle sent
today. Bundles now carry ``submitted_at`` (set on submit, backfilled from the
BUNDLE_SUBMITTED audit entry, else created_at), and Hermes tasks carry
``approval_requested_at`` (the run can start long before it asks).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.db import db
from api.migrate_rbac import _backfill_bundle_submitted_at
from api.models import AuditLog, ChangeBundle, ChangeBundleItem, DeploymentRequestSetting, User
from api.services import change_bundle_service
from tests.conftest import auth_headers
from tests.test_approvals import CLUSTER, _get, _inbox, _user, admin2_token  # noqa: F401
from tests.test_ticket_agent import agent, writes  # noqa: F401  (fixtures are used by name)


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _ms_ago(iso):
    when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - _aware(when)).total_seconds()


def _old_draft(owner: User, days: int = 108) -> ChangeBundle:
    bundle = ChangeBundle(
        requester_user_id=owner.id, status="draft",
        created_at=datetime.now(timezone.utc) - timedelta(days=days),
    )
    db.session.add(bundle)
    db.session.flush()
    db.session.add(ChangeBundleItem(
        bundle_id=bundle.id, position=0, action_type="scale", cluster_id=CLUSTER, cluster_name=CLUSTER,
        namespace="payments", resource_name="payments-api", validation_status="valid",
        new_payload_json={"replicas": 3},
    ))
    row = DeploymentRequestSetting.query.first() or DeploymentRequestSetting(group_ids=[])
    row.recipients = ["admin@example.com", "admin2@example.com"]
    row.required_approvals = 1
    row.cluster_required_approvals = {}
    db.session.add(row)
    db.session.commit()
    return bundle


def _submit(owner: User, bundle: ChangeBundle):
    now = datetime.now(timezone.utc)
    return change_bundle_service.submit_bundle(
        owner, bundle.id, note="Scale for the sale",
        window_start=(now + timedelta(hours=1)).isoformat(),
        window_end=(now + timedelta(hours=3)).isoformat(),
    )


def test_a_bundle_waits_from_submission_not_from_its_draft(client, admin_token, admin2_token):
    bundle = _old_draft(_user("admin2"))
    assert bundle.submitted_at is None  # a draft has not been submitted

    payload = _submit(_user("admin2"), bundle)
    assert payload["status"] == "pending_approval"
    assert payload["submittedAt"] and _ms_ago(payload["submittedAt"]) < 60
    assert _ms_ago(payload["createdAt"]) > 100 * 86400

    [item] = _inbox(client, admin_token)["items"]
    assert item["kind"] == "change_bundle"
    assert _ms_ago(item["requestedAt"]) < 60  # not 108 days

    response = client.post(item["actions"]["reject"], json={"reason": "not now"},
                           headers=auth_headers(admin_token))
    assert response.status_code == 200, response.get_json()
    [done] = _get(client, admin_token, "/api/approvals/history?kind=change_bundle")["items"]
    assert _ms_ago(done["requestedAt"]) < 60


def test_an_auto_approved_bundle_is_stamped_too(app):
    bundle = _old_draft(_user("admin"))
    setting = DeploymentRequestSetting.query.first()
    setting.cluster_required_approvals = {CLUSTER: 0}
    db.session.commit()
    payload = _submit(_user("admin"), bundle)
    assert payload["status"] == "approved"
    assert payload["submittedAt"] and _ms_ago(payload["submittedAt"]) < 60


def test_backfill_uses_the_submission_audit_entry_else_created_at(app):
    created = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    submitted = datetime(2026, 4, 20, 15, 30, tzinfo=timezone.utc)
    pending = ChangeBundle(status="pending_approval", created_at=created)
    approved = ChangeBundle(status="approved", created_at=created)
    draft = ChangeBundle(status="draft", created_at=created)
    db.session.add_all([pending, approved, draft])
    db.session.flush()
    db.session.add_all([
        # An older submit (sent back and sent again): the latest one counts.
        AuditLog(action="BUNDLE_SUBMITTED", target_type="change_bundle", target_id=str(pending.id),
                 created_at=submitted - timedelta(days=3)),
        AuditLog(action="BUNDLE_SUBMITTED", target_type="change_bundle", target_id=str(pending.id),
                 created_at=submitted),
        # Another kind's audit entry with the same id must not be picked up.
        AuditLog(action="BUNDLE_SUBMITTED", target_type="deployment_request", target_id=str(approved.id),
                 created_at=submitted),
    ])
    db.session.commit()

    _backfill_bundle_submitted_at(True)
    db.session.expire_all()

    assert _aware(db.session.get(ChangeBundle, pending.id).submitted_at) == submitted
    assert _aware(db.session.get(ChangeBundle, approved.id).submitted_at) == created
    assert db.session.get(ChangeBundle, draft.id).submitted_at is None

    # Running again leaves what is set alone.
    _backfill_bundle_submitted_at(False)
    db.session.expire_all()
    assert _aware(db.session.get(ChangeBundle, pending.id).submitted_at) == submitted


def test_a_hermes_approval_waits_from_when_it_was_asked(client, admin_token, agent, writes):
    from api.models_ticket_agent import TicketInterpretation
    from tests.test_ticket_agent import _request, _ticket

    ticket = _ticket()
    _request(ticket.id)
    task = TicketInterpretation.query.filter_by(status="awaiting_approval").one()
    assert task.approval_requested_at is not None
    # The run started hours before it asked for approval.
    task.created_at = datetime.now(timezone.utc) - timedelta(hours=6)
    db.session.commit()

    [item] = _inbox(client, admin_token)["items"]
    assert item["kind"] == "hermes" and _ms_ago(item["requestedAt"]) < 60

    # An older row without the stamp falls back to created_at.
    task.approval_requested_at = None
    db.session.commit()
    [item] = _inbox(client, admin_token)["items"]
    assert 6 * 3600 - 60 < _ms_ago(item["requestedAt"]) < 6 * 3600 + 60


def test_the_inbox_sorts_by_moment_across_kinds(client, admin_token, admin2_token):
    newer = _old_draft(_user("admin2"), days=1)
    _submit(_user("admin2"), newer)
    older = ChangeBundle(
        requester_user_id=_user("admin2").id, status="pending_approval", required_approvals=1,
        total_recipients=2, created_at=datetime.now(timezone.utc) - timedelta(days=200),
        submitted_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db.session.add(older)
    db.session.commit()
    items = _inbox(client, admin_token)["items"]
    assert [i["id"] for i in items if i["kind"] == "change_bundle"] == [newer.id, older.id]
