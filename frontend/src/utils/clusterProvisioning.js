/** Cluster templates and OpenTofu provisioning — the pure half.
 *
 *  The backend stays the authority on every rule here (role counts, sizes,
 *  address ranges, who may approve). These mirror it so the wizard can say
 *  what is wrong while someone is still typing, not after a round trip.
 */

export const ROLE_KEYS = ["loadbalancer", "controlPlane", "worker"];

export const ROLE_TO_NODE = {
  loadbalancer: "loadbalancer",
  controlPlane: "control_plane",
  worker: "worker",
};

export const ROLE_SHORT = { loadbalancer: "lb", controlPlane: "cp", worker: "wk" };

export const ROLE_TITLE = {
  loadbalancer: "Load balancers",
  controlPlane: "Control planes",
  worker: "Workers",
};

export const ROLE_ONE = {
  loadbalancer: "load balancer",
  controlPlane: "control plane",
  worker: "worker",
};

export const DEFAULT_MINIMUMS = {
  loadbalancer: { cpu: 1, memoryGb: 2, diskGb: 20 },
  controlPlane: { cpu: 2, memoryGb: 4, diskGb: 40 },
  worker: { cpu: 2, memoryGb: 4, diskGb: 40 },
};

export const DEFAULT_MAXIMUM = { cpu: 64, memoryGb: 512, diskGb: 4096 };

// ---------------------------------------------------------------------------
// Templates
// ---------------------------------------------------------------------------

export function allTemplates(catalog) {
  return [...(catalog?.builtin || []), ...(catalog?.custom || [])];
}

export function findTemplate(catalog, id) {
  return allTemplates(catalog).find((template) => template.id === id) || null;
}

/** Mirrors templates.normalize_counts on the backend. Empty string = fine. */
export function countsError(counts) {
  if (!counts) return "Choose a template.";
  const { loadbalancer = 0, controlPlane = 0, worker = 0 } = counts;
  if (![1, 3, 5].includes(controlPlane)) {
    return "Control planes must be 1, 3 or 5 — etcd needs an odd number to keep quorum, and 2 is less safe than 1.";
  }
  if (controlPlane === 1 && loadbalancer > 1) return "One control plane takes 0 or 1 load balancer.";
  if (controlPlane > 1 && loadbalancer !== 2) {
    return "Highly available control planes need exactly 2 load balancers for the floating API address.";
  }
  if (worker < 1) return "A cluster needs at least 1 worker.";
  if (worker > 50) return "At most 50 workers in one build.";
  return "";
}

/** One message per role and field that is out of bounds. */
export function sizeErrors(counts, sizes, minimums = DEFAULT_MINIMUMS, maximum = DEFAULT_MAXIMUM) {
  const errors = [];
  ROLE_KEYS.forEach((role) => {
    if (!counts?.[role]) return;
    const size = sizes?.[role] || {};
    const min = minimums[role] || DEFAULT_MINIMUMS[role];
    [["cpu", "vCPU"], ["memoryGb", "GB of memory"], ["diskGb", "GB of disk"]].forEach(([key, unit]) => {
      const value = Number(size[key]);
      if (!Number.isFinite(value) || value < min[key]) {
        errors.push(`${ROLE_TITLE[role]} need at least ${min[key]} ${unit}.`);
      } else if (value > maximum[key]) {
        errors.push(`${ROLE_TITLE[role]}: ${value} ${unit} is more than one VM may have here (${maximum[key]}).`);
      }
    });
  });
  return errors;
}

/** How the Cluster Builder's topology fields read for a shape. */
export function shapeFor(counts) {
  if (!counts) return { topologyType: "stacked_ha", endpointMode: "managed_haproxy" };
  if (counts.controlPlane === 1) {
    return {
      topologyType: "single_cp",
      endpointMode: counts.loadbalancer === 1 ? "managed_haproxy" : "manual_endpoint",
    };
  }
  return { topologyType: "stacked_ha", endpointMode: "managed_haproxy" };
}

export function machineCount(counts) {
  return ROLE_KEYS.reduce((sum, role) => sum + (counts?.[role] || 0), 0);
}

export function totals(counts, sizes) {
  return ROLE_KEYS.reduce((acc, role) => {
    const n = counts?.[role] || 0;
    const size = sizes?.[role] || {};
    return {
      machines: acc.machines + n,
      cpu: acc.cpu + n * (Number(size.cpu) || 0),
      memoryGb: acc.memoryGb + n * (Number(size.memoryGb) || 0),
      diskGb: acc.diskGb + n * (Number(size.diskGb) || 0),
    };
  }, { machines: 0, cpu: 0, memoryGb: 0, diskGb: 0 });
}

/** A short shape label: "1 load balancer · 1 control plane · 2 workers". */
export function shapeLabel(counts) {
  return ROLE_KEYS
    .filter((role) => counts?.[role])
    .map((role) => `${counts[role]} ${ROLE_ONE[role]}${counts[role] === 1 ? "" : "s"}`)
    .join(" · ");
}

/** The VM names a build will get, in the order the backend makes them. */
export function machineNames(clusterName, counts) {
  const out = [];
  ROLE_KEYS.forEach((role) => {
    for (let index = 1; index <= (counts?.[role] || 0); index += 1) {
      out.push({ name: `${clusterName || "cluster"}-${ROLE_SHORT[role]}-${index}`, role });
    }
  });
  return out;
}

/** Names with the addresses a preview handed back (VIP first when there is one). */
export function previewMachines(clusterName, counts, sizes, addresses = []) {
  const hasVip = (counts?.loadbalancer || 0) > 0;
  const pool = [...addresses];
  const vip = hasVip ? pool.shift() || null : null;
  const machines = machineNames(clusterName, counts).map((machine) => ({
    ...machine,
    ip: pool.shift() || null,
    cpu: sizes?.[machine.role]?.cpu,
    memoryGb: sizes?.[machine.role]?.memoryGb,
    diskGb: sizes?.[machine.role]?.diskGb,
  }));
  const primary = machines.find((machine) => machine.role === "controlPlane");
  return {
    vip,
    endpoint: vip ? `${vip}:6443` : primary?.ip ? `${primary.ip}:6443` : "",
    machines,
  };
}

const NAME_RE = /^[a-z0-9]([a-z0-9-]{0,48}[a-z0-9])?$/;

export function vmwareNameError(name) {
  if (!name) return "";
  return NAME_RE.test(name)
    ? ""
    : "Lowercase letters, digits and hyphens, at most 50 characters — this becomes the VMs' names and hostnames.";
}

/** Template ids → which saved template a build came from, for labels. */
export function templateName(catalog, id) {
  if (!id) return null;
  if (id === "custom") return "Custom";
  return findTemplate(catalog, id)?.name || null;
}

// ---------------------------------------------------------------------------
// Placement
// ---------------------------------------------------------------------------

export function datastoreFit(datastore, neededGb) {
  if (!datastore) return { tone: "plain", usedPct: 0, needPct: 0, text: "" };
  const capacity = Math.max(datastore.capacityGb || 0, 1);
  const used = Math.max(capacity - (datastore.freeGb || 0), 0);
  const usedPct = Math.min((used / capacity) * 100, 100);
  const needPct = Math.min((neededGb / capacity) * 100, 100 - usedPct);
  const over = neededGb > (datastore.freeGb || 0);
  return {
    tone: over ? "warn" : "ok",
    usedPct,
    needPct,
    over,
    text: over
      ? `${datastore.freeGb} GB free; thin disks for this cluster can grow to ${neededGb} GB`
      : `${datastore.freeGb} GB free · this cluster up to ${neededGb} GB`,
  };
}

/** Smallest disk a role may ask for: its own minimum, or the template's disk. */
export function minimumDisk(role, template, minimums = DEFAULT_MINIMUMS) {
  const templateDisk = template?.disks?.[0]?.sizeGb || 0;
  return Math.max(minimums[role]?.diskGb || 0, templateDisk);
}

// ---------------------------------------------------------------------------
// Plans
// ---------------------------------------------------------------------------

const KIND_ORDER = [
  ["vm", "Virtual machines"],
  ["folder", "Folder"],
  ["rule", "Placement rules"],
  ["other", "Other"],
];

export function planGroups(summary) {
  const resources = summary?.resources || [];
  return KIND_ORDER
    .map(([kind, label]) => ({ kind, label, items: resources.filter((r) => r.kind === kind) }))
    .filter((group) => group.items.length);
}

export const ACTION_SIGN = { create: "+", delete: "−", update: "~", replace: "±" };

// ---------------------------------------------------------------------------
// Progress
// ---------------------------------------------------------------------------

export const PROVISION_STATUS_LABELS = {
  planning: "Making a plan",
  planned: "Plan ready",
  plan_failed: "Plan failed",
  applying: "Creating VMs",
  connecting: "Waiting for SSH",
  apply_failed: "VM creation failed",
  connect_failed: "VMs not reachable",
  ready: "VMs created",
  grow_planning: "Planning new workers",
  grow_planned: "Worker plan ready",
  grow_plan_failed: "Worker plan failed",
  grow_applying: "Creating workers",
  grow_connecting: "Waiting for SSH",
  grow_failed: "Adding workers failed",
  destroy_planning: "Planning the destroy",
  destroy_pending: "Destroy waiting for approval",
  destroy_plan_failed: "Destroy plan failed",
  destroying: "Destroying",
  destroy_failed: "Destroy failed",
  destroyed: "Destroyed",
};

export const ACTIVE_JOB_STATUSES = new Set(["planning", "applying", "connecting"]);

/** Whether a build should be polled: anything OpenTofu or the phase machine is doing. */
export function provisioningActive(build) {
  const job = build?.provisioning?.job;
  return Boolean(
    ["provisioning", "destroying"].includes(build?.status)
    || (job && ACTIVE_JOB_STATUSES.has(job.status))
  );
}

/** The four-cell rail of a create or grow: Plan · Create VMs · SSH · Kubernetes. */
export function provisionRail(build) {
  const job = build?.provisioning?.job;
  const status = job?.status;
  const cells = [
    { key: "plan", label: "Plan" },
    { key: "create", label: job?.operation === "grow" ? "Create workers" : "Create VMs" },
    { key: "ssh", label: "Reach over SSH" },
    { key: "kubernetes", label: "Kubernetes" },
  ];
  const reached = {
    planning: 0, planned: 0, plan_failed: 0,
    applying: 1, apply_failed: 1, interrupted: 1,
    connecting: 2, connect_failed: 2,
    succeeded: 3,
  }[status] ?? 0;
  const failed = ["plan_failed", "apply_failed", "connect_failed"].includes(status);
  const waiting = status === "planned";
  const k8sDone = build?.status === "completed";
  return cells.map((cell, index) => {
    let state = "todo";
    if (index < reached) state = "done";
    else if (index === reached) {
      state = failed ? "fail" : waiting ? "wait" : "now";
      if (index === 3) state = k8sDone ? "done" : ["failed", "preflight_failed"].includes(build?.status) ? "fail" : "now";
    }
    return { ...cell, state };
  });
}

const VM_STATE_TEXT = {
  waiting: "Waiting",
  exists: "Already exists",
  creating: "Cloning and customizing",
  created: "Created",
  connecting: "Waiting for SSH",
  ready: "SSH answers",
  unreachable: "No SSH answer",
  failed: "Failed",
  destroying: "Deleting",
  destroyed: "Deleted",
};

const VM_STATE_TONE = {
  ready: "ok", created: "ok", destroyed: "ok", exists: "plain",
  failed: "bad", unreachable: "bad",
  creating: "live", connecting: "live", destroying: "live",
  waiting: "plain",
};

/** Rows for the per-VM table: every machine the build has, with live state. */
export function vmRows(build) {
  const machines = build?.provisioning?.spec?.machines || [];
  const job = build?.provisioning?.job;
  const vms = job?.progress?.vms || {};
  const names = new Set([...machines.map((m) => m.name), ...Object.keys(vms)]);
  const byName = Object.fromEntries(machines.map((m) => [m.name, m]));
  return [...names].map((name) => {
    const machine = byName[name] || { name };
    const progress = vms[name] || {};
    const state = progress.state || (build?.provisioning?.state?.vmCount ? "created" : "waiting");
    return {
      name,
      role: machine.role || "worker",
      ip: machine.ip || "",
      size: machine.cpu ? `${machine.cpu} vCPU · ${machine.memoryGb} GB · ${machine.diskGb} GB` : "",
      state,
      label: VM_STATE_TEXT[state] || state,
      tone: VM_STATE_TONE[state] || "plain",
      elapsed: progress.elapsed || null,
      error: progress.error || null,
    };
  }).sort((a, b) => ROLE_KEYS.indexOf(a.role) - ROLE_KEYS.indexOf(b.role) || a.name.localeCompare(b.name));
}

/** Slot states for the Blueprint while OpenTofu works. */
export function vmSlotState(row) {
  if (!row) return "set";
  if (["failed", "unreachable"].includes(row.state)) return "failed";
  if (["ready"].includes(row.state)) return "joined";
  if (["creating", "connecting", "destroying"].includes(row.state)) return "live";
  if (row.state === "waiting") return "waiting";
  return "set";
}

/** Who can act on a destroy request, from where the viewer stands. */
export function destroyStance(job, currentUserId) {
  if (!job || job.operation !== "destroy") return "none";
  if (job.status !== "awaiting_approval") return "none";
  if (currentUserId != null && job.requestedByUserId === currentUserId) return "requester";
  return "approver";
}

// ---------------------------------------------------------------------------
// From a placement read to a build payload
// ---------------------------------------------------------------------------

export const EMPTY_VM_PLACEMENT = {
  connectionId: "",
  datacenterId: "",
  clusterId: "",
  resourcePoolId: "",
  folderParentId: "",
  datastoreId: "",
  networkId: "",
  templateId: "",
  antiAffinity: true,
};

/** Fill the blanks from what vCenter offers: the likeliest sane choice for each. */
export function defaultPlacement(placement, current = EMPTY_VM_PLACEMENT, ranges = []) {
  const next = { ...current };
  const datacenters = placement?.datacenters || [];
  const dc = datacenters.find((d) => d.id === next.datacenterId) || datacenters[0];
  if (!dc) return next;
  next.datacenterId = dc.id;
  if (!dc.clusters.some((c) => c.id === next.clusterId)) {
    const cluster = dc.clusters.find((c) => c.drsEnabled && c.hostCount > 1) || dc.clusters[0];
    next.clusterId = cluster?.id || "";
    next.resourcePoolId = "";
  }
  if (next.folderParentId && !dc.folders.some((f) => f.id === next.folderParentId)) next.folderParentId = "";
  if (!current.folderParentId && !current.datastoreId) {
    const kubesight = dc.folders.find((f) => f.path.toLowerCase() === "kubesight");
    if (kubesight) next.folderParentId = kubesight.id;
  }
  if (!dc.datastores.some((d) => d.id === next.datastoreId)) {
    const best = [...dc.datastores].filter((d) => d.accessible !== false)
      .sort((a, b) => (b.freeGb || 0) - (a.freeGb || 0))[0];
    next.datastoreId = best?.id || "";
  }
  if (!dc.networks.some((n) => n.id === next.networkId)) {
    const withRange = dc.networks.find((n) => ranges.some((r) => r.networkName === n.name));
    next.networkId = withRange?.id || "";
  }
  if (!dc.templates.some((t) => t.id === next.templateId)) {
    const ok = dc.templates.find((t) => t.compatibility?.status === "ok")
      || dc.templates.find((t) => t.compatibility?.status === "warn");
    next.templateId = ok?.id || "";
  }
  return next;
}

/** Look up every chosen object; null where something is not chosen or gone. */
export function resolvePlacement(placement, vm, ranges = []) {
  const dc = (placement?.datacenters || []).find((d) => d.id === vm.datacenterId) || null;
  const cluster = dc?.clusters.find((c) => c.id === vm.clusterId) || null;
  const pool = cluster?.resourcePools.find((p) => p.id === vm.resourcePoolId) || null;
  const folder = dc?.folders.find((f) => f.id === vm.folderParentId) || null;
  const datastore = dc?.datastores.find((d) => d.id === vm.datastoreId) || null;
  const network = dc?.networks.find((n) => n.id === vm.networkId) || null;
  const template = dc?.templates.find((t) => t.id === vm.templateId) || null;
  const range = network ? ranges.find((r) => r.networkName === network.name) || null : null;
  return { dc, cluster, pool, folder, datastore, network, template, range };
}

/** What is still missing before a plan can be made, as one sentence. */
export function placementProblem(resolved, counts) {
  if (!resolved.dc) return "Choose a datacenter.";
  if (!resolved.cluster) return "Choose the vSphere cluster the VMs run on.";
  if (!resolved.datastore) return "Choose a datastore.";
  if (!resolved.network) return "Choose the network the VMs connect to.";
  if (!resolved.range) return `${resolved.network.name} has no address range. An administrator adds one in Sources.`;
  if (!resolved.template) return "Choose the VM template to clone.";
  if (resolved.template.compatibility?.status === "bad") {
    const bad = resolved.template.compatibility.checks.find((c) => c.status === "bad");
    return `${resolved.template.name} cannot be used: ${bad?.label} — ${bad?.detail}.`;
  }
  const needed = machineCount(counts) + ((counts?.loadbalancer || 0) ? 1 : 0);
  const free = (resolved.range.size || 0) - (resolved.range.reservedCount || 0) - (resolved.range.inUseCount || 0);
  if (free < needed) return `${resolved.range.networkName} has ${free} free addresses and this cluster needs ${needed}.`;
  return "";
}

/** The ``provisioning`` object the backend's normalize_spec reads. */
export function provisioningPayload(connectionId, resolved, vm, counts, sizes) {
  const { dc, cluster, pool, folder, datastore, network, template } = resolved;
  return {
    vsphereConnectionId: Number(connectionId),
    datacenterId: dc?.id,
    datacenterName: dc?.name,
    clusterId: cluster?.id,
    clusterName: cluster?.name,
    resourcePoolId: pool?.id || cluster?.rootResourcePoolId,
    resourcePoolName: pool?.path || "",
    folderParentId: folder?.id || null,
    folderParent: folder?.path || "",
    datastoreId: datastore?.id,
    datastoreName: datastore?.name,
    networkId: network?.id,
    networkName: network?.name,
    template,
    counts,
    sizes,
    antiAffinity: vm.antiAffinity !== false,
  };
}

/** A VMware build's saved spec, read back into the wizard's choices. */
export function placementFromSpec(spec) {
  if (!spec) return { ...EMPTY_VM_PLACEMENT };
  return {
    connectionId: spec.vsphereConnectionId ? String(spec.vsphereConnectionId) : "",
    datacenterId: spec.datacenterId || "",
    clusterId: spec.clusterId || "",
    resourcePoolId: spec.resourcePoolName ? spec.resourcePoolId || "" : "",
    folderParentId: spec.folderParentId || "",
    datastoreId: spec.datastoreId || "",
    networkId: spec.networkId || "",
    templateId: spec.template?.id || "",
    antiAffinity: spec.antiAffinity !== false,
  };
}
