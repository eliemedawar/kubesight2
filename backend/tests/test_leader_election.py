"""Leader election for the in-process loops (scheduler tick, CI passes, boot).

The Postgres behaviour is exercised against a fake connection that models
advisory locks the way Postgres does — per session, released when the session
closes — so the takeover and fork rules are tested without a server. A real
Postgres run happens when TEST_DATABASE_URL points at one.
"""

from __future__ import annotations

import os
import threading
from datetime import timedelta

import pytest

from api.services import leader_election as le

PG_URL = "postgresql+psycopg2://kubesight:x@db.invalid:5432/kubesight"


# ---------------------------------------------------------------------------
# A tiny Postgres: session-scoped advisory locks shared by every "process".
# ---------------------------------------------------------------------------


class _FakeServer:
    def __init__(self):
        self.locks = {}  # (ns, key) -> session id
        self.next_session = 1
        self.connects = 0


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConn:
    def __init__(self, server: _FakeServer):
        self.server = server
        self.session = server.next_session
        server.next_session += 1
        server.connects += 1
        self.dead = False
        self.closed = False

    def execute(self, statement, params=None):
        if self.dead or self.closed:
            raise RuntimeError("server closed the connection unexpectedly")
        sql = str(statement)
        params = params or {}
        key = (params.get("ns"), params.get("key"))
        if sql.startswith("SET"):
            return _Result(None)
        if "pg_try_advisory_lock" in sql:
            owner = self.server.locks.get(key)
            if owner is None or owner == self.session:
                self.server.locks[key] = self.session
                return _Result(True)
            return _Result(False)
        if "pg_advisory_unlock" in sql:
            if self.server.locks.get(key) == self.session:
                del self.server.locks[key]
                return _Result(True)
            return _Result(False)
        if "pg_advisory_lock" in sql:
            owner = self.server.locks.get(key)
            if owner not in (None, self.session):
                raise RuntimeError("canceling statement due to lock timeout")
            self.server.locks[key] = self.session
            return _Result(None)
        if "pg_locks" in sql:
            return _Result(self.server.locks.get(key) == self.session)
        raise AssertionError(f"unexpected SQL: {sql}")

    def kill(self):
        """The network or the server dropped this session."""
        self.dead = True
        self._release_all()

    def close(self):
        self.closed = True
        self._release_all()

    def _release_all(self):
        for k, owner in list(self.server.locks.items()):
            if owner == self.session:
                del self.server.locks[k]


class _FakeEngine:
    def __init__(self, server):
        self.server = server
        self.connections = []

    def connect(self):
        conn = _FakeConn(self.server)
        self.connections.append(conn)
        return conn

    def dispose(self):
        pass


@pytest.fixture()
def pg(monkeypatch):
    server = _FakeServer()
    monkeypatch.setattr(le, "_make_engine", lambda url: _FakeEngine(server))
    monkeypatch.delenv("SCHEDULER_LEADER_ELECTION", raising=False)
    return server


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------


def test_sqlite_is_always_leader_and_never_connects(monkeypatch):
    monkeypatch.setattr(le, "_make_engine", lambda url: pytest.fail("must not connect"))
    lock = le.AdvisoryLock(le.SCHEDULER_LOCK, "sqlite:///kubesight.db")
    assert lock.enabled is False
    assert lock.try_acquire() is True
    assert lock.held is True
    lock.release()
    assert le.LeaderElector(le.SCHEDULER_LOCK, "sqlite://").is_leader() is True
    with le.startup_lock("sqlite://") as held:
        assert held is False


def test_election_can_be_switched_off_for_postgres(monkeypatch):
    monkeypatch.setattr(le, "_make_engine", lambda url: pytest.fail("must not connect"))
    monkeypatch.setenv("SCHEDULER_LEADER_ELECTION", "off")
    assert le.AdvisoryLock(le.SCHEDULER_LOCK, PG_URL).try_acquire() is True


def test_lock_keys_are_stable_and_distinct():
    assert le.lock_key("scheduler") == le.lock_key("scheduler")
    names = {le.SCHEDULER_LOCK, le.CI_ENGINE_LOCK, le.STARTUP_LOCK}
    keys = {le.lock_key(n) for n in names}
    assert len(keys) == len(names)
    assert all(0 < k <= 0x7FFFFFFF for k in keys)


# ---------------------------------------------------------------------------
# Postgres semantics
# ---------------------------------------------------------------------------


def test_only_one_process_leads(pg):
    a = le.LeaderElector(le.SCHEDULER_LOCK, PG_URL)
    b = le.LeaderElector(le.SCHEDULER_LOCK, PG_URL)
    assert a.is_leader() is True
    assert b.is_leader() is False
    # Asking again is idempotent: the leader keeps it, the standby still waits,
    # and neither opens a new connection per tick.
    connects = pg.connects
    for _ in range(3):
        assert a.is_leader() is True
        assert b.is_leader() is False
    assert pg.connects == connects


def test_standby_takes_over_when_the_leader_dies(pg):
    a = le.AdvisoryLock(le.SCHEDULER_LOCK, PG_URL)
    b = le.AdvisoryLock(le.SCHEDULER_LOCK, PG_URL)
    assert a.try_acquire() and not b.try_acquire()

    a._conn.kill()  # leader's pod vanished; Postgres drops its session

    assert b.try_acquire() is True
    # The old leader notices on its next tick and stands down instead of
    # carrying on as a second leader.
    assert a.try_acquire() is False
    assert a.held is False


def test_leader_that_lost_its_connection_reelects_itself_when_free(pg):
    a = le.AdvisoryLock(le.SCHEDULER_LOCK, PG_URL)
    assert a.try_acquire()
    a._conn.kill()
    # Nobody else wanted it: the same process wins again on a new session.
    assert a.try_acquire() is True
    assert a.held is True


def test_database_down_means_not_leader(pg, monkeypatch):
    def refuse(url):
        class _Down:
            def connect(self):
                raise RuntimeError("could not connect to server")

        return _Down()

    monkeypatch.setattr(le, "_make_engine", refuse)
    assert le.AdvisoryLock(le.SCHEDULER_LOCK, PG_URL).try_acquire() is False


def test_per_pass_lock_is_released_for_the_next_process(pg):
    a = le.AdvisoryLock(le.CI_ENGINE_LOCK, PG_URL)
    b = le.AdvisoryLock(le.CI_ENGINE_LOCK, PG_URL)
    assert a.try_acquire()
    assert not b.try_acquire()
    a.release()
    assert b.try_acquire()
    # Different names never block each other.
    assert le.AdvisoryLock(le.SCHEDULER_LOCK, PG_URL).try_acquire()


def test_a_forked_child_never_inherits_or_closes_the_parents_lock(pg, monkeypatch):
    parent = le.AdvisoryLock(le.SCHEDULER_LOCK, PG_URL)
    assert parent.try_acquire()
    parent_conn = parent._conn

    # Same object, seen from a child after fork (gunicorn --preload).
    child_pid = parent._pid + 1
    monkeypatch.setattr(le.os, "getpid", lambda: child_pid)
    assert parent.held is False
    assert parent.try_acquire() is False  # the parent's session still holds it
    assert parent_conn.closed is False  # and the child did not close it
    assert pg.locks  # parent's lock survives


def test_startup_lock_serialises_boot(pg):
    with le.startup_lock(PG_URL) as held:
        assert held is True
        # A second process booting now cannot get in.
        with le.startup_lock(PG_URL) as second:
            assert second is False
    with le.startup_lock(PG_URL) as held:
        assert held is True  # released after the first boot finished
    assert not pg.locks


# ---------------------------------------------------------------------------
# Loops wired to the election
# ---------------------------------------------------------------------------


class _StopLoop(Exception):
    pass


def _run_loop_ticks(monkeypatch, module, ticks):
    calls = {"n": 0}

    def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] > ticks:
            raise _StopLoop

    monkeypatch.setattr(module.time, "sleep", fake_sleep)


def test_scheduler_ticks_only_on_the_leader(app, pg, monkeypatch):
    from api.services import alert_policy_scheduler as sched

    ran = []
    monkeypatch.setattr(sched, "run_scheduler_tick", lambda a: ran.append(a))
    monkeypatch.setattr(le, "database_url_for", lambda a: PG_URL)

    # Another replica already leads.
    other = le.AdvisoryLock(le.SCHEDULER_LOCK, PG_URL)
    assert other.try_acquire()

    _run_loop_ticks(monkeypatch, sched, 3)
    with pytest.raises(_StopLoop):
        sched._scheduler_loop(app)
    assert ran == []

    # The leader dies; this process takes over on its next tick.
    other._conn.kill()
    _run_loop_ticks(monkeypatch, sched, 2)
    with pytest.raises(_StopLoop):
        sched._scheduler_loop(app)
    assert len(ran) == 2


def test_scheduler_ticks_on_sqlite_without_election(app, monkeypatch):
    from api.services import alert_policy_scheduler as sched

    ran = []
    monkeypatch.setattr(sched, "run_scheduler_tick", lambda a: ran.append(a))
    _run_loop_ticks(monkeypatch, sched, 2)
    with pytest.raises(_StopLoop):
        sched._scheduler_loop(app)
    assert len(ran) == 2


def test_ci_pass_is_skipped_while_another_process_is_in_one(app, pg, monkeypatch):
    from api.services.ci import engine, ticker

    monkeypatch.setattr(ticker, "_pass_lock", None)
    monkeypatch.setattr(le, "database_url_for", lambda a: PG_URL)
    passes = []
    monkeypatch.setattr(engine, "advance_ci_builds", lambda: passes.append(1) or True)

    other = le.AdvisoryLock(le.CI_ENGINE_LOCK, PG_URL)
    assert other.try_acquire()
    assert ticker.guarded_pass(app) is None
    assert passes == []

    other.release()
    assert ticker.guarded_pass(app) is True
    assert passes == [1]
    # Released after the pass so the next woken process can run one.
    assert other.try_acquire() is True
    monkeypatch.setattr(ticker, "_pass_lock", None)


def test_a_request_thread_never_releases_the_tickers_pass(app, pg, monkeypatch):
    """Re-asking for an advisory lock this process holds says yes, so without
    the in-process mutex a callback's release would end the ticker's pass."""
    from api.services.ci import engine, ticker

    monkeypatch.setattr(ticker, "_pass_lock", None)
    monkeypatch.setattr(le, "database_url_for", lambda a: PG_URL)
    inside = threading.Event()
    finish = threading.Event()
    advanced = []

    def slow_pass():
        inside.set()
        finish.wait(5)
        return True

    monkeypatch.setattr(engine, "advance_ci_builds", slow_pass)
    monkeypatch.setattr(engine, "advance_build_now", lambda build_id: advanced.append(build_id))

    def run_ticker_pass():
        with app.app_context():
            ticker.guarded_pass(app)

    t = threading.Thread(target=run_ticker_pass)
    t.start()
    assert inside.wait(5)
    ticker.guarded_advance_build_now(7)  # the agent callback, mid-pass
    assert advanced == []
    other_process = le.AdvisoryLock(le.CI_ENGINE_LOCK, PG_URL)
    assert other_process.try_acquire() is False  # still held for the pass
    finish.set()
    t.join(5)

    ticker.guarded_advance_build_now(7)
    assert advanced == [7]
    monkeypatch.setattr(ticker, "_pass_lock", None)


def test_ci_pass_runs_directly_on_sqlite(app, monkeypatch):
    from api.services.ci import engine, ticker

    monkeypatch.setattr(ticker, "_pass_lock", None)
    monkeypatch.setattr(engine, "advance_ci_builds", lambda: False)
    assert ticker.guarded_pass(app) is False
    monkeypatch.setattr(ticker, "_pass_lock", None)


# ---------------------------------------------------------------------------
# Cluster Builder: a live build must not look orphaned to another process.
# ---------------------------------------------------------------------------


def test_cluster_build_heartbeat_keeps_a_long_phase_alive(app, monkeypatch):
    from api.db import db
    from api.models import ClusterBuild
    from api.services.cluster_build import executor

    build = ClusterBuild(name="hb", status="building")
    db.session.add(build)
    db.session.commit()
    stale = executor._utcnow() - timedelta(minutes=executor._STALE_BUILD_MINUTES + 5)
    ClusterBuild.query.filter_by(id=build.id).update(
        {"updated_at": stale}, synchronize_session=False
    )
    db.session.commit()

    class _OneBeat:
        def __init__(self):
            self.waits = 0

        def wait(self, _seconds):
            self.waits += 1
            return self.waits > 1  # beat once, then stop

    executor._heartbeat_loop(app, build.id, _OneBeat())

    db.session.expire_all()
    fresh = db.session.get(ClusterBuild, build.id).updated_at
    if fresh.tzinfo is None:
        from datetime import timezone

        fresh = fresh.replace(tzinfo=timezone.utc)
    assert fresh > executor._utcnow() - timedelta(minutes=1)


def test_cluster_build_heartbeat_is_not_started_under_testing(app):
    from api.services.cluster_build import executor

    assert executor._start_heartbeat(app, 1) is None


# ---------------------------------------------------------------------------
# Real Postgres, when the suite runs against one.
# ---------------------------------------------------------------------------


_REAL_PG = os.getenv("TEST_DATABASE_URL", "")


@pytest.mark.skipif(
    not _REAL_PG.startswith("postgresql"), reason="needs TEST_DATABASE_URL=postgresql://..."
)
def test_real_postgres_election_and_takeover():
    a = le.AdvisoryLock("test-election", _REAL_PG)
    b = le.AdvisoryLock("test-election", _REAL_PG)
    try:
        assert a.try_acquire() is True
        assert a.try_acquire() is True  # re-verified via pg_locks
        assert b.try_acquire() is False
        a.close()  # the leader's session ends
        assert b.try_acquire() is True
        results = []
        t = threading.Thread(target=lambda: results.append(a.try_acquire()))
        t.start()
        t.join()
        assert results == [False]
    finally:
        a.close()
        b.close()
