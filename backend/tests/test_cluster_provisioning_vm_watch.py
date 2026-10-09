"""The vCenter watcher that explains an apply's long "Still creating..." waits."""

from __future__ import annotations

import queue
import time
from types import SimpleNamespace

from pyVmomi import vim

from api.services.cluster_build.provisioning import inventory, jobs, vm_watch
from api.services.vsphere_client import VSphereConfig


def test_only_what_changed_is_said():
    assert vm_watch.describe_changes("vm-1", {}, {"power": "poweredOff", "tools": "", "ip": None}) == [
        "vCenter · vm-1: powered off",
    ]
    before = {"power": "poweredOff", "tools": "guestToolsNotRunning", "ip": None, "hostname": None}
    now = {"power": "poweredOn", "tools": "guestToolsRunning", "ip": "10.4.112.7", "hostname": "vm-1"}
    assert vm_watch.describe_changes("vm-1", before, now) == [
        "vCenter · vm-1: powered on",
        "vCenter · vm-1: VMware Tools running",
        "vCenter · vm-1: guest hostname is vm-1",
        "vCenter · vm-1: guest reports address 10.4.112.7",
    ]
    assert vm_watch.describe_changes("vm-1", now, dict(now)) == []


def test_a_customization_failure_says_so():
    event = vim.event.CustomizationNetworkSetupFailed(
        key=7, fullFormattedMessage="An error occurred while setting up network properties of the guest OS.")
    line = vm_watch.describe_event("vm-1", event)
    assert line.startswith("vCenter · vm-1: customization FAILED (CustomizationNetworkSetupFailed)")
    started = vim.event.CustomizationStartedEvent(key=6, fullFormattedMessage="Started customization of VM vm-1.")
    assert vm_watch.describe_event("vm-1", started) == "vCenter · vm-1: Started customization of VM vm-1."


def test_the_watcher_reports_a_vm_as_vcenter_sees_it(monkeypatch):
    state = {"power": "poweredOff", "tools": "guestToolsNotRunning", "ip": None}
    vm = SimpleNamespace(
        runtime=SimpleNamespace(powerState="poweredOff"),
        guest=SimpleNamespace(toolsRunningStatus="guestToolsNotRunning", ipAddress=None, hostName=None),
        _moId="vm-1201",
    )
    events = [vim.event.VmClonedEvent(key=1, fullFormattedMessage="Clone of AL-K8S-Template-DR completed")]

    class Events:
        def QueryEvents(self, spec):
            return list(events)

    content = SimpleNamespace(eventManager=Events())
    # The fake VM is not a vim.ManagedEntity, so skip building the filter spec
    # and ask the fake event manager directly.
    monkeypatch.setattr(vm_watch.VmWatcher, "_events",
                        lambda self, content, vm: sorted(content.eventManager.QueryEvents(None),
                                                         key=lambda e: e.key))
    si = SimpleNamespace(RetrieveContent=lambda: content)
    monkeypatch.setattr(inventory, "_connect", lambda cfg: si)
    monkeypatch.setattr(inventory, "_disconnect", lambda si: None)
    monkeypatch.setattr(inventory, "_collect", lambda content, kind, paths: [(vm, {"name": "test-vm-1"})])

    sink: "queue.Queue[str]" = queue.Queue()
    cfg = VSphereConfig(base_url="https://vc.example.test", username="u", password="p")
    watcher = vm_watch.VmWatcher(cfg, ["test-vm-1"], sink, interval=0.05).start()
    time.sleep(0.2)
    vm.runtime.powerState = "poweredOn"
    vm.guest.toolsRunningStatus = "guestToolsRunning"
    vm.guest.ipAddress = "10.4.112.7"
    events.append(vim.event.CustomizationLinuxIdentityFailed(
        key=2, fullFormattedMessage="An error occurred while customizing VM test-vm-1."))
    time.sleep(0.3)
    watcher.stop()

    said = []
    while not sink.empty():
        said.append(sink.get())
    assert said[0].startswith("vCenter · watching test-vm-1")
    assert "vCenter · test-vm-1: exists in vCenter (vm-1201)" in said
    assert "vCenter · test-vm-1: Clone of AL-K8S-Template-DR completed" in said
    assert "vCenter · test-vm-1: powered off" in said and "vCenter · test-vm-1: powered on" in said
    assert "vCenter · test-vm-1: guest reports address 10.4.112.7" in said
    assert any("customization FAILED (CustomizationLinuxIdentityFailed)" in line for line in said)
    assert said.count("vCenter · test-vm-1: Clone of AL-K8S-Template-DR completed") == 1  # events once


def test_the_job_log_takes_lines_from_the_watcher(app):
    job = SimpleNamespace(log_tail="", progress_json={})
    output = jobs._Output(job, ["s3cret"])
    output.side.put("vCenter · test-vm-1: powered on")
    output.side.put("vCenter · password s3cret leaked")
    output.on_line('vsphere_virtual_machine.node["test-vm-1"]: Still creating... [7m20s elapsed]')
    assert output.lines[:2] == ["vCenter · test-vm-1: powered on", "vCenter · password [REDACTED] leaked"]
    output.side.put("vCenter · test-vm-1: guest reports address 10.4.112.7")
    output.flush()
    assert "guest reports address 10.4.112.7" in job.log_tail


def test_an_event_query_that_fails_does_not_stop_the_watch(monkeypatch):
    vm = SimpleNamespace(
        runtime=SimpleNamespace(powerState="poweredOn"),
        guest=SimpleNamespace(toolsRunningStatus="guestToolsRunning", ipAddress="10.4.112.7", hostName=None),
        _moId="vm-1",
    )
    content = SimpleNamespace(eventManager=SimpleNamespace())  # no QueryEvents at all
    monkeypatch.setattr(inventory, "_connect", lambda cfg: SimpleNamespace(RetrieveContent=lambda: content))
    monkeypatch.setattr(inventory, "_disconnect", lambda si: None)
    monkeypatch.setattr(inventory, "_collect", lambda content, kind, paths: [(vm, {"name": "vm-a"})])
    sink: "queue.Queue[str]" = queue.Queue()
    cfg = VSphereConfig(base_url="https://vc.example.test", username="u", password="p")
    watcher = vm_watch.VmWatcher(cfg, ["vm-a"], sink, interval=0.05).start()
    time.sleep(0.15)
    vm.guest.hostName = "vm-a"
    time.sleep(0.15)
    watcher.stop()
    said = []
    while not sink.empty():
        said.append(sink.get())
    assert "vCenter · vm-a: guest hostname is vm-a" in said
    assert not any("stopped watching" in line for line in said)


def test_a_running_vm_with_its_card_disconnected_gets_it_connected(monkeypatch):
    card = vim.vm.device.VirtualVmxnet3(
        key=4000,
        deviceInfo=vim.Description(label="Network adapter 1", summary="VM-Net"),
        connectable=vim.vm.device.VirtualDevice.ConnectInfo(
            connected=False, startConnected=False, allowGuestControl=False),
    )
    sent = []

    def reconfigure(spec):
        sent.append(spec)
        card.connectable.connected = True  # what vCenter does with the edit
        return SimpleNamespace(info=SimpleNamespace(state="success"))

    vm = SimpleNamespace(
        runtime=SimpleNamespace(powerState="poweredOn"),
        guest=SimpleNamespace(toolsRunningStatus="guestToolsRunning", ipAddress=None, hostName=None),
        config=SimpleNamespace(hardware=SimpleNamespace(device=[card])),
        ReconfigVM_Task=reconfigure,
        _moId="vm-1201",
    )
    content = SimpleNamespace(eventManager=SimpleNamespace())
    monkeypatch.setattr(inventory, "_connect", lambda cfg: SimpleNamespace(RetrieveContent=lambda: content))
    monkeypatch.setattr(inventory, "_disconnect", lambda si: None)
    monkeypatch.setattr(inventory, "_collect", lambda content, kind, paths: [(vm, {"name": "test-vm-1"})])
    sink: "queue.Queue[str]" = queue.Queue()
    cfg = VSphereConfig(base_url="https://vc.example.test", username="u", password="p")
    watcher = vm_watch.VmWatcher(cfg, ["test-vm-1"], sink, interval=0.05).start()
    time.sleep(0.3)
    watcher.stop()
    said = []
    while not sink.empty():
        said.append(sink.get())
    assert "vCenter · test-vm-1: network card Network adapter 1 NOT connected" in said
    assert ("vCenter · test-vm-1: the network card was not connected and set to connect at power on "
            "— KubeSight set both") in said
    assert "vCenter · test-vm-1: network card Network adapter 1 connected" in said
    assert len(sent) == 1  # once, not on every poll
    change = sent[0].deviceChange[0]
    assert change.operation == "edit"
    assert change.device.connectable.connected and change.device.connectable.startConnected


def test_the_template_cards_connect_setting_is_read():
    off = vim.vm.device.VirtualVmxnet3(
        key=4000, connectable=vim.vm.device.VirtualDevice.ConnectInfo(startConnected=False),
        backing=vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(network=vim.Network("network-14763")),
    )
    nics = inventory._template_hardware([off])["nics"]
    assert nics == [{"type": "vmxnet3", "networkId": "network-14763", "startConnected": False}]


def test_a_connected_card_not_set_to_connect_at_power_on_is_fixed_too():
    card = vim.vm.device.VirtualVmxnet3(
        key=4000, connectable=vim.vm.device.VirtualDevice.ConnectInfo(connected=True, startConnected=False))
    fine = vim.vm.device.VirtualVmxnet3(
        key=4001, connectable=vim.vm.device.VirtualDevice.ConnectInfo(connected=True, startConnected=True))
    assert vm_watch.cards_needing_connect([card, fine]) == [card]
    assert vm_watch.cards_needing_connect([fine]) == []
