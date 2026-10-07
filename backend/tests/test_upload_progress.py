"""Upload progress: an artifact or store upload says how far it is.

A slow link used to show as a stage stuck on "uploading" for twenty minutes,
with nothing to tell a slow network from a slow store. These cover the three
places a binary travels: the agent sending it to KubeSight, and KubeSight
sending it to Google Play or App Store Connect.
"""

import importlib.util
import io
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.services import app_store_client, google_play_client, mobile_app_service

AGENT_PATH = Path(__file__).resolve().parents[2] / "agent" / "kubesight-agent.py"


@pytest.fixture
def agent():
    spec = importlib.util.spec_from_file_location("kubesight_agent_progress", AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _upload_detail(pub):
    return next(step["detail"] for step in pub.steps if step["key"] == "upload")


# --- Google Play -------------------------------------------------------------

def test_the_play_upload_reports_every_block_it_sends():
    seen = []
    reader = google_play_client._ProgressReader(io.BytesIO(b"x" * 10), 10, lambda s, t: seen.append((s, t)))
    while reader.read(4):
        pass
    assert seen[:3] == [(4, 10), (8, 10), (10, 10)]


def test_a_broken_progress_callback_never_breaks_the_upload():
    def boom(sent, total):
        raise RuntimeError("database is gone")

    reader = google_play_client._ProgressReader(io.BytesIO(b"abc"), 3, boom)
    assert reader.read(10) == b"abc"


# --- The publish step --------------------------------------------------------

def test_progress_is_written_to_the_publish_step_at_most_every_ten_seconds(app, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(mobile_app_service.time, "monotonic", clock)
    pub = SimpleNamespace(id=1, steps=None)
    mb = 1024 * 1024

    with app.app_context():
        report = mobile_app_service._upload_progress(pub, "app-release.aab", "Google Play")
        clock.now += 3
        report(5 * mb, 80 * mb)
        assert pub.steps is None  # too soon to say anything

        clock.now += 10
        report(20 * mb, 80 * mb)
        assert _upload_detail(pub) == "uploading app-release.aab: 20.0 of 80.0 MB (1.5 MB/s)"


def test_the_last_byte_says_who_the_upload_is_now_waiting_on(app, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(mobile_app_service.time, "monotonic", clock)
    pub = SimpleNamespace(id=1, steps=None)
    kb = 1024

    with app.app_context():
        report = mobile_app_service._upload_progress(pub, "app-release.aab", "Google Play")
        clock.now += 2  # sooner than the throttle: the final word is never held back
        report(100 * kb, 100 * kb)
        assert _upload_detail(pub) == (
            "sent all 0.1 MB of app-release.aab in 2s (50 KB/s); waiting for Google Play to accept it"
        )
        pub.steps = None
        report(100 * kb, 100 * kb)
        assert pub.steps is None  # said once


def test_the_app_store_upload_reports_after_each_part(monkeypatch, tmp_path):
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"x" * 30)
    calls = iter([{"data": {"id": "u1"}}, {"data": {"id": "f1", "attributes": {"uploadOperations": [
        {"url": "https://apple/1", "offset": 0, "length": 20},
        {"url": "https://apple/2", "offset": 20, "length": 10},
    ]}}}, {}])
    monkeypatch.setattr(app_store_client, "_request", lambda *a, **k: next(calls))
    monkeypatch.setattr(app_store_client, "_put_chunk", lambda op, path: None)
    monkeypatch.setattr(app_store_client, "ipa_versions", lambda path: ("1.0", "7"))
    cfg = app_store_client.AscConfig(issuer_id="i", key_id="k", private_key="p", bundle_id="b", app_id="9")

    seen = []
    app_store_client.upload_build(cfg, str(ipa), "app.ipa", progress=lambda s, t: seen.append((s, t)))
    assert seen == [(20, 30), (30, 30)]


# --- The agent ---------------------------------------------------------------

class _Capture(BaseHTTPRequestHandler):
    received = {}

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        _Capture.received = {"length": length, "body": self.rfile.read(length),
                             "type": self.headers["Content-Type"]}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def test_the_agent_streams_an_artifact_as_the_same_multipart_body(agent, tmp_path):
    artifact = tmp_path / "app-release.apk"
    artifact.write_bytes(b"\x00\x01" * 50000)
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = agent.Client("http://127.0.0.1:%d" % server.server_port, "tok")
        seen = []
        client.post_file("/tasks/1/artifacts", {"claimToken": "c", "name": "app-release.apk"},
                         str(artifact), progress=lambda s, t: seen.append((s, t)))
    finally:
        server.shutdown()

    body = _Capture.received["body"]
    boundary = _Capture.received["type"].split("boundary=")[1]
    assert _Capture.received["length"] == len(body)
    assert body.startswith(("--%s\r\n" % boundary).encode())
    assert artifact.read_bytes() in body
    assert body.endswith(("\r\n--%s--\r\n" % boundary).encode())
    # Progress counts the file's own bytes, ending at its size.
    assert seen[-1] == (100000, 100000)


def test_the_agent_logs_a_slow_upload_and_then_who_it_waits_on(agent, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(agent.time, "monotonic", clock)
    lines = []
    shipper = SimpleNamespace(add=lambda line, stream="stdout": lines.append(line))
    mb = 1024 * 1024

    report = agent._upload_progress(shipper, "app-release.aab", clock.now)
    clock.now += 4
    report(1 * mb, 60 * mb)
    clock.now += 8
    report(6 * mb, 60 * mb)
    clock.now += 48
    report(60 * mb, 60 * mb)

    assert lines == [
        "[agent] uploading app-release.aab to KubeSight: 6.0 of 60.0 MB (512 KB/s)",
        "[agent] sent all 60.0 MB of app-release.aab in 60s (1.0 MB/s); waiting for KubeSight to store it",
    ]


def test_a_fast_agent_upload_adds_no_progress_noise(agent, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(agent.time, "monotonic", clock)
    lines = []
    shipper = SimpleNamespace(add=lambda line, stream="stdout": lines.append(line))

    report = agent._upload_progress(shipper, "small.apk", clock.now)
    clock.now += 1
    report(500, 500)
    assert lines == []
