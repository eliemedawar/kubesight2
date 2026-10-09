/** Step 2, when KubeSight creates the VMs: where in vCenter, cloned from what,
 *  how big, and which names and addresses they will get.
 *
 *  Everything here is read from vCenter with the provisioning account — the
 *  account the VMs will actually be created with — so the lists show exactly
 *  what it is allowed to use.
 */

import { useCallback, useEffect, useState } from "react";
import { Field } from "./common.jsx";
import { timeAgo } from "../../utils/clusterBuilder.js";
import {
  DEFAULT_MINIMUMS,
  ROLE_KEYS,
  ROLE_ONE,
  ROLE_TITLE,
  VM_ROLE,
  cannotResize,
  datastoreFit,
  effectiveSizes,
  minimumDisk,
  sizeErrors,
  templateSize,
  totals,
} from "../../utils/clusterProvisioning.js";
import { getVSpherePlacement } from "../../api/clusterBuildsApi.js";

export function useVmwarePlacement(connectionId, notify) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const load = useCallback(async (refresh = false) => {
    if (!connectionId) { setData(null); return; }
    setLoading(true);
    setError("");
    try {
      setData(await getVSpherePlacement(connectionId, refresh));
    } catch (err) {
      setError(err.message || String(err));
      if (notify) notify(`vCenter could not be read: ${err.message}`, true);
    } finally {
      setLoading(false);
    }
  }, [connectionId, notify]);
  useEffect(() => { load(false); }, [load]);
  return { data, loading, error, refresh: () => load(true) };
}

const VERDICT_TEXT = { ok: "Compatible", warn: "Usable, with warnings", bad: "Cannot be used" };
const CHECK_GLYPH = { ok: "✓", warn: "!", bad: "✕", info: "i" };

function TemplatePicker({ templates, value, onChange }) {
  const selected = templates.find((t) => t.id === value);
  return (
    <>
      <div className="sg-cb-vmt" role="radiogroup" aria-label="VM template">
        {templates.map((template) => {
          const verdict = template.compatibility?.status || "warn";
          return (
            <button
              key={template.id}
              type="button"
              role="radio"
              aria-checked={value === template.id}
              disabled={verdict === "bad"}
              className={`sg-cb-vmt-row btn-ghost ${value === template.id ? "is-on" : ""}`}
              onClick={() => onChange(template.id)}
            >
              <span className="rad" aria-hidden="true" />
              <span className="nm sg-cb-mono">{template.path || template.name}</span>
              <span className="os">{template.guestDetail || template.guestFullName || template.guestId}</span>
              <span className={`sg-cb-pill ${verdict === "ok" ? "is-ok" : verdict === "warn" ? "is-warn" : "is-bad"}`}>
                {VERDICT_TEXT[verdict]}
              </span>
            </button>
          );
        })}
        {!templates.length ? (
          <p className="muted sg-cb-vmt-empty">
            No VM templates in this datacenter. Convert a prepared Ubuntu or Rocky VM to a template in vCenter, then refresh.
          </p>
        ) : null}
      </div>
      {selected ? (
        <ul className="sg-cb-pv-checks is-grid">
          {(selected.compatibility?.checks || []).map((check) => (
            <li key={check.label} className={`is-${check.status}`}>
              <i aria-hidden="true">{CHECK_GLYPH[check.status] || "·"}</i>
              <span><b>{check.label}</b> {check.detail}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </>
  );
}

/** Kept from the template: every VM gets the template's own size, read-only. */
function TemplateSizes({ counts, template }) {
  const kept = templateSize(template);
  const roles = [...ROLE_KEYS, VM_ROLE].filter((role) => counts[role]);
  if (!kept) {
    return (
      <p className="sg-cb-field-error">
        vCenter did not report {template?.name || "this template"}&apos;s CPU and memory, so its size cannot be kept.
        Refresh the vCenter list, or set the sizes.
      </p>
    );
  }
  return (
    <div className="table-wrap">
      <table className="sg-cb-sizes">
        <thead><tr><th>Role</th><th>Count</th><th>vCPU</th><th>Memory GB</th><th>Disk GB</th></tr></thead>
        <tbody>
          {roles.map((role) => (
            <tr key={role}>
              <td><span className={`sg-cb-rolechip is-${role}`}><i />{ROLE_TITLE[role]}</span></td>
              <td className="sg-cb-mono">{counts[role]}</td>
              <td className="sg-cb-mono">{kept.cpu}</td>
              <td className="sg-cb-mono">{kept.memoryMb % 1024 ? (kept.memoryMb / 1024).toFixed(1) : kept.memoryGb}</td>
              <td className="sg-cb-mono">{kept.diskGb}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SizesTable({ counts, sizes, setSizes, template, minimums }) {
  const errors = sizeErrors(counts, sizes, minimums);
  const roles = [...ROLE_KEYS, VM_ROLE].filter((role) => counts[role]);
  return (
    <>
      <div className="table-wrap">
        <table className="sg-cb-sizes">
          <thead>
            <tr><th>Role</th><th>Count</th><th>vCPU</th><th>Memory GB</th><th>Disk GB</th><th /></tr>
          </thead>
          <tbody>
            {roles.map((role) => {
              const min = minimums?.[role] || DEFAULT_MINIMUMS[role];
              const diskFloor = minimumDisk(role, template, minimums);
              return (
                <tr key={role}>
                  <td><span className={`sg-cb-rolechip is-${role}`}><i />{ROLE_TITLE[role]}</span></td>
                  <td className="sg-cb-mono">{counts[role]}</td>
                  {["cpu", "memoryGb", "diskGb"].map((key) => {
                    const value = sizes[role]?.[key] ?? "";
                    const floor = key === "diskGb" ? min.diskGb : min[key];
                    return (
                      <td key={key}>
                        <input
                          type="number"
                          min={floor}
                          className={`sg-cb-input sg-cb-mono sg-cb-pv-num ${Number(value) < floor ? "is-bad" : ""}`}
                          aria-label={`${ROLE_TITLE[role]} ${key}`}
                          value={value}
                          onChange={(event) => setSizes({
                            ...sizes,
                            [role]: { ...sizes[role], [key]: Number(event.target.value) },
                          })}
                        />
                      </td>
                    );
                  })}
                  <td className="muted sg-cb-mono sg-cb-sizes-min">
                    min {min.cpu} · {min.memoryGb} · {min.diskGb}
                    {diskFloor > (sizes[role]?.diskGb || 0) ? ` · clone gets ${diskFloor} GB` : ""}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {errors.map((error) => <p key={error} className="sg-cb-field-error">{error}</p>)}
    </>
  );
}

export default function VmwareMachines({
  connections, placementState, ranges, vm, setVm, counts, sizes, setSizes,
  minimums, resolved, preview, previewError,
}) {
  const { data, loading, error, refresh } = placementState;
  const datacenters = data?.datacenters || [];
  const { dc, cluster, datastore, network, template, range } = resolved;
  const keepTemplate = vm.sizeMode === "template";
  const existingFolder = vm.folderMode === "existing";
  const connection = connections.find((row) => String(row.id) === String(vm.connectionId));
  const sum = totals(counts, effectiveSizes(counts, sizes, vm.sizeMode, template));
  const fit = datastoreFit(datastore, sum.diskGb);
  const set = (key, value) => setVm({ ...vm, [key]: value });

  if (!connections.length) {
    return (
      <div className="card sg-cb-card">
        <p className="muted">
          No vCenter here has a provisioning account yet, so KubeSight cannot create VMs. An administrator
          adds one under Sources → vCenter. Until then, use machines you already have.
        </p>
      </div>
    );
  }

  return (
    <>
      <div className="card sg-cb-card">
        <div className="sg-cb-sect">
          <h2>Where in vCenter</h2>
          <span className="sg-cb-sect-right">
            {loading ? "Reading vCenter…" : data?.fetchedAt
              ? <>read {timeAgo(new Date(data.fetchedAt * 1000).toISOString())}{data.demo ? " · demo inventory" : ""}</>
              : null}
            <button className="btn-ghost btn-sm" type="button" onClick={refresh} disabled={loading}>Refresh</button>
          </span>
        </div>
        <p className="muted sg-cb-pv-lede">
          Read with the provisioning account.{" "}
          {existingFolder
            ? "The VMs go straight into the folder you choose; KubeSight never creates or deletes that folder."
            : "OpenTofu creates a folder for this build inside the one you choose, and removes it with the VMs."}
        </p>
        {error ? <p className="sg-cb-field-error">{error}</p> : null}
        <div className="sg-cb-qgrid">
          <Field label="vCenter" htmlFor="vm-vc">
            <select id="vm-vc" className="sg-cb-input" value={vm.connectionId}
                    onChange={(e) => setVm({ ...vm, connectionId: e.target.value, datacenterId: "" })}>
              {connections.map((row) => <option key={row.id} value={String(row.id)}>{row.name}</option>)}
            </select>
          </Field>
          <Field label="Datacenter" htmlFor="vm-dc">
            <select id="vm-dc" className="sg-cb-input" value={vm.datacenterId}
                    onChange={(e) => setVm({ ...vm, datacenterId: e.target.value, clusterId: "" })}>
              {datacenters.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
          </Field>
          <Field
            label="vSphere cluster"
            htmlFor="vm-cl"
            hint={cluster ? `${cluster.hostCount} ESXi host${cluster.hostCount === 1 ? "" : "s"} · DRS ${cluster.drsEnabled ? "on" : "off"}` : ""}
          >
            <select id="vm-cl" className="sg-cb-input" value={vm.clusterId}
                    onChange={(e) => setVm({ ...vm, clusterId: e.target.value, resourcePoolId: "" })}>
              {(dc?.clusters || []).map((c) => <option key={c.id} value={c.id}>{c.path || c.name}</option>)}
            </select>
          </Field>
          <Field label="Resource pool" htmlFor="vm-pool">
            <select id="vm-pool" className="sg-cb-input" value={vm.resourcePoolId}
                    onChange={(e) => set("resourcePoolId", e.target.value)}>
              <option value="">The cluster itself</option>
              {(cluster?.resourcePools || []).map((p) => <option key={p.id} value={p.id}>{p.path}</option>)}
            </select>
          </Field>
          <Field label="VM folder" htmlFor="vm-folder"
                 hint={existingFolder
                   ? (resolved.folder ? `VMs go into ${resolved.folder.path}` : "")
                   : `New folder: ${resolved.folder ? `${resolved.folder.path}/` : ""}<cluster name>`}
                 error={existingFolder && !resolved.folder ? "Choose the folder the VMs go into." : ""}>
            <select id="vm-folder" className="sg-cb-input" value={vm.folderParentId}
                    onChange={(e) => set("folderParentId", e.target.value)}>
              <option value="">{existingFolder ? "Choose…" : "At the datacenter's top level"}</option>
              {(dc?.folders || []).map((f) => <option key={f.id} value={f.id}>{f.path}</option>)}
            </select>
            <div className="sg-cb-seg sg-cb-pv-mode" role="group" aria-label="Folder">
              <button type="button" aria-pressed={!existingFolder} onClick={() => set("folderMode", "create")}>
                Create a folder inside it
              </button>
              <button type="button" aria-pressed={existingFolder} onClick={() => set("folderMode", "existing")}>
                Put the VMs straight in
              </button>
            </div>
          </Field>
          <Field label="Datastore" htmlFor="vm-ds">
            <select id="vm-ds" className="sg-cb-input" value={vm.datastoreId}
                    onChange={(e) => set("datastoreId", e.target.value)}>
              {(dc?.datastores || []).map((d) => (
                <option key={d.id} value={d.id} disabled={d.accessible === false}>
                  {d.name} — {d.freeGb >= 1024 ? `${(d.freeGb / 1024).toFixed(1)} TB` : `${d.freeGb} GB`} free
                </option>
              ))}
            </select>
            {datastore ? (
              <div className="sg-cb-cap">
                <div className="sg-cb-cap-bar">
                  <span className="used" style={{ width: `${fit.usedPct}%` }} />
                  <span className={`need ${fit.over ? "is-over" : ""}`}
                        style={{ left: `${fit.usedPct}%`, width: `${fit.needPct}%` }} />
                </div>
                <span className={fit.over ? "sg-cb-warn-text" : "muted"}>{fit.text}</span>
              </div>
            ) : null}
          </Field>
          <Field
            label="Network"
            htmlFor="vm-net"
            hint={range
              ? `Range ${range.rangeStart} – ${range.rangeEnd} · gateway ${range.gateway} · DNS ${range.dnsServers.join(", ")}`
              : network ? "" : "Only networks with an address range in Sources can be used."}
            error={network && !range ? `${network.name} has no address range. An administrator adds one in Sources.` : ""}
          >
            <select id="vm-net" className="sg-cb-input" value={vm.networkId}
                    onChange={(e) => set("networkId", e.target.value)}>
              <option value="">Choose…</option>
              {(dc?.networks || []).map((n) => {
                const has = ranges.some((r) => r.networkName === n.name);
                return <option key={n.id} value={n.id}>{n.name}{has ? "" : " (no range)"}</option>;
              })}
            </select>
          </Field>
        </div>
        {(counts.controlPlane > 1 || counts.loadbalancer > 1) ? (
          <label className="sg-cb-inlinecheck">
            <input type="checkbox" checked={vm.antiAffinity !== false}
                   onChange={(e) => set("antiAffinity", e.target.checked)} />
            Keep control planes and load balancers on different ESXi hosts (DRS rules)
          </label>
        ) : null}
      </div>

      <div className="card sg-cb-card">
        <div className="sg-cb-sect">
          <h2>VM template</h2>
          <span className="sg-cb-sect-right">{(dc?.templates || []).length} in {dc?.name || "this datacenter"}</span>
        </div>
        <p className="muted sg-cb-pv-lede">Every new machine is a clone of this. KubeSight checks it before any VM is created.</p>
        <TemplatePicker templates={dc?.templates || []} value={vm.templateId}
                        onChange={(id) => set("templateId", id)} />
      </div>

      <div className="card sg-cb-card">
        <div className="sg-cb-sect">
          <h2>Machine sizes</h2>
          <span className="sg-cb-sect-right">{sum.machines} VMs · {sum.cpu} vCPU · {sum.memoryGb} GB</span>
        </div>
        <div className="sg-cb-seg sg-cb-pv-mode" role="group" aria-label="Machine sizes">
          <button type="button" aria-pressed={!keepTemplate} onClick={() => set("sizeMode", "custom")}>
            Set the sizes
          </button>
          <button type="button" aria-pressed={keepTemplate} onClick={() => set("sizeMode", "template")}>
            Keep the VM template&apos;s size
          </button>
        </div>
        <p className="muted sg-cb-pv-lede">
          {keepTemplate
            ? "Every VM is left exactly as the template is: CPU, memory, disks, network cards, CD drive and settings. Only the hostname and address are set, so the account needs no CPU or memory privileges — only Modify device settings, for the network card."
            : "KubeSight sets CPU, memory and disk on each clone. That needs Change CPU count and Change memory on the folder."}
          {cannotResize(connection)
            ? ` The last privilege check on ${connection.name} says this account may not change CPU or memory — keep the template's size.`
            : ""}
        </p>
        {keepTemplate
          ? <TemplateSizes counts={counts} template={template} />
          : <SizesTable counts={counts} sizes={sizes} setSizes={setSizes} template={template} minimums={minimums} />}
      </div>

      <div className="card sg-cb-card">
        <div className="sg-cb-sect">
          <h2>Names and addresses</h2>
          <span className="sg-cb-sect-right"><span className="sg-cb-pill is-muted">preview — the plan reserves them</span></span>
        </div>
        <p className="muted sg-cb-pv-lede">
          Static addresses from {range ? `${range.rangeStart} – ${range.rangeEnd}` : "the network's range"}. The plan skips
          any address another build holds or that answers on the network.
        </p>
        {previewError ? <p className="sg-cb-field-error">{previewError}</p> : null}
        <div className="table-wrap">
          <table className="sg-cb-pv-vms">
            <thead><tr><th>VM name</th><th>Role</th><th>Address</th></tr></thead>
            <tbody>
              {preview.vip ? (
                <tr>
                  <td className="muted">—</td>
                  <td><span className="sg-cb-pill is-info">API address</span></td>
                  <td className="sg-cb-mono">{preview.vip}</td>
                </tr>
              ) : null}
              {preview.machines.map((machine) => (
                <tr key={machine.name}>
                  <td className="sg-cb-mono">{machine.name}</td>
                  <td>{ROLE_ONE[machine.role]}</td>
                  <td className="sg-cb-mono">{machine.ip || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
