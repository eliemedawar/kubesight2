"""Promotion rules: images climb Dev → SIT → UAT → Pre-prod in order.

The ladder runs on the mock clusters (``promotion_mock``): staging-eu-west holds
payments-dev / -sit / -uat, prod-us-east holds payments-preprod. payments-api
runs v2.9.0 in dev, v2.8.4 in sit, v2.8.1 in uat and preprod.
"""

from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models import ChangeBundle, DeploymentRequestSetting, User
from api.models_promotion import PromotionEnvironment, PromotionEvent, PromotionRecord
from api.services import promotion_mock
from api.services import promotion_service as svc
from tests.conftest import auth_headers

STAGING = "staging-eu-west"
PROD = "prod-us-east"
DEV_IMAGE = "ghcr.io/mock/payments:v2.9.0"
SIT_IMAGE = "ghcr.io/mock/payments:v2.8.4"


@pytest.fixture(autouse=True)
def _fresh_mock_ladder():
    promotion_mock.reset()
    svc.invalidate_scan()
    yield
    promotion_mock.reset()
    svc.invalidate_scan()


def _admin() -> User:
    return User.query.filter_by(username="admin").first()


def _approvals(count: int) -> None:
    row = DeploymentRequestSetting.query.first() or DeploymentRequestSetting(recipients=[], group_ids=[])
    row.required_approvals = count
    row.cluster_required_approvals = {}
    row.recipients = ["approver@example.com"]
    db.session.add(row)
    db.session.commit()


def _ladder(mode: str = "enforce") -> dict:
    """Dev/SIT/UAT/Pre-prod bound to the mock namespaces."""
    svc.create_default_ladder(_admin())
    envs = {env.key: env for env in svc.ladder()}
    for key, cluster, ns in (
        ("dev", STAGING, "payments-dev"),
        ("sit", STAGING, "payments-sit"),
        ("uat", STAGING, "payments-uat"),
        ("preprod", PROD, "payments-preprod"),
    ):
        svc.add_binding(_admin(), envs[key].id, {"clusterId": cluster, "namespace": ns})
        svc.update_environment(_admin(), envs[key].id, {"mode": mode})
    return envs


def _manifest(name: str, image: str) -> str:
    return (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n"
        f"  name: {name}\nspec:\n  replicas: 1\n  selector:\n    matchLabels:\n      app: {name}\n"
        f"  template:\n    metadata:\n      labels:\n        app: {name}\n    spec:\n"
        f"      containers:\n      - name: {name}\n        image: {image}\n"
    )


# ---------------------------------------------------------------------------
# Images and bindings
# ---------------------------------------------------------------------------

def test_image_references_are_compared_by_repository_and_tag():
    assert svc.parse_image("docker.io/library/redis:7.2")["image"] == "redis:7.2"
    assert svc.parse_image("redis:7.2")["image"] == "redis:7.2"
    parsed = svc.parse_image("nexus.local:8443/team/api:1.4.2@sha256:abc")
    assert parsed["repository"] == "nexus.local:8443/team/api"
    assert parsed["tag"] == "1.4.2" and parsed["digest"] == "sha256:abc"
    assert parsed["mutable"] is False
    assert svc.parse_image("nexus.local:8443/team/api")["mutable"] is True
    assert svc.parse_image("team/api:latest")["mutable"] is True


def test_a_namespace_binding_beats_the_whole_cluster_binding(app):
    envs = _ladder()
    svc.add_binding(_admin(), envs["dev"].id, {"clusterId": PROD, "wholeCluster": True})
    assert svc.environment_for(PROD, "payments-preprod").key == "preprod"
    assert svc.environment_for(PROD, "anything-else").key == "dev"
    # The platform's own namespaces are never part of an environment.
    assert svc.environment_for(PROD, "kube-system") is None
    assert svc.environment_for("unbound-cluster", "payments") is None


def test_a_namespace_belongs_to_one_environment_only(app):
    envs = _ladder()
    with pytest.raises(svc.PromotionError) as exc:
        svc.add_binding(_admin(), envs["uat"].id, {"clusterId": STAGING, "namespace": "payments-sit"})
    assert "already belongs to SIT" in str(exc.value)
    with pytest.raises(svc.PromotionError):
        svc.add_binding(_admin(), envs["uat"].id, {"clusterId": STAGING, "namespace": "kube-system"})


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

def test_the_entry_environment_takes_any_image(app):
    _ladder()
    verdict = svc.evaluate(STAGING, "payments-dev", ["ghcr.io/mock/payments:v9.9.9"])
    assert verdict["applies"] and verdict["allowed"]
    assert verdict["images"][0]["status"] == "entry"


def test_an_image_that_ran_in_the_previous_environment_may_enter(app):
    _ladder()
    verdict = svc.evaluate(STAGING, "payments-sit", [DEV_IMAGE])
    assert verdict["allowed"] is True
    assert verdict["images"][0]["status"] == "passed"
    # The live probe recorded it in the ledger.
    assert PromotionRecord.query.filter_by(image=DEV_IMAGE).count() == 1


def test_skipping_an_environment_is_refused_in_enforce(app):
    _ladder()
    verdict = svc.evaluate(STAGING, "payments-uat", [DEV_IMAGE])
    assert verdict["allowed"] is False
    assert verdict["images"][0]["status"] == "missing"
    assert "UAT only takes images that passed SIT" in verdict["message"]


def test_warn_mode_lets_it_through_but_says_so(app):
    _ladder(mode="warn")
    verdict = svc.evaluate(STAGING, "payments-uat", [DEV_IMAGE])
    assert verdict["allowed"] is True and verdict["warning"] is True


def test_off_mode_does_not_apply(app):
    envs = _ladder()
    svc.update_environment(_admin(), envs["uat"].id, {"mode": "off"})
    verdict = svc.evaluate(STAGING, "payments-uat", [DEV_IMAGE])
    assert verdict["applies"] is False and verdict["allowed"] is True


def test_a_rollback_to_an_image_that_ran_here_is_never_a_promotion(app):
    _ladder()
    # v2.8.1 runs in UAT now; it never ran in SIT (which runs v2.8.4).
    verdict = svc.evaluate(STAGING, "payments-uat", ["ghcr.io/mock/payments:v2.8.1"])
    assert verdict["allowed"] is True
    assert verdict["images"][0]["status"] == "here"


def test_mutable_tags_cannot_be_promoted(app):
    _ladder()
    verdict = svc.evaluate(STAGING, "payments-sit", ["ghcr.io/mock/payments:latest"])
    assert verdict["allowed"] is False
    assert verdict["images"][0]["status"] == "mutable"


def test_exempt_images_skip_the_ladder(app):
    _ladder()
    svc.update_policy(_admin(), {"exemptImages": ["redis", "docker.io/*"]})
    verdict = svc.evaluate(STAGING, "payments-uat", ["redis:7.4"])
    assert verdict["allowed"] is True
    assert verdict["images"][0]["status"] == "exempt"


def test_soak_time_holds_a_fresh_image_back(app):
    envs = _ladder()
    svc.update_environment(_admin(), envs["dev"].id, {"minSoakMinutes": 120})
    verdict = svc.evaluate(STAGING, "payments-sit", [DEV_IMAGE])
    assert verdict["allowed"] is False
    assert verdict["images"][0]["status"] == "soaking"
    # Two hours later it may go.
    record = PromotionRecord.query.filter_by(image=DEV_IMAGE).first()
    record.first_healthy_at = datetime.now(timezone.utc) - timedelta(hours=3)
    db.session.commit()
    assert svc.evaluate(STAGING, "payments-sit", [DEV_IMAGE])["allowed"] is True


def test_the_ledger_remembers_what_passed_after_the_environment_moved_on(app):
    """SIT ran v2.9.0 yesterday and v3.0.0 today: UAT may still take v2.9.0."""
    _ladder()
    svc.observe()
    promotion_mock.set_image(STAGING, "payments-sit", "payments-api", "ghcr.io/mock/payments:v3.0.0")
    svc.invalidate_scan()
    verdict = svc.evaluate(STAGING, "payments-uat", [SIT_IMAGE])
    assert verdict["allowed"] is True
    assert verdict["images"][0]["status"] == "passed"


# ---------------------------------------------------------------------------
# The deploy paths
# ---------------------------------------------------------------------------

def test_a_refused_yaml_apply_carries_the_verdict(client, admin_token, no_cluster_approvals):
    _ladder()
    response = client.post(
        "/api/inventory/deploy/yaml/apply",
        headers=auth_headers(admin_token),
        json={
            "clusterId": STAGING,
            "namespace": "payments-uat",
            "yaml": _manifest("payments-api", DEV_IMAGE),
            "confirmation": "APPLY payments-uat",
        },
    )
    assert response.status_code == 409
    body = response.get_json()
    assert "passed SIT" in body["error"]
    assert body["data"]["promotion"]["environment"]["name"] == "UAT"
    assert PromotionEvent.query.filter_by(kind="blocked").count() == 1


def test_staging_a_skip_in_a_change_bundle_is_invalid(app):
    from api.services.change_bundle_service import ChangeBundleError, queue_for_approval

    _ladder()
    _approvals(1)
    with pytest.raises(ChangeBundleError) as exc:
        queue_for_approval(
            _admin(),
            {"clusterId": STAGING, "namespace": "payments-uat", "actionType": "apply_yaml",
             "yaml": _manifest("payments-api", DEV_IMAGE)},
        )
    assert "passed SIT" in str(exc.value)


def test_promote_moves_the_image_and_the_board_follows(client, admin_token, no_cluster_approvals):
    envs = _ladder()
    board = client.get("/api/promotions/board", headers=auth_headers(admin_token)).get_json()["data"]
    payments = next(r for r in board["apps"] if r["repository"] == "ghcr.io/mock/payments")
    dev_to_sit = payments["steps"][0]
    assert dev_to_sit["state"] == "ready" and dev_to_sit["image"] == DEV_IMAGE
    # SIT → UAT offers what SIT runs, not what Dev runs.
    assert payments["steps"][1]["image"] == SIT_IMAGE

    response = client.post(
        "/api/promotions/promote",
        headers=auth_headers(admin_token),
        json={"image": DEV_IMAGE, "environmentId": envs["sit"].id, "targets": dev_to_sit["targets"]},
    )
    assert response.status_code == 200, response.get_json()
    results = response.get_json()["data"]["results"]
    assert [r["status"] for r in results] == ["applied"]

    board = client.get("/api/promotions/board?refresh=1", headers=auth_headers(admin_token)).get_json()["data"]
    payments = next(r for r in board["apps"] if r["repository"] == "ghcr.io/mock/payments")
    assert payments["steps"][0]["state"] == "in_sync"
    assert PromotionEvent.query.filter_by(kind="promoted").count() == 1


def test_promote_refuses_a_skip(client, admin_token, no_cluster_approvals):
    envs = _ladder()
    response = client.post(
        "/api/promotions/promote",
        headers=auth_headers(admin_token),
        json={
            "image": DEV_IMAGE,
            "environmentId": envs["uat"].id,
            "targets": [{"clusterId": STAGING, "namespace": "payments-uat", "kind": "Deployment", "name": "payments-api"}],
        },
    )
    result = response.get_json()["data"]["results"][0]
    assert result["status"] == "refused" and result["blocked"] is True


def test_an_exception_needs_an_approval_even_where_the_cluster_needs_none(app):
    from api.services.change_bundle_executor import process_due_bundles
    from api.services.change_bundle_service import ChangeBundleError, decide_bundle

    _ladder()
    _approvals(0)
    data = svc.request_exception(
        _admin(),
        changes=[{"clusterId": STAGING, "namespace": "payments-uat", "kind": "Deployment",
                  "name": "payments-api", "image": DEV_IMAGE}],
        reason="Hotfix for the card outage; SIT is down for maintenance today.",
    )
    bundle = db.session.get(ChangeBundle, data["bundleId"])
    assert bundle.status == "pending_approval"
    assert bundle.required_approvals == 1
    assert bundle.promotion_exception["previousEnvironment"] == "SIT"
    assert bundle.items[0].validation_status == "valid"

    # The requester cannot approve their own exception.
    with pytest.raises(ChangeBundleError):
        decide_bundle(bundle.id, "approve", actor=_admin())

    approver = User.query.filter_by(username="operator").first()
    approver.email = "approver@example.com"
    db.session.commit()
    decide_bundle(bundle.id, "approve", actor=approver)
    process_due_bundles()
    db.session.refresh(bundle)
    assert bundle.status == "completed", bundle.items[0].execution_result
    assert promotion_mock.get(STAGING, "payments-uat", "payments-api")["spec"]["template"]["spec"]["containers"][0]["image"] == DEV_IMAGE
    assert PromotionEvent.query.filter_by(kind="exception_applied").count() == 1


def test_an_exception_needs_a_reason(app):
    _ladder()
    with pytest.raises(svc.PromotionError):
        svc.request_exception(
            _admin(),
            changes=[{"clusterId": STAGING, "namespace": "payments-uat", "kind": "Deployment",
                      "name": "payments-api", "image": DEV_IMAGE}],
            reason="pls",
        )


def test_the_ci_deploy_stage_gate_refuses_a_skip(app):
    """deploy_stage calls promotion_service.gate before preparing a manifest."""
    _ladder()
    refusal, verdict = svc.gate(STAGING, "payments-uat", [DEV_IMAGE], path="ci", workload="payments-api")
    assert refusal is not None and refusal[1] == 409
    assert PromotionEvent.query.filter_by(kind="blocked", path="ci").count() == 1


# ---------------------------------------------------------------------------
# Setup and permissions
# ---------------------------------------------------------------------------

def test_viewer_sees_the_board_but_cannot_change_the_ladder(client, viewer_token):
    headers = auth_headers(viewer_token)
    assert client.get("/api/promotions/board", headers=headers).status_code == 200
    assert client.post("/api/promotions/setup/defaults", headers=headers).status_code == 403


def test_setup_creates_reorders_and_deletes(client, admin_token):
    headers = auth_headers(admin_token)
    data = client.post("/api/promotions/setup/defaults", headers=headers).get_json()["data"]
    assert [e["name"] for e in data["environments"]] == ["Dev", "SIT", "UAT", "Pre-prod"]
    # Starts by watching, not blocking.
    assert {e["mode"] for e in data["environments"]} == {"warn"}
    ids = [e["id"] for e in data["environments"]]
    reordered = client.put(
        "/api/promotions/environments/order", headers=headers, json={"ids": [ids[1], ids[0], ids[2], ids[3]]}
    ).get_json()["data"]
    assert [e["name"] for e in reordered["environments"]][:2] == ["SIT", "Dev"]
    assert client.delete(f"/api/promotions/environments/{ids[3]}", headers=headers).status_code == 200
    assert PromotionEnvironment.query.count() == 3


def test_drift_flags_a_skip_but_not_what_ran_before_the_ladder(client, admin_token):
    _ladder()
    headers = auth_headers(admin_token)
    board = client.get("/api/promotions/board", headers=headers).get_json()["data"]
    payments = next(r for r in board["apps"] if r["repository"] == "ghcr.io/mock/payments")
    # UAT ran v2.8.1 before the ladder existed: the starting point, not a skip.
    assert all(not cell["drift"] for cell in payments["cells"])

    # Someone deploys Dev's build straight into UAT outside KubeSight.
    promotion_mock.set_image(STAGING, "payments-uat", "payments-api", DEV_IMAGE)
    svc.invalidate_scan()
    board = client.get("/api/promotions/board", headers=headers).get_json()["data"]
    payments = next(r for r in board["apps"] if r["repository"] == "ghcr.io/mock/payments")
    uat = payments["cells"][2]
    assert [d["tag"] for d in uat["drift"]] == ["v2.9.0"]
    assert uat["drift"][0]["skipped"] == "SIT"


def test_a_promotion_waiting_for_approval_is_not_offered_twice(client, admin_token):
    envs = _ladder()
    _approvals(1)
    headers = auth_headers(admin_token)
    board = client.get("/api/promotions/board", headers=headers).get_json()["data"]
    payments = next(r for r in board["apps"] if r["repository"] == "ghcr.io/mock/payments")
    step = payments["steps"][0]
    result = client.post(
        "/api/promotions/promote",
        headers=headers,
        json={"image": step["image"], "environmentId": envs["sit"].id, "targets": step["targets"]},
    ).get_json()["data"]["results"][0]
    assert result["status"] == "pending_approval"

    board = client.get("/api/promotions/board", headers=headers).get_json()["data"]
    payments = next(r for r in board["apps"] if r["repository"] == "ghcr.io/mock/payments")
    assert payments["steps"][0]["state"] == "pending_approval"
    assert payments["steps"][0]["bundleIds"] == [result["bundleId"]]
    event = PromotionEvent.query.filter_by(kind="promoted").one()
    assert event.bundle_id == result["bundleId"]


# ---------------------------------------------------------------------------
# Namespace patterns, the namespace map, releases
# ---------------------------------------------------------------------------

def test_patterns_bind_namespaces_with_the_most_specific_rule_winning(app):
    svc.create_default_ladder(_admin())
    envs = {env.key: env for env in svc.ladder()}
    svc.add_binding(_admin(), envs["sit"].id, {"clusterId": STAGING, "pattern": "*-sit"})
    svc.add_binding(_admin(), envs["dev"].id, {"clusterId": STAGING, "wholeCluster": True})
    svc.add_binding(_admin(), envs["uat"].id, {"clusterId": STAGING, "pattern": "payments-u*"})
    assert svc.environment_for(STAGING, "payments-sit").key == "sit"
    assert svc.environment_for(STAGING, "cards-sit").key == "sit"
    assert svc.environment_for(STAGING, "payments-uat").key == "uat"
    assert svc.environment_for(STAGING, "sandbox").key == "dev"
    # An exact binding beats any pattern.
    svc.add_binding(_admin(), envs["preprod"].id, {"clusterId": STAGING, "namespace": "cards-sit"})
    assert svc.environment_for(STAGING, "cards-sit").key == "preprod"
    with pytest.raises(svc.PromotionError):
        svc.add_binding(_admin(), envs["uat"].id, {"clusterId": STAGING, "pattern": "Bad Pattern!"})


def test_the_namespace_map_and_a_rule_preview(client, admin_token):
    _ladder()
    headers = auth_headers(admin_token)
    data = client.get(f"/api/promotions/namespace-map?clusterId={STAGING}", headers=headers).get_json()["data"]
    rows = {r["namespace"]: r for r in data["items"]}
    assert rows["payments-sit"]["environmentName"] == "SIT"
    assert rows["payments-sit"]["ruleKind"] == "exact"
    assert rows["sandbox"]["environmentId"] is None
    assert data["unassigned"] >= 1

    envs = {env.key: env for env in svc.ladder()}
    preview = client.post(
        "/api/promotions/bindings/preview",
        headers=headers,
        json={"clusterId": STAGING, "pattern": "*-dev", "environmentId": envs["dev"].id},
    ).get_json()["data"]
    taken = {m["namespace"]: m for m in preview["matches"]}
    # payments-dev is already Dev by an exact binding — the pattern does not take it.
    assert taken["payments-dev"]["takes"] is False
    assert preview["count"] == 0
    # Nothing was saved by the preview.
    assert all(b["namespace"] != "*-dev" for e in svc.setup_payload()["environments"] for b in e["bindings"])


def test_a_release_promotes_several_apps_in_one_approval(client, admin_token):
    envs = _ladder()
    _approvals(1)
    headers = auth_headers(admin_token)
    data = client.get("/api/promotions/overview", headers=headers).get_json()["data"]
    gate = data["gates"][1]  # SIT → UAT
    assert gate["counts"]["ready"] >= 2
    ready = [a for a in data["apps"] if a["steps"][1]["state"] == "ready"]
    items = [{"image": a["steps"][1]["image"], "targets": a["steps"][1]["targets"]} for a in ready]
    release = client.post(
        "/api/promotions/releases",
        headers=headers,
        json={"environmentId": envs["uat"].id, "items": items, "name": "SIT drop", "reference": "CHG-1"},
    ).get_json()["data"]
    assert release["status"] == "pending_approval"
    assert len(release["bundleIds"]) == 1
    bundle = db.session.get(ChangeBundle, release["bundleIds"][0])
    assert len(bundle.items) == len(items)
    assert "SIT drop" in bundle.note

    listed = client.get("/api/promotions/releases", headers=headers).get_json()["data"]["items"]
    assert listed[0]["name"] == "SIT drop" and listed[0]["reference"] == "CHG-1"


def test_a_release_with_a_skip_needs_a_reason_and_goes_to_its_own_bundle(app):
    envs = _ladder()
    _approvals(0)
    targets = [{"clusterId": STAGING, "namespace": "payments-uat", "kind": "Deployment", "name": "payments-api"}]
    # Without a reason the skip is refused.
    release = svc.create_release(
        _admin(), environment_id=envs["uat"].id, items=[{"image": DEV_IMAGE, "targets": targets}]
    )
    assert release["items"][0]["targets"][0]["status"] == "refused"
    assert release["items"][0]["targets"][0]["blocked"] is True
    # With one, it becomes an exception bundle needing somebody else's approval.
    release = svc.create_release(
        _admin(), environment_id=envs["uat"].id, items=[{"image": DEV_IMAGE, "targets": targets}],
        exception_reason="Hotfix for the card outage; SIT is down today.",
    )
    assert release["kind"] == "exception"
    assert release["status"] == "pending_approval"
    bundle = db.session.get(ChangeBundle, release["bundleIds"][0])
    assert bundle.promotion_exception["release"] == release["name"]
    assert bundle.required_approvals == 1


def test_the_overview_groups_by_system_and_counts_lag(client, admin_token):
    _ladder()
    data = client.get("/api/promotions/overview", headers=auth_headers(admin_token)).get_json()["data"]
    payments = next(a for a in data["apps"] if a["repository"] == "ghcr.io/mock/payments")
    # No part-of label in the mock: the namespace minus its environment word.
    assert payments["system"] == "payments"
    assert payments["lag"] == 2  # Dev→SIT and SIT→UAT are behind; UAT→Pre-prod in sync
    assert data["environments"][0]["appCount"] >= 3
    assert data["clusters"][STAGING]["name"] == "Staging EU-West"
