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
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

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
    return lines


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
                for line in describe_changes(name, facts.get(name, {}), now):
                    self._say(line)
                facts[name] = now
                for event in self._events(content, vm):
                    key = getattr(event, "key", None)
                    if key in seen_events.setdefault(name, set()):
                        continue
                    seen_events[name].add(key)
                    self._say(describe_event(name, event))
            self._stop.wait(self.interval)

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
