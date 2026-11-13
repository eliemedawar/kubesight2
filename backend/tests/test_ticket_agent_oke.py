"""Hermes on OKE (workload identity): a new cluster and node pools from a
ticket, each approved ONCE — approving the request approves the plan's hash."""

from __future__ import annotations

import pytest

from api.db import db
from api.models import ClusterBuild, ClusterProvisionJob, TicketInterpretation, User
from api.services.ticket_agent import engine, oke_requests, settings as agent_settings, vm_requests

from tests.test_cluster_builds import auth_headers  # noqa: F401
from tests.test_oke_workload_identity import (  # noqa: F401  (fixtures are used by name)
    approve, audit_lines, create_build, create_wi_account, plan, second_person, wi_install,
)
from tests.test_ticket_agent import _ticket, agent, writes  # noqa: F401


@pytest.fixture()
def oke_agent(app, agent, wi_install, client, admin_token):
    agent_settings.update({"vmRequestsEnabled": True})
    return create_wi_account(client, admin_token)


def _hermes():
    return User.query.filter_by(username="admin").first()


def _common(**extra):
    return {"confidence": "High", "understanding": "As the ticket says.",
            "comment": "Understood; waiting for approval.", "commentOnApprove": "Approved — starting now.",
            **extra}


def _task(ticket):
    return (TicketInterpretation.query.filter_by(ticket_record_id=ticket.id, route=vm_requests.ROUTE)
            .order_by(TicketInterpretation.id.desc()).first())


def _vm(task):
    db.session.refresh(task)
    return (task.decision or {}).get("vmRequest") or {}


def _tick():
    for _ in range(4):
        vm_requests.advance()


def _approver(app, mfa=True, username="person"):
    token = second_person(app, mfa=mfa, username=username)
    return User.query.filter_by(username=username).first(), token


class TestNewCluster:
    def test_planned_offered_approved_once_and_ready(self, app, oke_agent, writes):
        ticket = _ticket(subject="New OKE cluster uat-pay-02")
        result = oke_requests.request_cluster(ticket.id, _common(
            name="uat-pay-02", ownerEmail="emedawer@areeba.com",
            nodePools=[{"name": "workers", "count": 2, "ocpus": 2, "memoryGb": 16},
                       {"name": "db", "count": 1, "ocpus": 4, "memoryGb": 32,
                        "labels": {"tier": "db"}, "taints": [{"key": "dedicated", "value": "db", "effect": "NoSchedule"}]}],
        ), user=_hermes())
        assert result["requested"] and result["status"] == "planning"
        task = _task(ticket)
        _tick()
        db.session.refresh(task)
        vm = _vm(task)
        assert task.status == "awaiting_approval", vm
        job = db.session.get(ClusterProvisionJob, vm["jobId"])
        assert job.status == "awaiting_approval" and job.plan_sha256
        assert "Plan sha256" in vm_requests.approval_text(task, ticket)

        person, _ = _approver(app)
        engine.approve(task.id, "person", user=person)
        _tick()
        vm = _vm(task)
        assert vm["phase"] == "cluster_ready", vm
        job = db.session.get(ClusterProvisionJob, vm["jobId"])
        assert job.status == "succeeded" and job.approved_plan_sha256 == job.plan_sha256
        build = db.session.get(ClusterBuild, vm["buildId"])
        assert [p["name"] for p in build.provisioning_json["nodePools"]] == ["workers", "db"]

    def test_an_approver_without_mfa_is_refused_and_it_keeps_waiting(self, app, oke_agent, writes):
        ticket = _ticket(subject="New OKE cluster uat-pay-03")
        oke_requests.request_cluster(ticket.id, _common(name="uat-pay-03", ownerEmail="emedawer@areeba.com"),
                                     user=_hermes())
        _tick()
        task = _task(ticket)
        person, _ = _approver(app, mfa=False, username="nomfa")
        with pytest.raises(engine.AgentError, match="MFA"):
            engine.approve(task.id, "nomfa", user=person)
        db.session.refresh(task)
        assert task.status == "awaiting_approval"

    def test_a_ticket_number_is_not_a_name(self, app, oke_agent, writes):
        ticket = _ticket(subject="New OKE cluster")
        with pytest.raises(engine.AgentError):
            oke_requests.request_cluster(ticket.id, _common(name=ticket.ticket_number.lower(),
                                                            ownerEmail="emedawer@areeba.com"), user=_hermes())


class TestNodePools:
    def _cluster(self, app, client, admin_token, oke_agent):
        build = create_build(client, admin_token, oke_agent, name="oke-np")
        job = plan(client, admin_token, build["id"])
        assert approve(client, second_person(app, username="first"), build["id"], job["id"],
                       job["planSha256"]).status_code == 200
        row = db.session.get(ClusterBuild, build["id"])
        row.status, row.result_cluster_id = "completed", "custom-np"
        db.session.commit()
        return row

    def test_catalog_lists_the_cluster_and_its_pools(self, app, client, admin_token, oke_agent):
        self._cluster(app, client, admin_token, oke_agent)
        listed = oke_requests.catalog()
        [cluster] = [c for c in listed["clusters"] if c["name"] == "oke-np"]
        assert cluster["nodePools"][0]["name"] == "workers"

    def test_add_a_pool_then_approve_once(self, app, client, admin_token, oke_agent, writes):
        row = self._cluster(app, client, admin_token, oke_agent)
        ticket = _ticket(subject="Add a db pool to oke-np")
        oke_requests.request_node_pools(ticket.id, _common(
            cluster="oke-np",
            nodePools=[{"name": "db", "count": 1, "ocpus": 4, "memoryGb": 32, "labels": {"tier": "db"}}],
        ), user=_hermes())
        task = _task(ticket)
        _tick()
        db.session.refresh(task)
        assert task.status == "awaiting_approval", _vm(task)
        person, _ = _approver(app)
        engine.approve(task.id, "person", user=person)
        _tick()
        vm = _vm(task)
        assert vm["phase"] == "pools_ready", vm
        db.session.refresh(row)
        assert [p["name"] for p in row.provisioning_json["nodePools"]] == ["workers", "db"]

    def test_rejected_request_discards_its_plan_and_keeps_the_cluster(self, app, client, admin_token,
                                                                     oke_agent, writes):
        row = self._cluster(app, client, admin_token, oke_agent)
        ticket = _ticket(subject="Grow workers on oke-np")
        oke_requests.request_node_pools(ticket.id, _common(cluster="oke-np", nodePools=[{"name": "workers", "count": 3}]),
                                        user=_hermes())
        _tick()
        task = _task(ticket)
        job_id = _vm(task)["jobId"]
        engine.reject(task.id, "person", note="not now")
        db.session.refresh(row)
        assert db.session.get(ClusterProvisionJob, job_id).status in ("discarded", "rejected")
        assert row.status == "completed"

    def test_nothing_to_change_is_refused(self, app, client, admin_token, oke_agent, writes):
        self._cluster(app, client, admin_token, oke_agent)
        ticket = _ticket(subject="Same pools")
        with pytest.raises(engine.AgentError, match="nothing to change"):
            oke_requests.request_node_pools(ticket.id, _common(cluster="oke-np", nodePools=[{"name": "workers"}]),
                                            user=_hermes())
