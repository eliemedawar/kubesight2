/** Oracle Cloud, AWS and Google Cloud machines and clusters — the pure half.
 *
 *  The backend stays the authority (clouds/*.py normalize every field). These
 *  mirror its rules so the wizard can say what is missing while someone is
 *  still choosing, and turn the wizard's choices into the build payload.
 *
 *  Two facts shape everything here:
 *    * On a cloud, a load-balancer count above 0 means "put the cloud's own
 *      internal load balancer in front of the API". No load-balancer VMs are
 *      created — a keepalived VIP cannot float on a cloud network.
 *    * The cloud picks each VM's private address. There is no range or VIP to
 *      choose, and no public address is ever asked for.
 */

import { ROLE_KEYS, ROLE_ONE, ROLE_SHORT, VM_ROLE } from "./clusterProvisioning.js";

export const CLOUD_PROVIDERS = ["oci", "aws", "gcp"];

export const PROVIDER_LABELS = {
  oci: "Oracle Cloud",
  aws: "AWS",
  gcp: "Google Cloud",
};

export const MANAGED_LABELS = {
  oci: "Oracle OKE",
  aws: "Amazon EKS",
  gcp: "Google GKE",
};

/** What the provider calls a zone: the wizard's field label. */
export const ZONE_LABELS = {
  oci: "Availability domain",
  aws: "Availability zone",
  gcp: "Zone",
};

export const NETWORK_LABELS = { oci: "VCN", aws: "VPC", gcp: "VPC network" };

export const DISK_TYPES = {
  aws: [["gp3", "gp3 (general purpose SSD)"], ["gp2", "gp2"], ["io2", "io2 (provisioned IOPS)"]],
  gcp: [["pd-balanced", "Balanced persistent disk"], ["pd-ssd", "SSD persistent disk"], ["pd-standard", "Standard persistent disk"]],
};

export const DEFAULT_DISK_TYPE = { aws: "gp3", gcp: "pd-balanced" };
/** The disk type to send: the chosen one when it belongs to this cloud. */
export function diskTypeFor(provider, chosen) {
  const known = (DISK_TYPES[provider] || []).map(([id]) => id);
  return known.includes(chosen) ? chosen : DEFAULT_DISK_TYPE[provider] || "";
}

export const DEFAULT_OCI_SHAPE = "VM.Standard.E5.Flex";
export const DEFAULT_GKE_MASTER_CIDR = "172.16.0.32/28";

export function isCloud(machineSource) {
  return CLOUD_PROVIDERS.includes(machineSource);
}

export function providerLabel(provider) {
  return PROVIDER_LABELS[provider] || provider || "";
}

export function managedLabel(provider) {
  return MANAGED_LABELS[provider] || "Managed Kubernetes";
}

/** "AWS · eu-west-1" — where a cloud build lives, in one line. */
export function cloudWhere(cloud) {
  if (!cloud) return "";
  return [cloud.providerLabel || providerLabel(cloud.provider), cloud.region, cloud.zone]
    .filter(Boolean).join(" · ");
}

// ---------------------------------------------------------------------------
// Networks
// ---------------------------------------------------------------------------

const CIDR_RE = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/;

/** Split free text into CIDRs: commas, spaces or new lines between them. */
export function parseCidrs(text) {
  const seen = [];
  String(text || "").split(/[\s,]+/).map((part) => part.trim()).filter(Boolean)
    .forEach((part) => { if (!seen.includes(part)) seen.push(part); });
  return seen;
}

/** One message for the first CIDR the backend would refuse; "" when fine. */
export function cidrError(cidrs) {
  const list = Array.isArray(cidrs) ? cidrs : parseCidrs(cidrs);
  if (!list.length) {
    return "Give at least one network allowed to reach SSH and the Kubernetes API — the one KubeSight connects from.";
  }
  for (const cidr of list) {
    const match = CIDR_RE.exec(cidr);
    if (!match) return `${cidr} is not an IPv4 network such as 10.20.0.0/16.`;
    const octets = match.slice(1, 5).map(Number);
    const prefix = Number(match[5]);
    if (octets.some((n) => n > 255) || prefix > 32) return `${cidr} is not an IPv4 network such as 10.20.0.0/16.`;
    if (prefix === 0) return "0.0.0.0/0 would open SSH and the API to everyone. Name your own network.";
  }
  return "";
}

/** The subnets of the chosen network, each with the network it belongs to. */
export function subnetsOf(inventory, networkId) {
  const network = (inventory?.networks || []).find((n) => n.id === networkId);
  return network?.subnets || [];
}

/** Whether the wizard asks for a zone: every VM build, and a zonal (not
    regional) GKE cluster, whose control plane lives in one zone. */
export function needsZone(provider, managed, cloud = {}) {
  if (!managed) return true;
  return provider === "gcp" && !cloud.regional;
}

/** EKS needs subnets in at least two availability zones. "" when it has them. */
export function awsManagedSubnetError(subnets, chosenIds) {
  const chosen = (subnets || []).filter((s) => (chosenIds || []).includes(s.id));
  if (chosen.length < 2) return "Amazon EKS needs at least 2 subnets, in different availability zones.";
  const zones = new Set(chosen.map((s) => s.zone).filter(Boolean));
  if (zones.size < 2) return "Amazon EKS needs subnets in at least 2 different availability zones.";
  return "";
}

// ---------------------------------------------------------------------------
// The wizard's cloud choices
// ---------------------------------------------------------------------------

export const EMPTY_CLOUD = {
  accountId: "",
  region: "",
  compartmentId: "",
  networkId: "",
  subnetId: "",
  zone: "",
  kind: "vms",
  imageId: "",
  kubernetesVersion: "",
  allowedCidrs: "",
  machineTypes: {},
  diskType: "",
  subnetIds: [],
  masterCidr: DEFAULT_GKE_MASTER_CIDR,
  regional: false,
  shape: DEFAULT_OCI_SHAPE,
  nodeImageId: "",
};

/** Fill blanks from what the cloud offers: the likeliest sane choice for each. */
export function defaultCloudChoices(inventory, current = EMPTY_CLOUD, provider = "") {
  const next = { ...current };
  if (!inventory) return next;
  const networks = inventory.networks || [];
  if (!networks.some((n) => n.id === next.networkId)) {
    next.networkId = (networks.find((n) => (n.subnets || []).length) || networks[0])?.id || "";
    next.subnetId = "";
  }
  const subnets = subnetsOf(inventory, next.networkId);
  if (!subnets.some((s) => s.id === next.subnetId)) next.subnetId = subnets[0]?.id || "";
  const subnet = subnets.find((s) => s.id === next.subnetId);
  const zones = inventory.zones || [];
  // A zonal subnet (AWS) decides the zone; a regional one leaves it free.
  if (subnet?.zone) next.zone = subnet.zone;
  else if (!zones.some((z) => z.id === next.zone)) next.zone = zones[0]?.id || "";
  const images = inventory.images || [];
  if (!images.some((i) => i.id === next.imageId)) {
    next.imageId = (images.find((i) => i.osFamily === "debian") || images[0])?.id || "";
  }
  const versions = inventory.kubernetesVersions || [];
  if (!versions.includes(next.kubernetesVersion)) next.kubernetesVersion = versions[0] || "";
  if (!next.allowedCidrs) {
    const network = networks.find((n) => n.id === next.networkId);
    next.allowedCidrs = network?.cidr || subnet?.cidr || "";
  }
  if (DEFAULT_DISK_TYPE[provider]) next.diskType = diskTypeFor(provider, next.diskType);
  if (provider === "oci" && !next.compartmentId) {
    next.compartmentId = inventory.compartments?.[0]?.id || "";
  }
  return next;
}

/** Look up every chosen object; null where nothing is chosen or it is gone. */
export function resolveCloud(inventory, cloud) {
  const network = (inventory?.networks || []).find((n) => n.id === cloud.networkId) || null;
  const subnet = (network?.subnets || []).find((s) => s.id === cloud.subnetId) || null;
  const zone = (inventory?.zones || []).find((z) => z.id === cloud.zone) || null;
  const image = (inventory?.images || []).find((i) => i.id === cloud.imageId) || null;
  return { network, subnet, zone, image };
}

/** What is still missing before a plan can be made, as one sentence. */
export function cloudProblem({ provider, account, cloud, inventory, resolved, vmsOnly = false }) {
  if (!account) return "Choose the cloud account to create them with.";
  if (!cloud.region) return "Choose a region.";
  if (provider === "oci" && !cloud.compartmentId) return "Choose the compartment.";
  if (!inventory) return "Reading the cloud…";
  if (!resolved.network) return `Choose the ${NETWORK_LABELS[provider] || "network"}.`;
  if (!resolved.subnet) return "Choose the subnet the machines go in.";
  const managed = !vmsOnly && cloud.kind === "managed";
  if (needsZone(provider, managed, cloud) && !resolved.subnet.zone && !cloud.zone) return `Choose the ${(ZONE_LABELS[provider] || "zone").toLowerCase()}.`;
  if (!managed && !resolved.image) return "Choose the image the machines start from.";
  if (managed && !cloud.kubernetesVersion) return "Choose the Kubernetes version.";
  if (managed && provider === "aws") {
    const error = awsManagedSubnetError(resolved.network.subnets, cloud.subnetIds);
    if (error) return error;
  }
  return cidrError(cloud.allowedCidrs);
}

/** The ``provisioning`` object the backend's cloud normalize_spec reads. */
export function cloudProvisioningPayload({ provider, cloud, resolved, counts, sizes, vmsOnly = false }) {
  const managed = !vmsOnly && cloud.kind === "managed";
  const { network, subnet, image } = resolved;
  const payload = {
    cloudAccountId: Number(cloud.accountId),
    region: cloud.region,
    zone: subnet?.zone || cloud.zone || null,
    kind: managed ? "managed" : "vms",
    networkId: network?.id,
    networkName: network?.name,
    networkCidr: network?.cidr || "",
    subnetId: subnet?.id,
    subnetName: subnet?.name,
    subnetCidr: subnet?.cidr || "",
    image: managed || !image ? null : { id: image.id, name: image.name, osFamily: image.osFamily },
    counts: vmsOnly ? null : managed ? { loadbalancer: 0, controlPlane: 0, worker: counts?.worker || 0 } : counts,
    vmCount: vmsOnly ? counts?.vm || 0 : null,
    sizes: vmsOnly ? { vm: sizes?.vm } : managed ? { worker: sizes?.worker } : sizes,
    machineTypes: Object.fromEntries(
      Object.entries(cloud.machineTypes || {}).filter(([, type]) => Boolean(type))
    ),
    allowedCidrs: parseCidrs(cloud.allowedCidrs),
    managed: managed ? { kubernetesVersion: cloud.kubernetesVersion } : null,
  };
  if (provider === "aws") {
    payload.diskType = diskTypeFor("aws", cloud.diskType);
    if (managed) payload.subnetIds = [...(cloud.subnetIds || [])];
  } else if (provider === "gcp") {
    payload.diskType = diskTypeFor("gcp", cloud.diskType);
    if (managed) {
      payload.masterCidr = cloud.masterCidr || DEFAULT_GKE_MASTER_CIDR;
      payload.regional = Boolean(cloud.regional);
    }
  } else if (provider === "oci") {
    payload.compartmentId = cloud.compartmentId;
    payload.shape = cloud.shape || DEFAULT_OCI_SHAPE;
    if (managed && cloud.nodeImageId) payload.nodeImageId = cloud.nodeImageId;
  }
  return payload;
}

/** A cloud build's saved spec, read back into the wizard's choices. */
export function cloudFromSpec(spec) {
  if (!spec) return { ...EMPTY_CLOUD };
  return {
    ...EMPTY_CLOUD,
    accountId: spec.cloudAccountId ? String(spec.cloudAccountId) : "",
    region: spec.region || "",
    compartmentId: spec.compartmentId || "",
    networkId: spec.networkId || "",
    subnetId: spec.subnetId || "",
    zone: spec.zone || "",
    kind: spec.kind === "managed" ? "managed" : "vms",
    imageId: spec.image?.id || "",
    kubernetesVersion: spec.managed?.kubernetesVersion || "",
    allowedCidrs: (spec.allowedCidrs || []).join(", "),
    machineTypes: { ...(spec.machineTypes || {}) },
    diskType: spec.diskType || "",
    subnetIds: [...(spec.subnetIds || [])],
    masterCidr: spec.masterCidr || DEFAULT_GKE_MASTER_CIDR,
    regional: Boolean(spec.regional),
    shape: spec.shape || DEFAULT_OCI_SHAPE,
    nodeImageId: spec.nodeImageId || "",
  };
}

// ---------------------------------------------------------------------------
// Machines and the drawing
// ---------------------------------------------------------------------------

/** The roles that become VMs on a cloud: never load balancers. */
export function cloudMachineRoles(counts) {
  return [...ROLE_KEYS, VM_ROLE].filter((role) => role !== "loadbalancer" && counts?.[role]);
}

/** The VM names a cloud build will get (no load-balancer VMs). */
export function cloudMachines(clusterName, counts, sizes, resolvedTypes = {}) {
  const out = [];
  cloudMachineRoles(counts).forEach((role) => {
    for (let index = 1; index <= counts[role]; index += 1) {
      out.push({
        name: `${clusterName || "cluster"}-${ROLE_SHORT[role]}-${index}`,
        role,
        ip: null,
        cpu: sizes?.[role]?.cpu,
        memoryGb: sizes?.[role]?.memoryGb,
        diskGb: sizes?.[role]?.diskGb,
        machineType: resolvedTypes?.[role]?.type || null,
      });
    }
  });
  return out;
}

/** "1 control plane · 2 workers · cloud load balancer". */
export function cloudShapeLabel(counts) {
  const parts = cloudMachineRoles(counts)
    .map((role) => `${counts[role]} ${ROLE_ONE[role]}${counts[role] === 1 ? "" : "s"}`);
  if (counts?.loadbalancer) parts.push("cloud load balancer");
  return parts.join(" · ");
}

/** Totals of the VMs a cloud build creates (load balancers excluded). */
export function cloudTotals(counts, sizes) {
  return cloudMachineRoles(counts).reduce((acc, role) => {
    const n = counts[role];
    const size = sizes?.[role] || {};
    return {
      machines: acc.machines + n,
      cpu: acc.cpu + n * (Number(size.cpu) || 0),
      memoryGb: acc.memoryGb + n * (Number(size.memoryGb) || 0),
      diskGb: acc.diskGb + n * (Number(size.diskGb) || 0),
    };
  }, { machines: 0, cpu: 0, memoryGb: 0, diskGb: 0 });
}

/** The Blueprint's API bar on a cloud: the cloud load balancer, or the first
    control plane when there is none. */
export function cloudBus({ loadBalancer, endpoint = "", state = "idle" }) {
  if (loadBalancer) {
    return {
      managed: true,
      address: endpoint ? endpoint.replace(/:6443$/, "") : "cloud load balancer",
      port: "6443",
      label: "Cloud LB",
      state,
    };
  }
  return { managed: false, address: endpoint || "first control plane", port: "", label: "Endpoint", state };
}

/** The drawing of a managed cluster: the cloud's control plane, then the node pool. */
export function managedBlueprint({ provider, count, machineType = "", state = "outline", nodeState = "set", endpoint = "" }) {
  const slots = [];
  for (let index = 1; index <= (count || 0); index += 1) {
    slots.push({
      key: `pool-${index}`, name: `node ${index}`, host: null,
      sub: machineType || "size on plan", state: nodeState, stamp: null, tie: false,
    });
  }
  return {
    state,
    conflictHosts: [],
    bus: { managed: true, address: endpoint || "private endpoint", port: "", label: "API", state: "idle" },
    tiers: [
      {
        role: "control_plane", label: "Control plane", target: 0, filled: 1,
        slots: [{
          key: "managed-cp", name: managedLabel(provider), host: null,
          sub: "run by the cloud", state: nodeState === "joined" ? "joined" : "set", stamp: null, tie: false,
        }],
      },
      { role: "worker", label: "Node pool", target: count || 0, filled: slots.length, slots },
    ],
  };
}

/** Relabel a drawing's tiers for a cloud: VM tiers stay, and the API bar is the cloud's. */
export function withCloudBus(plan, { loadBalancer, endpoint, state }) {
  if (!plan) return plan;
  return {
    ...plan,
    bus: cloudBus({ loadBalancer, endpoint, state: state || plan.bus?.state || "idle" }),
    tiers: plan.tiers.filter((tier) => tier.role !== "loadbalancer"),
  };
}

// ---------------------------------------------------------------------------
// Day two, labels
// ---------------------------------------------------------------------------

/** Kubernetes shapes for cloud VMs that already exist: only those without a
    load balancer (adding a cloud load balancer later is not offered yet). */
export function cloudShapesForVms(shapes) {
  return (shapes || []).filter((shape) => !shape.counts?.loadbalancer);
}

/** Plan rows grouped by kind, with the kinds a cloud plan adds. */
export const CLOUD_KIND_LABELS = {
  vm: "Virtual machines",
  lb: "Load balancer",
  network: "Network and firewall",
  cluster: "Managed cluster",
  nodepool: "Node pool",
  iam: "Access roles",
  folder: "Folder",
  rule: "Placement rules",
  other: "Other",
};

/** Where things are created, for sentences: "in AWS", "in vCenter". */
export function whereCreated(build) {
  if (build?.cloud) return `in ${build.cloud.providerLabel || providerLabel(build.cloud.provider)}`;
  return "in vCenter";
}

export function isManagedBuild(build) {
  return build?.cloud?.kind === "managed";
}

/** A machine type for one role, as resolve-sizes answered it. */
export function machineTypeText(entry) {
  if (!entry?.type) return "";
  return entry.note ? `${entry.type} · ${entry.note}` : entry.type;
}

/** Account freshness, for the Sources row: "ok"/"failed"/"untested". */
export function accountState(account) {
  if (account?.lastTestStatus === "ok") return "ok";
  if (account?.lastTestStatus === "failed") return "failed";
  return "untested";
}

/** Settings + secrets for the account form, from the provider's fields. */
export function accountPayload(form, fields) {
  const settings = {};
  const secrets = {};
  (fields || []).forEach((item) => {
    const value = form.values?.[item.key];
    if (item.secret) {
      if (typeof value === "string" && value.trim()) secrets[item.key] = value;
    } else if (value !== undefined && value !== null && String(value).trim() !== "") {
      settings[item.key] = String(value).trim();
    }
  });
  return {
    name: String(form.name || "").trim(),
    provider: form.provider,
    region: String(form.region || "").trim(),
    settings,
    secrets,
    caPem: form.caPem || "",
  };
}

/** The first thing missing in the account form, or "". */
export function accountFormError(form, fields, { editing = false, secretsSet = {} } = {}) {
  if (!String(form.name || "").trim()) return "Name the account.";
  if (!form.provider) return "Choose the cloud.";
  if (!String(form.region || "").trim()) return "Give the default region.";
  for (const item of fields || []) {
    if (!item.required) continue;
    const value = form.values?.[item.key];
    const blank = !(typeof value === "string" ? value.trim() : value);
    if (!blank) continue;
    if (item.secret && editing && secretsSet[item.key]) continue;
    return `${item.label} is required.`;
  }
  return "";
}
