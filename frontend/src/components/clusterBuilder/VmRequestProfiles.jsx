/** VM request profiles — where VMs asked for on a ticket go.
 *
 *  Hermes reads a ticket ("two RHEL VMs in DR, 4 vCPU / 8 GB") and picks one of
 *  these by name; everything vCenter-shaped comes from here, never from the
 *  ticket. The placement is chosen with the wizard's own pickers, read with the
 *  provisioning account, so a profile can only point where a build could.
 *  Nothing a ticket asks for is created before a VM approver approves the plan.
 */

import { useEffect, useMemo, useState } from "react";
import { Field } from "./common.jsx";
import { PlacementFields, TemplatePicker, useVmwarePlacement } from "./VmwareMachines.jsx";
import {
  EMPTY_VM_PLACEMENT,
  MAX_VMS,
  defaultPlacement,
  placementFromSpec,
  placementProblem,
  provisioningPayload,
  resolvePlacement,
  templateSize,
} from "../../utils/clusterProvisioning.js";
import {
  createVmRequestProfile,
  deleteVmRequestProfile,
  listVmRequestProfiles,
  updateVmRequestProfile,
} from "../../api/clusterBuildsApi.js";
import { confirmDialog } from "../../lib/dialogs.js";

const DEFAULT_SIZE = { cpu: 2, memoryGb: 4, diskGb: 40 };
const DEFAULT_MAX = { cpu: 8, memoryGb: 32, diskGb: 200 };
const SIZE_KEYS = [["cpu", "vCPU"], ["memoryGb", "Memory GB"], ["diskGb", "Disk GB"]];

const sizeText = (s) => (s ? `${s.cpu} vCPU · ${s.memoryGb} GB · ${s.diskGb} GB` : "—");

function emptyForm(connections) {
  return {
    id: null,
    name: "",
    description: "",
    enabled: true,
    vm: { ...EMPTY_VM_PLACEMENT, connectionId: connections[0] ? String(connections[0].id) : "", folderMode: "existing" },
    connectionProfileId: "",
    defaultSize: { ...DEFAULT_SIZE },
    maxSize: { ...DEFAULT_MAX },
    maxVms: 3,
    allowKubernetes: false,
  };
}

function formFromProfile(row) {
  return {
    id: row.id,
    name: row.name,
    description: row.description || "",
    enabled: row.enabled,
    vm: placementFromSpec(row.placement),
    connectionProfileId: row.connectionProfileId ? String(row.connectionProfileId) : "",
    defaultSize: { ...(row.defaultSize || DEFAULT_SIZE) },
    maxSize: { ...(row.maxSize || DEFAULT_MAX) },
    maxVms: row.maxVms,
    allowKubernetes: row.allowKubernetes,
  };
}

function SizeRow({ label, value, onChange, idPrefix }) {
  return (
    <tr>
      <td>{label}</td>
      {SIZE_KEYS.map(([key, title]) => (
        <td key={key}>
          <input
            id={`${idPrefix}-${key}`}
            type="number"
            min={1}
            className="sg-cb-input sg-cb-mono sg-cb-pv-num"
            aria-label={`${label} ${title}`}
            value={value[key] ?? ""}
            onChange={(e) => onChange({ ...value, [key]: Number(e.target.value) })}
          />
        </td>
      ))}
    </tr>
  );
}

function ProfileEditor({ initial, connections, sshProfiles, notify, onSaved, onClose }) {
  const [form, setForm] = useState(initial);
  const [busy, setBusy] = useState(false);
  const placementState = useVmwarePlacement(form.vm.connectionId, notify);
  const ranges = placementState.data?.networks || [];
  // Fill the blanks once vCenter answers (a new profile starts with nothing chosen).
  useEffect(() => {
    if (!placementState.data) return;
    setForm((previous) => {
      const vm = defaultPlacement(placementState.data, previous.vm, placementState.data.networks || []);
      return JSON.stringify(vm) === JSON.stringify(previous.vm) ? previous : { ...previous, vm };
    });
  }, [placementState.data]);
  const resolved = useMemo(
    () => resolvePlacement(placementState.data, form.vm, ranges),
    [placementState.data, form.vm, ranges]
  );
  const keepTemplate = form.vm.sizeMode === "template";
  const kept = keepTemplate ? templateSize(resolved.template) : null;
  const set = (key, value) => setForm((previous) => ({ ...previous, [key]: value }));
  const setVm = (vm) => set("vm", vm);

  const problem =
    (!form.name.trim() && "Name the profile.") ||
    (placementState.data ? placementProblem(resolved, { vm: 1 }) : "Reading vCenter…") ||
    (!form.connectionProfileId && "Choose the SSH route KubeSight reaches the new VMs by.") ||
    "";

  const save = async () => {
    setBusy(true);
    try {
      const placement = provisioningPayload(
        form.vm.connectionId, resolved, form.vm, { vm: 1 }, { vm: form.defaultSize }, { vmsOnly: true }
      );
      delete placement.counts;
      delete placement.vmCount;
      delete placement.sizes;
      const payload = {
        name: form.name.trim(),
        description: form.description,
        enabled: form.enabled,
        placement,
        connectionProfileId: Number(form.connectionProfileId),
        maxVms: Number(form.maxVms),
        allowKubernetes: form.allowKubernetes,
        ...(keepTemplate ? {} : { defaultSize: form.defaultSize, maxSize: form.maxSize }),
      };
      const saved = form.id
        ? await updateVmRequestProfile(form.id, payload)
        : await createVmRequestProfile(payload);
      notify(`Saved VM request profile ${saved.name}.`);
      onSaved();
    } catch (error) {
      notify(error.serverMessage || error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="sg-cb-vmprofile-edit">
      <div className="sg-cb-qgrid sg-cb-addform">
        <Field label="Name" htmlFor="vmp-name" hint="What Hermes copies onto the request, e.g. “DR — RHEL 8 test VMs”.">
          <input id="vmp-name" className="sg-cb-input" value={form.name}
                 onChange={(e) => set("name", e.target.value)} />
        </Field>
        <Field label="What it is for" htmlFor="vmp-desc"
               hint="Hermes reads this to choose: the site, the OS, who it is for.">
          <input id="vmp-desc" className="sg-cb-input" value={form.description}
                 onChange={(e) => set("description", e.target.value)} />
        </Field>
      </div>

      <h4 className="sg-cb-vmprofile-h">Where the VMs go</h4>
      {placementState.error ? <p className="sg-cb-field-error">{placementState.error}</p> : null}
      <PlacementFields
        connections={connections} placementState={placementState} ranges={ranges}
        vm={form.vm} setVm={setVm} resolved={resolved}
      />

      <h4 className="sg-cb-vmprofile-h">Cloned from</h4>
      <TemplatePicker templates={resolved.dc?.templates || []} value={form.vm.templateId}
                      onChange={(id) => setVm({ ...form.vm, templateId: id })} />

      <h4 className="sg-cb-vmprofile-h">Sizes a ticket may ask for</h4>
      <div className="sg-cb-seg sg-cb-pv-mode" role="group" aria-label="Machine sizes">
        <button type="button" aria-pressed={!keepTemplate} onClick={() => setVm({ ...form.vm, sizeMode: "custom" })}>
          Tickets choose, within limits
        </button>
        <button type="button" aria-pressed={keepTemplate} onClick={() => setVm({ ...form.vm, sizeMode: "template" })}>
          Always the VM template&apos;s size
        </button>
      </div>
      {keepTemplate ? (
        <p className="muted sg-cb-pv-lede">
          Every VM is {kept ? sizeText(kept) : "the template's size"}, whatever the ticket says. Use this
          when the provisioning account may not change CPU or memory.
        </p>
      ) : (
        <div className="table-wrap">
          <table className="sg-cb-sizes">
            <thead><tr><th /><th>vCPU</th><th>Memory GB</th><th>Disk GB</th></tr></thead>
            <tbody>
              <SizeRow label="When the ticket names no size" idPrefix="vmp-def" value={form.defaultSize}
                       onChange={(v) => set("defaultSize", v)} />
              <SizeRow label="The most per VM" idPrefix="vmp-max" value={form.maxSize}
                       onChange={(v) => set("maxSize", v)} />
            </tbody>
          </table>
        </div>
      )}

      <div className="sg-cb-qgrid sg-cb-addform">
        <Field label="VMs per ticket, at most" htmlFor="vmp-max-vms">
          <input id="vmp-max-vms" type="number" min={1} max={MAX_VMS} className="sg-cb-input sg-cb-mono"
                 value={form.maxVms} onChange={(e) => set("maxVms", e.target.value)} />
        </Field>
        <Field label="SSH route to the new VMs" htmlFor="vmp-route"
               hint="KubeSight logs in through it to check the VMs answer (and to install Kubernetes).">
          <select id="vmp-route" className="sg-cb-input" value={form.connectionProfileId}
                  onChange={(e) => set("connectionProfileId", e.target.value)}>
            <option value="">Choose…</option>
            {sshProfiles.map((row) => <option key={row.id} value={String(row.id)}>{row.name}</option>)}
          </select>
        </Field>
        <label className="sg-cb-inlinecheck sg-cb-span">
          <input type="checkbox" checked={form.allowKubernetes}
                 onChange={(e) => set("allowKubernetes", e.target.checked)} />
          Tickets may ask for Kubernetes on the VMs (Lab, Small or Standard HA). Every VM is then at least 2 vCPU · 4 GB · 40 GB.
        </label>
        <label className="sg-cb-inlinecheck sg-cb-span">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} />
          Offered to Hermes
        </label>
      </div>

      {problem ? <p className="sg-cb-field-error">{problem}</p> : null}
      <div className="sg-cb-actions">
        <button className="primary" type="button" disabled={busy || Boolean(problem)} onClick={save}>
          {busy ? "Saving…" : form.id ? "Save profile" : "Add profile"}
        </button>
        <button className="btn-ghost" type="button" onClick={onClose}>Cancel</button>
      </div>
    </div>
  );
}

export default function VmRequestProfiles({ vsphere, sshProfiles, notify }) {
  const connections = useMemo(() => vsphere.filter((row) => row.provisioningConfigured), [vsphere]);
  const [items, setItems] = useState([]);
  const [enabled, setEnabled] = useState(false);
  const [editing, setEditing] = useState(null);

  const load = async () => {
    try {
      const data = await listVmRequestProfiles();
      setItems(data.items || []);
      setEnabled(Boolean(data.vmRequestsEnabled));
    } catch (error) {
      notify(`VM request profiles could not be read: ${error.message}`, true);
    }
  };
  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = async (row) => {
    try {
      await updateVmRequestProfile(row.id, { enabled: !row.enabled });
      load();
    } catch (error) { notify(error.serverMessage || error.message, true); }
  };
  const remove = async (row) => {
    if (!(await confirmDialog({
      title: "Remove VM request profile",
      message: `Remove the VM request profile ${row.name}? Hermes stops offering it; VMs already made stay.`,
      confirmLabel: "Remove",
      tone: "danger",
    }))) return;
    try {
      await deleteVmRequestProfile(row.id);
      load();
    } catch (error) { notify(error.serverMessage || error.message, true); }
  };

  if (!connections.length) {
    return (
      <p className="muted">
        Add a provisioning account to a vCenter first: a profile says where KubeSight creates the VMs.
      </p>
    );
  }

  return (
    <div className="sg-cb-vmprofiles">
      {!enabled ? (
        <p className="sg-cb-warn-text">
          VM requests are switched off for Hermes. Turn them on in Settings → Integrations → Hermes ticket agent.
        </p>
      ) : null}

      {items.map((row) => (
        editing?.id === row.id ? (
          <ProfileEditor key={row.id} initial={editing} connections={connections} sshProfiles={sshProfiles}
                         notify={notify} onClose={() => setEditing(null)}
                         onSaved={() => { setEditing(null); load(); }} />
        ) : (
          <div className={`sg-cb-entry ${row.enabled ? "" : "is-off"}`} key={row.id}>
            <span className="sg-cb-entry-id">
              <span className="en">
                {row.name}
                {row.allowKubernetes ? <span className="sg-cb-pill is-info">Kubernetes allowed</span> : null}
              </span>
              <span className="ea sg-cb-mono">{row.where}</span>
              <span className="ea">
                {row.os || row.templateName} · up to {row.maxVms} VM{row.maxVms === 1 ? "" : "s"} ·{" "}
                {row.sizeMode === "template"
                  ? `always ${sizeText(row.defaultSize)}`
                  : `default ${sizeText(row.defaultSize)}, max ${sizeText(row.maxSize)}`}
                {row.connectionProfileName ? ` · via ${row.connectionProfileName}` : ""}
              </span>
            </span>
            <span className="sg-cb-entry-right">
              <span className={`sg-cb-pill ${row.enabled ? "is-ok" : "is-muted"}`}>
                {row.enabled ? "offered" : "off"}
              </span>
              <button className="btn-ghost btn-sm" type="button" onClick={() => toggle(row)}>
                {row.enabled ? "Turn off" : "Turn on"}
              </button>
              <button className="btn-ghost btn-sm" type="button" onClick={() => setEditing(formFromProfile(row))}>
                Edit
              </button>
              <button className="btn-ghost btn-sm" type="button" onClick={() => remove(row)}>Remove</button>
            </span>
          </div>
        )
      ))}
      {!items.length && !editing ? (
        <p className="muted">No profiles yet. Until there is one, Hermes parks VM tickets as Impediment.</p>
      ) : null}

      {editing && !editing.id ? (
        <ProfileEditor initial={editing} connections={connections} sshProfiles={sshProfiles} notify={notify}
                       onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />
      ) : !editing ? (
        <div className="sg-cb-actions">
          <button className="btn-outline" type="button" onClick={() => setEditing(emptyForm(connections))}>
            Add a profile
          </button>
        </div>
      ) : null}
    </div>
  );
}
