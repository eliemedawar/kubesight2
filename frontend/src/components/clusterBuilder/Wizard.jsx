/** The new-build wizard: Template → Machines → Add-ons → Workloads → Verify
 *  (or, when KubeSight creates the VMs with OpenTofu, Plan & create; and when
 *  it creates VMs only, without Kubernetes, just Template → Machines → Plan).
 *
 *  The old step 1 held eleven fields plus the whole add-on catalog in one grid,
 *  mixing what the cluster *is* with the infrastructure plumbing a build
 *  consumes. Here each step holds one kind of decision, the plumbing lives in a
 *  pre-resolved Sources row, and the Blueprint on the right is the same object
 *  from the first keystroke to the finished cluster.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import Blueprint from "./Blueprint.jsx";
import WorkloadsPicker from "./WorkloadsPicker.jsx";
import AppsPicker from "./AppsPicker.jsx";
import { ProvisionCard } from "./ProvisionPanels.jsx";
import { CountsEditor, TemplateGallery } from "./TemplateStep.jsx";
import VmwareMachines, { useVmwarePlacement } from "./VmwareMachines.jsx";
import { Field } from "./common.jsx";
import PreflightReport from "./PreflightReport.jsx";
import { addonSelectionError, ipRangeListError } from "../../utils/addonConfig.js";
import { appItemsFromBuild, appsError, appsPayload } from "../../utils/clusterApps.js";
import {
  ROLE_LABELS,
  addonProvenance,
  cniPluginsForK8s,
  defaultVersionForK8s,
  draftBlueprint,
  groupChecks,
  hostByAddress,
  machinesBlueprint,
  preferredSources,
  preflightBlueprint,
  versionsForK8s,
  emptyStorage,
  storageErrors,
  storageRows,
  storageSummary,
  workloadSelectionSummary,
} from "../../utils/clusterBuilder.js";
import {
  DEFAULT_MINIMUMS,
  EMPTY_VM_PLACEMENT,
  MAX_VMS,
  cannotResize,
  countsError,
  effectiveSizes,
  defaultPlacement,
  findTemplate,
  machineCount,
  placementFromSpec,
  placementProblem,
  previewMachines,
  provisioningPayload,
  resolvePlacement,
  shapeFor,
  shapeLabel,
  sizeErrors,
  totals,
  vmCountError,
  vmsOnlyCounts,
  vmwareNameError,
  planPollDelayMs,
} from "../../utils/clusterProvisioning.js";
import {
  createClusterBuild,
  getClusterBuild,
  listVSphereVms,
  planClusterVms,
  preflightClusterBuild,
  previewNetworkAddresses,
  startClusterBuild,
  updateClusterBuild,
} from "../../api/clusterBuildsApi.js";
import { whenVisible } from "../../lib/visibleInterval";

/** The steps this build walks, as [step, label]. A VMs-only build has no
    cluster to put add-ons, workloads or Helm charts on, so it skips those. */
function wizardSteps(machineSource, vmsOnly) {
  if (machineSource === "vmware" && vmsOnly) {
    return [[STEP_SHAPE, "Template"], [STEP_MACHINES, "Machines"], [STEP_VERIFY, "Plan & create"]];
  }
  return [
    [STEP_SHAPE, "Template"], [STEP_MACHINES, "Machines"], [STEP_ADDONS, "Add-ons"],
    [STEP_WORKLOADS, "Workloads"], [STEP_APPS, "Apps"],
    [STEP_VERIFY, machineSource === "vmware" ? "Plan & create" : "Verify & build"],
  ];
}

// The Small template — what a wizard opened before the catalog arrives shows.
const DEFAULT_COUNTS = { loadbalancer: 1, controlPlane: 1, worker: 2 };
const DEFAULT_SIZES = {
  loadbalancer: { cpu: 2, memoryGb: 2, diskGb: 40 },
  controlPlane: { cpu: 4, memoryGb: 8, diskGb: 80 },
  worker: { cpu: 4, memoryGb: 8, diskGb: 100 },
  vm: { cpu: 2, memoryGb: 4, diskGb: 40 },
};

// Named because the rail, the right-hand footer and the preflight hand-off
// all reference them, and off-by-one there is a silent wrong-panel bug.
const STEP_SHAPE = 0;
const STEP_MACHINES = 1;
const STEP_ADDONS = 2;
const STEP_WORKLOADS = 3;
const STEP_VERIFY = 4;
// After Workloads in the rail; numbered last so a draft saved before the step
// existed still reopens on the step it was on.
const STEP_APPS = 5;

const EMPTY_WORKLOADS = {
  sourceClusterId: "",
  sourceClusterName: "",
  registryConnectionId: null,
  storage: emptyStorage(),
  items: [],
};

// Where preflight measures free space when the build says nothing else. Mirrors
// preflight.DEFAULT_DISK_PATH on the backend, which stays the authority.
const DEFAULT_DISK_CHECK_PATH = "/var";

const EMPTY_BASICS = {
  name: "",
  k8sVersion: "",
  templateId: "small",
  counts: DEFAULT_COUNTS,
  sizes: DEFAULT_SIZES,
  machineSource: "existing",
  vmsOnly: false,
  vmCount: 2,
  vm: EMPTY_VM_PLACEMENT,
  topologyType: "single_cp",
  endpointMode: "managed_haproxy",
  vipAddress: "",
  controlPlaneEndpoint: "",
  cniPlugin: "calico",
  podCidr: "10.244.0.0/16",
  serviceCidr: "10.96.0.0/12",
  diskCheckPath: DEFAULT_DISK_CHECK_PATH,
  addons: [],
  workloads: { ...EMPTY_WORKLOADS },
  apps: [],
  vsphereConnectionId: "",
  buildProfileId: "",
  connectionProfileId: "",
};

function basicsFromBuild(build) {
  if (!build) return { ...EMPTY_BASICS, workloads: { ...EMPTY_WORKLOADS } };
  const spec = build.provisioning?.spec;
  const vmware = build.machineSource === "vmware";
  const nodeCounts = build.nodeCounts || {};
  return {
    ...EMPTY_BASICS,
    templateId: build.templateId || "custom",
    machineSource: vmware ? "vmware" : "existing",
    vmsOnly: vmware && Boolean(build.vmsOnly),
    vmCount: spec?.vmCount || EMPTY_BASICS.vmCount,
    counts: spec?.counts || {
      loadbalancer: nodeCounts.loadbalancer || 0,
      controlPlane: nodeCounts.controlPlane || 1,
      worker: nodeCounts.worker || 1,
    },
    sizes: { ...DEFAULT_SIZES, ...(spec?.sizes || {}) },
    vm: vmware ? placementFromSpec(spec) : EMPTY_VM_PLACEMENT,
    name: build.name || "",
    k8sVersion: build.k8sVersion || "",
    topologyType: build.topologyType || "stacked_ha",
    endpointMode: build.endpointMode || "managed_haproxy",
    vipAddress: build.vipAddress || "",
    controlPlaneEndpoint: build.controlPlaneEndpoint || "",
    cniPlugin: build.cniPlugin || "calico",
    podCidr: build.podCidr || "10.244.0.0/16",
    serviceCidr: build.serviceCidr || "10.96.0.0/12",
    diskCheckPath: build.diskCheckPath || DEFAULT_DISK_CHECK_PATH,
    addons: build.addons || [],
    workloads: build.workloadSelection?.items?.length
      ? { ...EMPTY_WORKLOADS, ...build.workloadSelection }
      : { ...EMPTY_WORKLOADS },
    apps: appItemsFromBuild(build),
    vsphereConnectionId: build.vsphereConnectionId
      ? String(build.vsphereConnectionId) : "",
    buildProfileId: build.buildProfileId ? String(build.buildProfileId) : "",
    connectionProfileId: build.connectionProfileId
      ? String(build.connectionProfileId) : "",
  };
}

function pickedFromBuild(build) {
  return Object.fromEntries(
    (build?.nodes || [])
      .filter((node) => node.vsphereVmMoid)
      .map((node) => [node.vsphereVmMoid, {
        role: node.role,
        address: node.address || "",
        hostname: node.hostname || "",
      }])
  );
}

function manualFromBuild(build) {
  return (build?.nodes || [])
    .filter((node) => !node.vsphereVmMoid)
    .map((node) => ({
      role: node.role,
      address: node.address || "",
      hostname: node.hostname || "",
    }));
}

const ROLE_KEYS = [
  ["loadbalancer", "LB"],
  ["control_plane", "CP"],
  ["worker", "W"],
];

function StepRail({ current, onGoBack, steps }) {
  // By position in the rail, not by step number: Apps sits before Verify but
  // carries a later number so older saved drafts keep theirs.
  const currentIndex = steps.findIndex(([step]) => step === current);
  return (
    <nav className="sg-cb-steps" aria-label="Build steps">
      {steps.map(([step, label], index) => {
        const state = step === current ? "is-on" : index < currentIndex ? "is-done" : "";
        const reachable = index < currentIndex;
        return (
          <span className="sg-cb-steps-cell" key={label}>
            {index > 0 ? <span className="sg-cb-steps-arrow" aria-hidden="true">→</span> : null}
            {reachable ? (
              <button type="button" className={`sg-cb-step ${state}`} onClick={() => onGoBack(step)}>
                <i>✓</i>{label}
              </button>
            ) : (
              <span className={`sg-cb-step ${state}`} aria-current={step === current ? "step" : undefined}>
                <i>{index + 1}</i>{label}
              </span>
            )}
          </span>
        );
      })}
    </nav>
  );
}

/** The plumbing every build consumes, resolved from what is already healthy.
    Visible, one click to change, and out of the decision form. */
function SourcesBar({ basics, infra, onChange, editing, setEditing }) {
  const vsphere = infra.vsphere.find((row) => String(row.id) === String(basics.vsphereConnectionId));
  const profile = infra.profiles.find((row) => String(row.id) === String(basics.connectionProfileId));
  const buildProfile = infra.buildProfiles.find(
    (row) => String(row.id) === String(basics.buildProfileId)
  );

  const chips = [
    // When KubeSight creates the VMs, the vCenter is chosen with the placement.
    ...(basics.machineSource === "vmware" ? [] : [{
      key: "vsphere",
      who: "vCenter",
      what: vsphere?.name || "None — manual hosts",
      state: vsphere ? (vsphere.lastConnectionStatus === "ok" ? "ok" : "warn") : "idle",
      field: "vsphereConnectionId",
      options: [
        { value: "", label: "None (manual hosts)" },
        ...infra.vsphere.map((row) => ({ value: String(row.id), label: row.name })),
      ],
    }]),
    {
      key: "ssh",
      who: "SSH",
      what: profile
        ? `${profile.name}${profile.hostKeyPolicy ? ` · ${profile.hostKeyPolicy}` : ""}`
        : "Required",
      state: profile ? (profile.lastTestStatus === "failed" ? "warn" : "ok") : "bad",
      field: "connectionProfileId",
      options: [
        { value: "", label: "Select a route…" },
        ...infra.profiles.map((row) => ({ value: String(row.id), label: row.name })),
      ],
    },
    {
      key: "sources",
      who: "Packages",
      what: buildProfile ? `${buildProfile.name} · ${buildProfile.repoMode}` : "Internet defaults",
      state: buildProfile ? "ok" : "idle",
      field: "buildProfileId",
      options: [
        { value: "", label: "Internet defaults (dev/test)" },
        ...infra.buildProfiles.map((row) => ({
          value: String(row.id), label: `${row.name} (${row.repoMode})`,
        })),
      ],
    },
  ];

  const open = chips.find((chip) => chip.key === editing);

  return (
    <div className="sg-cb-srcbar">
      <span className="sg-cb-srcbar-label">Sources</span>
      {chips.map((chip) => (
        <span className={`sg-cb-srcchip is-${chip.state}`} key={chip.key}>
          <span className="sg-cb-dot" />
          <span className="who">{chip.who}</span>
          <span className="what sg-cb-mono">{chip.what}</span>
          <button
            type="button"
            className="chg"
            aria-expanded={editing === chip.key}
            onClick={() => setEditing(editing === chip.key ? null : chip.key)}
          >
            Change
          </button>
        </span>
      ))}
      <span className="sg-cb-srcbar-note">
        Filled in from what is already healthy — most builds never touch this row.
      </span>
      {open ? (
        <div className="sg-cb-srcedit">
          <Field label={`${open.who} for this build`} htmlFor={`src-${open.key}`}>
            <select
              id={`src-${open.key}`}
              className="sg-cb-input"
              value={String(basics[open.field] || "")}
              onChange={(event) => onChange(open.field, event.target.value)}
            >
              {open.options.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </Field>
          <button className="btn-ghost" type="button" onClick={() => setEditing(null)}>Done</button>
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 1 — Shape
// ---------------------------------------------------------------------------

/** For machines that already exist: how clients reach the API server. When
    KubeSight creates the VMs, the address comes from the network range instead. */
function EndpointFields({ basics, setBasic, primaryAddress }) {
  if (basics.endpointMode === "managed_haproxy") {
    return (
      <div className="card sg-cb-card sg-cb-fields">
        <Field
          label="VIP address"
          htmlFor="cb-vip"
          hint={basics.topologyType === "single_cp"
            ? "An unused address on the same L2 network. KubeSight assigns it to the single managed load balancer; this shape has no failover."
            : "An unused address on the control-plane L2 segment. Keepalived floats it between the two load-balancer machines, and preflight confirms nothing answers on it yet."}
        >
          <input
            id="cb-vip"
            className="sg-cb-input sg-cb-mono"
            value={basics.vipAddress}
            onChange={(event) => setBasic("vipAddress", event.target.value)}
            placeholder="10.0.0.100"
          />
        </Field>
      </div>
    );
  }
  return (
    <div className="card sg-cb-card sg-cb-fields">
      <Field label="API endpoint">
        <div className="sg-cb-seg" role="group" aria-label="API endpoint">
          <button type="button" aria-pressed={basics.endpointMode === "manual_endpoint"}
                  onClick={() => setBasic("endpointMode", "manual_endpoint")}>
            The control plane&apos;s own address
          </button>
          <button type="button" aria-pressed={basics.endpointMode === "external_lb"}
                  onClick={() => setBasic("endpointMode", "external_lb")}>
            A load balancer you already run
          </button>
        </div>
      </Field>
      <Field
        label="Control-plane endpoint"
        htmlFor="cb-endpoint"
        hint="host:port. A stable endpoint is required even for a single control plane — it keeps the HA migration path open."
      >
        <input
          id="cb-endpoint"
          className="sg-cb-input sg-cb-mono"
          value={basics.controlPlaneEndpoint}
          onChange={(event) => setBasic("controlPlaneEndpoint", event.target.value)}
          placeholder={primaryAddress ? `${primaryAddress}:6443` : "k8s-api.example.com:6443"}
        />
      </Field>
      {primaryAddress && basics.endpointMode === "manual_endpoint"
        && basics.controlPlaneEndpoint !== `${primaryAddress}:6443` ? (
          <button className="btn-ghost btn-sm sg-cb-pv-usebtn" type="button"
                  onClick={() => setBasic("controlPlaneEndpoint", `${primaryAddress}:6443`)}>
            Use {primaryAddress}:6443
          </button>
        ) : null}
    </div>
  );
}

/** ``value`` is "vmware" (KubeSight creates the VMs) or "existing". */
function SourceChoice({ value, onChange, canVmware }) {
  const needsVcenter = "Needs a vCenter with a provisioning account — an administrator adds one under Sources.";
  return (
    <div className="card sg-cb-card">
      <div className="sg-cb-sect"><h2>Where the machines come from</h2></div>
      <div className="sg-cb-choices">
        <button type="button" className="sg-cb-choice" aria-pressed={value === "vmware"}
                disabled={!canVmware} onClick={() => onChange("vmware")}>
          <span className="ct">Create new VMs in VMware <span className="sg-cb-pill is-brand">OpenTofu</span></span>
          <span className="cd">
            {canVmware
              ? "KubeSight clones a VM template, sets sizes and addresses, then installs Kubernetes on them."
              : needsVcenter}
          </span>
        </button>
        <button type="button" className="sg-cb-choice" aria-pressed={value === "existing"}
                onClick={() => onChange("existing")}>
          <span className="ct">Use machines you already have</span>
          <span className="cd">Pick running VMs from vCenter or add hosts by address. Nothing is created in vCenter.</span>
        </button>
      </div>
    </div>
  );
}

/** The first question: a cluster, or VMs now and Kubernetes later (or never). */
function GoalChoice({ vmsOnly, onChange, canVmware }) {
  return (
    <div className="card sg-cb-card">
      <div className="sg-cb-sect"><h2>What do you want to create?</h2></div>
      <div className="sg-cb-choices">
        <button type="button" className="sg-cb-choice" aria-pressed={!vmsOnly} onClick={() => onChange(false)}>
          <span className="ct">A Kubernetes cluster</span>
          <span className="cd">Pick a shape (Lab, Small, Standard HA), then new VMs or machines you already have.</span>
        </button>
        <button type="button" className="sg-cb-choice" aria-pressed={vmsOnly}
                disabled={!canVmware} onClick={() => onChange(true)}>
          <span className="ct">VMs only <span className="sg-cb-pill is-brand">OpenTofu</span></span>
          <span className="cd">
            {canVmware
              ? "Just say how many. KubeSight creates them in VMware and stops once each answers SSH. Install Kubernetes on them later — it offers the shapes that fit that many VMs — or destroy them."
              : "Needs a vCenter with a provisioning account — an administrator adds one under Sources."}
          </span>
        </button>
      </div>
    </div>
  );
}

/** VMs only: a name and how many. Where they go and how big comes next. */
function VmsOnlyStep({ basics, setBasic }) {
  const countError = vmCountError(basics.vmCount);
  const step = (delta) => setBasic(
    "vmCount", Math.min(Math.max((Number(basics.vmCount) || 0) + delta, 1), MAX_VMS)
  );
  return (
    <div className="card sg-cb-card sg-cb-fields">
      <Field
        label="Name"
        htmlFor="cb-name"
        hint={`Names the VMs: ${basics.name || "name"}-vm-1, ${basics.name || "name"}-vm-2 …`}
        error={vmwareNameError(basics.name)}
      >
        <input
          id="cb-name"
          className="sg-cb-input sg-cb-mono"
          value={basics.name}
          onChange={(event) => setBasic("name", event.target.value)}
          placeholder="vm-test-01"
        />
      </Field>
      <Field
        label="Number of VMs"
        hint="All the same size. When you install Kubernetes later, KubeSight offers the shapes that fit: 1 VM → single node, 2 → Lab, 4 → Small, 8 → Standard HA, and others."
        error={countError}
      >
        <div className="sg-cb-vmcount">
          <div className="sg-cb-stepper" role="group" aria-label="Number of VMs">
            <button type="button" className="btn-ghost" aria-label="Fewer VMs" onClick={() => step(-1)}>−</button>
            <output>{basics.vmCount}</output>
            <button type="button" className="btn-ghost" aria-label="More VMs" onClick={() => step(1)}>+</button>
          </div>
        </div>
      </Field>
    </div>
  );
}

function ShapeStep({ options, basics, setBasic }) {
  // CNI support windows move with Kubernetes, so only plugins with a version
  // validated on the selected release are offered.
  const compatibleCnis = cniPluginsForK8s(options.cniPlugins || [], basics.k8sVersion);
  return (
    <div className="card sg-cb-card sg-cb-fields">
      <Field
        label="Cluster name"
        htmlFor="cb-name"
        hint={basics.machineSource === "vmware"
          ? `Also names the VMs: ${basics.name || "name"}-cp-1, ${basics.name || "name"}-wk-1 …`
          : "Becomes the cluster's name in Clusters, Dashboard and Inventory once it is alive."}
        error={basics.machineSource === "vmware" ? vmwareNameError(basics.name) : ""}
      >
        <input
          id="cb-name"
          className="sg-cb-input sg-cb-mono"
          value={basics.name}
          onChange={(event) => setBasic("name", event.target.value)}
          placeholder="areeba-uat-02"
        />
      </Field>

      <Field label="Kubernetes version" hint="Newest first. Preflight confirms your sources carry it.">
        {(options.k8sVersions || []).length ? (
          <div className="sg-cb-seg" role="group" aria-label="Kubernetes version">
            {options.k8sVersions.map((version) => (
              <button
                key={version}
                type="button"
                aria-pressed={basics.k8sVersion === version}
                onClick={() => setBasic("k8sVersion", version)}
              >
                v{version}
              </button>
            ))}
          </div>
        ) : (
          <p className="muted">
            No Kubernetes versions are available. KubeSight could not resolve a
            supported release; check the backend logs and retry.
          </p>
        )}
      </Field>

      <details className="sg-cb-adv">
        <summary>
          Networking
          <span className="sv sg-cb-mono">
            {basics.cniPlugin} · pods {basics.podCidr} · services {basics.serviceCidr}
          </span>
          <span className="cv">defaults are fine — open to change</span>
        </summary>
        <div className="sg-cb-adv-body">
          <Field
            label="CNI plugin"
            htmlFor="cb-cni"
            hint={
              compatibleCnis.length < (options.cniPlugins || []).length
                ? `Showing plugins validated on Kubernetes ${basics.k8sVersion}.`
                : ""
            }
          >
            <select
              id="cb-cni"
              className="sg-cb-input"
              value={basics.cniPlugin}
              onChange={(event) => setBasic("cniPlugin", event.target.value)}
            >
              {compatibleCnis.map((plugin) => (
                <option key={plugin.id} value={plugin.id}>
                  {plugin.displayName} ({plugin.supportTier})
                </option>
              ))}
            </select>
          </Field>
          <Field label="Pod CIDR" htmlFor="cb-pod">
            <input
              id="cb-pod"
              className="sg-cb-input sg-cb-mono"
              value={basics.podCidr}
              onChange={(event) => setBasic("podCidr", event.target.value)}
            />
          </Field>
          <Field label="Service CIDR" htmlFor="cb-svc">
            <input
              id="cb-svc"
              className="sg-cb-input sg-cb-mono"
              value={basics.serviceCidr}
              onChange={(event) => setBasic("serviceCidr", event.target.value)}
            />
          </Field>
        </div>
      </details>

      <details className="sg-cb-adv">
        <summary>
          Machine disk
          <span className="sv sg-cb-mono">free space checked on {basics.diskCheckPath || DEFAULT_DISK_CHECK_PATH}</span>
          <span className="cv">change it if container data lives on another mount</span>
        </summary>
        <div className="sg-cb-adv-body">
          <Field
            label="Disk check path"
            htmlFor="cb-diskpath"
            hint="Preflight measures free space on the filesystem behind this path, on every machine. /var is the default because kubeadm, containerd and pulled images land under /var/lib — point it at the mount that actually backs them if you moved them elsewhere."
          >
            <input
              id="cb-diskpath"
              className="sg-cb-input sg-cb-mono"
              value={basics.diskCheckPath}
              onChange={(event) => setBasic("diskCheckPath", event.target.value)}
              placeholder={DEFAULT_DISK_CHECK_PATH}
            />
          </Field>
        </div>
      </details>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 2 — Machines
// ---------------------------------------------------------------------------

const VM_FILTERS = [
  { key: "poweredOn", label: "Powered on", test: (vm) => vm.powerState === "POWERED_ON" },
  { key: "tools", label: "Has VMware Tools", test: (vm) => vm.toolsRunState === "RUNNING" },
  { key: "big", label: "≥ 4 vCPU", test: (vm) => (vm.cpuCount || 0) >= 4 },
];

function MachinesStep({
  basics, infra, vms, vmsLoading, search, setSearch, filters, toggleFilter,
  picked, setPicked, manualNodes, setManualNodes, conflictHosts,
}) {
  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    return vms.filter((vm) => {
      if (!VM_FILTERS.every((filter) => (filters[filter.key] ? filter.test(vm) : true))) {
        // A machine already assigned stays visible whatever the filters say.
        if (!picked[vm.moid]) return false;
      }
      if (!query) return true;
      return [vm.name, vm.guestHostname, vm.guestIp, vm.esxiHost]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(query));
    });
  }, [vms, search, filters, picked]);

  const setRole = (moid, role) => {
    setPicked((previous) => {
      const next = { ...previous };
      if (!role || next[moid]?.role === role) delete next[moid];
      else next[moid] = { ...(next[moid] || {}), role };
      return next;
    });
  };

  if (!basics.vsphereConnectionId && !infra.vsphere.length) {
    return (
      <div className="card sg-cb-card">
        <p className="muted">
          No vCenter is configured, so machines are entered by hand. Add hosts below, or
          connect a vCenter under Sources to pick from inventory.
        </p>
        <ManualHosts manualNodes={manualNodes} setManualNodes={setManualNodes} startOpen />
      </div>
    );
  }

  return (
    <div className="card sg-cb-card">
      {basics.vsphereConnectionId ? (
        <>
          <div className="sg-cb-pick-top">
            <input
              className="sg-cb-search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search by name, address or ESXi host…"
              aria-label="Search machines"
            />
            {VM_FILTERS.map((filter) => (
              <button
                key={filter.key}
                type="button"
                className="sg-cb-fchip"
                aria-pressed={Boolean(filters[filter.key])}
                onClick={() => toggleFilter(filter.key)}
              >
                {filter.label}
              </button>
            ))}
            <span className="muted sg-cb-pick-count">
              {vmsLoading ? "Loading inventory…" : `${filtered.length} of ${vms.length} machines`}
            </span>
          </div>

          <div className="sg-cb-vmlist">
            {filtered.map((vm) => {
              const selection = picked[vm.moid];
              const toolsOk = vm.toolsRunState === "RUNNING";
              const clash = Boolean(
                selection
                && conflictHosts.includes(vm.esxiHost)
                && (selection.role === "control_plane" || selection.role === "loadbalancer")
              );
              return (
                <div
                  key={vm.moid}
                  className={`sg-cb-vm ${selection ? `is-picked is-${selection.role}` : ""}`}
                >
                  <span className="sg-cb-vm-id">
                    <span className="vnm sg-cb-mono">{vm.name}</span>
                    <span className="vsub">
                      {vm.guestOs || "Unknown guest"}
                      {vm.powerState !== "POWERED_ON"
                        ? <span className="sg-cb-warn-text"> · powered off</span>
                        : null}
                      {toolsOk ? " · Tools running" : <span className="sg-cb-warn-text"> · no Tools</span>}
                    </span>
                  </span>
                  <span className="sg-cb-vm-addr">
                    {toolsOk && vm.guestIp ? (
                      <span className="sg-cb-mono">{vm.guestIp}</span>
                    ) : selection ? (
                      <input
                        className="sg-cb-input sg-cb-mono sg-cb-ipfix"
                        placeholder="management address"
                        aria-label={`Management address for ${vm.name}`}
                        value={selection.address || ""}
                        onChange={(event) => setPicked((previous) => ({
                          ...previous,
                          [vm.moid]: { ...previous[vm.moid], address: event.target.value },
                        }))}
                      />
                    ) : <span className="muted">no Tools address</span>}
                  </span>
                  <span className="sg-cb-vm-spec">
                    {vm.cpuCount ?? "—"} vCPU
                    {vm.memoryMiB ? ` · ${Math.round(vm.memoryMiB / 1024)} GiB` : ""}
                  </span>
                  <span className="sg-cb-vm-host">
                    <span className={`sg-cb-hostchip ${clash ? "is-conflict" : ""}`}>
                      {vm.esxiHost || "—"}
                    </span>
                  </span>
                  <span className="sg-cb-roleset" role="group" aria-label={`Role for ${vm.name}`}>
                    {ROLE_KEYS.map(([role, short]) => (
                      <button
                        key={role}
                        type="button"
                        title={ROLE_LABELS[role]}
                        aria-pressed={selection?.role === role}
                        onClick={() => setRole(vm.moid, role)}
                      >
                        {short}
                      </button>
                    ))}
                  </span>
                </div>
              );
            })}
            {!filtered.length && !vmsLoading ? (
              <p className="muted sg-cb-vm-empty">
                No machine matches those filters. Clear one, or add a manual host below.
              </p>
            ) : null}
          </div>
          <p className="muted sg-cb-tools-note">
            A machine without VMware Tools stays pickable — its address cell becomes an input.
            Tools is recommended, never required.
          </p>
        </>
      ) : null}

      <ManualHosts manualNodes={manualNodes} setManualNodes={setManualNodes} />
    </div>
  );
}

/** The exception, kept to one line until someone needs it. */
function ManualHosts({ manualNodes, setManualNodes, startOpen = false }) {
  // With no vCenter, manual hosts are the only way in: start open, with a row.
  const [open, setOpen] = useState(manualNodes.length > 0 || startOpen);
  useEffect(() => {
    if (startOpen && !manualNodes.length) setManualNodes([{ role: "", hostname: "", address: "" }]);
    // Once, on mount: removing every row afterwards is the user's choice.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  if (!open) {
    return (
      <div className="sg-cb-manual-teaser">
        <span className="muted">Machine not in vCenter?</span>
        <button
          className="btn-outline btn-sm"
          type="button"
          onClick={() => {
            // Opening the section should give a row to type into, not a
            // heading and another "Add" button.
            if (!manualNodes.length) setManualNodes([{ role: "", hostname: "", address: "" }]);
            setOpen(true);
          }}
        >
          Add a manual host
        </button>
      </div>
    );
  }
  const update = (index, key, value) => setManualNodes(
    (previous) => previous.map((node, i) => (i === index ? { ...node, [key]: value } : node))
  );
  return (
    <div className="sg-cb-manual">
      <h4>Manual hosts</h4>
      {manualNodes.map((node, index) => (
        <div key={index} className="sg-cb-manual-row">
          <select
            className="sg-cb-input"
            aria-label="Role"
            value={node.role}
            onChange={(event) => update(index, "role", event.target.value)}
          >
            <option value="">Role…</option>
            {Object.entries(ROLE_LABELS).map(([role, label]) => (
              <option key={role} value={role}>{label}</option>
            ))}
          </select>
          <input
            className="sg-cb-input sg-cb-mono"
            placeholder="hostname"
            aria-label="Hostname"
            value={node.hostname}
            onChange={(event) => update(index, "hostname", event.target.value)}
          />
          <input
            className="sg-cb-input sg-cb-mono"
            placeholder="address"
            aria-label="Address"
            value={node.address}
            onChange={(event) => update(index, "address", event.target.value)}
          />
          <button
            className="btn-ghost"
            type="button"
            onClick={() => setManualNodes((previous) => previous.filter((_, i) => i !== index))}
          >
            Remove
          </button>
        </div>
      ))}
      <button
        className="btn-outline btn-sm"
        type="button"
        onClick={() => setManualNodes((previous) => [...previous, { role: "", hostname: "", address: "" }])}
      >
        Add another
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 3 — Add-ons
// ---------------------------------------------------------------------------

/** The add-on catalog as a shelf of cards. Shared with day two, where
 *  ``installed`` lists what the cluster already runs: those cards stay on the
 *  shelf, ticked and locked, so the choice reads against what is there. */
export function AddonShelf({ catalog, value, onChange, k8sVersion, installed = [] }) {
  const selectedById = new Map(value.map((addon) => [addon.id, addon]));
  const installedById = new Map(installed.map((addon) => [addon.id, addon]));

  const toggle = (entry, checked) => {
    if (!checked) {
      onChange(value.filter((item) => item.id !== entry.id));
      return;
    }
    const version = defaultVersionForK8s(entry, k8sVersion)
      || entry.defaultVersion
      || entry.versions?.[0]
      || "";
    const config = {};
    for (const field of entry.configFields || []) config[field.key] = "";
    onChange([
      ...value.filter((item) => item.id !== entry.id),
      { id: entry.id, version, ...(entry.configFields?.length ? { config } : {}) },
    ]);
  };

  const setVersion = (id, version) => onChange(
    value.map((addon) => (addon.id === id ? { ...addon, version } : addon))
  );
  const setConfigField = (id, key, text) => onChange(
    value.map((addon) => (addon.id === id
      ? { ...addon, config: { ...(addon.config || {}), [key]: text } }
      : addon))
  );

  if (!catalog.length) {
    return <p className="muted">No optional add-ons are available on this KubeSight.</p>;
  }

  return (
    <div className="sg-cb-shelf">
      {catalog.map((entry) => {
        const selected = selectedById.get(entry.id);
        // Only versions validated on the chosen Kubernetes release; an add-on
        // with none is shown but cannot be selected, so the reason is visible
        // rather than the add-on silently vanishing from the shelf.
        const usableVersions = versionsForK8s(entry, k8sVersion);
        const version = selected?.version
          || usableVersions[0]
          || entry.defaultVersion
          || entry.versions?.[0]
          || "";
        const provenance = addonProvenance(entry, version);
        const installable = usableVersions.length > 0;
        const present = installedById.get(entry.id);
        if (present) {
          return (
            <div key={entry.id} className="sg-cb-addon is-on is-installed">
              <label className="sg-cb-addon-head">
                <input type="checkbox" checked disabled readOnly />
                <span>
                  <span className="an">
                    {entry.displayName}
                    <span className="sg-cb-tierchip">installed</span>
                  </span>
                  <span className="ad">
                    Already running on this cluster
                    {present.version ? ` · v${present.version}` : ""}
                    {Object.values(present.config || {})
                      .map((item) => (Array.isArray(item) ? item.join(", ") : item))
                      .filter(Boolean)
                      .map((item) => ` · ${item}`)
                      .join("")}
                  </span>
                </span>
              </label>
            </div>
          );
        }
        return (
          <div key={entry.id} className={`sg-cb-addon ${selected ? "is-on" : ""}`}>
            <label className="sg-cb-addon-head">
              <input
                type="checkbox"
                checked={Boolean(selected)}
                disabled={!installable}
                onChange={(event) => toggle(entry, event.target.checked)}
              />
              <span>
                <span className="an">
                  {entry.displayName}
                  {entry.supportTier ? <span className="sg-cb-tierchip">{entry.supportTier}</span> : null}
                </span>
                {entry.description ? <span className="ad">{entry.description}</span> : null}
                {!installable && (entry.versions || []).length ? (
                  <span className="ad">
                    No version is validated on Kubernetes {k8sVersion}.
                  </span>
                ) : null}
              </span>
            </label>

            {selected && usableVersions.length ? (
              <div className="sg-cb-addon-row">
                <span>Version</span>
                <select
                  className="sg-cb-input"
                  aria-label={`${entry.displayName} version`}
                  value={version}
                  onChange={(event) => setVersion(entry.id, event.target.value)}
                >
                  {usableVersions.map((candidate) => (
                    <option key={candidate} value={candidate}>v{candidate}</option>
                  ))}
                </select>
              </div>
            ) : null}

            {selected ? (entry.configFields || []).map((field) => {
              const raw = selected.config?.[field.key];
              const text = Array.isArray(raw) ? raw.join("\n") : (raw || "");
              const error = text || !field.required
                ? (field.type === "ipRangeList" && text ? ipRangeListError(text) : "")
                : `${field.label} is required.`;
              return (
                <div className="sg-cb-addon-cfg" key={field.key}>
                  <label htmlFor={`addon-${entry.id}-${field.key}`}>
                    {field.label}{field.required ? " — required" : ""}
                  </label>
                  <textarea
                    id={`addon-${entry.id}-${field.key}`}
                    rows={2}
                    value={text}
                    placeholder={field.placeholder}
                    aria-invalid={Boolean(error)}
                    onChange={(event) => setConfigField(entry.id, field.key, event.target.value)}
                  />
                  {error
                    ? <span className="sg-cb-field-error">{error}</span>
                    : field.help ? <span className="sg-cb-field-hint">{field.help}</span> : null}
                </div>
              );
            }) : null}

            {/* Every manifest is pinned to a digest and most are vendored, which
                is what makes an air-gapped build possible. Say so. */}
            <div className={`sg-cb-prov ${provenance.bundled ? "" : "is-remote"}`}>
              <span className="sg-cb-dot" />
              <span className="sg-cb-mono">
                {provenance.text}
                {provenance.digest ? ` · ${provenance.digest}` : ""}
                {provenance.manifestCount > 1 ? ` · ${provenance.manifestCount} manifests` : ""}
              </span>
            </div>
            {!provenance.bundled && provenance.digest ? (
              <p className="sg-cb-field-hint">
                An offline build needs this bundle on the KubeSight host — run{" "}
                <span className="sg-cb-mono">tools/fetch_cluster_build_bundles.py</span>.
              </p>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Wizard
// ---------------------------------------------------------------------------

export default function Wizard({
  options,
  infra,
  infraReady = true,
  canExecute = false,
  canManageTemplates = false,
  currentUserId = null,
  initialBuild = null,
  draft = null,
  onDraftChange,
  notify,
  onBuildSaved,
  onBuildLaunched,
  onCancel,
  onOptionsChanged,
}) {
  // An unfinished draft the page kept (another tab, a reload) picks up where
  // it was left. "Edit draft" on a saved build starts from that build instead.
  // Read once, at mount: the page keeps passing its latest copy back down.
  const [restored] = useState(() => (initialBuild ? null : draft));
  const [step, setStep] = useState(() => restored?.step ?? 0);
  const [basics, setBasics] = useState(() => restored?.basics || basicsFromBuild(initialBuild));
  const [vms, setVms] = useState([]);
  const [vmsLoading, setVmsLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState({ poweredOn: true, tools: false, big: false });
  const [picked, setPicked] = useState(() => restored?.picked || pickedFromBuild(initialBuild));
  const [manualNodes, setManualNodes] = useState(() => restored?.manualNodes || manualFromBuild(initialBuild));
  const [buildId, setBuildId] = useState(() => restored?.buildId ?? initialBuild?.id ?? null);
  const [preflightResult, setPreflightResult] = useState(() => restored?.preflightResult || null);
  const [busy, setBusy] = useState(false);
  const [acked, setAcked] = useState(() => Boolean(restored?.acked));
  const [editingSource, setEditingSource] = useState(null);
  // The last image check from the Workloads step, so the Blueprint can report
  // it while the user is still standing on that step.
  const [workloadPlan, setWorkloadPlan] = useState(() => restored?.workloadPlan || null);
  // VMware: the build as the plan step last read it, and the address preview.
  const [planBuild, setPlanBuild] = useState(() => restored?.planBuild || null);
  const [addresses, setAddresses] = useState([]);
  const [previewError, setPreviewError] = useState("");
  // A restored draft has already been seeded: re-seeding would put the Small
  // template's shape and the default machine source back over the user's.
  // The plumbing seed only fills blanks, so it may still run.
  const seeded = useRef(Boolean(initialBuild || restored));
  const plumbingSeeded = useRef(Boolean(initialBuild));
  const sourceSeeded = useRef(Boolean(initialBuild || restored));

  // --- Unsaved draft ----------------------------------------------------------
  // "Dirty" means the user changed something. Seeding effects also change
  // state as the catalog and source lists arrive, so the baseline is taken at
  // the first interaction inside the wizard (capture phase, before that
  // interaction's own change lands), and dirty is "differs from it".
  const fingerprint = JSON.stringify({ step, basics, picked, manualNodes });
  const fingerprintRef = useRef(fingerprint);
  fingerprintRef.current = fingerprint;
  const baselineRef = useRef(restored?.baseline ?? null);
  const markInteraction = () => {
    if (baselineRef.current === null) baselineRef.current = fingerprintRef.current;
  };
  const dirty = baselineRef.current !== null && fingerprint !== baselineRef.current;

  const setBasic = (key, value) => setBasics((previous) => ({ ...previous, [key]: value }));
  const catalog = options?.clusterTemplates;
  const minimums = catalog?.minimumSizes || DEFAULT_MINIMUMS;
  const vmware = basics.machineSource === "vmware";
  const provisioningConnections = useMemo(
    () => (infra.vsphere || []).filter((row) => row.provisioningConfigured),
    [infra.vsphere]
  );
  const vmsOnly = vmware && basics.vmsOnly;
  const steps = wizardSteps(basics.machineSource, basics.vmsOnly);

  // Hand the page every change, so the draft outlives this component. The
  // page keeps only a dirty draft; a clean one is nothing to lose.
  const stepLabel = steps.find(([id]) => id === step)?.[1] || "";
  useEffect(() => {
    onDraftChange?.({
      step,
      stepLabel,
      basics,
      picked,
      manualNodes,
      buildId,
      preflightResult,
      acked,
      workloadPlan,
      planBuild,
      baseline: baselineRef.current,
      dirty,
    });
  }, [onDraftChange, step, stepLabel, basics, picked, manualNodes, buildId, preflightResult, acked, workloadPlan, planBuild, dirty]);

  // A draft restored after a reload comes back without the VM plan (it is
  // not kept in the browser); read it again when the user is standing on it.
  const planReread = useRef(Boolean(restored?.planBuild));
  useEffect(() => {
    if (planReread.current || !restored || !buildId || step !== STEP_VERIFY) return;
    planReread.current = true;
    if (basics.machineSource !== "vmware") return;
    getClusterBuild(buildId).then(setPlanBuild).catch(() => {});
  }, [restored, buildId, step, basics.machineSource]);

  // Resolve the plumbing once, from whatever is already healthy. This is what
  // lets the Sources row be a statement rather than three questions.
  useEffect(() => {
    if (plumbingSeeded.current || !options || !infraReady) return;
    // Waits for the page's source lists: opened straight from a link, the
    // wizard mounts before they arrive, and seeding from the empty placeholder
    // left the SSH route unchosen for good.
    plumbingSeeded.current = true;
    const { vcenter, route, buildProfile } = preferredSources(infra);
    setBasics((previous) => ({
      ...previous,
      vsphereConnectionId: previous.vsphereConnectionId || (vcenter ? String(vcenter.id) : ""),
      connectionProfileId: previous.connectionProfileId || (route ? String(route.id) : ""),
      buildProfileId: previous.buildProfileId || (buildProfile ? String(buildProfile.id) : ""),
    }));
  }, [options, infra, infraReady]);

  useEffect(() => {
    if (seeded.current || !options) return;
    seeded.current = true;
    const small = findTemplate(options.clusterTemplates, "small");
    setBasics((previous) => ({
      ...previous,
      k8sVersion: previous.k8sVersion || options.k8sVersions[0] || "",
      podCidr: options.defaults?.podCidr || previous.podCidr,
      serviceCidr: options.defaults?.serviceCidr || previous.serviceCidr,
      ...(small ? {
        counts: { ...small.counts },
        // A template carries role sizes only; the plain-VM size stays.
        sizes: { ...previous.sizes, ...JSON.parse(JSON.stringify(small.sizes)) },
      } : {}),
    }));
  }, [options]);

  // A vCenter that can create VMs makes that the default — it is the reason
  // this wizard has a second path at all. Decided once; the user can switch.
  useEffect(() => {
    if (sourceSeeded.current || !provisioningConnections.length) return;
    sourceSeeded.current = true;
    setBasics((previous) => ({
      ...previous,
      machineSource: "vmware",
      vm: { ...previous.vm, connectionId: String(provisioningConnections[0].id) },
    }));
  }, [provisioningConnections]);

  // Topology follows the template's role counts. For machines that exist, a
  // shape with no load balancer may still sit behind one the user runs.
  useEffect(() => {
    const shape = shapeFor(basics.counts);
    setBasics((previous) => {
      const endpointMode = shape.endpointMode === "manual_endpoint"
        && ["manual_endpoint", "external_lb"].includes(previous.endpointMode)
        ? previous.endpointMode
        : shape.endpointMode;
      if (previous.topologyType === shape.topologyType && previous.endpointMode === endpointMode) {
        return previous;
      }
      return { ...previous, topologyType: shape.topologyType, endpointMode };
    });
  }, [basics.counts]);

  // Changing the Kubernetes version can strand a CNI plugin or add-on version
  // that the new release does not cover. Realign those here rather than letting
  // the user carry an invisible mismatch to preflight. The Kubernetes version
  // itself is never rewritten — only what depends on it.
  useEffect(() => {
    if (!options || !basics.k8sVersion) return;
    const cniCatalog = options.cniPlugins || [];
    const usableCnis = cniPluginsForK8s(cniCatalog, basics.k8sVersion);
    const addonCatalog = options.addons || [];

    setBasics((previous) => {
      const next = { ...previous };
      let changed = false;

      if (usableCnis.length && !usableCnis.some((p) => p.id === previous.cniPlugin)) {
        next.cniPlugin = usableCnis[0].id;
        changed = true;
      }

      const addons = [];
      for (const addon of previous.addons) {
        const entry = addonCatalog.find((item) => item.id === addon.id);
        if (!entry) { addons.push(addon); continue; }
        const usable = versionsForK8s(entry, basics.k8sVersion);
        if (!usable.length) { changed = true; continue; }  // drop: unsupported
        if (!usable.includes(addon.version)) {
          addons.push({ ...addon, version: usable[0] });
          changed = true;
        } else {
          addons.push(addon);
        }
      }
      if (changed) next.addons = addons;
      return changed ? next : previous;
    });
  }, [options, basics.k8sVersion]);

  useEffect(() => {
    let ignore = false;
    if (vmware || !basics.vsphereConnectionId) { setVms([]); return undefined; }
    setVmsLoading(true);
    listVSphereVms(basics.vsphereConnectionId)
      .then((data) => { if (!ignore) setVms(data.items || []); })
      .catch((error) => notify(`vCenter inventory failed: ${error.message}`, true))
      .finally(() => { if (!ignore) setVmsLoading(false); });
    return () => { ignore = true; };
  }, [vmware, basics.vsphereConnectionId, notify]);

  // --- VMware placement -----------------------------------------------------
  const placementState = useVmwarePlacement(vmware ? basics.vm.connectionId : "", notify);
  // An account the last privilege check says may not change CPU or memory
  // starts on "keep the template's size" — the clone then changes nothing.
  // Once; the user can switch back.
  const sizeModeSeeded = useRef(Boolean(initialBuild));
  useEffect(() => {
    if (!vmware || sizeModeSeeded.current) return;
    const connection = provisioningConnections.find((row) => String(row.id) === String(basics.vm.connectionId));
    if (!cannotResize(connection)) return;
    sizeModeSeeded.current = true;
    setBasics((previous) => ({ ...previous, vm: { ...previous.vm, sizeMode: "template" } }));
  }, [vmware, basics.vm.connectionId, provisioningConnections]);
  const ranges = placementState.data?.networks || [];
  useEffect(() => {
    if (!placementState.data) return;
    setBasics((previous) => {
      const vm = defaultPlacement(placementState.data, previous.vm, placementState.data.networks || []);
      return JSON.stringify(vm) === JSON.stringify(previous.vm) ? previous : { ...previous, vm };
    });
  }, [placementState.data]);
  const resolved = useMemo(
    () => resolvePlacement(placementState.data, basics.vm, ranges),
    [placementState.data, basics.vm, ranges]
  );
  // A VMs-only build has plain VMs; every helper below reads them as role "vm".
  const shapeCounts = vmsOnly ? vmsOnlyCounts(basics.vmCount) : basics.counts;
  const shapeSizes = effectiveSizes(shapeCounts, basics.sizes, basics.vm.sizeMode, resolved.template);
  const neededAddresses = machineCount(shapeCounts) + (shapeCounts?.loadbalancer ? 1 : 0);
  useEffect(() => {
    if (!vmware || !resolved.range || !neededAddresses) { setAddresses([]); return undefined; }
    let ignore = false;
    const id = setTimeout(() => {
      previewNetworkAddresses(resolved.range.id, neededAddresses)
        .then((data) => {
          if (ignore) return;
          setAddresses(data.addresses || []);
          setPreviewError(data.enough ? "" : `${resolved.range.networkName} does not have ${neededAddresses} free addresses.`);
        })
        .catch((error) => { if (!ignore) setPreviewError(error.message); });
    }, 250);
    return () => { ignore = true; clearTimeout(id); };
  }, [vmware, resolved.range, neededAddresses]);
  const preview = useMemo(
    () => previewMachines(basics.name, shapeCounts, shapeSizes, addresses),
    [basics.name, shapeCounts, shapeSizes, addresses]
  );
  const vmProblem = vmware
    ? ((vmsOnly ? vmCountError(basics.vmCount) : countsError(basics.counts))
      || vmwareNameError(basics.name)
      || (placementState.loading && !placementState.data ? "Reading vCenter…" : "")
      || placementProblem(resolved, shapeCounts)
      || (basics.vm.sizeMode === "template" ? "" : sizeErrors(shapeCounts, basics.sizes, minimums)[0])
      || previewError)
    : "";

  // --- Existing machines -----------------------------------------------------
  const plan = useMemo(
    () => draftBlueprint({ basics, picked, manualNodes, vms }),
    [basics, picked, manualNodes, vms]
  );

  const nodesPayload = useMemo(() => {
    const fromVms = Object.entries(picked).map(([moid, pick]) => ({
      role: pick.role,
      vsphereVmMoid: moid,
      address: pick.address || undefined,
      hostname: pick.hostname || undefined,
    }));
    const manual = manualNodes
      .filter((node) => node.address && node.role)
      .map((node) => ({ role: node.role, hostname: node.hostname, address: node.address }));
    return [...fromVms, ...manual];
  }, [picked, manualNodes]);

  const countsOk = plan.tiers
    .filter((tier) => tier.target > 0)
    .every((tier) => tier.filled === tier.target);

  const primaryAddress = useMemo(() => {
    const vmByMoid = new Map(vms.map((vm) => [vm.moid, vm]));
    const fromPick = Object.entries(picked).find(([, pick]) => pick.role === "control_plane");
    if (fromPick) return fromPick[1].address || vmByMoid.get(fromPick[0])?.guestIp || "";
    return manualNodes.find((node) => node.role === "control_plane")?.address || "";
  }, [picked, manualNodes, vms]);

  const endpointReady = basics.endpointMode === "managed_haproxy"
    ? Boolean(basics.vipAddress.trim())
    : Boolean(basics.controlPlaneEndpoint.trim());

  const addonError = useMemo(
    () => addonSelectionError(basics.addons, options?.addons || []),
    [basics.addons, options]
  );

  const shapeReady = Boolean(
    basics.name.trim()
    && basics.k8sVersion
    && basics.connectionProfileId
    && !(vmsOnly ? vmCountError(basics.vmCount) : countsError(basics.counts))
    && !(vmware && vmwareNameError(basics.name))
  );
  const machinesReady = vmware
    ? !vmProblem
    : countsOk && !plan.conflictHosts.length && endpointReady;

  // Why a Next button is disabled, said next to it rather than left to guess.
  const shapeProblem = !basics.name.trim() ? (vmsOnly ? "Name the VMs." : "Name the cluster.")
    : vmware && vmwareNameError(basics.name) ? vmwareNameError(basics.name)
      : !basics.k8sVersion ? "Choose a Kubernetes version."
        : vmsOnly && vmCountError(basics.vmCount) ? vmCountError(basics.vmCount)
        : !vmsOnly && countsError(basics.counts) ? countsError(basics.counts)
          : !basics.connectionProfileId
            ? "Choose the SSH route in the Sources row above. KubeSight logs in to every machine with it."
            : "";
  const machinesProblem = vmware ? vmProblem
    : !countsOk ? `Assign ${shapeLabel(basics.counts)}.`
      : plan.conflictHosts.length ? "Move a machine off the shared ESXi host."
        : !endpointReady
          ? (basics.endpointMode === "managed_haproxy" ? "Give the VIP address." : "Give the control-plane endpoint.")
          : "";

  const chooseTemplate = (template) => {
    if (template.id === "custom") {
      setBasic("templateId", "custom");
      return;
    }
    setBasics((previous) => {
      const next = {
        ...previous,
        templateId: template.id,
        counts: { ...template.counts },
        sizes: { ...previous.sizes, ...JSON.parse(JSON.stringify(template.sizes || DEFAULT_SIZES)) },
      };
      if (template.network?.cniPlugin) next.cniPlugin = template.network.cniPlugin;
      if (template.network?.podCidr) next.podCidr = template.network.podCidr;
      if (template.network?.serviceCidr) next.serviceCidr = template.network.serviceCidr;
      if (!template.builtin && template.addons?.length) {
        next.addons = template.addons
          .map((addon) => {
            const entry = (options?.addons || []).find((item) => item.id === addon.id);
            if (!entry) return null;
            const version = defaultVersionForK8s(entry, previous.k8sVersion) || entry.defaultVersion;
            return version ? { id: addon.id, version, ...(addon.config ? { config: addon.config } : {}) } : null;
          })
          .filter(Boolean);
      }
      return next;
    });
  };

  const buildPayload = () => {
    const common = {
      name: basics.name,
      k8sVersion: basics.k8sVersion,
      templateId: basics.templateId,
      machineSource: basics.machineSource,
      vmsOnly,
      cniPlugin: basics.cniPlugin,
      podCidr: basics.podCidr,
      serviceCidr: basics.serviceCidr,
      diskCheckPath: basics.diskCheckPath,
      // A VMs-only build skips those steps; Install Kubernetes later builds a
      // bare cluster, and plugins can be added to it on day two.
      addons: vmsOnly ? [] : basics.addons,
      buildProfileId: basics.buildProfileId || undefined,
      connectionProfileId: basics.connectionProfileId || undefined,
      workloads: !vmsOnly && basics.workloads.items.length ? basics.workloads : null,
      // Inventory Helm charts, installed in this order once the cluster is up.
      apps: vmsOnly ? [] : appsPayload(basics.apps || []),
    };
    if (vmware) {
      return {
        ...common,
        provisioning: provisioningPayload(
          basics.vm.connectionId, resolved, basics.vm, shapeCounts, basics.sizes, { vmsOnly }
        ),
      };
    }
    return {
      ...common,
      topologyType: basics.topologyType,
      endpointMode: basics.endpointMode,
      vipAddress: basics.vipAddress,
      controlPlaneEndpoint: basics.controlPlaneEndpoint,
      vsphereConnectionId: basics.vsphereConnectionId || undefined,
      nodes: nodesPayload,
    };
  };

  const save = async () => {
    let id = buildId;
    const payload = buildPayload();
    if (id) await updateClusterBuild(id, payload);
    else {
      const created = await createClusterBuild(payload);
      id = created.id;
      setBuildId(id);
    }
    return id;
  };

  const saveDraft = async () => {
    setBusy(true);
    try {
      const id = await save();
      notify(`Draft ${basics.name} saved.`);
      if (onBuildSaved) onBuildSaved(id);
      return id;
    } catch (error) {
      notify(error.message || String(error), true);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const runPreflight = async () => {
    setBusy(true);
    setAcked(false);
    try {
      const id = await save();
      setPreflightResult(await preflightClusterBuild(id));
      setStep(STEP_VERIFY);
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };

  const makePlan = async () => {
    setBusy(true);
    try {
      const id = await save();
      setPlanBuild(await planClusterVms(id));
      setStep(STEP_VERIFY);
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };

  const refreshPlanBuild = async () => {
    if (!buildId) return;
    const data = await getClusterBuild(buildId);
    setPlanBuild(data);
    const jobStatus = data.provisioning?.job?.status;
    if (data.status === "provisioning" || ["applying", "connecting"].includes(jobStatus)) {
      onBuildLaunched(buildId);
    }
  };

  // While OpenTofu plans, keep reading the build until it says how it went:
  // fast at first, then backing off (planPollDelayMs). A hidden tab stops
  // asking until it is looked at again, then reads at once.
  const planning = planBuild?.provisioning?.job?.status === "planning";
  useEffect(() => {
    if (step !== STEP_VERIFY || !planning || !buildId) return undefined;
    let cancelled = false;
    let timer = null;
    const started = Date.now();
    const schedule = () => {
      timer = setTimeout(tick, planPollDelayMs(Date.now() - started));
    };
    const tick = async () => {
      await whenVisible();
      if (cancelled) return;
      try {
        const data = await getClusterBuild(buildId);
        if (!cancelled) setPlanBuild(data);
      } catch {
        // The next tick tries again.
      }
      if (!cancelled) schedule();
    };
    schedule();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [step, planning, buildId]);

  const launch = async () => {
    setBusy(true);
    try {
      const grouped = groupChecks(preflightResult);
      await startClusterBuild(
        buildId,
        grouped.verdict === "warn" ? { ackWarnings: ["Acknowledged in wizard"] } : {}
      );
      onBuildLaunched(buildId);
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };

  const grouped = useMemo(
    () => (preflightResult ? groupChecks(preflightResult) : null),
    [preflightResult]
  );
  const stampedPlan = useMemo(() => (
    preflightResult
      ? preflightBlueprint(basics, preflightResult, hostByAddress({ picked, vms }))
      : null
  ), [basics, preflightResult, picked, vms]);

  const selectedAddons = basics.addons;
  const addonFacts = selectedAddons.map((addon) => {
    const entry = (options?.addons || []).find((item) => item.id === addon.id);
    const pool = Object.values(addon.config || {})[0];
    return {
      label: entry?.displayName || addon.id,
      value: Array.isArray(pool) ? pool.join(", ") : pool || `v${addon.version}`,
    };
  });

  // Recomputed here as well as in the picker: the Blueprint reports the
  // volume plan, and the step's own gate is "every claim has a destination".
  const workloadStorageRows = workloadPlan
    ? storageRows(workloadPlan, basics.workloads.storage)
    : [];
  const workloadStorageErrors = storageErrors(workloadStorageRows);
  const appsProblem = vmsOnly ? "" : appsError(basics.apps || []);
  // What stops the steps after Machines from being saved, first one first.
  const laterProblem = workloadStorageErrors[0] || appsProblem;
  const appFacts = (basics.apps || []).map((item) => ({
    label: item.releaseName || "chart",
    value: item.chartSlug
      ? `${item.chartSlug} ${item.chartVersion || ""} → ${item.namespace || "?"}`
      : "no chart picked",
  }));
  const appsNote = (basics.apps || []).length
    ? appsProblem
      ? { tone: "warn", text: appsProblem }
      : {
        tone: "good",
        text: "Installed in this order once the cluster is up, after add-ons and "
          + "workloads. Preflight renders each chart with its values first.",
      }
    : {
      tone: "plain",
      text: "Optional. Pick charts from Inventory → Helm Charts to have the new "
        + "cluster come up with your applications on it.",
    };

  const workloadFacts = basics.workloads.items.length
    ? [
      {
        label: "From",
        value: basics.workloads.sourceClusterName || basics.workloads.sourceClusterId,
      },
      { label: "Selected", value: workloadSelectionSummary(basics.workloads.items) },
      ...(workloadStorageRows.length
        ? [{ label: "Volumes", value: storageSummary(workloadStorageRows) }]
        : []),
      ...(workloadPlan ? [{
        label: "Images",
        value: workloadPlan.counts?.missingImages
          ? `${workloadPlan.counts.missingImages} missing from the registry`
          : workloadPlan.registryConnectionId
            ? `all ${workloadPlan.counts?.images || 0} in the registry`
            : `${workloadPlan.counts?.images || 0}, not checked`,
      }] : []),
    ]
    : [];

  const workloadNote = !basics.workloads.items.length
    ? {
      tone: "plain",
      text: "Optional. A new cluster is usually empty — pick namespaces or "
        + "workloads here only when this cluster is replacing or mirroring "
        + "another one.",
    }
    : workloadStorageErrors.length
      ? {
        tone: "warn",
        text: `${workloadStorageErrors.length} volume claim`
          + `${workloadStorageErrors.length === 1 ? " has" : "s have"} no valid `
          + 'destination yet. Every claim needs somewhere to land, even if that '
          + 'is "leave pending".',
      }
      : workloadPlan?.counts?.missingImages
        ? {
          tone: "warn",
          text: `${workloadPlan.counts.missingImages} image`
            + `${workloadPlan.counts.missingImages === 1 ? " is" : "s are"} not in `
            + "the chosen registry. Those workloads are still created — their pods "
            + "wait in ImagePullBackOff until the image is pushed. Remove them if "
            + "that is not what you want.",
        }
        : {
          tone: "good",
          text: "Copied after the cluster registers, as the last phase. The source "
            + "cluster is only read — nothing there moves.",
        };

  const affinityNote = plan.conflictHosts.length
    ? {
      tone: "warn",
      text: `Machines in one HA tier share ${plan.conflictHosts.join(", ")}. Losing that host `
        + "would drop the tier and etcd would lose quorum. Move one to another ESXi host — "
        + "this is a hard preflight failure, not a warning.",
    }
    : countsOk
      ? { tone: "good", text: "✓ Placement is clean — every HA tier spans distinct ESXi hosts." }
      : { tone: "plain", text: `Assign ${shapeLabel(basics.counts)}. Nothing is reserved until preflight runs.` };

  const sum = totals(shapeCounts, shapeSizes);
  const vmwareFacts = [
    vmsOnly
      ? { label: "Kubernetes", value: "later, or never" }
      : { label: "Template", value: findTemplate(catalog, basics.templateId)?.name || "Custom" },
    { label: "Machines", value: `${sum.machines} VMs · ${sum.cpu} vCPU · ${sum.memoryGb} GB` },
    { label: "Disk (thin)", value: `up to ${sum.diskGb} GB` },
    ...(resolved.template ? [{ label: "Clone of", value: resolved.template.name }] : []),
    ...(resolved.datastore ? [{ label: "Datastore", value: resolved.datastore.name }] : []),
  ];

  const rightRail = (() => {
    if (step === STEP_VERIFY && vmware) {
      const machines = planBuild?.provisioning?.spec?.machines || preview.machines;
      return (
        <Blueprint
          plan={machinesBlueprint({
            machines,
            vip: planBuild?.vipAddress || preview.vip,
            endpoint: planBuild?.controlPlaneEndpoint || preview.endpoint,
            state: "stamped",
          })}
          caption={planning ? "planning" : "planned"}
          facts={vmwareFacts}
          note={{
            tone: "plain",
            text: vmsOnly
              ? "Creating the VMs takes a few minutes. KubeSight stops once every VM answers SSH — Kubernetes is not installed."
              : "Creating the VMs takes a few minutes. Kubernetes starts on them right after, as a normal Cluster Builder build.",
          }}
        />
      );
    }
    if (step === STEP_VERIFY && stampedPlan && grouped) {
      return (
        <Blueprint
          plan={stampedPlan}
          note={grouped.counts.fail
            ? { tone: "bad", text: `${grouped.machineCounts.fail} machine(s) must be fixed before this cluster can be built.` }
            : { tone: "good", text: "Every machine answered. Nothing has been changed on them yet." }}
        />
      );
    }
    const planFooter = (
      <>
        {!vmsOnly && laterProblem ? (
          <span className="sg-cb-field-error">{laterProblem}</span>
        ) : null}
        {!canExecute ? (
          <span className="muted">
            Save this draft for a reviewer with execute permission to make the plan and create the VMs.
          </span>
        ) : null}
        <button
          className="primary sg-cb-bp-cta"
          type="button"
          disabled={busy || Boolean(vmProblem)
            || (!vmsOnly && (Boolean(addonError) || Boolean(laterProblem)))}
          onClick={canExecute ? makePlan : saveDraft}
        >
          {canExecute ? (busy ? "Saving…" : "Make the plan") : "Save draft for review"}
        </button>
        <small className="muted">Nothing is created until you approve the plan.</small>
      </>
    );
    const footer = step === STEP_SHAPE ? (
      <>
        {!shapeReady && shapeProblem ? <span className="sg-cb-field-hint">{shapeProblem}</span> : null}
        <button
          className="primary sg-cb-bp-cta"
          type="button"
          disabled={!shapeReady}
          onClick={() => setStep(STEP_MACHINES)}
        >
          Next — machines
        </button>
      </>
    ) : step === STEP_MACHINES && vmsOnly ? planFooter : step === STEP_MACHINES ? (
      <>
        {!machinesReady && machinesProblem && !vmware
          ? <span className="sg-cb-field-hint">{machinesProblem}</span> : null}
        <button
          className="primary sg-cb-bp-cta"
          type="button"
          disabled={!machinesReady}
          onClick={() => setStep(STEP_ADDONS)}
        >
          Next — add-ons
        </button>
      </>
    ) : step === STEP_ADDONS ? (
      <>
        {addonError ? <span className="sg-cb-field-error">{addonError}</span> : null}
        <button
          className="primary sg-cb-bp-cta"
          type="button"
          disabled={Boolean(addonError)}
          onClick={() => setStep(STEP_WORKLOADS)}
        >
          Next — workloads
        </button>
      </>
    ) : step === STEP_WORKLOADS ? (
      <>
        {workloadStorageErrors.length ? (
          <span className="sg-cb-field-error">{workloadStorageErrors[0]}</span>
        ) : null}
        <button
          className="primary sg-cb-bp-cta"
          type="button"
          disabled={workloadStorageErrors.length > 0}
          onClick={() => setStep(STEP_APPS)}
        >
          Next — apps
        </button>
      </>
    ) : vmware ? planFooter : canExecute ? (
      <>
        {laterProblem ? (
          <span className="sg-cb-field-error">{laterProblem}</span>
        ) : null}
        <button
          className="primary sg-cb-bp-cta"
          type="button"
          disabled={busy || !countsOk || Boolean(addonError) || !nodesPayload.length
            || Boolean(laterProblem)}
          onClick={runPreflight}
        >
          {busy ? "Running preflight…" : "Run preflight"}
        </button>
      </>
    ) : (
      <>
        {laterProblem ? (
          <span className="sg-cb-field-error">{laterProblem}</span>
        ) : (
          <span className="muted">
            Save this draft for a reviewer with execute permission to run preflight and launch it.
          </span>
        )}
        <button
          className="primary sg-cb-bp-cta"
          type="button"
          disabled={busy || !countsOk || Boolean(addonError) || !nodesPayload.length
            || Boolean(laterProblem)}
          onClick={saveDraft}
        >
          Save draft for review
        </button>
      </>
    );
    if (vmware) {
      return (
        <Blueprint
          plan={machinesBlueprint({
            machines: preview.machines,
            vip: preview.vip,
            endpoint: preview.endpoint,
            state: "outline",
            slotState: () => "set",
          })}
          caption="new VMs · outline"
          facts={step === STEP_ADDONS ? addonFacts : step === STEP_WORKLOADS ? workloadFacts
            : step === STEP_APPS ? appFacts : vmwareFacts}
          note={step === STEP_WORKLOADS
            ? workloadNote
            : step === STEP_APPS ? appsNote
            : step === STEP_MACHINES
              ? (vmProblem
                ? { tone: "warn", text: vmProblem }
                : { tone: "good", text: vmsOnly
                  ? "Ready to plan. KubeSight creates the VMs and stops once they answer SSH."
                  : "Ready to plan. Nothing is created until you approve the plan." })
              : {
                tone: "plain",
                text: `${shapeLabel(shapeCounts)}. Addresses are previews until the plan reserves them.`,
              }}
          footer={footer}
        />
      );
    }
    return (
      <Blueprint
        plan={plan}
        note={step === STEP_WORKLOADS
          ? workloadNote
          : step === STEP_APPS ? appsNote
          : step === STEP_SHAPE
            ? {
              tone: "plain",
              text: `${shapeLabel(basics.counts)}. The drawing fills in as you assign machines.`,
            }
          : affinityNote}
        facts={step === STEP_ADDONS ? addonFacts
          : step === STEP_WORKLOADS ? workloadFacts
            : step === STEP_APPS ? appFacts : []}
        footer={footer}
      />
    );
  })();

  if (!options) return <div className="card sg-cb-card"><p className="muted">Loading…</p></div>;

  return (
    <div
      className="sg-cb-wizard"
      onClickCapture={markInteraction}
      onChangeCapture={markInteraction}
      onKeyDownCapture={markInteraction}
    >
      <div className="sg-cb-wizard-top">
        <StepRail current={step} onGoBack={setStep} steps={steps} />
        <button className="btn-ghost" type="button" onClick={onCancel}>Cancel</button>
      </div>

      <SourcesBar
        basics={basics}
        infra={infra}
        onChange={setBasic}
        editing={editingSource}
        setEditing={setEditingSource}
      />

      <div className="sg-cb-split">
        <div className="sg-cb-vstack">
          {step === STEP_SHAPE ? (
            <GoalChoice
              vmsOnly={vmsOnly}
              canVmware={provisioningConnections.length > 0}
              onChange={(only) => setBasics((previous) => ({
                ...previous,
                vmsOnly: only,
                machineSource: only ? "vmware" : previous.machineSource,
                vm: only && !previous.vm.connectionId && provisioningConnections[0]
                  ? { ...previous.vm, connectionId: String(provisioningConnections[0].id) }
                  : previous.vm,
              }))}
            />
          ) : null}

          {step === STEP_SHAPE && vmsOnly ? <VmsOnlyStep basics={basics} setBasic={setBasic} /> : null}

          {step === STEP_SHAPE && !vmsOnly ? (
            <>
              <div className="card sg-cb-card">
                <div className="sg-cb-sect">
                  <h2>Start from a template</h2>
                  <span className="sg-cb-sect-right">sizes can change on the next step</span>
                </div>
                <p className="muted sg-cb-pv-lede">
                  A template sets the shape, the machine sizes and, for saved ones, the add-ons. Next you choose
                  whether KubeSight creates the VMs or uses machines you already have.
                </p>
                <TemplateGallery
                  catalog={catalog}
                  selectedId={basics.templateId}
                  onSelect={chooseTemplate}
                  canManage={canManageTemplates}
                  notify={notify}
                  onCatalogChanged={onOptionsChanged}
                />
                {basics.templateId === "custom" ? (
                  <CountsEditor
                    counts={basics.counts}
                    onChange={(counts) => setBasic("counts", counts)}
                  />
                ) : null}
              </div>
              <ShapeStep options={options} basics={basics} setBasic={setBasic} />
            </>
          ) : null}

          {step === STEP_MACHINES ? (
            <>
              {vmsOnly ? null : (
                <SourceChoice
                  value={basics.machineSource}
                  canVmware={provisioningConnections.length > 0}
                  onChange={(source) => setBasics((previous) => ({
                    ...previous,
                    machineSource: source,
                    vm: source === "vmware" && !previous.vm.connectionId && provisioningConnections[0]
                      ? { ...previous.vm, connectionId: String(provisioningConnections[0].id) }
                      : previous.vm,
                  }))}
                />
              )}
              {vmware ? (
                <VmwareMachines
                  connections={provisioningConnections}
                  placementState={placementState}
                  ranges={ranges}
                  vm={basics.vm}
                  setVm={(vm) => setBasic("vm", vm)}
                  counts={shapeCounts}
                  sizes={basics.sizes}
                  setSizes={(sizes) => setBasic("sizes", sizes)}
                  minimums={minimums}
                  resolved={resolved}
                  preview={preview}
                  previewError={previewError}
                />
              ) : (
                <>
                  <EndpointFields basics={basics} setBasic={setBasic} primaryAddress={primaryAddress} />
                  <MachinesStep
                    basics={basics}
                    infra={infra}
                    vms={vms}
                    vmsLoading={vmsLoading}
                    search={search}
                    setSearch={setSearch}
                    filters={filters}
                    toggleFilter={(key) => setFilters((previous) => ({ ...previous, [key]: !previous[key] }))}
                    picked={picked}
                    setPicked={setPicked}
                    manualNodes={manualNodes}
                    setManualNodes={setManualNodes}
                    conflictHosts={plan.conflictHosts}
                  />
                </>
              )}
            </>
          ) : null}

          {step === STEP_ADDONS ? (
            <div className="card sg-cb-card">
              <div className="sg-cb-sect">
                <h2>Installed after the cluster registers</h2>
                <span className="sg-cb-sect-right">
                  {selectedAddons.length
                    ? `${selectedAddons.length} selected · runs as the last phase`
                    : "None selected"}
                </span>
              </div>
              <AddonShelf
                catalog={options.addons || []}
                value={basics.addons}
                onChange={(addons) => setBasic("addons", addons)}
                k8sVersion={basics.k8sVersion}
              />
            </div>
          ) : null}

          {step === STEP_WORKLOADS ? (
            <div className="card sg-cb-card">
              <div className="sg-cb-sect">
                <h2>Bring workloads from an existing cluster</h2>
                <span className="sg-cb-sect-right">
                  {basics.workloads.items.length
                    ? `${workloadSelectionSummary(basics.workloads.items)} · copied as the last phase`
                    : "Optional — skip to build an empty cluster"}
                </span>
              </div>
              <p className="muted sg-cb-wl-lede">
                A copy, not a move: the source cluster is only read. Each workload
                travels with the ConfigMaps, Secrets, volume claims, Services and
                Ingresses it references, so it starts on its own — cluster IPs,
                node ports and bound volumes are left behind for the new cluster
                to allocate its own.
              </p>
              <WorkloadsPicker
                value={basics.workloads}
                onChange={(next) => setBasic("workloads", next)}
                onPlanChange={setWorkloadPlan}
                notify={notify}
              />
            </div>
          ) : null}

          {step === STEP_APPS ? (
            <div className="card sg-cb-card">
              <div className="sg-cb-sect">
                <h2>Helm charts from Inventory</h2>
                <span className="sg-cb-sect-right">
                  {(basics.apps || []).length
                    ? `${basics.apps.length} chart${basics.apps.length === 1 ? "" : "s"} · installed as the last phase`
                    : "Optional — skip to build an empty cluster"}
                </span>
              </div>
              <p className="muted sg-cb-wl-lede">
                Each chart goes in with your values.yaml layered over the chart&apos;s own
                values (or one of its values-*.yaml files). Values are kept encrypted
                and deleted once the chart is installed; preflight renders every chart
                before any machine is touched.
              </p>
              <AppsPicker
                value={basics.apps || []}
                onChange={(next) => setBasic("apps", next)}
                notify={notify}
              />
            </div>
          ) : null}

          {step === STEP_VERIFY && vmware && planBuild ? (
            <>
              <ProvisionCard
                build={planBuild}
                canExecute={canExecute}
                canCreate
                currentUserId={currentUserId}
                notify={notify}
                onChanged={refreshPlanBuild}
              />
              <div className="sg-cb-actions">
                <button className="btn-outline" type="button" onClick={() => setStep(STEP_MACHINES)}>
                  Change something
                </button>
                <span className="muted">
                  Changing anything throws this plan away; making the plan again reserves the same addresses.
                </span>
              </div>
            </>
          ) : null}

          {step === STEP_VERIFY && !vmware && preflightResult && grouped ? (
            <>
              <PreflightReport
                grouped={grouped}
                preflightResult={preflightResult}
                busy={busy}
                onRerun={runPreflight}
              />
              {grouped.verdict === "warn" ? (
                <div className="sg-cb-ackbar">
                  <label>
                    <input
                      type="checkbox"
                      checked={acked}
                      onChange={(event) => setAcked(event.target.checked)}
                    />
                    I understand the {grouped.counts.warn} warning
                    {grouped.counts.warn === 1 ? "" : "s"} and want to proceed.
                  </label>
                  <button
                    className="primary"
                    type="button"
                    disabled={!acked || busy}
                    onClick={launch}
                  >
                    {busy ? "Starting…" : "Build cluster"}
                  </button>
                </div>
              ) : (
                <div className="sg-cb-actions">
                  <button
                    className="btn-outline"
                    type="button"
                    onClick={() => setStep(STEP_MACHINES)}
                  >
                    Back to machines
                  </button>
                  <button
                    className="primary"
                    type="button"
                    disabled={grouped.verdict === "fail" || busy}
                    onClick={launch}
                  >
                    {busy ? "Starting…" : "Build cluster"}
                  </button>
                </div>
              )}
            </>
          ) : null}
        </div>

        {rightRail}
      </div>
    </div>
  );
}
