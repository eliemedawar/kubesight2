"""Build log ingest: incremental reads, idempotent storage, no build failed by a log.

The Kubernetes runner used to re-read a stage's whole log (up to 16 MB) every
second and drop what it already had. It now reads ``--since-time`` the second
of the last entry it took, skipping that second's entries it already has —
and every one of these tests is about the sequence numbers staying exactly
what a full read would have produced.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models_ci import CiBuild, CiBuildStage, CiLogChunk, CiService
from api.services.ci import logs as logs_service
from api.services.ci.runners import kubernetes as k8s
from api.services.ci.runners.base import RUNNING, SUCCEEDED, RunnerHandle

_T0 = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)


def _stamp(seconds: float) -> str:
    moment = _T0 + timedelta(seconds=seconds)
    # kubelet's RFC3339Nano trims trailing zeros; mimic the variable width.
    text = moment.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".")
    return text + "Z"


class TimedCluster:
    """A fake kubectl whose logs carry timestamps and honour --since-time."""

    def __init__(self, honour_since=True):
        self.honour_since = honour_since
        self.entries = {}  # container -> [(seconds, text)]
        self.calls = []
        self.init_states = {"stage-0": {"running": {}}}
        self.collector_state = {"waiting": {}}
        self.job_status = {"active": 1}

    def say(self, container, seconds, text):
        self.entries.setdefault(container, []).append((seconds, text))

    def pod(self):
        return {
            "metadata": {"creationTimestamp": "2026-10-12T10:00:00Z", "annotations": {}},
            "spec": {"initContainers": [{"name": name} for name in self.init_states]},
            "status": {
                "initContainerStatuses": [
                    {"name": name, "state": state} for name, state in self.init_states.items()
                ],
                "containerStatuses": [{"name": "collector", "state": self.collector_state}],
            },
        }

    def __call__(self, args, input_text=None):
        self.calls.append(list(args))
        if args[:2] == ["get", "job"]:
            return 0, json.dumps({"metadata": {}, "status": self.job_status}), ""
        if args[:2] == ["get", "pods"]:
            return 0, json.dumps({"items": [self.pod()]}), ""
        if args[0] == "logs":
            container = args[args.index("-c") + 1]
            if container not in self.entries:
                return 1, "", "container is waiting to start"
            since = None
            if "--since-time" in args and self.honour_since:
                since = datetime.strptime(
                    args[args.index("--since-time") + 1], "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=timezone.utc)
            out = []
            for seconds, text in self.entries[container]:
                if since is not None and _T0 + timedelta(seconds=seconds) < since:
                    continue
                if "--timestamps" in args:
                    out.append(f"{_stamp(seconds)} {text}\n")
                else:
                    out.append(f"{text}\n")
            if "--tail" in args:
                out = out[-int(args[args.index("--tail") + 1]):]
            return 0, "".join(out), ""
        return 0, "", ""

    def log_calls(self):
        return [call for call in self.calls if call[0] == "logs"]


@pytest.fixture(autouse=True)
def _clean_runner_state():
    k8s._log_cursors.clear()
    yield
    k8s.set_kubectl_runner(None)
    k8s._log_cursors.clear()


def _handle(container="stage-0"):
    return RunnerHandle(runner_id=1, external_ref=f"ci-b7-svc#{container}")


def _drain_all(adapter, handle, stored):
    """Drain like the engine does: after the highest stored seq."""
    after = stored[-1][0] if stored else 0
    for chunk in adapter.drain_logs(handle, after):
        assert chunk.seq == (stored[-1][0] if stored else 0) + 1, "sequence must be gapless"
        stored.append((chunk.seq, chunk.content))
    return stored


def _canonical(cluster, container="stage-0"):
    """What one full read (the old reader) numbers the lines as."""
    adapter = k8s.KubernetesJobRunnerAdapter()
    return adapter._container_log_lines("ci-b7-svc", container)


def test_incremental_reads_number_lines_exactly_like_a_full_read():
    cluster = TimedCluster()
    k8s.set_kubectl_runner(cluster)
    adapter = k8s.KubernetesJobRunnerAdapter()
    stored = []

    cluster.say("stage-0", 0.1, "one")
    cluster.say("stage-0", 0.5, "two")
    _drain_all(adapter, _handle(), stored)
    # More output in the SAME second as the last line read: the boundary.
    cluster.say("stage-0", 0.7, "three")
    cluster.say("stage-0", 0.7, "three again, same instant")
    _drain_all(adapter, _handle(), stored)
    _drain_all(adapter, _handle(), stored)  # nothing new
    cluster.say("stage-0", 1.2, "four\rfour-b")  # one entry, two lines
    cluster.say("stage-0", 3.0, "")
    cluster.say("stage-0", 3.4, "six")
    _drain_all(adapter, _handle(), stored)

    assert [content for _, content in stored] == _canonical(cluster)
    assert [content for _, content in stored] == [
        "one", "two", "three", "three again, same instant", "four", "four-b", "", "six",
    ]
    # Only the first read started from the beginning.
    since = ["--since-time" in call for call in cluster.log_calls() if "--timestamps" in call]
    assert since[0] is False and all(since[1:-1])


def test_a_fake_that_ignores_since_time_still_numbers_correctly():
    """Misalignment (here: the reader returned everything again) falls back to
    a full read rather than storing a line twice."""
    cluster = TimedCluster(honour_since=False)
    k8s.set_kubectl_runner(cluster)
    adapter = k8s.KubernetesJobRunnerAdapter()
    stored = []
    for second in range(5):
        cluster.say("stage-0", second + 0.2, f"line {second}")
        _drain_all(adapter, _handle(), stored)
    assert [content for _, content in stored] == [f"line {n}" for n in range(5)]


def test_a_lost_cursor_resumes_from_the_stored_sequence():
    """A restart (or another process draining) loses the cursor: one full read
    picks up after the stored seq, numbering unchanged."""
    cluster = TimedCluster()
    k8s.set_kubectl_runner(cluster)
    adapter = k8s.KubernetesJobRunnerAdapter()
    stored = []
    cluster.say("stage-0", 0.1, "a")
    cluster.say("stage-0", 0.2, "b")
    _drain_all(adapter, _handle(), stored)
    k8s._log_cursors.clear()
    cluster.say("stage-0", 0.3, "c")
    _drain_all(adapter, _handle(), stored)
    cluster.say("stage-0", 2.0, "d")
    _drain_all(adapter, _handle(), stored)
    assert stored == [(1, "a"), (2, "b"), (3, "c"), (4, "d")]


def test_a_cursor_that_disagrees_with_the_stored_seq_is_not_trusted():
    cluster = TimedCluster()
    k8s.set_kubectl_runner(cluster)
    adapter = k8s.KubernetesJobRunnerAdapter()
    cluster.say("stage-0", 0.1, "a")
    cluster.say("stage-0", 0.2, "b")
    assert [c.seq for c in adapter.drain_logs(_handle(), 0)] == [1, 2]
    # Those two were never stored (say, the insert rolled back): asked again
    # from 0, the runner hands them out again rather than skipping ahead.
    assert [c.content for c in adapter.drain_logs(_handle(), 0)] == ["a", "b"]


def test_collector_lines_follow_the_last_stage_once_it_has_finished():
    cluster = TimedCluster()
    k8s.set_kubectl_runner(cluster)
    adapter = k8s.KubernetesJobRunnerAdapter()
    stored = []
    cluster.say("stage-0", 0.1, "building")
    _drain_all(adapter, _handle(), stored)

    cluster.say("stage-0", 0.4, "[kubesight-exit] 0")
    cluster.init_states["stage-0"] = {"terminated": {"exitCode": 0}}
    cluster.collector_state = {"running": {}}
    cluster.say("collector", 1.0, "uploading app.jar")
    _drain_all(adapter, _handle(), stored)
    cluster.say("collector", 1.0, "uploaded")
    cluster.say("collector", 2.5, "done")
    cluster.collector_state = {"terminated": {"exitCode": 0}}
    _drain_all(adapter, _handle(), stored)
    calls_before = len(cluster.log_calls())
    _drain_all(adapter, _handle(), stored)  # everything is known complete

    assert [content for _, content in stored] == [
        "building",
        "[kubesight-exit] 0",
        "[collector] uploading app.jar",
        "[collector] uploaded",
        "[collector] done",
    ]
    assert len(cluster.log_calls()) == calls_before, "a finished stage is not read again"

    # And a full read from scratch agrees on every number.
    k8s._log_cursors.clear()
    assert [c.content for c in adapter.drain_logs(_handle(), 0)] == [c for _, c in stored]


def test_one_observation_serves_the_drain_the_poll_and_the_exit_marker():
    cluster = TimedCluster()
    k8s.set_kubectl_runner(cluster)
    adapter = k8s.KubernetesJobRunnerAdapter()
    cluster.init_states = {"stage-0": {"terminated": {"exitCode": 0}}, "stage-1": {"running": {}}}
    cluster.say("stage-0", 0.1, "compiled")
    cluster.say("stage-0", 0.2, "[kubesight-exit] 0")

    observation = adapter.observe([_handle()])
    gets = len([call for call in cluster.calls if call[0] == "get"])
    lines = list(adapter.drain_logs(_handle(), 0, observation=observation))
    status = adapter.poll(_handle(), observation=observation)

    assert [line.content for line in lines] == ["compiled", "[kubesight-exit] 0"]
    assert status == SUCCEEDED
    assert len([call for call in cluster.calls if call[0] == "get"]) == gets == 2
    # The exit marker came from the drained tail, not another `logs --tail`.
    assert not any("--tail" in call for call in cluster.log_calls())


def test_drain_reads_the_pod_before_the_log():
    """A pod snapshot showing the stage terminated must precede the log read,
    or the read could miss the stage's last lines while the collector's get
    numbered in their place."""
    cluster = TimedCluster()
    k8s.set_kubectl_runner(cluster)
    adapter = k8s.KubernetesJobRunnerAdapter()
    cluster.say("stage-0", 0.1, "x")
    list(adapter.drain_logs(_handle(), 0))
    kinds = [call[0] + ":" + call[1] if call[0] == "get" else call[0] for call in cluster.calls]
    assert kinds.index("get:pods") < kinds.index("logs")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _stage(app):
    service = CiService(name="Log Svc", slug="log-svc")
    db.session.add(service)
    db.session.flush()
    build = CiBuild(service_id=service.id, number=1, status="running")
    db.session.add(build)
    db.session.flush()
    stage = CiBuildStage(build_id=build.id, name="Build", position=0, status="running")
    db.session.add(stage)
    db.session.commit()
    return build, stage


def test_append_is_idempotent_when_two_writers_race_on_the_same_lines(app):
    """The log viewer's request used to drain beside the ticker; both read the
    same highest seq and the second insert broke the unique constraint, which
    failed the build. A stale ``known_max`` reproduces the race."""
    with app.app_context():
        _, stage = _stage(app)
        logs_service.append(stage, [(1, "a", "stdout"), (2, "b", "stdout")], known_max=0)
        # The second writer read highest_seq before the first committed.
        logs_service.append(
            stage, [(1, "a", "stdout"), (2, "b", "stdout"), (3, "c", "stdout")], known_max=0
        )
        rows = CiLogChunk.query.filter_by(build_stage_id=stage.id).order_by(CiLogChunk.seq).all()
        assert [(row.seq, row.content) for row in rows] == [(1, "a"), (2, "b"), (3, "c")]
        assert db.session.get(CiBuildStage, stage.id).log_line_count == 3


def test_append_masks_and_honours_the_stage_cap(app, monkeypatch):
    monkeypatch.setattr(logs_service, "_MAX_LINES_PER_STAGE", 3)
    with app.app_context():
        _, stage = _stage(app)
        mask = logs_service.build_masker(["s3cr3t-value"])
        logs_service.append(
            stage,
            [(n, f"line {n} s3cr3t-value", "stdout") for n in range(1, 6)],
            mask=mask,
        )
        rows = CiLogChunk.query.filter_by(build_stage_id=stage.id).order_by(CiLogChunk.seq).all()
        assert [row.seq for row in rows] == [1, 2, 3]
        assert all("s3cr3t-value" not in row.content for row in rows)
        assert db.session.get(CiBuildStage, stage.id).log_truncated is True


def test_read_tail_returns_the_last_lines_in_order(app):
    with app.app_context():
        _, stage = _stage(app)
        logs_service.append(stage, [(n, f"line {n}", "stdout") for n in range(1, 2001)])
        tail = logs_service.read_tail(stage.id, 5)
        assert [line["content"] for line in tail["lines"]] == [f"line {n}" for n in range(1996, 2001)]
        assert tail["truncated"] is True
        assert logs_service.read_tail(stage.id, 5000)["truncated"] is False


def test_the_download_streams_every_line_in_order(app):
    with app.app_context():
        _, stage = _stage(app)
        logs_service.append(stage, [(n, f"line {n}", "stdout") for n in range(1, 4501)])
        pieces = list(logs_service.iter_download(stage.id, batch=2000))
        assert len(pieces) == 3
        assert "".join(pieces) == "\n".join(f"line {n}" for n in range(1, 4501))


def test_a_log_storage_error_never_fails_the_build(app, monkeypatch):
    """Whatever goes wrong storing output, the build advances as if the lines
    had simply not arrived yet: they are read again on the next pass."""
    from api.services.ci import engine

    class Adapter:
        def drain_logs(self, handle, after):
            yield k8s.LogChunk(seq=after + 1, content="hello")

    def boom(*args, **kwargs):
        raise RuntimeError("database hiccup")

    with app.app_context():
        build, stage = _stage(app)
        monkeypatch.setattr(logs_service, "append", boom)
        engine._pump_logs(build, stage, Adapter(), _handle())
        assert db.session.get(CiBuild, build.id).status == "running"


def test_the_log_endpoint_only_reads(app, client, admin_token, monkeypatch):
    """GET .../logs no longer drains the runner in the request thread."""
    from api.services.ci import engine

    with app.app_context():
        build, stage = _stage(app)
        stage.external_ref = "ci-b1-log-svc#stage-0"
        db.session.add(stage)
        db.session.commit()
        build_id, stage_id = build.id, stage.id
        logs_service.append(stage, [(1, "stored", "stdout")])

    called = []
    monkeypatch.setattr(engine, "_pump_logs", lambda *a, **k: called.append(a))
    response = client.get(
        f"/api/ci/builds/{build_id}/stages/{stage_id}/logs",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200
    assert [line["content"] for line in response.get_json()["data"]["lines"]] == ["stored"]
    assert called == []
