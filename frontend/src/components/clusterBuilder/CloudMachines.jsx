/** Step 2, when KubeSight creates the machines on Oracle Cloud, AWS or Google
 *  Cloud: which account and region, which network and subnet, Kubernetes on VMs
 *  or the cloud's managed service, what the machines start from and how big.
 *
 *  Everything is read live with the account's keys (``/inventory``), so the
 *  lists show exactly what those keys may use. Two things a VMware build has
 *  are absent on purpose: an address range (the cloud picks each private
 *  address) and load-balancer VMs (an HA API sits behind the cloud's own
 *  internal load balancer).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Field } from "./common.jsx";
import NodePoolsEditor from "./NodePoolsEditor.jsx";
import { SizesTable } from "./VmwareMachines.jsx";
import { timeAgo } from "../../utils/clusterBuilder.js";
import { DEFAULT_MINIMUMS, ROLE_ONE, ROLE_TITLE } from "../../utils/clusterProvisioning.js";
import {
  DISK_TYPES,
  NETWORK_LABELS,
  ZONE_LABELS,
  cloudMachineRoles,
  cloudMachines,
  cloudTotals,
  machineTypeText,
  managedLabel,
  needsZone,
  nodeImagesFor,
  nodePoolsProblem,
  providerLabel,
  subnetsOf,
  WI_OCI_SHAPE,
} from "../../utils/cloudProvisioning.js";
import { getCloudInventory, listCloudRegions, resolveCloudSizes } from "../../api/clusterBuildsApi.js";

export function useCloudRegions(accountId) {
  const [regions, setRegions] = useState([]);
  useEffect(() => {
    let ignore = false;
    if (!accountId) { setRegions([]); return undefined; }
    listCloudRegions(accountId)
      .then((data) => { if (!ignore) setRegions(data.regions || []); })
      .catch(() => { if (!ignore) setRegions([]); });
    return () => { ignore = true; };
  }, [accountId]);
  return regions;
}

export function useCloudInventory(accountId, region, compartmentId, notify) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [readAt, setReadAt] = useState(null);
  const load = useCallback(async (refresh = false) => {
    if (!accountId || !region) { setData(null); return; }
    setLoading(true);
    setError("");
    try {
      setData(await getCloudInventory(accountId, { region, compartmentId, refresh }));
      setReadAt(new Date().toISOString());
    } catch (err) {
      setData(null);
      setError(err.message || String(err));
      if (notify) notify(`The cloud could not be read: ${err.message}`, true);
    } finally {
      setLoading(false);
    }
  }, [accountId, region, compartmentId, notify]);
  useEffect(() => { load(false); }, [load]);
  return { data, loading, error, readAt, refresh: () => load(true) };
}

/** What each role's CPU and memory become in this cloud, asked a moment after
    the last change. {role: {type, cpu, memoryGb, note}} */
export function useResolvedSizes(accountId, region, provisioning, sizes) {
  const [resolved, setResolved] = useState({});
  const [error, setError] = useState("");
  const key = JSON.stringify({ accountId, region, provisioning, sizes });
  useEffect(() => {
    if (!accountId || !region) { setResolved({}); return undefined; }
    let ignore = false;
    const id = setTimeout(() => {
      resolveCloudSizes(accountId, { region, provisioning, sizes })
        .then((data) => { if (!ignore) { setResolved(data.sizes || {}); setError(""); } })
        .catch((err) => { if (!ignore) setError(err.message || String(err)); });
    }, 350);
    return () => { ignore = true; clearTimeout(id); };
    // ``key`` carries every input; the objects themselves change identity each render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return { resolved, error };
}

function MachineTypes({ roles, counts, resolved, error, overrides, catalog, onOverride }) {
  return (
    <>
      <div className="table-wrap">
        <table className="sg-cb-sizes">
          <thead><tr><th>Role</th><th>Count</th><th>Machine type</th><th>Choose another</th></tr></thead>
          <tbody>
            {roles.map((role) => (
              <tr key={role}>
                <td><span className={`sg-cb-rolechip is-${role}`}><i />{ROLE_TITLE[role]}</span></td>
                <td className="sg-cb-mono">{counts[role]}</td>
                <td className="sg-cb-mono">
                  {machineTypeText(resolved[role]) || <span className="muted">working it out…</span>}
                  {resolved[role]?.cpu ? (
                    <span className="muted"> · {resolved[role].cpu} vCPU · {resolved[role].memoryGb} GB</span>
                  ) : null}
                </td>
                <td>
                  <select className="sg-cb-input" aria-label={`${ROLE_TITLE[role]} machine type`}
                          value={overrides[role] || ""}
                          onChange={(event) => onOverride(role, event.target.value)}>
                    <option value="">The smallest that fits</option>
                    {catalog.map((type) => (
                      <option key={type.id} value={type.id}>
                        {type.id}{type.flexible ? " (flexible)" : ` — ${type.cpu} vCPU · ${type.memoryGb} GB`}
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {error ? <p className="sg-cb-field-error">{error}</p> : null}
    </>
  );
}

export default function CloudMachines({
  accounts, provider, cloud, setCloud, inventoryState, regions, counts, setCounts, sizes, setSizes,
  minimums = DEFAULT_MINIMUMS, vmsOnly = false, clusterName = "", resolvedSizes, problem,
}) {
  const { data: inventory, loading, error, readAt, refresh } = inventoryState;
  const account = accounts.find((row) => String(row.id) === String(cloud.accountId)) || null;
  const wi = Boolean(inventory?.workloadIdentity);
  const managed = !vmsOnly && (cloud.kind === "managed" || wi);
  const network = (inventory?.networks || []).find((n) => n.id === cloud.networkId) || null;
  const subnets = subnetsOf(inventory, cloud.networkId);
  const subnet = subnets.find((s) => s.id === cloud.subnetId) || null;
  const set = (patch) => setCloud({ ...cloud, ...patch });
  const zoneLabel = ZONE_LABELS[provider] || "Zone";
  const roles = managed ? ["worker"] : cloudMachineRoles(counts);
  const shownCounts = managed ? { worker: counts.worker || 0 } : counts;
  const sum = cloudTotals(shownCounts, sizes);
  const machines = useMemo(
    () => (managed ? [] : cloudMachines(clusterName, counts, sizes, resolvedSizes.resolved)),
    [managed, clusterName, counts, sizes, resolvedSizes.resolved]
  );

  if (!accounts.length) {
    return (
      <div className="card sg-cb-card">
        <p className="muted">
          No cloud account yet, so KubeSight cannot create machines on Oracle Cloud, AWS or Google Cloud. An
          administrator adds one under Sources → Cloud accounts.
        </p>
      </div>
    );
  }

  const toggleManagedSubnet = (id) => {
    const chosen = new Set(cloud.subnetIds || []);
    if (chosen.has(id)) chosen.delete(id); else chosen.add(id);
    set({ subnetIds: [...chosen] });
  };

  return (
    <>
      <div className="card sg-cb-card">
        <div className="sg-cb-sect">
          <h2>Where in {providerLabel(provider) || "the cloud"}</h2>
          <span className="sg-cb-sect-right">
            {loading ? "Reading the cloud…" : readAt ? <>read {timeAgo(readAt)}</> : null}
            <button className="btn-ghost btn-sm" type="button" onClick={refresh} disabled={loading || !cloud.region}>
              Refresh
            </button>
          </span>
        </div>
        <p className="muted sg-cb-pv-lede">
          {wi
            ? "Read by KubeSight's OKE workload identity (no key). The cluster goes into the platform's compartment and network; its API has a private address only."
            : <>Read with the account&apos;s own keys. The machines get private addresses only, picked by the cloud from the
              subnet — KubeSight reaches them over your VPN or peering, or through the SSH route&apos;s bastion.</>}
        </p>
        {error ? <p className="sg-cb-field-error">{error}</p> : null}
        <div className="sg-cb-qgrid">
          <Field label="Cloud account" htmlFor="cl-account"
                 hint={account?.identity ? `Signs in as ${account.identity}` : ""}
                 error={account?.lastTestStatus === "failed" ? "The last test of this account failed — check it in Sources." : ""}>
            <select id="cl-account" className="sg-cb-input" value={cloud.accountId}
                    onChange={(e) => {
                      const next = accounts.find((row) => String(row.id) === e.target.value);
                      setCloud({
                        ...cloud, accountId: e.target.value, region: next?.region || "",
                        compartmentId: next?.settings?.compartmentOcid || "", networkId: "", subnetId: "",
                        zone: "", imageId: "", subnetIds: [], machineTypes: {},
                      });
                    }}>
              <option value="">Choose…</option>
              {accounts.map((row) => (
                <option key={row.id} value={String(row.id)}>{row.providerLabel || providerLabel(row.provider)} — {row.name}</option>
              ))}
            </select>
          </Field>
          <Field label="Region" htmlFor="cl-region">
            <select id="cl-region" className="sg-cb-input" value={cloud.region}
                    onChange={(e) => set({ region: e.target.value, networkId: "", subnetId: "", zone: "", imageId: "", subnetIds: [] })}>
              {!regions.some((r) => r.id === cloud.region) && cloud.region
                ? <option value={cloud.region}>{cloud.region}</option> : null}
              {!cloud.region ? <option value="">Choose…</option> : null}
              {regions.map((r) => <option key={r.id} value={r.id}>{r.name && r.name !== r.id ? `${r.id} — ${r.name}` : r.id}</option>)}
            </select>
          </Field>
          {provider === "oci" ? (
            <Field label="Compartment" htmlFor="cl-comp">
              <select id="cl-comp" className="sg-cb-input" value={cloud.compartmentId}
                      onChange={(e) => set({ compartmentId: e.target.value, networkId: "", subnetId: "" })}>
                {!cloud.compartmentId ? <option value="">Choose…</option> : null}
                {(inventory?.compartments || []).map((c) => <option key={c.id} value={c.id}>{c.path || c.name}</option>)}
              </select>
            </Field>
          ) : null}
          {wi ? (
            <div className="sg-cb-wide">
              <div className="sg-cb-field-label">Network (fixed by the platform)</div>
              <ul className="sg-cb-plain-list sg-cb-mono">
                <li>{network?.name || "VCN"} {network?.cidr ? `· ${network.cidr}` : ""}</li>
                {Object.entries(network?.fixed || inventory?.networks?.[0]?.fixed || {}).map(([role, item]) => (
                  <li key={role}>{role === "endpoint" ? "API endpoint" : role === "workers" ? "Workers" : "Load balancers"}: {item.name} · {item.cidr || "?"}</li>
                ))}
              </ul>
              <p className="muted">
                Signed in by OKE workload identity: no key. Clusters are ENHANCED, private, on {WI_OCI_SHAPE} nodes,
                behind two NSGs with no SSH, and every plan waits for an approval of its hash.
              </p>
            </div>
          ) : null}
          {wi ? null : (
          <Field label={NETWORK_LABELS[provider] || "Network"} htmlFor="cl-net" hint={network?.cidr || ""}>
            <select id="cl-net" className="sg-cb-input" value={cloud.networkId}
                    onChange={(e) => set({ networkId: e.target.value, subnetId: "", subnetIds: [] })}>
              <option value="">Choose…</option>
              {(inventory?.networks || []).map((n) => (
                <option key={n.id} value={n.id}>{n.name || n.id}{n.cidr ? ` (${n.cidr})` : ""}</option>
              ))}
            </select>
          </Field>
          )}
          {wi ? null : (
          <Field label="Subnet" htmlFor="cl-sub"
                 hint={subnet ? [subnet.cidr, subnet.zone ? `in ${subnet.zone}` : "regional"].filter(Boolean).join(" · ") : ""}>
            <select id="cl-sub" className="sg-cb-input" value={cloud.subnetId}
                    onChange={(e) => {
                      const next = subnets.find((s) => s.id === e.target.value);
                      set({ subnetId: e.target.value, ...(next?.zone ? { zone: next.zone } : {}) });
                    }}>
              <option value="">Choose…</option>
              {subnets.map((s) => (
                <option key={s.id} value={s.id}>{s.name || s.id} — {s.cidr}{s.zone ? ` · ${s.zone}` : ""}</option>
              ))}
            </select>
          </Field>
          )}
          {needsZone(provider, managed, cloud) ? (
            <Field label={zoneLabel} htmlFor="cl-zone"
                   hint={subnet?.zone ? `Set by the subnet, which lives in ${subnet.zone}.` : ""}>
              <select id="cl-zone" className="sg-cb-input" value={cloud.zone} disabled={Boolean(subnet?.zone)}
                      onChange={(e) => set({ zone: e.target.value })}>
                <option value="">Choose…</option>
                {(inventory?.zones || []).map((z) => <option key={z.id} value={z.id}>{z.name || z.id}</option>)}
              </select>
            </Field>
          ) : null}
        </div>
      </div>

      {wi ? (
        <div className="card sg-cb-card">
          <div className="sg-cb-sect"><h2>Owner and expiry</h2></div>
          <p className="muted sg-cb-pv-lede">
            Tagged on the cluster and its nodes (the platform requires both, K17): who owns it and until when it is
            needed, at most 180 days ahead. KubeSight only sets the tags; it never deletes a cluster on that date.
          </p>
          <div className="sg-cb-qgrid">
            <Field label="Owner (work email)" htmlFor="cl-owner">
              <input id="cl-owner" className="sg-cb-input" type="email" value={cloud.ownerEmail || ""}
                     placeholder="name@areeba.com" onChange={(e) => set({ ownerEmail: e.target.value.trim() })} />
            </Field>
            <Field label="Expires on" htmlFor="cl-expiry">
              <input id="cl-expiry" className="sg-cb-input" type="date" value={cloud.expiresOn || ""}
                     onChange={(e) => set({ expiresOn: e.target.value })} />
            </Field>
          </div>
        </div>
      ) : null}

      {!vmsOnly && !wi ? (
        <div className="card sg-cb-card">
          <div className="sg-cb-sect"><h2>Who runs Kubernetes</h2></div>
          <div className="sg-cb-choices">
            <button type="button" className="sg-cb-choice" aria-pressed={!managed} onClick={() => set({ kind: "vms" })}>
              <span className="ct">Kubernetes on VMs <span className="sg-cb-pill is-brand">KubeSight installs it</span></span>
              <span className="cd">
                OpenTofu creates the VMs, then KubeSight installs Kubernetes on them over SSH, as for VMware: same
                add-ons, workloads, apps and day-two growth.
                {counts.loadbalancer ? " The API sits behind the cloud's internal load balancer." : ""}
              </span>
            </button>
            <button type="button" className="sg-cb-choice" aria-pressed={managed} onClick={() => set({ kind: "managed" })}>
              <span className="ct">Managed {managedLabel(provider)}</span>
              <span className="cd">
                The cloud runs the control plane; you choose the node pool. KubeSight registers the cluster when it is
                up. No SSH, add-ons, workloads or apps steps.
              </span>
            </button>
          </div>
        </div>
      ) : null}

      <div className="card sg-cb-card">
        <div className="sg-cb-sect">
          <h2>{managed ? "Kubernetes version" : "Image"}</h2>
          <span className="sg-cb-sect-right">
            {managed
              ? `${(inventory?.kubernetesVersions || []).length} offered in ${cloud.region || "this region"}`
              : `${(inventory?.images || []).length} in ${cloud.region || "this region"}`}
          </span>
        </div>
        {managed ? (
          <>
            {(inventory?.kubernetesVersions || []).length ? (
              <div className="sg-cb-seg" role="group" aria-label="Kubernetes version">
                {inventory.kubernetesVersions.map((version) => (
                  <button key={version} type="button" aria-pressed={cloud.kubernetesVersion === version}
                          onClick={() => set({ kubernetesVersion: version })}>
                    {String(version).startsWith("v") ? version : `v${version}`}
                  </button>
                ))}
              </div>
            ) : <p className="muted">{loading ? "Reading the versions…" : "This region offers no versions to this account."}</p>}
            {provider === "aws" ? (
              <Field label="Subnets for Amazon EKS" hint="EKS needs at least two, in different availability zones. The control plane's private endpoint and the nodes go in them.">
                <div className="sg-cb-cloud-subnets">
                  {subnets.map((s) => (
                    <label key={s.id} className="sg-cb-inlinecheck">
                      <input type="checkbox" checked={(cloud.subnetIds || []).includes(s.id)}
                             onChange={() => toggleManagedSubnet(s.id)} />
                      <span className="sg-cb-mono">{s.name || s.id}</span>
                      <span className="muted">{s.cidr}{s.zone ? ` · ${s.zone}` : ""}</span>
                    </label>
                  ))}
                </div>
              </Field>
            ) : null}
            {provider === "gcp" ? (
              <div className="sg-cb-qgrid">
                <Field label="Control-plane range" htmlFor="cl-master"
                       hint="A private /28 GKE gives the control plane. It must not overlap your networks.">
                  <input id="cl-master" className="sg-cb-input sg-cb-mono" value={cloud.masterCidr}
                         onChange={(e) => set({ masterCidr: e.target.value })} />
                </Field>
                <label className="sg-cb-inlinecheck">
                  <input type="checkbox" checked={Boolean(cloud.regional)} onChange={(e) => set({ regional: e.target.checked })} />
                  Regional cluster (control plane and nodes in every zone of {cloud.region || "the region"})
                </label>
              </div>
            ) : null}
          </>
        ) : (
          <div className="sg-cb-vmt" role="radiogroup" aria-label="Image">
            {(inventory?.images || []).map((image) => (
              <button key={image.id} type="button" role="radio" aria-checked={cloud.imageId === image.id}
                      className={`sg-cb-vmt-row btn-ghost ${cloud.imageId === image.id ? "is-on" : ""}`}
                      onClick={() => set({ imageId: image.id })}>
                <span className="rad" aria-hidden="true" />
                <span className="nm sg-cb-mono">{image.name}</span>
                <span className="os">{image.description || image.id}</span>
                <span className="sg-cb-pill is-muted">{image.osFamily === "rhel" ? "RHEL family" : image.osFamily === "debian" ? "Debian family" : "other"}</span>
              </button>
            ))}
            {!(inventory?.images || []).length ? (
              <p className="muted sg-cb-vmt-empty">{loading ? "Reading the images…" : "No images to offer in this region."}</p>
            ) : null}
          </div>
        )}
      </div>

      {wi ? (
        <NodePoolsEditor pools={cloud.nodePools || []} onChange={(nodePools) => set({ nodePools })}
                         images={nodeImagesFor(inventory?.nodeImages, cloud.kubernetesVersion)}
                         version={cloud.kubernetesVersion}
                         problem={nodePoolsProblem(cloud.nodePools, nodeImagesFor(inventory?.nodeImages, cloud.kubernetesVersion))} />
      ) : (
      <div className="card sg-cb-card">
        <div className="sg-cb-sect">
          <h2>{managed ? "Node pool" : "Machine sizes"}</h2>
          <span className="sg-cb-sect-right">{sum.machines} {managed ? "nodes" : "VMs"} · {sum.cpu} vCPU · {sum.memoryGb} GB</span>
        </div>
        {managed ? (
          <Field label="Nodes" hint="The workers the cloud runs for this cluster. Add more on day two.">
            <div className="sg-cb-stepper" role="group" aria-label="Nodes">
              <button type="button" className="btn-ghost" aria-label="Fewer nodes"
                      onClick={() => setCounts({ ...counts, worker: Math.max(1, (counts.worker || 1) - 1) })}>−</button>
              <output>{counts.worker || 0}</output>
              <button type="button" className="btn-ghost" aria-label="More nodes"
                      onClick={() => setCounts({ ...counts, worker: Math.min(50, (counts.worker || 0) + 1) })}>+</button>
            </div>
          </Field>
        ) : (
          <p className="muted sg-cb-pv-lede">
            {counts.loadbalancer
              ? "No load-balancer VMs on a cloud: the API address is the cloud's internal load balancer, made with the cluster."
              : "Each role gets the smallest machine type that has at least this CPU and memory, unless you choose one."}
          </p>
        )}
        <SizesTable counts={shownCounts} sizes={sizes} setSizes={setSizes} template={null} minimums={minimums} />
        <MachineTypes
          roles={roles}
          counts={shownCounts}
          resolved={resolvedSizes.resolved}
          error={resolvedSizes.error}
          overrides={cloud.machineTypes || {}}
          catalog={inventory?.machineTypes || []}
          onOverride={(role, type) => set({ machineTypes: { ...(cloud.machineTypes || {}), [role]: type } })}
        />
        {provider === "aws" || provider === "gcp" ? (
          <Field label="Disk type" htmlFor="cl-disk">
            <select id="cl-disk" className="sg-cb-input" value={cloud.diskType}
                    onChange={(e) => set({ diskType: e.target.value })}>
              {DISK_TYPES[provider].map(([id, label]) => <option key={id} value={id}>{label}</option>)}
            </select>
          </Field>
        ) : null}
      </div>
      )}

      {wi ? (problem ?<p className="sg-cb-field-hint">{problem}</p> : null) : (
      <div className="card sg-cb-card">
        <div className="sg-cb-sect"><h2>Who may reach them</h2></div>
        <Field
          label="Allowed networks"
          htmlFor="cl-cidrs"
          hint={managed
            ? "Networks allowed to reach the cluster's private API — the one KubeSight connects from must be here. Comma or one per line."
            : "Networks allowed to reach SSH (22) and the Kubernetes API (6443) — the one KubeSight connects from must be here. The machines always reach each other. Comma or one per line."}
        >
          <textarea id="cl-cidrs" className="sg-cb-input sg-cb-mono" rows={2} value={cloud.allowedCidrs}
                    placeholder="10.0.0.0/8" onChange={(e) => set({ allowedCidrs: e.target.value })} />
        </Field>
        {problem ? <p className="sg-cb-field-hint">{problem}</p> : null}
      </div>
      )}

      {!managed && machines.length ? (
        <div className="card sg-cb-card">
          <div className="sg-cb-sect">
            <h2>Names</h2>
            <span className="sg-cb-sect-right"><span className="sg-cb-pill is-muted">addresses come from the cloud</span></span>
          </div>
          <div className="table-wrap">
            <table className="sg-cb-pv-vms">
              <thead><tr><th>VM name</th><th>Role</th><th>Machine type</th></tr></thead>
              <tbody>
                {counts.loadbalancer ? (
                  <tr>
                    <td className="muted">—</td>
                    <td><span className="sg-cb-pill is-info">API address</span></td>
                    <td className="muted">cloud internal load balancer · TCP 6443</td>
                  </tr>
                ) : null}
                {machines.map((machine) => (
                  <tr key={machine.name}>
                    <td className="sg-cb-mono">{machine.name}</td>
                    <td>{ROLE_ONE[machine.role]}</td>
                    <td className="sg-cb-mono">{machine.machineType || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </>
  );
}
