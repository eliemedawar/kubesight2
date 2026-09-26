"""Cross-process leader election for the loops that run inside the backend.

KubeSight drives alerts, deploy automation, change bundles, ticketing syncs,
the ticket agent, mobile releases and Cluster Builder recovery from ONE
in-process scheduler tick, and native CI from its own one-second clock. Those
loops assumed exactly one backend process. Running two gunicorn workers, or two
replicas, would run every tick twice: two alert evaluations firing the same
notification, two deploy-automation passes advancing the same run, and so on.

This module makes that safe with PostgreSQL session-level advisory locks.

Why an advisory lock and not a lease row with a TTL heartbeat:

* The lock lives exactly as long as the database session that took it. A
  process that crashes, is OOM-killed or loses its pod closes its socket and
  Postgres drops the lock at once; a lease row has to wait out its TTL.
* A leader that stalls (a slow tick, a long GC, a blocked kubectl) keeps the
  lock, because its session is still there. A lease that expires under a
  stalled leader hands the work to a second process while the first is still
  mid-tick — the split brain the lease was meant to prevent.
* No table, no migration, no clock-skew arithmetic between replicas.

The cost is one dedicated connection per lock per process, opened outside the
SQLAlchemy pool (``NullPool``) so it never competes with request traffic, in
AUTOCOMMIT so it is never "idle in transaction", and with TCP keepalives on
both ends so a dead peer is noticed in about a minute instead of the kernel's
two hours. Every check verifies the lock is still granted to *this* session
(``pg_locks``), so a dropped connection — or a transaction-mode pgbouncer,
which cannot hold session locks at all — shows up as "not leader" instead of
two leaders. DATABASE_URL must therefore reach Postgres directly or through a
session-mode pooler.

Fork safety (gunicorn ``--preload``): the connection is opened lazily, on the
loop's own thread, never in ``create_app``. A lock object that finds itself in
a different PID than the one that opened its connection forgets that
connection without touching it — the socket belongs to the parent.

SQLite (dev, tests) and ``SCHEDULER_LEADER_ELECTION=off``: there is one
process by definition, so every lock is granted immediately and nothing
connects anywhere.
"""

from __future__ import annotations

import logging
import os
import threading
import zlib
from contextlib import contextmanager
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# First half of the two-int advisory-lock key ("KS"). Keeps KubeSight's locks
# apart from anything else sharing the database that also uses advisory locks.
LOCK_NAMESPACE = 0x4B53

# The shared scheduler tick (alerts, deploy automation, bundles, ticketing,
# ticket agent, mobile, CI artifact purge, Cluster Builder recovery). Held for
# as long as the process leads.
SCHEDULER_LOCK = "scheduler"
# One CI engine pass. Taken and released around each pass, so whichever process
# was woken by a trigger runs the next pass without waiting for a leader.
CI_ENGINE_LOCK = "ci-engine-pass"
# Schema migrations + seeding at boot. Blocking; serialises workers/replicas.
STARTUP_LOCK = "startup"

_DEFAULT_STARTUP_LOCK_TIMEOUT_SECONDS = 600


def lock_key(name: str) -> int:
    """Stable, positive int4 for a lock name (same in every process)."""
    return zlib.crc32(name.encode("utf-8")) & 0x7FFFFFFF


def election_enabled() -> bool:
    return os.getenv("SCHEDULER_LEADER_ELECTION", "auto").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
        "disabled",
    }


def uses_advisory_locks(database_url: Optional[str]) -> bool:
    """Whether locks are real. Only PostgreSQL has advisory locks; anything
    else is treated as the single-process development setup it is."""
    if not election_enabled():
        return False
    return str(database_url or "").startswith(("postgresql", "postgres://"))


def _make_engine(database_url: str):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    url = make_url(database_url)
    connect_args = {}
    if url.get_driver_name() == "psycopg2":
        connect_args = {
            "application_name": "kubesight-leader",
            "connect_timeout": 10,
            # Client side: notice a dead server or a black-holed route.
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        }
    return create_engine(
        url,
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
        connect_args=connect_args,
    )


def _harden_session(conn) -> None:
    """Server side: drop a vanished client's session (and so its locks) in
    about a minute. Best effort — managed Postgres may refuse, and Unix
    sockets ignore it."""
    from sqlalchemy import text

    for statement in (
        "SET tcp_keepalives_idle = 30",
        "SET tcp_keepalives_interval = 10",
        "SET tcp_keepalives_count = 3",
    ):
        try:
            conn.execute(text(statement))
        except Exception:  # noqa: BLE001
            logger.debug("Could not apply %s on the leader connection", statement)


class AdvisoryLock:
    """A named, non-blocking, session-level Postgres advisory lock.

    ``try_acquire`` is idempotent while held (it re-verifies instead of
    re-locking) and is the only thing callers need: call it every tick.
    Thread-safe; one instance per process per lock name.
    """

    def __init__(self, name: str, database_url: Optional[str]):
        self.name = name
        self.key = lock_key(name)
        self._url = database_url or ""
        self._engine = None
        self._conn = None
        self._pid: Optional[int] = None
        self._held = False
        self._mutex = threading.Lock()

    @property
    def enabled(self) -> bool:
        return uses_advisory_locks(self._url)

    @property
    def held(self) -> bool:
        if not self.enabled:
            return True
        return self._held and self._pid == os.getpid()

    def try_acquire(self) -> bool:
        if not self.enabled:
            return True
        with self._mutex:
            self._forget_if_forked()
            if self._held:
                if self._still_granted():
                    return True
                logger.warning(
                    "Lost advisory lock %r (connection dropped); re-electing", self.name
                )
                self._drop()
            try:
                from sqlalchemy import text

                conn = self._connection()
                got = bool(
                    conn.execute(
                        text("SELECT pg_try_advisory_lock(:ns, :key)"),
                        {"ns": LOCK_NAMESPACE, "key": self.key},
                    ).scalar()
                )
            except Exception:  # noqa: BLE001 — "not leader" is the safe answer
                logger.warning(
                    "Advisory lock %r check failed; not leading this round",
                    self.name,
                    exc_info=True,
                )
                self._drop()
                return False
            self._held = got
            return got

    def release(self) -> None:
        if not self.enabled:
            return
        with self._mutex:
            self._forget_if_forked()
            if not self._held:
                return
            try:
                from sqlalchemy import text

                self._conn.execute(
                    text("SELECT pg_advisory_unlock(:ns, :key)"),
                    {"ns": LOCK_NAMESPACE, "key": self.key},
                )
                self._held = False
            except Exception:  # noqa: BLE001 — closing the session releases it too
                self._drop()

    def close(self) -> None:
        with self._mutex:
            self._drop()

    # -- internals ---------------------------------------------------------

    def _connection(self):
        if self._conn is None:
            if self._engine is None:
                self._engine = _make_engine(self._url)
            conn = self._engine.connect()
            _harden_session(conn)
            self._conn = conn
            self._pid = os.getpid()
        return self._conn

    def _still_granted(self) -> bool:
        from sqlalchemy import text

        try:
            return bool(
                self._conn.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks"
                        " WHERE locktype = 'advisory' AND granted"
                        " AND pid = pg_backend_pid()"
                        " AND classid::bigint = :ns AND objid::bigint = :key"
                        " AND objsubid = 2)"
                    ),
                    {"ns": LOCK_NAMESPACE, "key": self.key},
                ).scalar()
            )
        except Exception:  # noqa: BLE001
            return False

    def _forget_if_forked(self) -> None:
        if self._pid is not None and self._pid != os.getpid():
            # Inherited across fork: the socket is the parent's. Never close it
            # from here — that would release the PARENT's lock.
            self._conn = None
            self._engine = None
            self._pid = None
            self._held = False

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        self._held = False
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


class LeaderElector:
    """``is_leader()`` once per tick; logs leadership changes."""

    def __init__(self, name: str, database_url: Optional[str]):
        self.lock = AdvisoryLock(name, database_url)
        self._was_leader: Optional[bool] = None

    def is_leader(self) -> bool:
        leading = self.lock.try_acquire()
        if leading != self._was_leader:
            if self.lock.enabled:
                if leading:
                    logger.info(
                        "Process %s is now the %r leader", os.getpid(), self.lock.name
                    )
                else:
                    logger.info(
                        "Process %s is standing by for %r (another process leads)",
                        os.getpid(),
                        self.lock.name,
                    )
            self._was_leader = leading
        return leading


def database_url_for(app) -> str:
    return str(app.config.get("SQLALCHEMY_DATABASE_URI") or "")


@contextmanager
def startup_lock(database_url: Optional[str]) -> Iterator[bool]:
    """Serialise boot-time migrations and seeding across workers and replicas.

    ``db.create_all()`` and the ALTER TABLE migrations race each other when two
    processes boot at once (duplicate-type and duplicate-column errors on
    Postgres). Blocks up to ``STARTUP_LOCK_TIMEOUT_SECONDS``; if the lock can't
    be had, logs and proceeds — which is exactly the behaviour before this
    lock existed, not a new failure mode. Yields whether the lock is held.
    """
    if not uses_advisory_locks(database_url):
        yield False
        return
    from sqlalchemy import text

    try:
        timeout = max(
            1,
            int(
                os.getenv(
                    "STARTUP_LOCK_TIMEOUT_SECONDS",
                    str(_DEFAULT_STARTUP_LOCK_TIMEOUT_SECONDS),
                )
            ),
        )
    except ValueError:
        timeout = _DEFAULT_STARTUP_LOCK_TIMEOUT_SECONDS
    engine = None
    conn = None
    held = False
    try:
        engine = _make_engine(str(database_url))
        conn = engine.connect()
        # SET takes no bind parameters; the value is an int we produced.
        conn.execute(text(f"SET lock_timeout = {int(timeout) * 1000}"))
        conn.execute(
            text("SELECT pg_advisory_lock(:ns, :key)"),
            {"ns": LOCK_NAMESPACE, "key": lock_key(STARTUP_LOCK)},
        )
        held = True
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not take the startup migration lock; migrating without it",
            exc_info=True,
        )
    try:
        yield held
    finally:
        if conn is not None:
            try:
                if held:
                    conn.execute(
                        text("SELECT pg_advisory_unlock(:ns, :key)"),
                        {"ns": LOCK_NAMESPACE, "key": lock_key(STARTUP_LOCK)},
                    )
            except Exception:  # noqa: BLE001
                pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if engine is not None:
            engine.dispose()
