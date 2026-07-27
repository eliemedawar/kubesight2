/** The new-build wizard: Shape → Machines → Add-ons → Verify & build.
 *
 *  The old step 1 held eleven fields plus the whole add-on catalog in one grid,
 *  mixing what the cluster *is* with the infrastructure plumbing a build
 *  consumes. Here each step holds one kind of decision, the plumbing lives in a
 *  pre-resolved Sources row, and the Blueprint on the right is the same object
 *  from the first keystroke to the finished cluster.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import Blueprint from "./Blueprint.jsx";
import { Field, StatusPill } from "./common.jsx";
import { addonSelectionError, ipRangeListError } from "../../utils/addonConfig.js";
import {
  ROLE_LABELS,
  addonProvenance,
  draftBlueprint,
  groupChecks,
  hostByAddress,
  preferredSources,
  preflightBlueprint,
} from "../../utils/clusterBuilder.js";
import {
  createClusterBuild,
  listVSphereVms,
  preflightClusterBuild,
  startClusterBuild,
  updateClusterBuild,
} from "../../api/clusterBuildsApi.js";

const STEPS = ["Shape", "Machines", "Add-ons", "Verify & build"];

const EMPTY_BASICS = {
  name: "",
  k8sVersion: "",
  topologyType: "stacked_ha",
  endpointMode: "managed_haproxy",
  vipAddress: "",
  controlPlaneEndpoint: "",
  cniPlugin: "calico",
  podCidr: "10.244.0.0/16",
  serviceCidr: "10.96.0.0/12",
  addons: [],
  vsphereConnectionId: "",
  buildProfileId: "",
  connectionProfileId: "",
};

const ROLE_KEYS = [
  ["loadbalancer", "LB"],
  ["control_plane", "CP"],
  ["worker", "W"],
];

function StepRail({ current, onGoBack }) {
  return (
    <nav className="sg-cb-steps" aria-label="Build steps">
      {STEPS.map((label, index) => {
        const state = index === current ? "is-on" : index < current ? "is-done" : "";
        const reachable = index < current;
        return (
          <span className="sg-cb-steps-cell" key={label}>
            {index > 0 ? <span className="sg-cb-steps-arrow" aria-hidden="true">→</span> : null}
            {reachable ? (
              <button type="button" className={`sg-cb-step ${state}`} onClick={() => onGoBack(index)}>
                <i>✓</i>{label}
              </button>
            ) : (
              <span className={`sg-cb-step ${state}`} aria-current={index === current ? "step" : undefined}>
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
    {
      key: "vsphere",
      who: "vCenter",
      what: vsphere?.name || "None — manual hosts",
      state: vsphere ? (vsphere.lastConnectionStatus === "ok" ? "ok" : "warn") : "idle",
      field: "vsphereConnectionId",
      options: [
        { value: "", label: "None (manual hosts)" },
        ...infra.vsphere.map((row) => ({ value: String(row.id), label: row.name })),
      ],
    },
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

function ChoiceCard({ selected, title, shape, description, onSelect }) {
  return (
    <button
      type="button"
      className="sg-cb-choice"
      aria-pressed={selected}
      onClick={onSelect}
    >
      <span className="ct">{title}</span>
      {shape ? <span className="cs sg-cb-mono">{shape}</span> : null}
      {description ? <span className="cd">{description}</span> : null}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Step 1 — Shape
// ---------------------------------------------------------------------------

function ShapeStep({ options, basics, setBasic }) {
  return (
    <div className="card sg-cb-card sg-cb-fields">
      <Field
        label="Cluster name"
        htmlFor="cb-name"
        hint="Becomes the cluster's name in Clusters, Dashboard and Inventory once it is alive."
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
      </Field>

      <Field label="Topology">
        <div className="sg-cb-choices">
          <ChoiceCard
            selected={basics.topologyType === "stacked_ha"}
            title="Highly available"
            shape="2 LB · 3 CP · N workers"
            description="Survives losing one control plane and one load balancer. etcd keeps quorum at 2 of 3."
            onSelect={() => setBasic("topologyType", "stacked_ha")}
          />
          <ChoiceCard
            selected={basics.topologyType === "single_cp"}
            title="Single control plane"
            shape="1 CP · N workers"
            description="Lab shape. One machine failure takes the cluster with it, and there is no LB failover."
            onSelect={() => setBasic("topologyType", "single_cp")}
          />
        </div>
      </Field>

      <Field label="API endpoint">
        <div className="sg-cb-choices">
          {options.endpointModes.map((mode) => (
            <ChoiceCard
              key={mode.id}
              selected={basics.endpointMode === mode.id}
              title={mode.id === "managed_haproxy" ? "KubeSight manages a VIP" : mode.label}
              shape={mode.id === "managed_haproxy" ? "haproxy + keepalived" : "host:port"}
              description={mode.description}
              onSelect={() => setBasic("endpointMode", mode.id)}
            />
          ))}
        </div>
      </Field>

      {basics.endpointMode === "managed_haproxy" ? (
        <Field
          label="VIP address"
          htmlFor="cb-vip"
          hint={basics.topologyType === "single_cp"
            ? "An unused address on the same L2 network. KubeSight assigns it to the single managed load balancer; this lab shape has no failover."
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
      ) : (
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
            placeholder="k8s-api.example.com:6443"
          />
        </Field>
      )}

      <details className="sg-cb-adv">
        <summary>
          Networking
          <span className="sv sg-cb-mono">
            {basics.cniPlugin} · pods {basics.podCidr} · services {basics.serviceCidr}
          </span>
          <span className="cv">defaults are fine — open to change</span>
        </summary>
        <div className="sg-cb-adv-body">
          <Field label="CNI plugin" htmlFor="cb-cni">
            <select
              id="cb-cni"
              className="sg-cb-input"
              value={basics.cniPlugin}
              onChange={(event) => setBasic("cniPlugin", event.target.value)}
            >
              {options.cniPlugins.map((plugin) => (
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
        <ManualHosts manualNodes={manualNodes} setManualNodes={setManualNodes} />
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
function ManualHosts({ manualNodes, setManualNodes }) {
  const [open, setOpen] = useState(manualNodes.length > 0);
  if (!open) {
    return (
      <div className="sg-cb-manual-teaser">
        <span className="muted">Machine not in vCenter?</span>
        <button className="btn-outline btn-sm" type="button" onClick={() => setOpen(true)}>
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

function AddonShelf({ catalog, value, onChange }) {
  const selectedById = new Map(value.map((addon) => [addon.id, addon]));

  const toggle = (entry, checked) => {
    if (!checked) {
      onChange(value.filter((item) => item.id !== entry.id));
      return;
    }
    const version = entry.defaultVersion || entry.versions?.[0] || "";
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
        const version = selected?.version || entry.defaultVersion || entry.versions?.[0] || "";
        const provenance = addonProvenance(entry, version);
        const installable = (entry.versions || []).length > 0;
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
              </span>
            </label>

            {selected && (entry.versions || []).length ? (
              <div className="sg-cb-addon-row">
                <span>Version</span>
                <select
                  className="sg-cb-input"
                  aria-label={`${entry.displayName} version`}
                  value={version}
                  onChange={(event) => setVersion(entry.id, event.target.value)}
                >
                  {entry.versions.map((candidate) => (
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
// Step 4 — Verify
// ---------------------------------------------------------------------------

function CheckGroup({ group, open }) {
  return (
    <details className="sg-cb-chk" open={open}>
      <summary>
        <StatusPill status={group.status} />
        <span className="sg-cb-chk-id">
          <span className="ct">{group.label}</span>
          {group.machines[0]?.detail
            ? <span className="cw">{group.machines[0].detail}</span>
            : null}
        </span>
        <span className="sg-cb-chk-n">
          {group.machines.length} machine{group.machines.length === 1 ? "" : "s"}
        </span>
        <span className="sg-cb-chev" aria-hidden="true">›</span>
      </summary>
      <div className="sg-cb-chk-body">
        <div className="sg-cb-whos">
          {group.machines.map((machine) => (
            <span className="sg-cb-who sg-cb-mono" key={`${machine.nodeId}-${machine.name}`}>
              {machine.name}
              {machine.detail && machine.detail !== group.machines[0].detail
                ? ` — ${machine.detail}`
                : ""}
            </span>
          ))}
        </div>
        {group.hint ? (
          <p className={`sg-cb-fix is-${group.status}`}>
            <b>Fix</b>
            <span>{group.hint}</span>
          </p>
        ) : null}
      </div>
    </details>
  );
}

function VerifyStep({ grouped, preflightResult, busy, onRerun }) {
  const { counts, total, attention, passSummary, verdict } = grouped;
  const good = total ? (counts.pass / total) * 100 : 0;
  const ringStyle = {
    background: `conic-gradient(var(--ok) 0 ${good}%, var(--warn) ${good}% ${
      ((counts.pass + counts.warn) / (total || 1)) * 100
    }%, var(--danger) ${((counts.pass + counts.warn) / (total || 1)) * 100}% 100%)`,
  };

  const headline = counts.fail
    ? `${counts.pass} of ${total} checks pass — ${counts.fail} must be fixed`
    : counts.warn
      ? `${counts.pass} of ${total} checks pass — ${counts.warn} warning${
        counts.warn === 1 ? "" : "s"}, nothing blocking`
      : `All ${total} checks pass`;

  return (
    <>
      <div className="card sg-cb-verdict">
        <div className="sg-cb-vring" style={ringStyle} role="img"
             aria-label={`${counts.pass} of ${total} checks pass`}>
          <b>{total}</b>
        </div>
        <div>
          <h3>{headline}</h3>
          <p className="muted">
            {verdict === "fail"
              ? "Fix the failures and re-run — failures are never acknowledgeable."
              : verdict === "warn"
                ? "Warnings can be acknowledged below, and the acknowledgement is recorded against the build."
                : "Ready to build."}
          </p>
        </div>
        <button className="btn-outline sg-cb-rerun" type="button" disabled={busy} onClick={onRerun}>
          Re-run preflight
        </button>
      </div>

      {(preflightResult.topologyWarnings || []).map((warning) => (
        <p key={warning} className="sg-cb-topowarn">⚠ {warning}</p>
      ))}

      {/* One row per check, not per machine: a kernel module missing on three
          machines is one finding with one fix, and the count is the severity. */}
      {attention.map((group) => (
        <CheckGroup key={group.key} group={group} open={group.status === "fail"} />
      ))}

      {passSummary.checkCount ? (
        <details className="sg-cb-chk">
          <summary>
            <StatusPill status="pass" />
            <span className="sg-cb-chk-id">
              <span className="ct">
                {passSummary.checkCount} check{passSummary.checkCount === 1 ? "" : "s"} passed
              </span>
              <span className="cw">
                grouped so the page stays about what needs attention
              </span>
            </span>
            <span className="sg-cb-chk-n">
              {passSummary.machines.length} machine{passSummary.machines.length === 1 ? "" : "s"}
            </span>
            <span className="sg-cb-chev" aria-hidden="true">›</span>
          </summary>
          <div className="sg-cb-chk-body">
            <div className="sg-cb-whos">
              {grouped.passing.map((group) => (
                <span className="sg-cb-who" key={group.key}>{group.label}</span>
              ))}
            </div>
          </div>
        </details>
      ) : null}
    </>
  );
}

// ---------------------------------------------------------------------------
// Wizard
// ---------------------------------------------------------------------------

export default function Wizard({ options, infra, notify, onBuildLaunched, onCancel }) {
  const [step, setStep] = useState(0);
  const [basics, setBasics] = useState({ ...EMPTY_BASICS });
  const [vms, setVms] = useState([]);
  const [vmsLoading, setVmsLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState({ poweredOn: true, tools: false, big: false });
  const [picked, setPicked] = useState({});
  const [manualNodes, setManualNodes] = useState([]);
  const [buildId, setBuildId] = useState(null);
  const [preflightResult, setPreflightResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [acked, setAcked] = useState(false);
  const [editingSource, setEditingSource] = useState(null);
  const seeded = useRef(false);

  const setBasic = (key, value) => setBasics((previous) => ({ ...previous, [key]: value }));

  // Resolve the plumbing once, from whatever is already healthy. This is what
  // lets the Sources row be a statement rather than three questions.
  useEffect(() => {
    if (seeded.current || !options) return;
    seeded.current = true;
    const { vcenter, route, buildProfile } = preferredSources(infra);
    setBasics((previous) => ({
      ...previous,
      k8sVersion: previous.k8sVersion || options.k8sVersions[0] || "",
      podCidr: options.defaults?.podCidr || previous.podCidr,
      serviceCidr: options.defaults?.serviceCidr || previous.serviceCidr,
      vsphereConnectionId: vcenter ? String(vcenter.id) : "",
      connectionProfileId: route ? String(route.id) : "",
      buildProfileId: buildProfile ? String(buildProfile.id) : "",
    }));
  }, [options, infra]);

  useEffect(() => {
    let ignore = false;
    if (!basics.vsphereConnectionId) { setVms([]); return undefined; }
    setVmsLoading(true);
    listVSphereVms(basics.vsphereConnectionId)
      .then((data) => { if (!ignore) setVms(data.items || []); })
      .catch((error) => notify(`vCenter inventory failed: ${error.message}`, true))
      .finally(() => { if (!ignore) setVmsLoading(false); });
    return () => { ignore = true; };
  }, [basics.vsphereConnectionId, notify]);

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

  const addonError = useMemo(
    () => addonSelectionError(basics.addons, options?.addons || []),
    [basics.addons, options]
  );

  const shapeReady = Boolean(
    basics.name.trim()
    && basics.k8sVersion
    && basics.connectionProfileId
    && (basics.endpointMode === "managed_haproxy" ? basics.vipAddress.trim() : basics.controlPlaneEndpoint.trim())
  );

  const runPreflight = async () => {
    setBusy(true);
    setAcked(false);
    try {
      let id = buildId;
      const payload = {
        ...basics,
        vsphereConnectionId: basics.vsphereConnectionId || undefined,
        buildProfileId: basics.buildProfileId || undefined,
        connectionProfileId: basics.connectionProfileId || undefined,
        nodes: nodesPayload,
      };
      if (id) await updateClusterBuild(id, payload);
      else {
        const created = await createClusterBuild(payload);
        id = created.id;
        setBuildId(id);
      }
      setPreflightResult(await preflightClusterBuild(id));
      setStep(3);
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };

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

  const affinityNote = plan.conflictHosts.length
    ? {
      tone: "warn",
      text: `Machines in one HA tier share ${plan.conflictHosts.join(", ")}. Losing that host `
        + "would drop the tier and etcd would lose quorum. Move one to another ESXi host — "
        + "this is a hard preflight failure, not a warning.",
    }
    : countsOk
      ? { tone: "good", text: "✓ Placement is clean — every HA tier spans distinct ESXi hosts." }
      : { tone: "plain", text: "Assign a machine to every slot. Nothing is reserved until preflight runs." };

  const rightRail = (() => {
    if (step === 3 && stampedPlan && grouped) {
      return (
        <Blueprint
          plan={stampedPlan}
          note={grouped.counts.fail
            ? { tone: "bad", text: `${grouped.machineCounts.fail} machine(s) must be fixed before this cluster can be built.` }
            : { tone: "good", text: "Every machine answered. Nothing has been changed on them yet." }}
        />
      );
    }
    const footer = step === 0 ? (
      <button
        className="primary sg-cb-bp-cta"
        type="button"
        disabled={!shapeReady}
        onClick={() => setStep(1)}
      >
        Next — pick machines
      </button>
    ) : step === 1 ? (
      <button
        className="primary sg-cb-bp-cta"
        type="button"
        disabled={!countsOk || Boolean(plan.conflictHosts.length)}
        onClick={() => setStep(2)}
      >
        Next — add-ons
      </button>
    ) : (
      <>
        {addonError ? <span className="sg-cb-field-error">{addonError}</span> : null}
        <button
          className="primary sg-cb-bp-cta"
          type="button"
          disabled={busy || !countsOk || Boolean(addonError) || !nodesPayload.length}
          onClick={runPreflight}
        >
          {busy ? "Running preflight…" : "Run preflight"}
        </button>
      </>
    );
    return (
      <Blueprint
        plan={plan}
        note={step === 0
          ? {
            tone: "plain",
            text: basics.topologyType === "stacked_ha"
              ? "Nine machines is the usual production shape. The drawing fills in as you assign roles."
              : "A lab cluster. The drawing fills in as you assign roles.",
          }
          : affinityNote}
        facts={step === 2 ? addonFacts : []}
        footer={footer}
      />
    );
  })();

  if (!options) return <div className="card sg-cb-card"><p className="muted">Loading…</p></div>;

  return (
    <div className="sg-cb-wizard">
      <div className="sg-cb-wizard-top">
        <StepRail current={step} onGoBack={setStep} />
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
          {step === 0 ? (
            <ShapeStep options={options} basics={basics} setBasic={setBasic} />
          ) : null}

          {step === 1 ? (
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
          ) : null}

          {step === 2 ? (
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
              />
            </div>
          ) : null}

          {step === 3 && preflightResult && grouped ? (
            <>
              <VerifyStep
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
                    Build cluster
                  </button>
                </div>
              ) : (
                <div className="sg-cb-actions">
                  <button className="btn-outline" type="button" onClick={() => setStep(1)}>
                    Back to machines
                  </button>
                  <button
                    className="primary"
                    type="button"
                    disabled={grouped.verdict === "fail" || busy}
                    onClick={launch}
                  >
                    Build cluster
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
