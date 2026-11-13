/**
 * OKE node pools (workload identity): one card per pool — name, nodes, OCPUs,
 * memory, boot disk, labels (for nodeSelector / node affinity), taints (keep
 * other pods off), and whether its nodes spread over the three fault domains.
 * Used by the wizard's Machines step and by a built cluster's day-two change.
 */

import { Field } from "./common.jsx";
import {
  MAX_NODE_POOLS,
  POOL_LABEL,
  TAINT_EFFECTS,
  newNodePool,
  nodeKeyError,
  nodePoolTotals,
} from "../../utils/cloudProvisioning.js";

function Rows({ rows, onChange, kind, poolName }) {
  const isTaint = kind === "taint";
  const set = (index, patch) => onChange(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  return (
    <div className="sg-cb-np-rows">
      {rows.map((row, index) => {
        const error = row.key ? nodeKeyError(row.key) : "";
        return (
          // eslint-disable-next-line react/no-array-index-key -- rows have no identity but their place
          <div key={index} className="sg-cb-np-row">
            <input className="sg-cb-input sg-cb-mono" value={row.key} placeholder={isTaint ? "dedicated" : "team"}
                   aria-label={`${poolName} ${kind} ${index + 1} key`} aria-invalid={Boolean(error)}
                   onChange={(e) => set(index, { key: e.target.value.trim() })} />
            <span aria-hidden="true">=</span>
            <input className="sg-cb-input sg-cb-mono" value={row.value} placeholder={isTaint ? "database" : "payments"}
                   aria-label={`${poolName} ${kind} ${index + 1} value`}
                   onChange={(e) => set(index, { value: e.target.value.trim() })} />
            {isTaint ? (
              <select className="sg-cb-input" value={row.effect} aria-label={`${poolName} taint ${index + 1} effect`}
                      onChange={(e) => set(index, { effect: e.target.value })}>
                {TAINT_EFFECTS.map((effect) => <option key={effect} value={effect}>{effect}</option>)}
              </select>
            ) : null}
            <button type="button" className="btn-ghost btn-sm" aria-label={`Remove ${kind} ${index + 1}`}
                    onClick={() => onChange(rows.filter((_, i) => i !== index))}>Remove</button>
            {error ? <p className="sg-cb-field-error sg-cb-np-rowerr">{error}</p> : null}
          </div>
        );
      })}
      <button type="button" className="btn-ghost btn-sm"
              disabled={rows.length >= (isTaint ? 10 : 20)}
              onClick={() => onChange([...rows, isTaint ? { key: "", value: "", effect: "NoSchedule" } : { key: "", value: "" }])}>
        + {isTaint ? "Taint" : "Label"}
      </button>
    </div>
  );
}

function NumberField({ id, label, value, onChange, min, max, hint }) {
  return (
    <Field label={label} htmlFor={id} hint={hint}>
      <input id={id} className="sg-cb-input sg-cb-mono" type="number" min={min} max={max} value={value}
             onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))} />
    </Field>
  );
}

export default function NodePoolsEditor({ pools, onChange, problem = "", locked = false, images = null, version = "" }) {
  const list = pools || [];
  const totals = nodePoolTotals(list);
  const set = (index, patch) => onChange(list.map((pool, i) => (i === index ? { ...pool, ...patch } : pool)));
  const nextName = () => {
    for (let n = list.length + 1; n < 100; n += 1) {
      const name = `pool-${n}`;
      if (!list.some((p) => p.name === name)) return name;
    }
    return "";
  };
  return (
    <div className="card sg-cb-card">
      <div className="sg-cb-sect">
        <h2>Node pools</h2>
        <span className="sg-cb-sect-right">{list.length} {list.length === 1 ? "pool" : "pools"} · {totals.nodes} nodes · {totals.ocpus} OCPUs · {totals.memoryGb} GB</span>
      </div>
      <p className="muted sg-cb-pv-lede">
        Each pool is a group of identical E6 nodes. Every node gets the label <code className="sg-cb-mono">{POOL_LABEL}=&lt;pool&gt;</code>:
        point a workload at a pool with a <code>nodeSelector</code> or node affinity on it or on your own labels. A taint keeps
        every pod off the pool unless the pod tolerates it.
      </p>
      {list.map((pool, index) => {
        const id = `np-${index}`;
        return (
          // eslint-disable-next-line react/no-array-index-key -- a pool may be renamed while typed
          <fieldset key={index} className="sg-cb-np" disabled={locked}>
            <legend className="sg-cb-np-head">
              <span className="sg-cb-mono">{pool.name || "new pool"}</span>
              <span className="muted">{(Number(pool.count) || 0)} × {(Number(pool.ocpus) || 0)} OCPU · {(Number(pool.memoryGb) || 0)} GB</span>
              {list.length > 1 ? (
                <button type="button" className="btn-ghost btn-sm" onClick={() => onChange(list.filter((_, i) => i !== index))}>
                  Remove pool
                </button>
              ) : null}
            </legend>
            <div className="sg-cb-qgrid">
              <Field label="Name" htmlFor={`${id}-name`} hint="Lowercase letters, digits, dashes">
                <input id={`${id}-name`} className="sg-cb-input sg-cb-mono" value={pool.name} placeholder="workers"
                       onChange={(e) => set(index, { name: e.target.value.trim().toLowerCase() })} />
              </Field>
              <NumberField id={`${id}-count`} label="Nodes" value={pool.count} min={1} max={50} onChange={(v) => set(index, { count: v })} />
              <NumberField id={`${id}-ocpus`} label="OCPUs per node" value={pool.ocpus} min={1} max={64}
                       hint="1 OCPU = 2 vCPU" onChange={(v) => set(index, { ocpus: v })} />
              <NumberField id={`${id}-mem`} label="Memory per node (GB)" value={pool.memoryGb} min={1} max={1024}
                       hint={`${Number(pool.ocpus) || 1}–${Math.min(1024, (Number(pool.ocpus) || 1) * 64)} GB`}
                       onChange={(v) => set(index, { memoryGb: v })} />
              <NumberField id={`${id}-disk`} label="Boot disk (GB)" value={pool.diskGb} min={50} max={2048} onChange={(v) => set(index, { diskGb: v })} />
              {images ? (
                <div style={{ gridColumn: "span 2", minWidth: 0 }}>
                <Field label="Node image" htmlFor={`${id}-img`}
                       hint={pool.imageId ? "Every node of this pool boots from it." : `The newest for ${version || "the version"}: ${images[0]?.name || "none offered"}`}>
                  <select id={`${id}-img`} className="sg-cb-input sg-cb-mono" value={pool.imageId || ""}
                          onChange={(e) => set(index, { imageId: e.target.value })}>
                    <option value="">Newest for {version || "this version"}{images[0] ? ` (${images[0].name})` : ""}</option>
                    {images.map((image) => <option key={image.id} value={image.id}>{image.name}</option>)}
                    {pool.imageId && !images.some((image) => image.id === pool.imageId)
                      ? <option value={pool.imageId}>{pool.imageId} (not built for {version || "this version"})</option> : null}
                  </select>
                </Field>
                </div>
              ) : null}
              <Field label="Fault domains" htmlFor={`${id}-fd`}
                     hint={pool.faultDomains === "spread" ? "Nodes spread over all three: one hardware failure takes at most a third." : "Oracle places the nodes."}>
                <select id={`${id}-fd`} className="sg-cb-input" value={pool.faultDomains}
                        onChange={(e) => set(index, { faultDomains: e.target.value })}>
                  <option value="spread">Spread over all three</option>
                  <option value="any">Let Oracle choose</option>
                </select>
              </Field>
            </div>
            <div className="sg-cb-np-split">
              <div>
                <div className="sg-cb-field-label">Labels</div>
                <Rows rows={pool.labels || []} kind="label" poolName={pool.name || `pool ${index + 1}`}
                      onChange={(labels) => set(index, { labels })} />
              </div>
              <div>
                <div className="sg-cb-field-label">Taints</div>
                <Rows rows={pool.taints || []} kind="taint" poolName={pool.name || `pool ${index + 1}`}
                      onChange={(taints) => set(index, { taints })} />
              </div>
            </div>
          </fieldset>
        );
      })}
      <div className="sg-cb-actions">
        <button type="button" className="btn-outline" disabled={locked || list.length >= MAX_NODE_POOLS}
                onClick={() => onChange([...list, newNodePool(nextName())])}>
          + Add node pool
        </button>
      </div>
      {problem ? <p className="sg-cb-field-error">{problem}</p> : null}
    </div>
  );
}
