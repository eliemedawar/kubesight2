"""The Cluster Builder pages poll every few seconds; what they read stays small.

* GET /api/cluster-builds issues the same number of statements however many
  builds exist (nodes, steps, provisioning jobs and state are read in batches)
  and never loads a step log, a plan or an OpenTofu state.
* The VM count comes from the stored count, not from decrypting the state; a
  state written before the count existed is counted once and kept.
* The log panel reads one step's log, not every step's.
* A plan file is not kept once it can no longer be applied.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from api.db import db
from api.models import (
    ClusterBuild,
    ClusterBuildNode,
    ClusterBuildStep,
    ClusterInfraState,
    ClusterProvisionJob,
)
from api.secret_encryption import encrypt_secret
from api.services.cluster_build.provisioning import jobs, state_store
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


def _state_document(vm_names):
    return {
        "version": 4,
        "serial": 3,
        "lineage": "lineage-1",
        "resources": [
            {"mode": "managed", "type": "vsphere_folder", "name": "build", "instances": [{}]},
            {
                "mode": "managed",
                "type": "vsphere_virtual_machine",
                "name": "node",
                "instances": [{"index_key": name, "attributes": {"name": name}} for name in vm_names],
            },
        ],
    }


def _build(*, vmware: bool, jobs_count: int = 3, vms: int = 3) -> ClusterBuild:
    _SEQ["n"] += 1
    n = _SEQ["n"]
    build = ClusterBuild(
        name=f"perf-{n}",
        status="building",
        k8s_version="1.32.4",
        control_plane_endpoint=f"10.0.{n}.10:6443",
        machine_source="vmware" if vmware else "existing",
        provision_status="ready" if vmware else None,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=n),
    )
    db.session.add(build)
    db.session.flush()
    for index in range(3):
        node = ClusterBuildNode(
            build_id=build.id,
            hostname=f"perf-{n}-{index}",
            address=f"10.0.{n}.{index + 20}",
            role="control_plane" if index == 0 else "worker",
            status="joined",
            position=index,
            preflight_json={"status": "pass", "checks": [{"id": "x" * 50}] * 20},
        )
        db.session.add(node)
    for phase in ("base_prep", "init", "cni", "join_workers"):
        db.session.add(ClusterBuildStep(
            build_id=build.id, phase=phase,
            status="running" if phase == "join_workers" else "completed",
            log_tail="L" * 8000,
        ))
    if vmware:
        for index in range(jobs_count):
            db.session.add(ClusterProvisionJob(
                build_id=build.id,
                operation="create",
                status="succeeded" if index < jobs_count - 1 else "planned",
                config_json={"tofu": {"big": "c" * 5000}},
                plan_cipher=encrypt_secret("p" * 2000),
                plan_text="T" * 20000,
                log_tail="G" * 20000,
            ))
        db.session.flush()
        state_store.write_state(
            build.id, json.dumps(_state_document([f"perf-{n}-{i}" for i in range(vms)]))
        )
    db.session.commit()
    return build


def _list(client, token):
    with _statements() as statements:
        response = client.get("/api/cluster-builds", headers=auth_headers(token))
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]["items"], statements


class TestBuildsList:
    def test_statement_count_does_not_grow_with_builds(self, app, client, admin_token):
        with app.app_context():
            _build(vmware=True)
            _build(vmware=False)
        client.get("/api/cluster-builds", headers=auth_headers(admin_token))  # warm caches
        items, few = _list(client, admin_token)
        assert len(items) == 2

        with app.app_context():
            for _ in range(5):
                _build(vmware=True, jobs_count=4)
                _build(vmware=False)
        items, many = _list(client, admin_token)
        assert len(items) == 12
        assert len(many) == len(few), "\n".join(many)

        # Nothing big is read for a card.
        joined = "\n".join(many).lower()
        for column in (
            "cluster_infra_states.state_cipher as",
            "cluster_provision_jobs.plan_text",
            "cluster_provision_jobs.plan_cipher",
            "cluster_provision_jobs.config_json",
            "cluster_provision_jobs.log_tail",
            "cluster_build_steps.log_tail",
            "cluster_build_nodes.preflight_json",
        ):
            assert column not in joined, column

    def test_cards_still_carry_job_vm_count_and_phase(self, app, client, admin_token):
        with app.app_context():
            build = _build(vmware=True, jobs_count=3, vms=4)
            build_id = build.id
            planned_id = max(j.id for j in jobs.jobs_for(build_id))
        items, _ = _list(client, admin_token)
        card = next(item for item in items if item["id"] == build_id)
        assert card["currentPhase"] == "join_workers"
        assert card["provisioning"]["vmCount"] == 4
        # The open (planned) job wins over newer-looking finished ones.
        assert card["provisioning"]["job"]["id"] == planned_id
        assert card["provisioning"]["job"]["status"] == "planned"
        assert "planText" not in card["provisioning"]["job"]
        assert card["nodeCounts"] == {"controlPlane": 1, "worker": 2, "loadbalancer": 0}

    def test_state_written_before_the_count_existed_is_counted_once(self, app, client, admin_token):
        with app.app_context():
            build = _build(vmware=True, vms=2)
            build_id = build.id
            ClusterInfraState.query.filter_by(build_id=build_id).update({"vm_count": None})
            db.session.commit()
        items, _ = _list(client, admin_token)
        assert next(i for i in items if i["id"] == build_id)["provisioning"]["vmCount"] == 2
        with app.app_context():
            assert ClusterInfraState.query.filter_by(build_id=build_id).first().vm_count == 2


class TestStateCount:
    def test_write_and_delete_keep_the_vm_count(self, app):
        with app.app_context():
            build = _build(vmware=True, vms=3)
            row = ClusterInfraState.query.filter_by(build_id=build.id).first()
            assert row.vm_count == 3
            assert state_store.summary(build.id)["vmCount"] == 3
            state_store.delete_state(build.id)
            assert ClusterInfraState.query.filter_by(build_id=build.id).first().vm_count == 0
            assert state_store.summary(build.id)["vmCount"] == 0
            assert state_store.has_resources(build.id) is False


class TestDetail:
    def test_detail_leaves_out_what_the_card_does_not_show(self, app, client, admin_token):
        with app.app_context():
            build = _build(vmware=True, jobs_count=2)
            build_id = build.id
            job = jobs.open_job(build_id)
            job_id = job.id
        response = client.get(f"/api/cluster-builds/{build_id}", headers=auth_headers(admin_token))
        current = response.get_json()["data"]["provisioning"]["job"]
        # Planned: the plan review shows OpenTofu's own plan.
        assert current["planText"] == "T" * 20000

        with app.app_context():
            row = db.session.get(ClusterProvisionJob, job_id)
            row.status = "applying"
            db.session.commit()
        current = client.get(
            f"/api/cluster-builds/{build_id}", headers=auth_headers(admin_token)
        ).get_json()["data"]["provisioning"]["job"]
        assert "planText" not in current
        assert current["logTail"] == "G" * 20000

        with app.app_context():
            row = db.session.get(ClusterProvisionJob, job_id)
            row.status = "succeeded"
            db.session.commit()
        current = client.get(
            f"/api/cluster-builds/{build_id}", headers=auth_headers(admin_token)
        ).get_json()["data"]["provisioning"]["job"]
        # Kubernetes is building on the VMs: the card is a one-liner.
        assert "logTail" not in current and "planText" not in current

        # The job endpoint still returns everything.
        full = client.get(
            f"/api/cluster-builds/{build_id}/provision/jobs/{job_id}", headers=auth_headers(admin_token)
        ).get_json()["data"]
        assert full["planText"] == "T" * 20000 and full["logTail"] == "G" * 20000


class TestStepLogs:
    def test_step_parameter_returns_only_that_step(self, app, client, admin_token):
        with app.app_context():
            build = _build(vmware=False)
            build_id = build.id
            step_id = ClusterBuildStep.query.filter_by(build_id=build_id, phase="init").first().id
        everything = client.get(
            f"/api/cluster-builds/{build_id}/logs", headers=auth_headers(admin_token)
        ).get_json()["data"]["items"]
        assert len(everything) == 4
        one = client.get(
            f"/api/cluster-builds/{build_id}/logs?step={step_id}", headers=auth_headers(admin_token)
        ).get_json()["data"]["items"]
        assert [item["id"] for item in one] == [step_id]
        assert one[0]["logTail"] == "L" * 8000


class TestPlanFileLifetime:
    def test_superseded_rejected_discarded_and_failed_plans_drop_the_file(self, app):
        from api.services.cluster_build.provisioning import service as provisioning_service

        with app.app_context():
            build = _build(vmware=True, jobs_count=1)
            job = jobs.open_job(build.id)
            assert job.plan_cipher
            provisioning_service._require_no_open_job(build, supersede_planned=True)
            assert db.session.get(ClusterProvisionJob, job.id).plan_cipher is None

            failing = ClusterProvisionJob(
                build_id=build.id, operation="create", status="applying",
                plan_cipher=encrypt_secret("plan"),
            )
            db.session.add(failing)
            db.session.commit()
            jobs._fail(failing.id, "apply_failed", "boom")
            assert db.session.get(ClusterProvisionJob, failing.id).plan_cipher is None


class TestTofuOutput:
    def test_the_transcript_is_bounded_and_the_stored_tail_unchanged(self, app, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setattr(jobs, "_TRANSCRIPT_CHARS", 5000)
        job = SimpleNamespace(log_tail="earlier phase", progress_json={})
        with app.app_context():
            output = jobs._Output(job, ["s3cret"])
            for number in range(2000):
                output.on_line(f"line {number:05d} with s3cret inside")
            output.on_line("Error: something broke")
            output.on_line("  detail of the failure")
            output.flush()
        assert sum(len(line) + 1 for line in output.lines) <= 5000
        assert job.log_tail.endswith("Error: something broke\n  detail of the failure")
        assert "s3cret" not in job.log_tail
        assert len(job.log_tail) <= jobs._LOG_TAIL_CHARS
        assert output.error_summary().startswith("Error: something broke")
