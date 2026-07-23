"""Low-level SSH execution: direct or bastion-routed, key or password auth,
root / passwordless-sudo / sudo-with-password escalation.

Design rules (see CLUSTER-BUILDER-PLAN.md §8):
  * Sudo passwords are written to stdin only — never argv.
  * Scripts are shipped base64-encoded and decoded on the far side, so shell
    quoting can never corrupt them.
  * Host keys are verified through ``hostkeys`` (strict | tofu | pinned);
    an unverifiable key refuses the connection.
  * paramiko is imported lazily — the fake transport used in tests and mock
    mode keeps the dependency optional.
"""

from __future__ import annotations

import io
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


class SshConnectionError(RuntimeError):
    """Could not establish (or authenticate) the SSH session."""


class SshCommandError(RuntimeError):
    """The remote command ran but exited non-zero."""

    def __init__(self, message: str, exit_code: int, output: str):
        super().__init__(message)
        self.exit_code = exit_code
        self.output = output


@dataclass(frozen=True)
class SshTarget:
    """Everything needed to reach one host and escalate. Secrets arrive here
    already decrypted; this object must never be logged or serialized."""

    host: str
    port: int = 22
    username: str = "root"
    auth_method: str = "key"  # key | password
    private_key_pem: str = ""
    key_passphrase: str = ""
    password: str = ""
    sudo_mode: str = "nopasswd"  # root | nopasswd | password
    sudo_password: str = ""
    host_key_policy: str = "tofu"  # strict | tofu | pinned
    connect_timeout_s: int = 15
    command_timeout_s: int = 600
    retry_count: int = 2
    retry_backoff_s: int = 5
    # Optional bastion hop (auth fields mirror the primary target).
    bastion: Optional["SshTarget"] = None
    # Free-form label used in error messages ("cp-1 (10.0.0.11)").
    label: str = ""

    def describe(self) -> str:
        return self.label or f"{self.username}@{self.host}:{self.port}"


@dataclass
class SshResult:
    exit_code: int
    output: str  # merged stdout+stderr, in arrival order


@dataclass
class _CmdSpec:
    """A remote invocation: base command plus optional stdin payload."""

    command: str
    stdin_payload: Optional[str] = None


def _escalated_command(target: SshTarget, script: str) -> _CmdSpec:
    """Run ``script`` through stdin for the target's sudo mode.

    Scripts used by the builder can be large (notably a base64-encoded CNI
    manifest). Putting that data in an SSH ``exec`` request can exceed the
    channel packet limit and corrupt/close the connection. ``sh -s`` keeps the
    exec request tiny and streams the script over the channel instead. The sudo
    password, when needed, is the first stdin line and never appears in argv.
    """
    if target.sudo_mode == "root" or target.username == "root":
        return _CmdSpec(command="sh -s", stdin_payload=script)
    if target.sudo_mode == "password":
        return _CmdSpec(
            command="sudo -S -p '' sh -s",
            stdin_payload=(target.sudo_password or "") + "\n" + script,
        )
    # nopasswd (default): fail fast with a clear error instead of hanging on a
    # password prompt.
    return _CmdSpec(command="sudo -n sh -s", stdin_payload=script)


class ParamikoTransport:
    """Real SSH transport. One instance is safe to share across threads —
    connections are created per call, never cached (build phases run minutes
    apart and firewalls kill idle sessions anyway)."""

    def _paramiko(self):
        try:
            import paramiko  # noqa: PLC0415 — lazy on purpose
        except ImportError as exc:  # pragma: no cover
            raise SshConnectionError(
                "paramiko is not installed. Add it to the backend environment "
                "(pip install paramiko) to use the Cluster Builder."
            ) from exc
        return paramiko

    # -- connection ---------------------------------------------------------

    def _load_pkey(self, paramiko, target: SshTarget):
        if not target.private_key_pem:
            raise SshConnectionError(
                f"{target.describe()}: key auth selected but no private key configured."
            )
        last_error: Optional[Exception] = None
        for key_cls in (
            paramiko.Ed25519Key,
            paramiko.ECDSAKey,
            paramiko.RSAKey,
        ):
            try:
                return key_cls.from_private_key(
                    io.StringIO(target.private_key_pem),
                    password=target.key_passphrase or None,
                )
            except Exception as exc:  # noqa: BLE001 — try next key type
                last_error = exc
        raise SshConnectionError(
            f"{target.describe()}: could not parse the SSH private key "
            f"({last_error})."
        )

    def _host_key_policy(self, paramiko, target: SshTarget):
        from . import hostkeys  # local import: needs app context, not paramiko

        class _DbBackedPolicy(paramiko.client.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):  # noqa: N802
                ok, reason = hostkeys.verify_host_key(
                    host=target.host,
                    port=target.port,
                    key_type=key.get_name(),
                    fingerprint_sha256=hostkeys.fingerprint_sha256(key.asbytes()),
                    policy=target.host_key_policy,
                )
                if not ok:
                    raise SshConnectionError(
                        f"{target.describe()}: host key rejected — {reason}"
                    )

        return _DbBackedPolicy()

    def _connect_one(self, paramiko, target: SshTarget, sock=None):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(self._host_key_policy(paramiko, target))
        kwargs = {
            "hostname": target.host,
            "port": target.port,
            "username": target.username,
            "timeout": target.connect_timeout_s,
            "banner_timeout": target.connect_timeout_s,
            "auth_timeout": target.connect_timeout_s,
            "allow_agent": False,
            "look_for_keys": False,
            "sock": sock,
        }
        if target.auth_method == "password":
            kwargs["password"] = target.password
        else:
            kwargs["pkey"] = self._load_pkey(paramiko, target)
        try:
            client.connect(**kwargs)
        except SshConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalize paramiko/socket errors
            client.close()
            raise SshConnectionError(f"{target.describe()}: {exc}") from exc
        # Keepalive so a long, silent command (e.g. `kubectl rollout status`
        # waiting minutes for a DaemonSet) doesn't get its idle TCP connection
        # torn down by a NAT/firewall — the top cause of mid-phase 10054 drops.
        try:
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(15)
        except Exception:  # noqa: BLE001 — keepalive is best-effort
            pass
        return client

    def _connect(self, target: SshTarget):
        """Returns (client, bastion_client_or_None). Caller closes both."""
        paramiko = self._paramiko()
        if target.bastion is None:
            return self._connect_one(paramiko, target), None
        bastion_client = self._connect_one(paramiko, target.bastion)
        try:
            channel = bastion_client.get_transport().open_channel(
                "direct-tcpip",
                (target.host, target.port),
                ("", 0),
                timeout=target.connect_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            bastion_client.close()
            raise SshConnectionError(
                f"{target.describe()}: bastion tunnel via "
                f"{target.bastion.describe()} failed — {exc}"
            ) from exc
        try:
            client = self._connect_one(paramiko, target, sock=channel)
        except Exception:
            bastion_client.close()
            raise
        return client, bastion_client

    # -- execution ----------------------------------------------------------

    def run(
        self,
        target: SshTarget,
        script: str,
        *,
        timeout_s: Optional[int] = None,
        on_output: Optional[Callable[[str], None]] = None,
        escalate: bool = True,
    ) -> SshResult:
        """Run ``script`` on the target, retrying connection failures per the
        target's retry policy. Raises SshCommandError on non-zero exit."""
        attempts = max(1, target.retry_count + 1)
        last_exc: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                return self._run_once(
                    target, script, timeout_s=timeout_s, on_output=on_output,
                    escalate=escalate,
                )
            except SshConnectionError as exc:
                last_exc = exc
                if attempt < attempts:
                    time.sleep(max(1, target.retry_backoff_s))
        raise last_exc  # type: ignore[misc]

    def _run_once(
        self,
        target: SshTarget,
        script: str,
        *,
        timeout_s: Optional[int],
        on_output: Optional[Callable[[str], None]],
        escalate: bool,
    ) -> SshResult:
        spec = (
            _escalated_command(target, script)
            if escalate
            else _CmdSpec(command=script)
        )
        effective_timeout = timeout_s or target.command_timeout_s
        paramiko = self._paramiko()
        client, bastion_client = self._connect(target)
        try:
            # Any socket/SSH error from ANY channel op below — open_session,
            # exec_command, recv, or recv_exit_status — means the connection
            # dropped mid-command (e.g. WinError 10054 when NAT reaps a long,
            # silent channel, or CNI bring-up resets networking). Convert it to
            # SshConnectionError so run()'s retry loop reconnects and re-runs the
            # (idempotent) command. SshCommandError (timeout / non-zero exit) is
            # intentional and re-raised untouched.
            try:
                channel = client.get_transport().open_session()
                channel.set_combine_stderr(True)
                channel.settimeout(10)
                channel.exec_command(spec.command)
                if spec.stdin_payload is not None:
                    channel.sendall(spec.stdin_payload.encode("utf-8"))
                    # ``sh -s`` reads until EOF. Half-close only the write side
                    # so stdout/stderr remain available to the receive loop.
                    channel.shutdown_write()
                chunks = []
                deadline = time.monotonic() + effective_timeout
                while True:
                    if time.monotonic() > deadline:
                        channel.close()
                        raise SshCommandError(
                            f"{target.describe()}: command timed out after "
                            f"{effective_timeout}s.",
                            exit_code=-1,
                            output="".join(chunks),
                        )
                    try:
                        data = channel.recv(32768)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    text = data.decode("utf-8", errors="replace")
                    chunks.append(text)
                    if on_output:
                        on_output(text)
                exit_code = channel.recv_exit_status()
                output = "".join(chunks)
                if exit_code != 0:
                    raise SshCommandError(
                        f"{target.describe()}: command exited {exit_code}.",
                        exit_code=exit_code,
                        output=output,
                    )
                return SshResult(exit_code=0, output=output)
            except SshCommandError:
                raise
            except (OSError, EOFError, paramiko.SSHException) as exc:
                raise SshConnectionError(
                    f"{target.describe()}: connection dropped mid-command — {exc}"
                ) from exc
        finally:
            client.close()
            if bastion_client is not None:
                bastion_client.close()

    def put_file(self, target: SshTarget, local_path: str, remote_path: str) -> None:
        """SFTP upload (offline bundles). Escalation is the caller's problem —
        upload somewhere writable, then move with a sudo script."""
        client, bastion_client = self._connect(target)
        try:
            sftp = client.open_sftp()
            try:
                sftp.put(local_path, remote_path)
            finally:
                sftp.close()
        except Exception as exc:  # noqa: BLE001
            raise SshConnectionError(
                f"{target.describe()}: file upload failed — {exc}"
            ) from exc
        finally:
            client.close()
            if bastion_client is not None:
                bastion_client.close()


# ---------------------------------------------------------------------------
# Transport factory — tests swap in a fake with set_transport_factory().
# ---------------------------------------------------------------------------

_factory_lock = threading.Lock()
_transport_factory: Optional[Callable[[], object]] = None
_shared_transport: Optional[object] = None


def set_transport_factory(factory: Optional[Callable[[], object]]) -> None:
    """Override the transport (tests / mock mode). Pass None to restore."""
    global _transport_factory, _shared_transport
    with _factory_lock:
        _transport_factory = factory
        _shared_transport = None


def get_transport():
    global _shared_transport
    with _factory_lock:
        if _shared_transport is None:
            _shared_transport = (
                _transport_factory() if _transport_factory else ParamikoTransport()
            )
        return _shared_transport
