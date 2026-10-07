"""List endpoints load their rows in a fixed number of queries.

Each list here once asked the database something per row — a cluster name per
bundle item, a build, an artifact and a pipeline per CI service, one user load
per distinct requester — which is invisible on an empty table and seconds on a
two-year-old one. Every test seeds a few rows, counts the statements one
request issues, seeds many more (each with its own users and clusters, so a
per-row lookup cannot hide behind the session's identity map), and checks the
count did not move. The payload assertions keep the batched loads honest.

Users here carry several cluster entries AND several access rules on purpose:
those two collections are joined eager loads of User, and loading them together
multiplies the rows — the other half of what made these lists slow.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from api.db import db
from api.models import (
    AccessRule,
    AuditLog,
    ChangeBundle,
    ChangeBundleItem,
    ChangeBundleVote,
    Cluster,
    DeploymentRequest,
    DeploymentRequestVote,
    Role,
    User,
    UserClusterAccess,
)
from api.models_ci import CiArtifact, CiBuild, CiBuildStage, CiService
from tests.conftest import auth_headers

_SEQ = {"n": 0}


@contextmanager
def _statements():
    statements: list[str] = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield statements
    finally:
        event.remove(db.engine, "before_cursor_execute", before_cursor_execute)


def _get(client, token, path):
    with _statements() as statements:
        response = client.get(path, headers=auth_headers(token))
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"], len(statements)


def _user(clusters: int = 3, rules: int = 4) -> User:
    """A viewer with legacy cluster entries and access rules, like a real one."""
    _SEQ["n"] += 1
    n = _SEQ["n"]
    user = User(
        username=f"lister-{n}",
        email=f"lister-{n}@example.com",
        full_name=f"Lister {n}",
        role_id=Role.query.filter_by(name="viewer").first().id,
    )
    db.session.add(user)
    db.session.flush()
    for index in range(clusters):
        db.session.add(UserClusterAccess(user_id=user.id, cluster_id=f"custom-{index + 1}"))
    for index in range(rules):
        db.session.add(
            AccessRule(
                user_id=user.id,
                cluster_id=f"custom-{index % clusters + 1}",
                namespace=f"ns-{index}",
                resource_type="namespace",
                permission_key="namespaces:view",
                effect="allow",
            )
        )
    return user


def _cluster(name: str, *, active: bool = True) -> Cluster:
    row = Cluster(name=name, host=f"{name}.example.com", port=6443, is_active=active)
    db.session.add(row)
    db.session.flush()
    return row


# ---------------------------------------------------------------------------
# CI services
# ---------------------------------------------------------------------------

def _ci_service(client, token, builds: int) -> int:
    _SEQ["n"] += 1
    response = client.post(
        "/api/ci/services",
        json={"name": f"Query Count Service {_SEQ['n']}", "applicationType": "java"},
        headers=auth_headers(token),
    )
    assert response.status_code == 201, response.get_json()
    service_id = response.get_json()["data"]["id"]
    base = datetime.now(timezone.utc) - timedelta(days=1)
    for number in range(1, builds + 1):
        build = CiBuild(
            service_id=service_id,
            number=number,
            status="failed" if number % 3 == 0 else "success",
            requested_by_user_id=_user().id,
            queued_at=base + timedelta(minutes=number),
        )
        db.session.add(build)
        db.session.flush()
        db.session.add(CiBuildStage(build_id=build.id, name="compile", position=0, status="success"))
        db.session.add(
            CiBuildStage(
                build_id=build.id,
                name="test",
                position=1,
                status="failed" if build.status == "failed" else "success",
            )
        )
        db.session.add(
            CiArtifact(
                service_id=service_id,
                build_id=build.id,
                name=f"app-{number}.jar",
                created_at=base + timedelta(minutes=number),
            )
        )
    db.session.commit()
    return service_id


def test_ci_service_list_does_not_query_per_service(client, admin_token):
    for _ in range(2):
        _ci_service(client, admin_token, builds=3)
    _, small = _get(client, admin_token, "/api/ci/services")

    for _ in range(10):
        _ci_service(client, admin_token, builds=12)
    data, large = _get(client, admin_token, "/api/ci/services")

    assert data["count"] == 12
    assert large == small
    assert large <= 20

    card = next(item for item in data["items"] if item["latestBuild"]["number"] == 12)
    # Newest build is the verdict, the last ten statuses its sparkline.
    assert card["latestBuild"]["status"] == "failed"
    assert card["latestBuild"]["failedStage"] == "test"
    assert card["latestBuild"]["requestedBy"].startswith("lister-")
    assert card["recentBuildStatuses"] == [
        "failed" if number % 3 == 0 else "success" for number in range(12, 2, -1)
    ]
    assert card["latestArtifact"]["name"] == "app-12.jar"


# ---------------------------------------------------------------------------
# Change bundles
# ---------------------------------------------------------------------------

def _bundles(count: int, requester: User | None = None) -> None:
    clusters = [_cluster(f"bundle-cluster-{_SEQ['n']}-{index}") for index in range(3)]
    for index in range(count):
        bundle = ChangeBundle(
            requester_user_id=(requester or _user()).id,
            approved_by_user_id=_user().id,
            status="approved",
            note=f"bundle {index}",
        )
        db.session.add(bundle)
        db.session.flush()
        for position, cluster in enumerate(clusters):
            db.session.add(
                ChangeBundleItem(
                    bundle_id=bundle.id,
                    position=position,
                    action_type="edit_deployment",
                    cluster_id=f"custom-{cluster.id}",
                    cluster_name="stale name",
                )
            )
        for decision in ("approve", "approve", "decline"):
            _SEQ["n"] += 1
            db.session.add(
                ChangeBundleVote(
                    bundle_id=bundle.id,
                    voter_email=f"voter-{_SEQ['n']}@example.com",
                    decision=decision,
                )
            )
    db.session.commit()


def test_change_bundle_lists_do_not_query_per_bundle(client, admin_token):
    admin = User.query.filter_by(username="admin").first()
    _bundles(2)
    _bundles(2, requester=admin)
    _, small = _get(client, admin_token, "/api/change-bundles")
    _, small_mine = _get(client, admin_token, "/api/change-bundles/mine")

    _bundles(15)
    _bundles(15, requester=admin)
    gone = _cluster("decommissioned", active=False)
    bundle = ChangeBundle(requester_user_id=admin.id, status="approved")
    db.session.add(bundle)
    db.session.flush()
    db.session.add(
        ChangeBundleItem(
            bundle_id=bundle.id,
            action_type="edit_deployment",
            cluster_id=f"custom-{gone.id}",
            cluster_name="Old Prod",
        )
    )
    db.session.commit()
    data, large = _get(client, admin_token, "/api/change-bundles")
    mine, large_mine = _get(client, admin_token, "/api/change-bundles/mine")

    assert len(data["items"]) == 35
    assert len(mine["items"]) == 18
    assert (large, large_mine) == (small, small_mine)
    assert large <= 12 and large_mine <= 12

    # Active clusters show their registered name; an inactive one falls back to
    # the name the item was staged with, exactly as the one-by-one lookup did.
    assert data["items"][0]["clusterNames"] == ["Old Prod"]
    names = data["items"][1]["clusterNames"]
    assert len(names) == 3 and all(name.startswith("bundle-cluster-") for name in names)
    assert (data["items"][1]["approvals"], data["items"][1]["declines"]) == (2, 1)
    assert data["items"][1]["itemCount"] == 3
    assert data["items"][1]["approvedByName"].startswith("Lister ")


# ---------------------------------------------------------------------------
# Deployment requests
# ---------------------------------------------------------------------------

def _requests(count: int, requester: User | None = None) -> None:
    for index in range(count):
        row = DeploymentRequest(
            requester_id=(requester or _user()).id,
            decided_by_user_id=_user().id,
            cluster_id="custom-1",
            cluster_name="dev",
            message=f"request {index}",
            status="approved",
        )
        db.session.add(row)
        db.session.flush()
        _SEQ["n"] += 1
        db.session.add(
            DeploymentRequestVote(
                request_id=row.id,
                voter_email=f"voter-{_SEQ['n']}@example.com",
                decision="approve",
            )
        )
    db.session.commit()


def test_deployment_request_lists_do_not_query_per_user(client, admin_token):
    admin = User.query.filter_by(username="admin").first()
    _requests(2)
    _requests(2, requester=admin)
    _, small = _get(client, admin_token, "/api/deployment-requests")
    _, small_mine = _get(client, admin_token, "/api/deployment-requests/mine")

    _requests(15)
    _requests(15, requester=admin)
    data, large = _get(client, admin_token, "/api/deployment-requests")
    mine, large_mine = _get(client, admin_token, "/api/deployment-requests/mine")

    assert len(data["items"]) == 34
    assert len(mine["items"]) == 17
    assert (large, large_mine) == (small, small_mine)
    # /mine is polled by every page; it has to stay a handful of statements.
    assert large <= 10 and large_mine <= 10

    item = next(entry for entry in data["items"] if entry["requesterUsername"] != "admin")
    assert item["requesterName"].startswith("Lister ")
    assert item["decidedByName"].startswith("Lister ")
    assert item["approvals"] == 1 and len(item["votes"]) == 1
    assert {entry["requesterUsername"] for entry in mine["items"]} == {"admin"}


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def _joins_two_access_collections(statement: str) -> bool:
    text = statement.lower()
    return "join user_cluster_access" in text and "join access_rules" in text


def test_user_list_does_not_multiply_access_collections(app, client, admin_token):
    for _ in range(3):
        _user(clusters=3, rules=8)
    db.session.commit()
    _, small = _get(client, admin_token, "/api/users")

    for _ in range(20):
        _user(clusters=3, rules=8)
    db.session.commit()
    data, large = _get(client, admin_token, "/api/users")

    assert data["count"] == User.query.count()
    assert large == small
    assert large <= 14
    lister = next(item for item in data["items"] if item["username"].startswith("lister-"))
    assert lister["clusterAccess"] == ["custom-1", "custom-2", "custom-3"]

    # The list's own load brings each collection in its own query, never two
    # of them joined (the requesting user's auth lookup aside).
    from api.services.user_service import list_users

    db.session.expunge_all()
    with app.test_request_context("/"):
        with _statements() as statements:
            list_users()
    assert not [s for s in statements if _joins_two_access_collections(s)]


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _audit_entries(count: int) -> None:
    for index in range(count):
        db.session.add(
            AuditLog(
                actor_user_id=_user().id,
                action="query_count_probe",
                target_type="cluster",
                target_id=f"custom-{index}",
            )
        )
    db.session.commit()


def test_audit_log_list_does_not_query_per_actor(client, admin_token):
    _audit_entries(3)
    _, small = _get(client, admin_token, "/api/audit-logs")

    _audit_entries(40)
    data, large = _get(client, admin_token, "/api/audit-logs")

    assert data["count"] >= 43
    assert large == small
    assert large <= 8
    probes = [item for item in data["items"] if item["action"] == "query_count_probe"]
    assert len(probes) == 43
    assert all(item["actorUsername"].startswith("lister-") for item in probes)
