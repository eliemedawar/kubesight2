"""The connect step against a real SSH server, not a fake transport.

A paramiko SSH server runs on localhost (password login, records each command
and what it reads on stdin). KubeSight's real ParamikoTransport logs in to it
from the SSH waiter's pool thread — exactly the path that failed in production
when the host-key check had no app context — through the same `sudo -n sh -s`
the check uses.
"""

from __future__ import annotations

import socket
import threading

import pytest

paramiko = pytest.importorskip("paramiko")

from api.models import SshHostKey  # noqa: E402
from api.services.cluster_build.provisioning import jobs  # noqa: E402
from api.services.ssh import SshTarget, set_transport_factory  # noqa: E402


class _Server(paramiko.ServerInterface):
    def __init__(self, seen):
        self.seen = seen

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        ok = (username, password) == ("kube", "s3cret-pw")
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        def answer():
            stdin = b""
            while True:
                data = channel.recv(1024)
                if not data:
                    break
                stdin += data
            self.seen.append((command.decode(), stdin.decode()))
            # Plays `sudo -n sh -s`: succeeds only for the check's script.
            channel.send_exit_status(0 if stdin.decode().strip() == "true" else 1)
            channel.close()

        threading.Thread(target=answer, daemon=True).start()
        return True


@pytest.fixture()
def ssh_server():
    host_key = paramiko.RSAKey.generate(2048)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    seen, transports = [], []
    stop = threading.Event()

    def serve():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            transport = paramiko.Transport(sock)
            transport.add_server_key(host_key)
            transport.start_server(server=_Server(seen))
            transports.append(transport)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield {"port": listener.getsockname()[1], "seen": seen}
    stop.set()
    listener.close()
    for transport in transports:
        transport.close()


def test_the_ssh_wait_logs_in_to_a_real_server(app, ssh_server):
    set_transport_factory(None)  # the real paramiko transport
    target = SshTarget(
        host="127.0.0.1", port=ssh_server["port"], username="kube", auth_method="password",
        password="s3cret-pw", sudo_mode="nopasswd", host_key_policy="tofu",
        connect_timeout_s=10, retry_count=0, label="vm-1 (127.0.0.1)",
    )
    results, attempts = {}, []
    jobs._default_ssh_waiter(
        [("vm-1", target)], 60,
        lambda name, ok, detail: results.__setitem__(name, (ok, detail)),
        on_attempt=lambda name, detail: attempts.append((name, detail)),
        app=app,
    )
    assert results == {"vm-1": (True, "")}, attempts
    assert attempts == []  # the first login worked
    # The exact command KubeSight sends, with the check on stdin.
    assert ssh_server["seen"] == [("sudo -n sh -s", "true")]
    # Trust on first use recorded the VM's key — from the pool thread.
    with app.app_context():
        assert SshHostKey.query.filter_by(host="127.0.0.1", port=ssh_server["port"]).count() == 1


def test_a_wrong_password_is_reported_with_its_reason(app, ssh_server, monkeypatch):
    set_transport_factory(None)
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    target = SshTarget(
        host="127.0.0.1", port=ssh_server["port"], username="kube", auth_method="password",
        password="wrong", host_key_policy="tofu", connect_timeout_s=10, retry_count=0,
    )
    results, attempts = {}, []
    stop = threading.Event()

    def on_attempt(name, detail):
        attempts.append(detail)
        stop.set()  # one failed attempt is enough to see the reason

    jobs._default_ssh_waiter(
        [("vm-1", target)], 60,
        lambda name, ok, detail: results.__setitem__(name, (ok, detail)),
        on_attempt=on_attempt, app=app, stop=stop,
    )
    assert results["vm-1"][0] is False
    assert attempts and "auth" in attempts[0].lower()
