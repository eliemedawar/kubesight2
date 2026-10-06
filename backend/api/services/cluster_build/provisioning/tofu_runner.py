"""Running ``tofu``: the real binary, or a simulation of it.

The real engine is a subprocess whose output is streamed line by line to a
callback, so a job can show progress while an apply is still cloning VMs.

The simulated engine exists for tests and for walking through the feature
without a vCenter (KUBESIGH_PROVISIONING_SIMULATE). It reads the same
main.tf.json and writes the same state store the HTTP backend serves, so
everything above it — jobs, plan summaries, recovery — runs unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from . import inventory, tofu_config

OnLine = Callable[[str], None]


class TofuUnavailable(RuntimeError):
    pass


class TofuTimeout(RuntimeError):
    pass


def provider_mirror_path() -> str:
    return os.getenv("KUBESIGHT_TOFU_PROVIDER_MIRROR", "/opt/kubesight/tofu/providers")


def provider_mirror_present() -> bool:
    path = os.path.join(
        provider_mirror_path(), "registry.opentofu.org",
        *tofu_config.PROVIDER_SOURCE.split("/"), tofu_config.PROVIDER_VERSION,
    )
    return os.path.isdir(path)


# ---------------------------------------------------------------------------
# Real engine
# ---------------------------------------------------------------------------

class RealEngine:
    mode = "real"

    def __init__(self, binary: Optional[str] = None):
        self.binary = binary or os.getenv("KUBESIGHT_TOFU_BINARY") or shutil.which("tofu")

    def available(self) -> bool:
        return bool(self.binary and os.path.exists(self.binary))

    def version(self) -> Optional[str]:
        if not self.available():
            return None
        try:
            completed = subprocess.run(
                [self.binary, "version", "-json"], capture_output=True, text=True, timeout=20,
            )
            return json.loads(completed.stdout or "{}").get("terraform_version")
        except Exception:  # noqa: BLE001 — status is best-effort
            return None

    def run(
        self, workdir: str, args: List[str], env: Dict[str, str],
        on_line: Optional[OnLine] = None, timeout_s: int = 900,
    ) -> int:
        if not self.available():
            raise TofuUnavailable(
                "OpenTofu is not installed in this KubeSight image (no `tofu` on "
                "PATH). Rebuild the backend image; its Dockerfile installs it."
            )
        process = subprocess.Popen(
            [self.binary, *args], cwd=workdir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        killed = threading.Event()

        def _kill() -> None:
            killed.set()
            process.kill()

        timer = threading.Timer(timeout_s, _kill)
        timer.daemon = True
        timer.start()
        try:
            for line in process.stdout:  # type: ignore[union-attr]
                if on_line:
                    on_line(line.rstrip("\n"))
            process.wait()
        finally:
            timer.cancel()
        if killed.is_set():
            raise TofuTimeout(f"`tofu {args[0]}` ran longer than {timeout_s // 60} minutes and was stopped.")
        return int(process.returncode or 0)


# ---------------------------------------------------------------------------
# Simulated engine
# ---------------------------------------------------------------------------

def _desired_resources(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """address -> {type, name, key, after} for everything the config declares."""
    out: Dict[str, Dict[str, Any]] = {}
    resources = config.get("resource") or {}
    for rtype, blocks in resources.items():
        for rname, body in (blocks or {}).items():
            for_each = body.get("for_each")
            if isinstance(for_each, dict):
                for key, values in for_each.items():
                    address = f'{rtype}.{rname}["{key}"]'
                    after = {"name": key}
                    if rtype == "vsphere_virtual_machine":
                        after.update({
                            "num_cpus": values.get("cpu"),
                            "memory": values.get("memory_mb"),
                            "disk": [{"size": values.get("disk_gb")}],
                            "clone": [{"customize": [{"network_interface": [
                                {"ipv4_address": values.get("ip")}
                            ]}]}],
                        })
                    out[address] = {"type": rtype, "name": rname, "key": key, "after": after}
            else:
                after = {k: v for k, v in body.items() if isinstance(v, (str, int, bool))}
                out[f"{rtype}.{rname}"] = {"type": rtype, "name": rname, "key": None, "after": after}
    return out


class SimulatedEngine:
    """Answers ``init``, ``plan``, ``show`` and ``apply`` like OpenTofu would.

    ``delay_s`` paces an apply so a demo shows VMs being created one after
    another; tests leave it at zero. ``fail_on`` names a VM whose creation
    fails, to exercise partial applies.
    """

    mode = "simulated"

    def __init__(self, delay_s: float = 0.0, fail_on: Optional[str] = None):
        self.delay_s = delay_s
        self.fail_on = fail_on

    def available(self) -> bool:
        return True

    def version(self) -> Optional[str]:
        return "simulated"

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def _build_id(env: Dict[str, str]) -> int:
        return int(env["KUBESIGHT_BUILD_ID"])

    @staticmethod
    def _load_config(workdir: str) -> Dict[str, Any]:
        with open(os.path.join(workdir, "main.tf.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def _say(self, on_line: Optional[OnLine], text: str) -> None:
        if on_line:
            for line in text.splitlines():
                on_line(line)

    # -- commands -------------------------------------------------------------
    def run(
        self, workdir: str, args: List[str], env: Dict[str, str],
        on_line: Optional[OnLine] = None, timeout_s: int = 900,
    ) -> int:
        command = args[0]
        if command == "init":
            self._say(on_line, "Initializing the backend...\nInitializing provider plugins...\n"
                      f"- Installing {tofu_config.PROVIDER_SOURCE} v{tofu_config.PROVIDER_VERSION} (simulated)\n"
                      "OpenTofu has been successfully initialized!")
            return 0
        if command == "plan":
            return self._plan(workdir, args, env, on_line)
        if command == "show":
            return self._show(workdir, args, on_line)
        if command == "apply":
            return self._apply(workdir, args, env, on_line)
        self._say(on_line, f"Error: simulated tofu does not know `{command}`")
        return 1

    def _plan(self, workdir, args, env, on_line) -> int:
        from . import state_store

        build_id = self._build_id(env)
        destroy = "-destroy" in args
        out_file = next((a.split("=", 1)[1] for a in args if a.startswith("-out=")), "plan.tfplan")
        config = self._load_config(workdir)
        state = state_store.read_state_document(build_id) or {}
        existing = {item["address"]: item for item in state_store.managed_resources(build_id)}
        desired = {} if destroy else _desired_resources(config)
        changes = []
        for address, item in desired.items():
            if address not in existing:
                changes.append({**item, "address": address, "actions": ["create"], "before": None})
        for address, item in existing.items():
            if address not in desired:
                changes.append({
                    "address": address, "type": item["type"], "name": item["name"],
                    "key": item["key"], "actions": ["delete"],
                    "before": item["attributes"], "after": None,
                })
        add = sum(1 for c in changes if c["actions"] == ["create"])
        remove = sum(1 for c in changes if c["actions"] == ["delete"])
        for change in changes:
            verb = "will be created" if change["actions"] == ["create"] else "will be destroyed"
            self._say(on_line, f"  # {change['address']} {verb}")
        self._say(on_line, f"\nPlan: {add} to add, 0 to change, {remove} to destroy.")
        with open(os.path.join(workdir, out_file), "w", encoding="utf-8") as handle:
            json.dump({
                "simulated": True, "destroy": destroy, "changes": changes,
                "serial": state.get("serial"), "lineage": state.get("lineage"),
            }, handle)
        return 0

    def _show(self, workdir, args, on_line) -> int:
        plan_file = args[-1]
        with open(os.path.join(workdir, plan_file), encoding="utf-8") as handle:
            plan = json.load(handle)
        if "-json" in args:
            document = {
                "format_version": "1.2",
                "resource_changes": [
                    {
                        "address": c["address"], "mode": "managed", "type": c["type"],
                        "name": c["name"], "index": c["key"],
                        "change": {"actions": c["actions"], "before": c.get("before"),
                                   "after": c.get("after")},
                    }
                    for c in plan["changes"]
                ],
            }
            self._say(on_line, json.dumps(document))
            return 0
        lines = ["OpenTofu will perform the following actions (simulated):", ""]
        for change in plan["changes"]:
            sign = "+" if change["actions"] == ["create"] else "-"
            verb = "will be created" if sign == "+" else "will be destroyed"
            lines.append(f"  # {change['address']} {verb}")
            lines.append(f"  {sign} resource \"{change['type']}\" \"{change['name']}\" {{")
            for key, value in sorted((change.get("after") or change.get("before") or {}).items()):
                if isinstance(value, (str, int, bool)):
                    lines.append(f"      {sign} {key} = {json.dumps(value)}")
            lines.append("    }")
            lines.append("")
        add = sum(1 for c in plan["changes"] if c["actions"] == ["create"])
        remove = sum(1 for c in plan["changes"] if c["actions"] == ["delete"])
        lines.append(f"Plan: {add} to add, 0 to change, {remove} to destroy.")
        self._say(on_line, "\n".join(lines))
        return 0

    def _apply(self, workdir, args, env, on_line) -> int:
        from . import state_store

        build_id = self._build_id(env)
        plan_file = args[-1]
        with open(os.path.join(workdir, plan_file), encoding="utf-8") as handle:
            plan = json.load(handle)
        state = state_store.read_state_document(build_id) or {
            "version": 4, "terraform_version": "simulated", "serial": 0,
            "lineage": str(uuid.uuid4()), "outputs": {}, "resources": [],
        }
        if state.get("serial") != plan.get("serial") and plan.get("serial") is not None:
            self._say(on_line, "Error: Saved plan is stale\n\nThe given plan file can no longer be "
                      "applied because the state was changed by another operation after the plan "
                      "was created.")
            return 1

        lock_id = str(uuid.uuid4())
        ok, _ = state_store.lock(build_id, {"ID": lock_id, "Operation": "OperationTypeApply",
                                            "Who": "kubesight-simulated"},
                                 job_id=int(env.get("KUBESIGHT_JOB_ID") or 0) or None)
        if not ok:
            self._say(on_line, "Error: Error acquiring the state lock")
            return 1
        fail_on = self.fail_on or env.get("KUBESIGHT_SIMULATE_FAIL_VM")
        try:
            # The order OpenTofu's dependency graph gives: creating goes folder,
            # VMs, then rules; destroying goes the other way round.
            rank = {"vsphere_folder": 0, "vsphere_virtual_machine": 1}
            creates = sorted(
                (c for c in plan["changes"] if c["actions"] == ["create"]),
                key=lambda c: rank.get(c["type"], 2),
            )
            deletes = sorted(
                (c for c in plan["changes"] if c["actions"] == ["delete"]),
                key=lambda c: -rank.get(c["type"], 2),
            )
            changes = deletes + creates
            added = removed = 0
            for change in changes:
                address = change["address"]
                if change["actions"] == ["create"]:
                    self._say(on_line, f"{address}: Creating...")
                    if change["type"] == "vsphere_virtual_machine" and self.delay_s:
                        steps = 3
                        for step in range(1, steps + 1):
                            time.sleep(self.delay_s / steps)
                            self._say(on_line, f"{address}: Still creating... [{step * 10}s elapsed]")
                    if fail_on and change.get("key") == fail_on:
                        self._say(on_line, "")
                        self._say(on_line, "Error: error cloning virtual machine: Insufficient disk "
                                  "space on datastore 'simulated'.")
                        self._say(on_line, "")
                        self._say(on_line, f"  with {address},")
                        state_store.write_state(build_id, json.dumps(state), lock_id=lock_id)
                        self._say(on_line, f"\nApply incomplete! Resources: {added} added, 0 changed, {removed} destroyed.")
                        return 1
                    self._add_instance(state, change)
                    added += 1
                    self._say(on_line, f"{address}: Creation complete after 1m{10 + added}s "
                              f"[id={self._fake_id(change)}]")
                else:
                    self._say(on_line, f"{address}: Destroying...")
                    if change["type"] == "vsphere_virtual_machine" and self.delay_s:
                        time.sleep(self.delay_s / 2)
                    self._remove_instance(state, change)
                    removed += 1
                    self._say(on_line, f"{address}: Destruction complete after 6s")
                state["serial"] = int(state.get("serial") or 0) + 1
                state_store.write_state(build_id, json.dumps(state), lock_id=lock_id)
            self._say(on_line, f"\nApply complete! Resources: {added} added, 0 changed, {removed} destroyed.")
            return 0
        finally:
            state_store.unlock(build_id, {"ID": lock_id})

    @staticmethod
    def _fake_id(change) -> str:
        return f"4211{abs(hash(change['address'])) % 10 ** 8:08d}-sim"

    def _add_instance(self, state, change) -> None:
        resources = state.setdefault("resources", [])
        resource = next(
            (r for r in resources if r["type"] == change["type"] and r["name"] == change["name"]),
            None,
        )
        if resource is None:
            resource = {
                "mode": "managed", "type": change["type"], "name": change["name"],
                "provider": f'provider["{tofu_config.PROVIDER_ADDRESS}"]', "instances": [],
            }
            resources.append(resource)
        attributes = {"id": self._fake_id(change), **(change.get("after") or {})}
        if change["type"] == "vsphere_virtual_machine":
            ip = (((change.get("after") or {}).get("clone") or [{}])[0].get("customize") or [{}])[0] \
                .get("network_interface", [{}])[0].get("ipv4_address")
            attributes.update({
                "moid": f"vm-{900 + len(resource['instances'])}",
                "default_ip_address": ip,
                "host_system_id": f"host-{10 + len(resource['instances']) % 4}",
                "uuid": attributes["id"],
            })
        instance = {"schema_version": 0, "attributes": attributes}
        if change.get("key") is not None:
            instance["index_key"] = change["key"]
        resource["instances"].append(instance)

    def _remove_instance(self, state, change) -> None:
        for resource in state.get("resources") or []:
            if resource["type"] != change["type"] or resource["name"] != change["name"]:
                continue
            resource["instances"] = [
                i for i in resource.get("instances") or [] if i.get("index_key") != change.get("key")
            ]
        state["resources"] = [r for r in state.get("resources") or [] if r.get("instances")]


# ---------------------------------------------------------------------------
# Which engine
# ---------------------------------------------------------------------------

_engine_override = None


def set_engine(engine) -> None:
    """Test seam."""
    global _engine_override
    _engine_override = engine


def engine():
    if _engine_override is not None:
        return _engine_override
    if inventory.simulation_enabled():
        return SimulatedEngine(delay_s=float(os.getenv("KUBESIGHT_PROVISIONING_SIMULATE_DELAY", "4")))
    return RealEngine()


_status_cache: Dict[str, Any] = {}


def engine_status() -> Dict[str, Any]:
    current = engine()
    key = f"{current.mode}:{getattr(current, 'binary', '')}"
    cached = _status_cache.get(key)
    if cached and time.time() - cached["at"] < 300:
        return cached["value"]
    value = {
        "mode": current.mode,
        "available": current.available(),
        "version": current.version() if current.available() else None,
        "providerSource": tofu_config.PROVIDER_SOURCE,
        "providerVersion": tofu_config.PROVIDER_VERSION,
        "providerBundled": current.mode != "real" or provider_mirror_present(),
        "providerMirror": provider_mirror_path(),
        "checkedAt": datetime.now(timezone.utc).isoformat(),
    }
    _status_cache.clear()
    _status_cache[key] = {"at": time.time(), "value": value}
    return value
