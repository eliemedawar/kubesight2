"""What vCenter says about the VMs while OpenTofu creates them.

OpenTofu prints only "Still creating... [7m20s elapsed]" while the vSphere
provider waits for a clone, for guest customization (up to 20 minutes) and for
the VM's address — and the provider itself logs nothing during those waits. So
an apply that hangs says nothing about why. This watcher asks vCenter instead,
every few seconds, and reports what changes: the VM appearing, its power state,
VMware Tools, the guest's hostname and address, and vCenter's own events on the
VM (clone, reconfigure, power on, customization started / succeeded / failed —
with vCenter's reason).

It runs on its own thread with its own vCenter session and only ever hands
plain text to a queue; the job's worker thread, which owns the database
session, writes those lines into the job log (``jobs._Output``).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ...vsphere_client import VSphereConfig

logger = logging.getLogger(__name__)

PREFIX = "vCenter ·"
_POLL_SECONDS = 10.0
_MESSAGE_CHARS = 400

_POWER = {"poweredOn": "powered on", "poweredOff": "powered off", "suspended": "suspended"}
_TOOLS = {
    "guestToolsRunning": "VMware Tools running",
    "guestToolsNotRunning": "VMware Tools not running",
    "guestToolsExecutingScripts": "VMware Tools starting",
}


def describe_changes(name: str, before: Dict[str, Any], now: Dict[str, Any]) -> List[str]:
    """One line per fact about a VM that changed since the last look."""
    lines: List[str] = []
    if now.get("power") != before.get("power") and now.get("power"):
        lines.append(f"{PREFIX} {name}: {_POWER.get(now['power'], now['power'])}")
    if now.get("tools") != before.get("tools") and now.get("tools"):
        lines.append(f"{PREFIX} {name}: {_TOOLS.get(now['tools'], now['tools'])}")
    if now.get("hostname") != before.get("hostname") and now.get("hostname"):
        lines.append(f"{PREFIX} {name}: guest hostname is {now['hostname']}")
    if now.get("ip") != before.get("ip") and now.get("ip"):
        lines.append(f"{PREFIX} {name}: guest reports address {now['ip']}")
    if now.get("nics") != before.get("nics") and now.get("nics"):
        state = ", ".join(
            f"{label} {'connected' if connected else 'NOT connected'}"
            for label, connected in now["nics"]
        )
        lines.append(f"{PREFIX} {name}: network card {state}")
    return lines


def ethernet_cards(devices) -> List[Any]:
    from . import inventory

    return [d for d in devices or [] if type(d).__name__.split(".")[-1] in inventory._NIC_TYPES]


def cards_needing_connect(devices) -> List[Any]:
    """Cards of a running VM that are not both connected and set to connect at
    power on — the state every deployed VM's card must end up in."""
    out = []
    for card in ethernet_cards(devices):
        connectable = getattr(card, "connectable", None)
        if not (getattr(connectable, "connected", False) and getattr(connectable, "startConnected", False)):
            out.append(card)
    return out


def nic_states(devices) -> List[Tuple[str, bool]]:
    """(label, connected) for each network card, as vCenter reports it."""
    out = []
    for card in ethernet_cards(devices):
        info = getattr(card, "deviceInfo", None)
        label = getattr(info, "label", None) or "card"
        out.append((label, bool(getattr(getattr(card, "connectable", None), "connected", False))))
    return out


def describe_event(name: str, event: Any) -> str:
    """vCenter's own sentence for an event on the VM, e.g. why customization failed."""
    kind = type(event).__name__.split(".")[-1]
    message = str(getattr(event, "fullFormattedMessage", "") or "").strip() or kind
    if "Customization" in kind and "Failed" in kind:
        message = f"customization FAILED ({kind}): {message}"
    return f"{PREFIX} {name}: {message}"[:_MESSAGE_CHARS]


class VmWatcher:
    """Polls vCenter for ``names`` until stopped. Never raises into the job."""

    def __init__(self, cfg: VSphereConfig, names: Iterable[str], sink: "queue.Queue[str]",
                 interval: float = _POLL_SECONDS):
        self.cfg = cfg
        self.names = sorted(set(names))
        self.sink = sink
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = datetime.now(timezone.utc)

    def start(self) -> "VmWatcher":
        if self.names:
            self._thread = threading.Thread(target=self._run, name="vm-watch", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)

    def _say(self, line: str) -> None:
        self.sink.put(line)

    def _run(self) -> None:
        from . import inventory

        try:
            si = inventory._connect(self.cfg)
        except Exception as exc:  # noqa: BLE001 — watching is a nicety
            self._say(f"{PREFIX} not watching the VMs: {exc}"[:_MESSAGE_CHARS])
            return
        try:
            self._loop(si)
        except Exception as exc:  # noqa: BLE001
            logger.warning("VM watch stopped", exc_info=True)
            self._say(f"{PREFIX} stopped watching the VMs: {exc}"[:_MESSAGE_CHARS])
        finally:
            inventory._disconnect(si)

    def _loop(self, si) -> None:
        from pyVmomi import vim

        from . import inventory

        content = si.RetrieveContent()
        found: Dict[str, Any] = {}
        facts: Dict[str, Dict[str, Any]] = {}
        seen_events: Dict[str, set] = {}
        tried_connect: set = set()
        self._say(f"{PREFIX} watching {', '.join(self.names)} (power, VMware Tools, address, events)")
        while not self._stop.is_set():
            if len(found) < len(self.names):
                wanted = set(self.names) - set(found)
                for obj, props in inventory._collect(content, vim.VirtualMachine, ["name"]):
                    name = props.get("name")
                    if name in wanted:
                        found[name] = obj
                        self._say(f"{PREFIX} {name}: exists in vCenter ({inventory._moid(obj)})")
            for name, vm in found.items():
                try:
                    now = {
                        "power": str(vm.runtime.powerState),
                        "tools": str(vm.guest.toolsRunningStatus or ""),
                        "ip": vm.guest.ipAddress,
                        "hostname": vm.guest.hostName,
                    }
                except Exception:  # noqa: BLE001 — the VM may be mid-destroy
                    continue
                try:  # read on its own: no device list must not hide the rest
                    devices = list(vm.config.hardware.device or [])
                except Exception:  # noqa: BLE001
                    devices = []
                now["nics"] = nic_states(devices)
                for line in describe_changes(name, facts.get(name, {}), now):
                    self._say(line)
                facts[name] = now
                # A running VM's card must be connected now AND set to connect
                # at power on; one that is not never gets (or keeps) its address.
                # Fix it once instead of waiting for the timeout.
                if (now["power"] == "poweredOn" and name not in tried_connect
                        and cards_needing_connect(devices)):
                    tried_connect.add(name)
                    self._say(self._connect_cards(name, vm, devices))
                for event in self._events(content, vm):
                    key = getattr(event, "key", None)
                    if key in seen_events.setdefault(name, set()):
                        continue
                    seen_events[name].add(key)
                    self._say(describe_event(name, event))
            self._stop.wait(self.interval)

    def _connect_cards(self, name: str, vm, devices) -> str:
        """Connect every disconnected card of a running VM (and set it to connect
        at power on). Needs "Connect devices" / "Modify device settings"."""
        from pyVmomi import vim

        changes = []
        for card in cards_needing_connect(devices):
            connectable = getattr(card, "connectable", None)
            if connectable is None:
                connectable = card.connectable = vim.vm.device.VirtualDevice.ConnectInfo()
            connectable.connected = True
            connectable.startConnected = True
            connectable.allowGuestControl = True
            changes.append(vim.vm.device.VirtualDeviceSpec(operation="edit", device=card))
        if not changes:
            return f"{PREFIX} {name}: network card connected and set to connect at power on"
        try:
            task = vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(deviceChange=changes))
            deadline = time.monotonic() + 60
            while getattr(task.info, "state", "success") in ("queued", "running") and time.monotonic() < deadline:
                time.sleep(1)
            if getattr(task.info, "state", "success") == "error":
                reason = getattr(getattr(task.info, "error", None), "msg", None) or task.info.error
                return f"{PREFIX} {name}: could not connect the network card: {reason}"[:_MESSAGE_CHARS]
        except Exception as exc:  # noqa: BLE001 — report it; the apply goes on
            return f"{PREFIX} {name}: could not connect the network card: {exc}"[:_MESSAGE_CHARS]
        return (f"{PREFIX} {name}: the network card was not connected and set to connect at power on "
                "— KubeSight set both")

    def _events(self, content, vm) -> List[Any]:
        from pyVmomi import vim

        try:
            spec = vim.event.EventFilterSpec(
                entity=vim.event.EventFilterSpec.ByEntity(entity=vm, recursion="self"),
                time=vim.event.EventFilterSpec.ByTime(beginTime=self._started),
            )
            events = content.eventManager.QueryEvents(spec) or []
        except Exception:  # noqa: BLE001 — events are optional detail; keep watching
            return []
        return sorted(events, key=lambda e: getattr(e, "key", 0))
