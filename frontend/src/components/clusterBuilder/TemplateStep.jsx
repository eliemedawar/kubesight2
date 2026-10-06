/** Step 1 of the wizard: start from a shape.
 *
 *  Built-in templates are fixed; saved ones belong to whoever saved them and
 *  can be renamed or removed by anyone allowed to manage templates. "Custom"
 *  opens the role counts, held to the same etcd and load-balancer rules.
 */

import { useState } from "react";
import {
  ROLE_KEYS,
  ROLE_TITLE,
  countsError,
  shapeLabel,
  totals,
} from "../../utils/clusterProvisioning.js";
import { deleteClusterTemplate, updateClusterTemplate } from "../../api/clusterBuildsApi.js";

/** The shape at thumbnail scale: pills = load balancers, red = control planes. */
export function MiniShape({ counts }) {
  if (!counts) {
    return <div className="sg-cb-tpl-mini is-empty"><span>you set the counts</span></div>;
  }
  const row = (role, n, cap = 8) => (n ? (
    <div className="row">
      {Array.from({ length: Math.min(n, cap) }, (_, i) => <i key={i} className={`is-${role}`} />)}
      {n > cap ? <span className="more">+{n - cap}</span> : null}
    </div>
  ) : null);
  return (
    <div className="sg-cb-tpl-mini" aria-hidden="true">
      {row("lb", counts.loadbalancer)}
      {row("cp", counts.controlPlane)}
      {row("wk", counts.worker)}
    </div>
  );
}

function TemplateCard({ template, selected, onSelect, canManage, onRename, onDelete }) {
  const sum = totals(template.counts, template.sizes);
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(template.name);
  return (
    <div className={`sg-cb-tpl ${selected ? "is-on" : ""}`}>
      <button type="button" className="sg-cb-tpl-hit btn-ghost" aria-pressed={selected} onClick={onSelect}>
        <span className="sg-cb-tpl-name">
          {template.name}
          <span className={`sg-cb-pill ${template.builtin ? "is-muted" : "is-brand"}`}>
            {template.builtin ? "Built-in" : "Saved"}
          </span>
        </span>
        <MiniShape counts={template.counts} />
        <span className="sg-cb-tpl-shape sg-cb-mono">{shapeLabel(template.counts)}</span>
        {template.description ? <span className="sg-cb-tpl-desc">{template.description}</span> : null}
        <dl className="sg-cb-tpl-facts">
          <div><dt>Machines</dt><dd>{sum.machines} VMs</dd></div>
          <div><dt>Compute</dt><dd>{sum.cpu} vCPU · {sum.memoryGb} GB</dd></div>
          <div>
            <dt>API</dt>
            <dd>
              {template.endpointMode === "manual_endpoint"
                ? "the control plane's address"
                : template.topologyType === "stacked_ha" ? "floating address (keepalived)" : "address on the load balancer"}
            </dd>
          </div>
          {template.survives ? <div><dt>Survives</dt><dd>{template.survives}</dd></div> : null}
          {template.addons?.length ? (
            <div><dt>Add-ons</dt><dd>{template.addons.map((a) => a.id).join(", ")}</dd></div>
          ) : null}
          {template.createdBy ? <div><dt>Saved by</dt><dd>{template.createdBy}</dd></div> : null}
        </dl>
      </button>
      {!template.builtin && canManage ? (
        renaming ? (
          <div className="sg-cb-tpl-edit">
            <input className="sg-cb-input" aria-label="Template name" value={name}
                   onChange={(event) => setName(event.target.value)} />
            <button className="btn-outline btn-sm" type="button" onClick={async () => {
              await onRename(template, name);
              setRenaming(false);
            }}>Save</button>
            <button className="btn-ghost btn-sm" type="button" onClick={() => setRenaming(false)}>Cancel</button>
          </div>
        ) : (
          <div className="sg-cb-tpl-edit">
            <button className="btn-ghost btn-sm" type="button" onClick={() => setRenaming(true)}>Rename</button>
            <button className="btn-ghost btn-sm" type="button" onClick={() => onDelete(template)}>Delete</button>
          </div>
        )
      ) : null}
    </div>
  );
}

export function TemplateGallery({ catalog, selectedId, onSelect, canManage, notify, onCatalogChanged }) {
  const builtin = catalog?.builtin || [];
  const custom = catalog?.custom || [];
  const rename = async (template, name) => {
    try {
      await updateClusterTemplate(template.dbId, { name });
      notify(`Renamed to “${name}”.`);
      onCatalogChanged?.();
    } catch (error) {
      notify(error.message || String(error), true);
    }
  };
  const remove = async (template) => {
    if (!window.confirm(`Delete the template “${template.name}”? Builds made from it are not affected.`)) return;
    try {
      await deleteClusterTemplate(template.dbId);
      notify(`Deleted “${template.name}”.`);
      if (selectedId === template.id) onSelect(builtin[1] || builtin[0]);
      onCatalogChanged?.();
    } catch (error) {
      notify(error.message || String(error), true);
    }
  };
  return (
    <>
      <div className="sg-cb-tpl-grid">
        {builtin.map((template) => (
          <TemplateCard key={template.id} template={template} selected={selectedId === template.id}
                        onSelect={() => onSelect(template)} />
        ))}
        <div className={`sg-cb-tpl is-custom ${selectedId === "custom" ? "is-on" : ""}`}>
          <button type="button" className="sg-cb-tpl-hit btn-ghost" aria-pressed={selectedId === "custom"}
                  onClick={() => onSelect({ id: "custom" })}>
            <span className="sg-cb-tpl-name">Custom</span>
            <MiniShape counts={null} />
            <span className="sg-cb-tpl-shape sg-cb-mono">any counts · any sizes</span>
            <span className="sg-cb-tpl-desc">
              Start from Small and set every count yourself. The same rules apply: 1, 3 or 5 control planes.
            </span>
          </button>
        </div>
      </div>
      {custom.length ? (
        <>
          <div className="sg-cb-tpl-head">
            <h3>Saved by admins</h3>
            <span className="muted">Save any build as a template from its page</span>
          </div>
          <div className="sg-cb-tpl-grid">
            {custom.map((template) => (
              <TemplateCard key={template.id} template={template} selected={selectedId === template.id}
                            onSelect={() => onSelect(template)} canManage={canManage}
                            onRename={rename} onDelete={remove} />
            ))}
          </div>
        </>
      ) : null}
    </>
  );
}

/** Role counts for Custom, with the rule that breaks shown as it breaks. */
export function CountsEditor({ counts, onChange }) {
  const error = countsError(counts);
  const set = (role, value) => onChange({ ...counts, [role]: value });
  return (
    <div className="sg-cb-counts">
      {ROLE_KEYS.map((role) => (
        <div className="sg-cb-field" key={role}>
          <span className="sg-cb-field-label">{ROLE_TITLE[role]}</span>
          {role === "controlPlane" ? (
            <div className="sg-cb-seg" role="group" aria-label="Control planes">
              {[1, 3, 5].map((n) => (
                <button key={n} type="button" aria-pressed={counts.controlPlane === n}
                        onClick={() => onChange({
                          ...counts,
                          controlPlane: n,
                          loadbalancer: n > 1 ? 2 : Math.min(counts.loadbalancer, 1),
                        })}>
                  {n}
                </button>
              ))}
            </div>
          ) : role === "loadbalancer" ? (
            <div className="sg-cb-seg" role="group" aria-label="Load balancers">
              {(counts.controlPlane > 1 ? [2] : [0, 1]).map((n) => (
                <button key={n} type="button" aria-pressed={counts.loadbalancer === n}
                        onClick={() => set("loadbalancer", n)}>
                  {n}
                </button>
              ))}
            </div>
          ) : (
            <div className="sg-cb-stepper" role="group" aria-label="Workers">
              <button type="button" className="btn-ghost" aria-label="Fewer workers"
                      onClick={() => set("worker", Math.max(1, counts.worker - 1))}>−</button>
              <output>{counts.worker}</output>
              <button type="button" className="btn-ghost" aria-label="More workers"
                      onClick={() => set("worker", Math.min(50, counts.worker + 1))}>+</button>
            </div>
          )}
        </div>
      ))}
      <p className="muted sg-cb-counts-note">
        {error || (counts.controlPlane > 1
          ? "Highly available: survives losing one control plane and one load balancer."
          : counts.loadbalancer
            ? "One control plane behind a load balancer, so it can become highly available later without a new address."
            : "One control plane, addressed directly. No failover.")}
      </p>
    </div>
  );
}
