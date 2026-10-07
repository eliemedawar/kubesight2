"""Serve KubeSight the way production does, with per-request measurements.

Production runs ``gunicorn -w 1 --threads 8`` (k8s_entrypoint.sh): one process,
eight request threads, connections queued behind them. gunicorn does not run
on Windows, so this harness reproduces that shape with werkzeug's server and a
fixed pool of request threads. Every request is recorded to a JSONL file:

    {"t": 1700000000.1, "m": "GET", "rule": "/api/clusters/<cluster_id>/overview",
     "path": "...", "status": 200, "ms": 123.4, "queue_ms": 5.0,
     "sql": 12, "sql_ms": 8.1, "kubectl": 2, "kubectl_ms": 300.2, "bytes": 5120}

``queue_ms`` is the time a connection waited for a free request thread — the
number that explodes first when the server is saturated. A sampler thread
appends process vitals (RSS, threads, CPU seconds, DB pool, kubectl
subprocesses in flight) to a second JSONL file every few seconds.

Usage (from backend/):

    python tools/stress/serve.py --port 5091 --threads 8 --out <dir>

Configuration comes from the environment exactly as for ``python app.py``
(DATABASE_URL, KUBESIGHT_KUBECONFIG_DIR, ...).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Dict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..")))

_local = threading.local()
_kubectl_inflight = 0
_kubectl_total = 0
_kubectl_lock = threading.Lock()


def _req() -> Dict[str, Any] | None:
    return getattr(_local, "req", None)


# -- kubectl accounting: wrap subprocess so every kubectl spawn is counted ----

_orig_popen_init = subprocess.Popen.__init__


def _is_kubectl(args: Any) -> bool:
    first = args[0] if isinstance(args, (list, tuple)) and args else args
    return isinstance(first, str) and os.path.basename(first).lower().startswith("kubectl")


_kubectl_log = None  # set in main(): one JSON line per kubectl process


def _kubectl_summary(args: Any) -> str:
    """The kubectl verb and target without connection flags."""
    out = []
    skip = False
    for a in list(args)[1:]:
        if skip:
            skip = False
            continue
        a = str(a)
        if a in ("--kubeconfig", "--context", "-n", "--namespace"):
            skip = a in ("--kubeconfig", "--context")
            if not skip:
                out.append(a)
            continue
        if a.startswith("--kubeconfig=") or a.startswith("--context=") or a.startswith("--request-timeout"):
            continue
        out.append(a)
    return " ".join(out)[:160]


def _popen_init(self, args, *a, **kw):  # type: ignore[no-untyped-def]
    global _kubectl_inflight, _kubectl_total
    kubectl = _is_kubectl(args)
    if kubectl:
        with _kubectl_lock:
            _kubectl_inflight += 1
            _kubectl_total += 1
        self._stress_kubectl_start = time.perf_counter()
        self._stress_req = _req()
        self._stress_args = args
        self._stress_ctx = next((str(x) for i, x in enumerate(args) if i and str(args[i - 1]) == "--kubeconfig"), "")
    try:
        _orig_popen_init(self, args, *a, **kw)
    except Exception:
        if kubectl:
            with _kubectl_lock:
                _kubectl_inflight -= 1
        raise


_orig_wait = subprocess.Popen._wait  # type: ignore[attr-defined]


def _wait(self, *a, **kw):  # type: ignore[no-untyped-def]
    rc = _orig_wait(self, *a, **kw)
    start = getattr(self, "_stress_kubectl_start", None)
    if start is not None and rc is not None:
        global _kubectl_inflight
        self._stress_kubectl_start = None
        with _kubectl_lock:
            _kubectl_inflight -= 1
        elapsed = (time.perf_counter() - start) * 1000
        req = getattr(self, "_stress_req", None)
        if req is not None:
            req["kubectl"] += 1
            req["kubectl_ms"] += elapsed
        if _kubectl_log is not None:
            try:
                _kubectl_log.write(json.dumps({
                    "t": round(time.time(), 2), "ms": round(elapsed, 1), "rc": rc,
                    "thread": threading.current_thread().name, "on_request": req is not None,
                    "args": _kubectl_summary(getattr(self, "_stress_args", [])),
                    "kcfg": os.path.basename(getattr(self, "_stress_ctx", "")),
                }) + "\n")
            except Exception:
                pass
    return rc


subprocess.Popen.__init__ = _popen_init  # type: ignore[method-assign]
subprocess.Popen._wait = _wait  # type: ignore[attr-defined,method-assign]


# -- process vitals (Windows, no psutil) ---------------------------------------


class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def _rss_mb() -> float:
    if os.name != "nt":
        try:
            with open("/proc/self/statm") as fh:
                return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
        except OSError:
            return -1
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_ulong]
    k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    return pmc.PrivateUsage / 1e6


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5091)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--out", required=True)
    parser.add_argument("--sample-seconds", type=float, default=5.0)
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)
    global _kubectl_log
    _kubectl_log = open(os.path.join(args.out, "kubectl.jsonl"), "a", encoding="utf-8", buffering=1)

    from sqlalchemy import event
    from werkzeug.serving import BaseWSGIServer, WSGIRequestHandler

    import api as api_pkg
    from api import create_app
    from api.db import db

    # Production runs on Postgres (row locks, MVCC). SQLite locks the whole
    # file per writer and gives up after 5 s, which turns any background write
    # into "database is locked" 500s that Postgres would never produce. Wait
    # like Postgres would instead (the DB file itself is put in WAL mode by
    # the run scripts, so readers never wait for writers).
    original_options = api_pkg._default_engine_options

    def _engine_options(url: str) -> dict:
        options = original_options(url)
        if url.startswith("sqlite"):
            options["connect_args"] = {"timeout": 30}
        return options

    api_pkg._default_engine_options = _engine_options

    app = create_app()

    # SQL accounting, per request thread.
    with app.app_context():
        engine = db.engine

    # Never let accounting break a query: the start time rides on the
    # statement's own execution context, and every failure is swallowed.
    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        try:
            if context is not None:
                context._stress_t = time.perf_counter()
        except Exception:
            pass

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        try:
            start = getattr(context, "_stress_t", None)
            req = _req()
            if req is not None:
                req["sql"] += 1
                if start is not None:
                    req["sql_ms"] += (time.perf_counter() - start) * 1000
        except Exception:
            pass

    req_log = open(os.path.join(args.out, "requests.jsonl"), "a", encoding="utf-8", buffering=1)
    log_lock = threading.Lock()

    class Measured:
        def __init__(self, wsgi):
            self.wsgi = wsgi

        def __call__(self, environ, start_response):
            req = {"sql": 0, "sql_ms": 0.0, "kubectl": 0, "kubectl_ms": 0.0}
            _local.req = req
            started = time.perf_counter()
            status_holder: Dict[str, Any] = {}

            def _start(status, headers, exc_info=None):
                status_holder["status"] = int(status.split()[0])
                return start_response(status, headers, exc_info)

            size = 0
            try:
                for chunk in self.wsgi(environ, _start):
                    size += len(chunk)
                    yield chunk
            finally:
                _local.req = None
                path = environ.get("PATH_INFO", "")
                if path.startswith("/api/") or path == "/health":
                    rule = environ.get("stress.rule") or path
                    record = {
                        "t": round(time.time(), 3), "m": environ.get("REQUEST_METHOD"), "rule": rule, "path": path,
                        "status": status_holder.get("status", 0), "ms": round((time.perf_counter() - started) * 1000, 1),
                        "queue_ms": round(environ.get("stress.queue_ms", 0.0), 1),
                        "sql": req["sql"], "sql_ms": round(req["sql_ms"], 1),
                        "kubectl": req["kubectl"], "kubectl_ms": round(req["kubectl_ms"], 1), "bytes": size,
                        "user": environ.get("HTTP_X_STRESS_USER", ""), "page": environ.get("HTTP_X_STRESS_PAGE", ""),
                    }
                    with log_lock:
                        req_log.write(json.dumps(record) + "\n")

    @app.before_request
    def _rule():
        from flask import request

        if request.url_rule is not None:
            request.environ["stress.rule"] = request.url_rule.rule

    app.wsgi_app = Measured(app.wsgi_app)  # type: ignore[method-assign]

    class Handler(WSGIRequestHandler):
        # One request per connection: an idle keep-alive connection must not
        # pin one of the few request threads (gunicorn's gthread parks idle
        # keep-alive sockets in a selector instead).
        protocol_version = "HTTP/1.0"

        def log_request(self, *a, **kw):  # quiet access log
            pass

        def make_environ(self):
            environ = super().make_environ()
            environ["stress.queue_ms"] = getattr(self, "_stress_queue_ms", 0.0)
            return environ

    work: "queue.Queue" = queue.Queue()

    class PooledServer(BaseWSGIServer):
        multithread = True
        # On Windows SO_REUSEADDR lets a second server bind a port that is
        # still in use; fail loudly instead of splitting traffic between two.
        allow_reuse_address = os.name != "nt"

        def process_request(self, request, client_address):
            work.put((request, client_address, time.perf_counter()))

    server = PooledServer("127.0.0.1", args.port, app, handler=Handler)
    server.socket.listen(1024)

    def worker():
        while True:
            request, client_address, queued = work.get()
            wait_ms = (time.perf_counter() - queued) * 1000
            try:
                handler = Handler.__new__(Handler)
                handler._stress_queue_ms = wait_ms  # type: ignore[attr-defined]
                Handler.__init__(handler, request, client_address, server)
            except Exception:
                pass
            finally:
                try:
                    server.shutdown_request(request)
                except Exception:
                    pass

    for i in range(args.threads):
        threading.Thread(target=worker, daemon=True, name=f"req-{i}").start()

    vit_log = open(os.path.join(args.out, "vitals.jsonl"), "a", encoding="utf-8", buffering=1)

    def sampler():
        last_cpu = time.process_time()
        last_t = time.perf_counter()
        while True:
            time.sleep(args.sample_seconds)
            now_cpu = time.process_time()
            now_t = time.perf_counter()
            cpu_pct = (now_cpu - last_cpu) / (now_t - last_t) * 100
            last_cpu, last_t = now_cpu, now_t
            try:
                pool = engine.pool
                pool_status = pool.status()
                checked_out = pool.checkedout() if hasattr(pool, "checkedout") else None
            except Exception:
                pool_status, checked_out = "?", None
            try:
                from api.kubeconfig_vault import live_materializations

                kcfg = live_materializations()
            except Exception:
                kcfg = None
            try:
                from api.k8s_provider import _K8S_READ_CACHE

                cache_size = len(getattr(_K8S_READ_CACHE, "_entries", {}) or getattr(_K8S_READ_CACHE, "_data", {}) or {})
            except Exception:
                cache_size = None
            vit_log.write(json.dumps({
                "t": round(time.time(), 1), "rss_mb": round(_rss_mb(), 1), "threads": threading.active_count(),
                "cpu_pct": round(cpu_pct, 1), "queue": work.qsize(), "kubectl_inflight": _kubectl_inflight,
                "kubectl_total": _kubectl_total, "db_checked_out": checked_out, "db_pool": pool_status,
                "kubeconfig_materialized": kcfg, "k8s_cache_entries": cache_size,
            }) + "\n")

    threading.Thread(target=sampler, daemon=True, name="stress-sampler").start()

    if os.getenv("STRESS_TRACEMALLOC"):
        # Memory growth by allocation site: snapshot every few minutes and
        # report what grew since the first one (memory.txt).
        import tracemalloc

        tracemalloc.start(8)
        interval = float(os.getenv("STRESS_TRACEMALLOC_SECONDS", "300"))

        def memory_watch():
            time.sleep(120)
            first = tracemalloc.take_snapshot()
            while True:
                time.sleep(interval)
                snap = tracemalloc.take_snapshot()
                filters = [tracemalloc.Filter(False, tracemalloc.__file__)]
                growth = snap.filter_traces(filters).compare_to(first.filter_traces(filters), "traceback")
                current, peak = tracemalloc.get_traced_memory()
                with open(os.path.join(args.out, "memory.txt"), "w", encoding="utf-8") as fh:
                    fh.write(f"traced now {current / 1e6:.0f} MB, peak {peak / 1e6:.0f} MB, rss {_rss_mb():.0f} MB\n\n")
                    for stat in growth[:25]:
                        fh.write(f"{stat.size_diff / 1e6:+8.1f} MB  {stat.count_diff:+8d} blocks  (now {stat.size / 1e6:.1f} MB)\n")
                        for line in stat.traceback.format()[-8:]:
                            fh.write(f"        {line}\n")
                        fh.write("\n")

        threading.Thread(target=memory_watch, daemon=True, name="stress-memory").start()

    if os.getenv("STRESS_PROFILE"):
        # Poor man's sampling profiler: every few ms, note where each thread is.
        # Threads parked in waits/IO are counted separately, so the "busy"
        # table shows what actually holds the GIL.
        import collections
        import linecache
        import traceback

        idle_funcs = {"wait", "_wait", "select", "accept", "recv", "recv_into", "readinto", "read",
                      "sleep", "get", "_wait_for_tstate_lock", "communicate", "_communicate", "poll",
                      "_poll", "acquire", "worker", "_worker", "serve_forever", "_readerthread", "join"}
        busy = collections.Counter()
        busy_stacks = collections.Counter()
        me = threading.get_ident()

        def profiler():
            last_dump = time.time()
            while True:
                time.sleep(0.005)
                for tid, frame in sys._current_frames().items():
                    if tid == me or tid == threading.get_ident():
                        continue
                    if frame.f_code.co_name in idle_funcs:
                        continue
                    # A frame parked in a C-level wait shows the Python line
                    # that called it: treat sleep/wait/select calls as idle.
                    line = linecache.getline(frame.f_code.co_filename, frame.f_lineno)
                    if any(marker in line for marker in (".sleep(", ".wait(", "sleep(", ".get(timeout", "select(", ".acquire(")):
                        continue
                    stack = traceback.extract_stack(frame, limit=60)
                    leaf = stack[-1]
                    busy[f"{os.path.basename(leaf.filename)}:{leaf.name}"] += 1
                    ours = [f"{os.path.basename(s.filename)}:{s.name}" for s in stack if "/api/" in s.filename.replace("\\", "/")]
                    if ours:
                        busy_stacks[" <- ".join(reversed(ours[-4:]))] += 1
                if time.time() - last_dump > 30:
                    last_dump = time.time()
                    with open(os.path.join(args.out, "profile.txt"), "w", encoding="utf-8") as fh:
                        total = sum(busy.values()) or 1
                        fh.write(f"samples={total}\n\n== busy leaf functions\n")
                        for name, n in busy.most_common(40):
                            fh.write(f"{n * 100 / total:5.1f}%  {name}\n")
                        fh.write("\n== busy KubeSight call paths (innermost first)\n")
                        for name, n in busy_stacks.most_common(40):
                            fh.write(f"{n * 100 / total:5.1f}%  {name}\n")

        threading.Thread(target=profiler, daemon=True, name="stress-profiler").start()
    print(f"[serve] KubeSight on http://127.0.0.1:{args.port} with {args.threads} request threads", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
