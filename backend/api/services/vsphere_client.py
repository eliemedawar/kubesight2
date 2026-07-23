"""Minimal vSphere Automation REST client (vCenter 7/8), stdlib urllib only —
matching the rest of the backend (see ``registry_client`` / ``jenkins_client``).

Read-only by design: v1 browses inventory (VM list, guest identity, Tools
state, host/datastore placement) and never mutates vSphere, so the service
account only needs the Read-Only role.
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

_TIMEOUT_SECONDS = 20
_GUEST_DETAIL_WORKERS = 8


class VSphereError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class VSphereConfig:
    base_url: str  # e.g. "https://vcenter.example.com"
    username: str
    password: str
    skip_tls_verify: bool = False
    ca_pem: str = ""

    @property
    def root(self) -> str:
        base = (self.base_url or "").strip().rstrip("/")
        if not base.lower().startswith(("http://", "https://")):
            base = f"https://{base}"
        return base


def _ssl_context(cfg: VSphereConfig) -> Optional[ssl.SSLContext]:
    if not cfg.root.lower().startswith("https://"):
        return None
    if cfg.skip_tls_verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if cfg.ca_pem:
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cadata=cfg.ca_pem)
        return ctx
    return ssl.create_default_context()


def _request(
    cfg: VSphereConfig,
    method: str,
    path: str,
    *,
    session_id: Optional[str] = None,
    query: Optional[Dict[str, Any]] = None,
) -> Any:
    url = cfg.root + path
    if query:
        pairs = []
        for key, value in query.items():
            if isinstance(value, (list, tuple)):
                pairs.extend((key, str(item)) for item in value)
            elif value is not None:
                pairs.append((key, str(value)))
        if pairs:
            url += "?" + urllib.parse.urlencode(pairs)
    request = urllib.request.Request(url, method=method)
    request.add_header("Accept", "application/json")
    if session_id:
        request.add_header("vmware-api-session-id", session_id)
    else:
        token = base64.b64encode(
            f"{cfg.username}:{cfg.password}".encode("utf-8")
        ).decode("ascii")
        request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(
            request, timeout=_TIMEOUT_SECONDS, context=_ssl_context(cfg)
        ) as response:
            body = response.read().decode("utf-8", errors="replace")
            return json.loads(body) if body.strip() else None
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        if exc.code == 401:
            raise VSphereError(
                "vCenter rejected the credentials (401).", status=401
            ) from exc
        raise VSphereError(
            f"vCenter API error {exc.code} on {path}: {detail}", status=exc.code
        ) from exc
    except urllib.error.URLError as exc:
        raise VSphereError(f"vCenter unreachable: {exc.reason}") from exc
    except (ssl.SSLError, OSError, json.JSONDecodeError) as exc:
        raise VSphereError(f"vCenter request failed: {exc}") from exc


def create_session(cfg: VSphereConfig) -> str:
    """POST /api/session → session token. Basic auth only for this one call."""
    token = _request(cfg, "POST", "/api/session")
    if not isinstance(token, str) or not token:
        raise VSphereError("vCenter did not return a session token.")
    return token


def delete_session(cfg: VSphereConfig, session_id: str) -> None:
    try:
        _request(cfg, "DELETE", "/api/session", session_id=session_id)
    except VSphereError:
        pass  # best-effort logout


def list_vms(cfg: VSphereConfig, session_id: str) -> List[Dict[str, Any]]:
    """VM summaries: {vm, name, power_state, cpu_count, memory_size_MiB}."""
    data = _request(cfg, "GET", "/api/vcenter/vm", session_id=session_id)
    return data if isinstance(data, list) else []


def list_hosts(cfg: VSphereConfig, session_id: str) -> List[Dict[str, Any]]:
    data = _request(cfg, "GET", "/api/vcenter/host", session_id=session_id)
    return data if isinstance(data, list) else []


def list_datastores(cfg: VSphereConfig, session_id: str) -> List[Dict[str, Any]]:
    data = _request(cfg, "GET", "/api/vcenter/datastore", session_id=session_id)
    return data if isinstance(data, list) else []


def vms_on_host(cfg: VSphereConfig, session_id: str, host_moid: str) -> List[str]:
    data = _request(
        cfg, "GET", "/api/vcenter/vm", session_id=session_id, query={"hosts": host_moid}
    )
    return [item.get("vm") for item in data or [] if item.get("vm")]


def vms_on_datastore(
    cfg: VSphereConfig, session_id: str, datastore_moid: str
) -> List[str]:
    data = _request(
        cfg,
        "GET",
        "/api/vcenter/vm",
        session_id=session_id,
        query={"datastores": datastore_moid},
    )
    return [item.get("vm") for item in data or [] if item.get("vm")]


def guest_identity(
    cfg: VSphereConfig, session_id: str, vm_moid: str
) -> Optional[Dict[str, Any]]:
    """Guest hostname/IP/OS as reported by VMware Tools. None when Tools is
    absent or not running (the API 503s) — callers treat that as 'no data',
    not an error: Tools is recommended, not required."""
    try:
        return _request(
            cfg, "GET", f"/api/vcenter/vm/{vm_moid}/guest/identity",
            session_id=session_id,
        )
    except VSphereError:
        return None


def tools_state(
    cfg: VSphereConfig, session_id: str, vm_moid: str
) -> Optional[Dict[str, Any]]:
    try:
        return _request(
            cfg, "GET", f"/api/vcenter/vm/{vm_moid}/tools", session_id=session_id
        )
    except VSphereError:
        return None


def full_inventory(cfg: VSphereConfig) -> List[Dict[str, Any]]:
    """One flattened inventory pass: VM summary + placement + guest details.

    Placement comes from filtered VM lists per host/datastore (the summary API
    doesn't expose it); guest identity/Tools are fetched concurrently for
    powered-on VMs only.
    """
    session_id = create_session(cfg)
    try:
        vms = list_vms(cfg, session_id)
        hosts = list_hosts(cfg, session_id)
        datastores = list_datastores(cfg, session_id)

        vm_to_host: Dict[str, str] = {}
        for host in hosts:
            moid, name = host.get("host"), host.get("name")
            if not moid:
                continue
            for vm_moid in vms_on_host(cfg, session_id, moid):
                vm_to_host[vm_moid] = name or moid
        vm_to_datastore: Dict[str, str] = {}
        for datastore in datastores:
            moid, name = datastore.get("datastore"), datastore.get("name")
            if not moid:
                continue
            for vm_moid in vms_on_datastore(cfg, session_id, moid):
                vm_to_datastore.setdefault(vm_moid, name or moid)

        powered_on = [
            vm.get("vm") for vm in vms
            if vm.get("vm") and str(vm.get("power_state", "")).upper() == "POWERED_ON"
        ]

        guest: Dict[str, Dict[str, Any]] = {}
        tools: Dict[str, Dict[str, Any]] = {}

        def _fetch(vm_moid: str) -> None:
            guest_data = guest_identity(cfg, session_id, vm_moid)
            if guest_data:
                guest[vm_moid] = guest_data
            tools_data = tools_state(cfg, session_id, vm_moid)
            if tools_data:
                tools[vm_moid] = tools_data

        if powered_on:
            with ThreadPoolExecutor(max_workers=_GUEST_DETAIL_WORKERS) as pool:
                list(pool.map(_fetch, powered_on))

        items = []
        for vm in vms:
            moid = vm.get("vm")
            if not moid:
                continue
            guest_data = guest.get(moid) or {}
            tools_data = tools.get(moid) or {}
            items.append(
                {
                    "moid": moid,
                    "name": vm.get("name") or moid,
                    "powerState": vm.get("power_state"),
                    "cpuCount": vm.get("cpu_count"),
                    "memoryMiB": vm.get("memory_size_MiB"),
                    "esxiHost": vm_to_host.get(moid),
                    "datastore": vm_to_datastore.get(moid),
                    "toolsRunState": tools_data.get("run_state"),
                    "toolsVersionStatus": tools_data.get("version_status"),
                    "guestHostname": guest_data.get("host_name"),
                    "guestIp": guest_data.get("ip_address"),
                    "guestFamily": guest_data.get("family"),
                    "guestOs": (
                        (guest_data.get("full_name") or {}).get("default_message")
                        if isinstance(guest_data.get("full_name"), dict)
                        else guest_data.get("full_name")
                    ),
                }
            )
        return items
    finally:
        delete_session(cfg, session_id)


def test_connection(cfg: VSphereConfig) -> Dict[str, Any]:
    session_id = create_session(cfg)
    try:
        vms = list_vms(cfg, session_id)
        return {"status": "ok", "vmCount": len(vms)}
    finally:
        delete_session(cfg, session_id)
