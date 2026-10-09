"""The OpenTofu configuration for one build's VMs, as main.tf.json.

Deliberate choices, each one a failure avoided:

  * **Objects by managed-object id, no data sources.** Datastore, network,
    resource pool, datacenter and cluster are ids captured when the plan was
    made, and the VM template is its UUID plus the hardware facts the clone
    must repeat (guest id, firmware, SCSI type, disk layout). A renamed
    datastore or a template someone deleted afterwards cannot stop a later
    destroy from being planned.
  * **VMs keyed by name (``for_each``), not by count.** Adding workers adds
    keys; it never renumbers the machines that already exist.
  * **``ignore_changes`` on everything a clone copies from its template.**
    Replacing the template later (a new image under the same name) must not
    plan to rebuild a running cluster. KubeSight only creates and deletes.
  * **No credentials in the file.** vCenter's address, user and password
    reach the provider through its environment (VSPHERE_*), and the state
    backend's through TF_HTTP_*.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Any, Dict, List

PROVIDER_SOURCE = "vmware/vsphere"
PROVIDER_VERSION = os.getenv("KUBESIGHT_TOFU_VSPHERE_PROVIDER_VERSION", "2.17.1")
PROVIDER_ADDRESS = f"registry.opentofu.org/{PROVIDER_SOURCE}"
VM_RESOURCE = "vsphere_virtual_machine.node"
# Minutes the provider waits for VMware Tools to report the customized address.
GUEST_NET_TIMEOUT_MIN = 10
# Minutes guest customization may take inside a VM before the clone fails.
CUSTOMIZE_TIMEOUT_MIN = 20


def vm_address(name: str) -> str:
    return f'{VM_RESOURCE}["{name}"]'


def folder_path(spec: Dict[str, Any], cluster_name: str) -> str:
    parent = str(spec.get("folderParent") or "").strip("/")
    if spec.get("folderMode") == "existing":
        # A folder someone else made (and may have granted the account on):
        # the VMs go straight in, and OpenTofu never creates or deletes it.
        return parent
    return f"{parent}/{cluster_name}" if parent else cluster_name


def creates_folder(spec: Dict[str, Any]) -> bool:
    return spec.get("folderMode") != "existing"


def _disk_block(index: int, disk: Dict[str, Any], size: Any) -> Dict[str, Any]:
    """One template disk, repeated as the provider reads it back, so the clone's
    disk is left as it is (only ``size`` may grow disk0 when sizes are set)."""
    block: Dict[str, Any] = {
        "label": f"disk{index}",
        "unit_number": int(disk.get("unit", index) or 0),
        "size": size,
        "thin_provisioned": bool(disk.get("thin", True)),
        "eagerly_scrub": bool(disk.get("eagerlyScrub", False)),
    }
    for key, attr in (
        ("controllerType", "controller_type"), ("diskMode", "disk_mode"),
        ("sharing", "disk_sharing"), ("ioLimit", "io_limit"),
        ("ioReservation", "io_reservation"), ("ioShareLevel", "io_share_level"),
    ):
        if disk.get(key) is not None:
            block[attr] = disk[key]
    if "writeThrough" in disk:
        block["write_through"] = bool(disk["writeThrough"])
    if disk.get("ioShareLevel") == "custom" and disk.get("ioShareCount") is not None:
        block["io_share_count"] = disk["ioShareCount"]
    return block


# Template settings repeated as-is in every VM. Sizes are only repeated when the
# build keeps the template's size: cores per socket must divide a CPU count
# someone typed in, and a set memory is the person's choice.
_SIZE_SETTINGS = ("num_cores_per_socket",)


def template_settings(template: Dict[str, Any], keep_size: bool) -> Dict[str, Any]:
    settings = dict(template.get("settings") or {})
    if not keep_size:
        for key in _SIZE_SETTINGS:
            settings.pop(key, None)
    for key in ("cpu", "memory"):
        # A share count only means something for a custom share level; for the
        # named levels vCenter derives it and the provider leaves it alone.
        if settings.get(f"{key}_share_level") != "custom":
            settings.pop(f"{key}_share_count", None)
    return settings


def render(
    *,
    cluster_name: str,
    build_id: int,
    spec: Dict[str, Any],
    nodes: List[Dict[str, Any]],
    network_range: Dict[str, Any],
    allow_unverified_ssl: bool = False,
) -> Dict[str, Any]:
    """main.tf.json for ``nodes`` — [{name, role, ip, cpu, memoryGb, diskGb}].

    ``role`` is the template role key: loadbalancer | controlPlane | worker.
    """
    template = spec.get("template") or {}
    disks = template.get("disks") or [{"unit": 0, "sizeGb": 0, "thin": True, "eagerlyScrub": False}]
    prefix = ipaddress.ip_network(network_range["cidr"]).prefixlen
    domain = network_range.get("dnsDomain") or "localdomain"
    dns_servers = [item for item in network_range.get("dnsServers") or [] if item]

    for_each = {
        node["name"]: {
            "role": node["role"],
            "ip": node["ip"],
            "cpu": int(node["cpu"]),
            # Exact MB, so a clone that keeps the template's size asks for
            # precisely what the template has (and nothing is reconfigured).
            "memory_mb": int(node.get("memoryMb") or int(node["memoryGb"]) * 1024),
            "disk_gb": max(int(node["diskGb"]), int(disks[0].get("sizeGb") or 0)),
        }
        for node in nodes
    }

    disk_blocks = [_disk_block(0, disks[0], "${each.value.disk_gb}")]
    disk_blocks += [
        _disk_block(index, disk, int(disk.get("sizeGb") or 1))
        for index, disk in enumerate(disks[1:], start=1)
    ]

    # Every network card the template has, in its order: the first joins the
    # build's network, the others stay where the template put them. A card
    # left out would be removed after the clone.
    template_nics = template.get("nics") or [{}]
    nic_blocks = [{
        "network_id": spec["networkId"],
        "adapter_type": template.get("nicType") or template_nics[0].get("type") or "vmxnet3",
    }]
    for nic in template_nics[1:]:
        if nic.get("networkId"):
            nic_blocks.append({"network_id": nic["networkId"], "adapter_type": nic.get("type") or "vmxnet3"})

    customize: Dict[str, Any] = {
        "timeout": CUSTOMIZE_TIMEOUT_MIN,
        "linux_options": [{"host_name": "${each.key}", "domain": domain}],
        # One entry per card (vCenter requires it): the build's address on the
        # first, DHCP on any other.
        "network_interface": [{"ipv4_address": "${each.value.ip}", "ipv4_netmask": prefix}]
        + [{} for _ in nic_blocks[1:]],
        "ipv4_gateway": network_range["gateway"],
    }
    if dns_servers:
        customize["dns_server_list"] = dns_servers
        customize["dns_suffix_list"] = [domain]

    vm: Dict[str, Any] = {
        "for_each": for_each,
        "name": "${each.key}",
        "resource_pool_id": spec["resourcePoolId"],
        "datastore_id": spec["datastoreId"],
        "folder": ("${vsphere_folder.cluster.path}" if creates_folder(spec)
                   else folder_path(spec, cluster_name)),
        "num_cpus": "${each.value.cpu}",
        "memory": "${each.value.memory_mb}",
        "guest_id": template.get("guestId") or "otherLinux64Guest",
        "firmware": template.get("firmware") or "bios",
        "scsi_type": template.get("scsiType") or "pvscsi",
        "annotation": (
            f"Created by KubeSight for cluster {cluster_name} (build #{build_id}). "
            "Managed by OpenTofu: change it from KubeSight, not by hand."
        ),
        "wait_for_guest_net_timeout": GUEST_NET_TIMEOUT_MIN,
        "network_interface": nic_blocks,
        "disk": disk_blocks,
        "clone": [{"template_uuid": template["uuid"], "customize": [customize]}],
    }
    # Repeat the template so the provider's post-clone reconfigure changes
    # nothing: its settings (the note included — KubeSight writes none of its
    # own then), its controllers, CD drives and vTPM.
    keep_size = spec.get("sizeMode") == "template"
    settings = template_settings(template, keep_size)
    if settings:
        vm.update(settings)
    controllers = template.get("controllers") or {}
    for kind in ("scsi", "sata", "ide", "nvme"):
        if kind in controllers:
            vm[f"{kind}_controller_count"] = int(controllers[kind])
    cdroms = []
    for cdrom in template.get("cdroms") or []:
        if cdrom.get("datastoreId") and cdrom.get("path"):
            cdroms.append({"datastore_id": cdrom["datastoreId"], "path": cdrom["path"]})
        else:
            cdroms.append({"client_device": True})
    if cdroms:
        vm["cdrom"] = cdroms
    if template.get("vtpm"):
        vm["vtpm"] = [{"version": "2.0"}]
    vm["lifecycle"] = [{
        # KubeSight only creates and deletes these VMs; nothing it repeated
        # from the template should ever plan a change to a running one.
        "ignore_changes": sorted({
            "clone", "annotation", "guest_id", "firmware", "scsi_type",
            "disk", "network_interface", "cdrom", "vtpm", *settings,
            *(f"{kind}_controller_count" for kind in controllers),
        }),
    }]

    resources: Dict[str, Any] = {}
    if creates_folder(spec):
        resources["vsphere_folder"] = {
            "cluster": {
                "path": folder_path(spec, cluster_name),
                "type": "vm",
                "datacenter_id": spec["datacenterId"],
            }
        }
    if for_each:
        resources["vsphere_virtual_machine"] = {"node": vm}

    rules: Dict[str, Any] = {}
    if spec.get("antiAffinity", True) and spec.get("clusterId"):
        for role, rule_key, label in (
            ("controlPlane", "control_planes", "control-planes"),
            ("loadbalancer", "load_balancers", "load-balancers"),
        ):
            names = [node["name"] for node in nodes if node["role"] == role]
            if len(names) < 2:
                continue
            rules[rule_key] = {
                "name": f"kubesight-{cluster_name}-{label}",
                "compute_cluster_id": spec["clusterId"],
                "virtual_machine_ids": [
                    "${" + vm_address(name) + ".id}" for name in names
                ],
            }
    if rules:
        resources["vsphere_compute_cluster_vm_anti_affinity_rule"] = rules

    return {
        "terraform": {
            "required_providers": {
                "vsphere": {"source": PROVIDER_SOURCE, "version": f"= {PROVIDER_VERSION}"}
            },
            "backend": {"http": {}},
        },
        "provider": {"vsphere": {"allow_unverified_ssl": bool(allow_unverified_ssl)}},
        "resource": resources,
    }


def planned_vm_names(config: Dict[str, Any]) -> List[str]:
    node = ((config.get("resource") or {}).get("vsphere_virtual_machine") or {}).get("node") or {}
    return sorted((node.get("for_each") or {}).keys())


def cli_config(mirror_path: str) -> str:
    """Install the vSphere provider only from the copy baked into the image."""
    return (
        "provider_installation {\n"
        "  filesystem_mirror {\n"
        f'    path    = "{mirror_path}"\n'
        f'    include = ["{PROVIDER_ADDRESS}"]\n'
        "  }\n"
        "  direct {\n"
        f'    exclude = ["{PROVIDER_ADDRESS}"]\n'
        "  }\n"
        "}\n"
    )
