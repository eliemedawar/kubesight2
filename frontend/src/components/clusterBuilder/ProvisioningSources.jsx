/** Sources for KubeSight-created VMs: the account that may create them, the
 *  addresses they may take, and OpenTofu itself (its engine, state and locks).
 */

import { useCallback, useEffect, useState } from "react";
import { Field } from "./common.jsx";
import { timeAgo } from "../../utils/clusterBuilder.js";
import { PROVISION_STATUS_LABELS } from "../../utils/clusterProvisioning.js";
import {
  createVSphereNetwork,
  deleteVSphereNetwork,
  getProvisioningOverview,
  getVSpherePlacement,
  listVSphereNetworks,
  releaseProvisionLock,
  testVSphereProvisioning,
  updateVSphereConnection,
  updateVSphereNetwork,
} from "../../api/clusterBuildsApi.js";

// What a missing privilege means for creating VMs.
const NEED_TEXT = {
  adapts: "not needed: KubeSight adapts the plan",
  destroy: "only needed to destroy the VMs later",
  other: "optional: vCenter decides when the plan is applied",
};

/** The second, VM-creating account on one vCenter, and what it may do. */
export function ProvisioningAccount({ row, notify, reloadInfra }) {
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({ provisioningUsername: row.provisioningUsername || "", provisioningPassword: "" });
  const [busy, setBusy] = useState(false);
  const [showPrivileges, setShowPrivileges] = useState(false);
  const privileges = row.provisioningPrivileges || [];
  const missing = privileges.filter((item) => !item.granted);
  // Only these stop KubeSight cloning a VM; the rest it works around.
  const blocking = missing.filter((item) => (item.need || "required") === "required");

  const save = async () => {
    setBusy(true);
    try {
      await updateVSphereConnection(row.id, {
        name: row.name, baseUrl: row.baseUrl, username: row.username,
        provisioningUsername: form.provisioningUsername,
        ...(form.provisioningPassword ? { provisioningPassword: form.provisioningPassword } : {}),
      });
      setEditing(false);
      setForm((f) => ({ ...f, provisioningPassword: "" }));
      await reloadInfra();
      notify(form.provisioningUsername ? "Provisioning account saved. Check its privileges next." : "Provisioning account removed.");
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };

  const check = async () => {
    setBusy(true);
    try {
      const result = await testVSphereProvisioning(row.id);
      notify(result.status === "failed" ? result.error : result.message, result.status !== "ok");
      setShowPrivileges(result.status !== "failed");
      await reloadInfra();
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="sg-cb-pv-acct">
      <div className="sg-cb-pv-acct-line">
        <span className="sg-cb-pv-acct-label">Provisioning account</span>
        {row.provisioningConfigured ? (
          <>
            <span className="sg-cb-mono">{row.provisioningUsername}</span>
            {row.provisioningLastTestStatus ? (
              <span className={`sg-cb-pill ${row.provisioningLastTestStatus === "ok" ? "is-ok" : row.provisioningLastTestStatus === "warn" ? "is-warn" : "is-bad"}`}>
                {row.provisioningLastTestStatus === "ok"
                  ? `${privileges.length} of ${privileges.length} privileges`
                  : row.provisioningLastTestStatus === "warn"
                    ? `${blocking.length || missing.length} needed privilege${(blocking.length || missing.length) === 1 ? "" : "s"} missing`
                    : "check failed"}
              </span>
            ) : <span className="sg-cb-fresh is-stale">privileges never checked</span>}
            {row.provisioningLastTestAt ? <span className="sg-cb-fresh">{timeAgo(row.provisioningLastTestAt)}</span> : null}
          </>
        ) : (
          <span className="muted">None — KubeSight cannot create VMs on this vCenter.</span>
        )}
        <span className="sg-cb-pv-acct-acts">
          {row.provisioningConfigured ? (
            <button className="btn-ghost btn-sm" type="button" disabled={busy} onClick={check}>
              {busy ? "Checking…" : "Check privileges"}
            </button>
          ) : null}
          <button className="btn-ghost btn-sm" type="button" onClick={() => setEditing(!editing)}>
            {editing ? "Close" : row.provisioningConfigured ? "Change" : "Add one"}
          </button>
        </span>
      </div>
      {row.provisioningLastTestMessage && (row.provisioningLastTestStatus !== "ok" || missing.length) ? (
        <p className="muted sg-cb-pv-acct-msg">{row.provisioningLastTestMessage}</p>
      ) : null}
      {editing ? (
        <div className="sg-cb-qgrid sg-cb-addform">
          <p className="muted sg-cb-span">
            A separate account, so browsing never needs more than Read-Only. Give it a role holding the
            privileges below, on the folder, resource pool, datastore and network builds use.
          </p>
          <Field label="Username" htmlFor={`prov-user-${row.id}`} hint="Leave empty and save to remove the account.">
            <input id={`prov-user-${row.id}`} className="sg-cb-input" value={form.provisioningUsername}
                   placeholder="svc-kubesight-prov@vsphere.local"
                   onChange={(e) => setForm({ ...form, provisioningUsername: e.target.value })} />
          </Field>
          <Field label="Password" htmlFor={`prov-pass-${row.id}`}
                 hint={row.provisioningConfigured ? "Leave empty to keep the stored one." : ""}>
            <input id={`prov-pass-${row.id}`} type="password" className="sg-cb-input" value={form.provisioningPassword}
                   onChange={(e) => setForm({ ...form, provisioningPassword: e.target.value })} />
          </Field>
          <div className="sg-cb-actions sg-cb-span">
            <button className="primary" type="button" disabled={busy} onClick={save}>Save account</button>
          </div>
        </div>
      ) : null}
      {privileges.length && (showPrivileges || missing.length) ? (
        <ul className="sg-cb-pv-privs">
          {privileges.map((item) => (
            <li key={`${item.privilege}-${item.entity}`}
                className={item.granted ? "is-ok" : (item.need || "required") === "required" ? "is-bad" : "is-warn"}>
              <i aria-hidden="true">{item.granted ? "✓" : (item.need || "required") === "required" ? "✕" : "!"}</i>
              <span>
                <b className="sg-cb-mono">{item.privilege}</b> <span className="muted">{item.purpose}</span>
                {!item.granted && NEED_TEXT[item.need] ? <span className="muted"> — {NEED_TEXT[item.need]}</span> : null}
              </span>
            </li>
          ))}
        </ul>
      ) : privileges.length ? (
        <button className="sg-cb-linkbtn" type="button" onClick={() => setShowPrivileges(true)}>
          Show the {privileges.length} privileges checked
        </button>
      ) : null}
    </div>
  );
}

const EMPTY_RANGE = {
  networkName: "", cidr: "", rangeStart: "", rangeEnd: "", gateway: "", dnsServers: "", dnsDomain: "",
};

function RangeForm({ initial, networks, onSave, onCancel, busy }) {
  const [form, setForm] = useState(() => ({
    ...EMPTY_RANGE,
    ...(initial ? { ...initial, dnsServers: (initial.dnsServers || []).join(", "), dnsDomain: initial.dnsDomain || "" } : {}),
  }));
  const set = (key) => (event) => setForm({ ...form, [key]: event.target.value });
  return (
    <div className="sg-cb-qgrid sg-cb-addform">
      <Field label="vCenter network" htmlFor="rg-net">
        {networks.length && !initial ? (
          <select id="rg-net" className="sg-cb-input" value={form.networkName} onChange={set("networkName")}>
            <option value="">Choose…</option>
            {networks.map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
        ) : (
          <input id="rg-net" className="sg-cb-input sg-cb-mono" value={form.networkName} disabled={Boolean(initial)}
                 onChange={set("networkName")} placeholder="VM-Net-K8S-30" />
        )}
      </Field>
      <Field label="Subnet" htmlFor="rg-cidr">
        <input id="rg-cidr" className="sg-cb-input sg-cb-mono" value={form.cidr} onChange={set("cidr")} placeholder="10.20.30.0/24" />
      </Field>
      <Field label="First address" htmlFor="rg-start">
        <input id="rg-start" className="sg-cb-input sg-cb-mono" value={form.rangeStart} onChange={set("rangeStart")} placeholder="10.20.30.50" />
      </Field>
      <Field label="Last address" htmlFor="rg-end">
        <input id="rg-end" className="sg-cb-input sg-cb-mono" value={form.rangeEnd} onChange={set("rangeEnd")} placeholder="10.20.30.90" />
      </Field>
      <Field label="Gateway" htmlFor="rg-gw" hint="Outside the range, so no VM is ever given it.">
        <input id="rg-gw" className="sg-cb-input sg-cb-mono" value={form.gateway} onChange={set("gateway")} placeholder="10.20.30.1" />
      </Field>
      <Field label="DNS servers" htmlFor="rg-dns">
        <input id="rg-dns" className="sg-cb-input sg-cb-mono" value={form.dnsServers} onChange={set("dnsServers")} placeholder="10.20.1.10, 10.20.1.11" />
      </Field>
      <Field label="DNS domain" htmlFor="rg-domain" hint="Optional. Written into each VM.">
        <input id="rg-domain" className="sg-cb-input sg-cb-mono" value={form.dnsDomain} onChange={set("dnsDomain")} placeholder="areeba.local" />
      </Field>
      <div className="sg-cb-actions sg-cb-span">
        <button className="btn-ghost" type="button" onClick={onCancel}>Cancel</button>
        <button className="primary" type="button" disabled={busy} onClick={() => onSave(form)}>Save range</button>
      </div>
    </div>
  );
}

/** Address ranges per vCenter network: where new VMs get their addresses. */
export function NetworkRanges({ connections, notify }) {
  const [rangesById, setRangesById] = useState({});
  const [networksById, setNetworksById] = useState({});
  const [editing, setEditing] = useState(null); // {connectionId, range|null}
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const entries = await Promise.all(connections.map(async (row) => {
      try {
        return [row.id, (await listVSphereNetworks(row.id)).items || []];
      } catch {
        return [row.id, []];
      }
    }));
    setRangesById(Object.fromEntries(entries));
  }, [connections]);
  useEffect(() => { load(); }, [load]);

  const openAdd = async (connectionId) => {
    setEditing({ connectionId, range: null });
    if (networksById[connectionId]) return;
    try {
      const placement = await getVSpherePlacement(connectionId);
      const names = [...new Set((placement.datacenters || []).flatMap((dc) => dc.networks.map((n) => n.name)))].sort();
      setNetworksById((prev) => ({ ...prev, [connectionId]: names }));
    } catch {
      setNetworksById((prev) => ({ ...prev, [connectionId]: [] }));
    }
  };

  const save = async (form) => {
    setBusy(true);
    try {
      if (editing.range) await updateVSphereNetwork(editing.connectionId, editing.range.id, form);
      else await createVSphereNetwork(editing.connectionId, form);
      setEditing(null);
      await load();
      notify(`Range for ${form.networkName} saved.`);
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (connectionId, range) => {
    if (!window.confirm(`Remove the address range for ${range.networkName}?`)) return;
    try {
      await deleteVSphereNetwork(connectionId, range.id);
      await load();
    } catch (error) {
      notify(error.message || String(error), true);
    }
  };

  if (!connections.length) {
    return <p className="muted">Add a vCenter first.</p>;
  }
  return (
    <>
      {connections.map((row) => {
        const ranges = rangesById[row.id] || [];
        return (
          <div className="sg-cb-pv-ranges" key={row.id}>
            <div className="sg-cb-pv-ranges-head">
              <b>{row.name}</b>
              <button className="btn-ghost btn-sm" type="button" onClick={() => openAdd(row.id)}>Add a range</button>
            </div>
            {editing && editing.connectionId === row.id ? (
              <RangeForm initial={editing.range} networks={networksById[row.id] || []}
                         busy={busy} onSave={save} onCancel={() => setEditing(null)} />
            ) : null}
            {ranges.length ? (
              <div className="table-wrap">
                <table className="sg-cb-pv-vms">
                  <thead>
                    <tr><th>Network</th><th>Subnet</th><th>Range</th><th>Gateway · DNS</th><th>Held</th><th /></tr>
                  </thead>
                  <tbody>
                    {ranges.map((range) => (
                      <tr key={range.id}>
                        <td className="sg-cb-mono">{range.networkName}</td>
                        <td className="sg-cb-mono">{range.cidr}</td>
                        <td className="sg-cb-mono">{range.rangeStart} – {range.rangeEnd}</td>
                        <td className="sg-cb-mono muted">{range.gateway} · {range.dnsServers.join(", ")}</td>
                        <td className="sg-cb-mono">{range.inUseCount + range.reservedCount} of {range.size}</td>
                        <td className="sg-cb-pv-rowacts">
                          <button className="btn-ghost btn-sm" type="button"
                                  onClick={() => setEditing({ connectionId: row.id, range })}>Edit</button>
                          <button className="btn-ghost btn-sm" type="button"
                                  onClick={() => remove(row.id, range)}>Remove</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <p className="muted">No ranges. New VMs on this vCenter cannot get an address until one is added.</p>}
          </div>
        );
      })}
    </>
  );
}

/** OpenTofu in this KubeSight: is it there, and what state and locks does it hold. */
export function OpenTofuStatus({ notify, canRelease }) {
  const [data, setData] = useState(null);
  const load = useCallback(() => {
    getProvisioningOverview().then(setData).catch((error) => notify(error.message, true));
  }, [notify]);
  useEffect(() => { load(); }, [load]);
  if (!data) return <p className="muted">Loading…</p>;
  const { engine, states } = data;
  const release = async (state) => {
    if (!window.confirm(
      `Release the lock on ${state.name}? Only do this when no OpenTofu job for it is running — `
      + "the release is recorded in the audit log."
    )) return;
    try {
      await releaseProvisionLock(state.buildId);
      notify(`Lock on ${state.name} released.`);
      load();
    } catch (error) {
      notify(error.message || String(error), true);
    }
  };
  return (
    <>
      <div className="sg-cb-facts">
        <div className="sg-cb-fact">
          <div className="k">Engine</div>
          <div className="v">
            {engine.mode === "simulated" ? "Simulated (demo mode)"
              : engine.available ? `OpenTofu ${engine.version || ""}`.trim() : "Not installed in this image"}
          </div>
        </div>
        <div className="sg-cb-fact">
          <div className="k">vSphere provider</div>
          <div className="v sg-cb-mono">
            {engine.providerSource} {engine.providerVersion}
            {engine.providerBundled ? " · bundled" : " · not bundled — downloads at runtime"}
          </div>
        </div>
        <div className="sg-cb-fact">
          <div className="k">State</div>
          <div className="v">KubeSight database · one per cluster · encrypted</div>
        </div>
      </div>
      {!engine.available && engine.mode === "real" ? (
        <p className="sg-cb-field-error">
          The `tofu` binary is not in this backend image. Rebuild it from backend/Dockerfile, which installs OpenTofu and the provider.
        </p>
      ) : null}
      {states.length ? (
        <div className="table-wrap">
          <table className="sg-cb-pv-vms">
            <thead><tr><th>Cluster</th><th>State</th><th>Lock</th><th>Last job</th><th /></tr></thead>
            <tbody>
              {states.map((state) => (
                <tr key={state.buildId}>
                  <td className="sg-cb-mono">{state.name}</td>
                  <td className="sg-cb-mono">
                    v{state.state.version} · {state.state.vmCount} VM{state.state.vmCount === 1 ? "" : "s"}
                  </td>
                  <td>
                    {state.state.locked ? (
                      <span className="sg-cb-pill is-info">
                        held{state.state.lockJobId ? ` by job #${state.state.lockJobId}` : ""} · {timeAgo(state.state.lockedAt)}
                      </span>
                    ) : <span className="sg-cb-pill is-muted">free</span>}
                  </td>
                  <td className="muted">
                    {state.lastJob
                      ? `#${state.lastJob.id} · ${state.lastJob.operation} · ${state.lastJob.status.replace(/_/g, " ")}`
                      : "—"}
                    {state.provisionStatus ? ` · ${PROVISION_STATUS_LABELS[state.provisionStatus] || state.provisionStatus}` : ""}
                  </td>
                  <td>
                    {state.state.locked && canRelease ? (
                      <button className="btn-ghost btn-sm" type="button" onClick={() => release(state)}>Release lock</button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="muted">No cluster has OpenTofu state yet.</p>}
      <p className="muted sg-cb-pv-lede">
        If KubeSight restarts mid-job, the job resumes on its own and releases only its own lock. Release a lock here
        only when the job that held it is gone; KubeSight refuses while that job is still running.
      </p>
    </>
  );
}
