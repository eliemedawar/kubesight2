import { useState } from "react";
import NamespaceSelect from "../NamespaceSelect.jsx";
import {
  Field,
  KeyValueEditor,
  MountedFilesEditor,
  PortListEditor,
  VolumeMountsEditor,
  WizardSectionHeader,
} from "./WizardShared.jsx";
import { STORAGE_TOOLTIPS } from "../../../lib/storageValidation.js";
import {
  applyNewPvcStorageDefaults,
  EMPTY_ADVANCED_STORAGE,
  EMPTY_CONFIGMAP_REF,
  EMPTY_ENV_VAR,
  EMPTY_SECRET_REF,
  POD_WORKLOAD_TYPES,
  STORAGE_DEFAULTS,
  WORKLOAD_TYPES,
} from "./wizardDefaults.js";

const CRITICALITY_OPTIONS = ["Low", "Medium", "High", "Critical"];
const ENVIRONMENT_OPTIONS = ["Development", "Staging", "Production", "DR"];
const SERVICE_TYPES = ["ClusterIP", "NodePort", "LoadBalancer"];
const ACCESS_MODES = ["ReadWriteOnce", "ReadOnlyMany", "ReadWriteMany"];
const ACCESS_MODE_LABELS = {
  ReadWriteOnce: "ReadWriteOnce (RWO)",
  ReadOnlyMany: "ReadOnlyMany (ROX)",
  ReadWriteMany: "ReadWriteMany (RWX)",
};
const PULL_POLICIES = ["IfNotPresent", "Always", "Never"];
const PV_STORAGE_TYPES = [
  { value: "hostPath", label: "hostPath" },
  { value: "nfs", label: "NFS" },
  { value: "local", label: "Local" },
];
const RECLAIM_POLICIES = ["Retain", "Delete"];

function formatStorageClassLabel(sc) {
  return sc.default ? `${sc.name} (Default)` : sc.name;
}

function StorageClassSelect({ value, onChange, storageClasses, loading, disabled }) {
  if (loading) {
    return <p className="muted">Loading storage classes…</p>;
  }
  if (!storageClasses.length) {
    return (
      <select value="" onChange={() => {}} disabled>
        <option value="">No StorageClasses available</option>
      </select>
    );
  }
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled}>
      <option value="">Select StorageClass</option>
      {storageClasses.map((sc) => (
        <option key={sc.name} value={sc.name}>
          {formatStorageClassLabel(sc)}
        </option>
      ))}
    </select>
  );
}

function NodeSelect({ value, onChange, nodes, loading }) {
  if (loading) {
    return <p className="muted">Loading nodes…</p>;
  }

  const readyNodes = nodes.filter((node) => node.status === "Ready");
  if (!readyNodes.length) {
    return <p className="wizard-storage-warning">No schedulable nodes found.</p>;
  }

  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">Select node</option>
      {nodes.map((node) => (
        <option key={node.name} value={node.name} disabled={node.status !== "Ready"}>
          {node.name} ({node.status})
        </option>
      ))}
    </select>
  );
}

function AdvancedStorageOptions({
  advanced,
  setAdvanced,
  clusterNodes = [],
  clusterNodesLoading = false,
  onMarkStorageEdit,
}) {
  const [open, setOpen] = useState(false);
  const update = (patch) => setAdvanced({ ...advanced, ...patch });
  const storageType = advanced.storageType || "hostPath";

  return (
    <div className="wizard-advanced-storage">
      <button
        type="button"
        className="wizard-advanced-storage__toggle btn-outline btn-sm"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? "▾" : "▸"} Advanced Storage Options
      </button>
      {open ? (
        <div className="wizard-advanced-storage__panel card">
          <label className="wizard-checkbox">
            <input
              type="checkbox"
              checked={Boolean(advanced.createManualPv)}
              onChange={(e) => update({ createManualPv: e.target.checked })}
            />
            Create PersistentVolume manually
          </label>
          {advanced.createManualPv ? (
            <div className="wizard-form-grid">
              <Field label="PV Name" tooltip={STORAGE_TOOLTIPS.pv}>
                <input
                  value={advanced.pvName}
                  onChange={(e) => {
                    onMarkStorageEdit?.("pvName");
                    update({ pvName: e.target.value });
                  }}
                  placeholder={STORAGE_DEFAULTS.pvName}
                />
              </Field>
              <Field label="Capacity" hint="e.g. 1Gi">
                <input value={advanced.capacity} onChange={(e) => update({ capacity: e.target.value })} />
              </Field>
              <Field label="Storage Type">
                <select value={storageType} onChange={(e) => update({ storageType: e.target.value })}>
                  {PV_STORAGE_TYPES.map((t) => (
                    <option key={t.value} value={t.value}>
                      {t.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Reclaim Policy">
                <select value={advanced.reclaimPolicy} onChange={(e) => update({ reclaimPolicy: e.target.value })}>
                  {RECLAIM_POLICIES.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </Field>
              {storageType === "hostPath" ? (
                <Field label="Host Path">
                  <input value={advanced.hostPath} onChange={(e) => update({ hostPath: e.target.value })} placeholder="/mnt/data" />
                </Field>
              ) : null}
              {storageType === "nfs" ? (
                <>
                  <Field label="NFS Server">
                    <input value={advanced.nfsServer} onChange={(e) => update({ nfsServer: e.target.value })} placeholder="nfs.example.com" />
                  </Field>
                  <Field label="NFS Path">
                    <input value={advanced.nfsPath} onChange={(e) => update({ nfsPath: e.target.value })} placeholder="/exports/data" />
                  </Field>
                </>
              ) : null}
              {storageType === "local" ? (
                <>
                  <Field label="Local Path">
                    <input value={advanced.localPath} onChange={(e) => update({ localPath: e.target.value })} placeholder="/mnt/disks/ssd1" />
                  </Field>
                  <Field label="Node" hint="Local volumes must be scheduled on a specific node">
                    <NodeSelect
                      value={advanced.nodeName}
                      onChange={(nodeName) => update({ nodeName })}
                      nodes={clusterNodes}
                      loading={clusterNodesLoading}
                    />
                  </Field>
                </>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function PvcFields({
  pvc,
  setPvc,
  storageClasses,
  storageClassesLoading,
  storageWarnings = [],
  advanced,
  setAdvanced,
  clusterNodes = [],
  clusterNodesLoading = false,
  storageEdits,
  onMarkStorageEdit,
}) {
  const manualPv = Boolean(advanced?.createManualPv);

  const handleAdvancedChange = (nextAdvanced) => {
    if (nextAdvanced.createManualPv && !manualPv) {
      setPvc({ storageClass: "" });
      if (!storageEdits?.pvName && !String(nextAdvanced.pvName || "").trim()) {
        nextAdvanced = { ...nextAdvanced, pvName: STORAGE_DEFAULTS.pvName };
      }
    }
    setAdvanced(nextAdvanced);
  };

  return (
    <>
      {manualPv ? (
        <p className="wizard-storage-helper muted">
          Manual PV mode does not use dynamic provisioning. KubeSight will create a matching PV and PVC directly.
        </p>
      ) : (
        <p className="wizard-storage-helper muted">
          Recommended: Use a StorageClass and let Kubernetes dynamically create the PersistentVolume for you.
        </p>
      )}
      <div className="wizard-form-grid">
        <Field label="PVC Name" tooltip={STORAGE_TOOLTIPS.pvc}>
          <input
            value={pvc.name}
            onChange={(e) => {
              onMarkStorageEdit?.("pvcName");
              setPvc({ name: e.target.value });
            }}
            placeholder={STORAGE_DEFAULTS.pvcName}
          />
        </Field>
        <Field label="Size" hint="e.g. 1Gi">
          <input value={pvc.size} onChange={(e) => setPvc({ size: e.target.value })} />
        </Field>
        {!manualPv ? (
          <Field label="Storage Class" tooltip={STORAGE_TOOLTIPS.storageClass}>
            <StorageClassSelect
              value={pvc.storageClass}
              onChange={(val) => setPvc({ storageClass: val })}
              storageClasses={storageClasses}
              loading={storageClassesLoading}
            />
          </Field>
        ) : null}
        <Field
          label="Access Mode"
          tooltip={
            pvc.accessMode === "ReadWriteMany"
              ? STORAGE_TOOLTIPS.readWriteMany
              : pvc.accessMode === "ReadWriteOnce"
                ? STORAGE_TOOLTIPS.readWriteOnce
                : undefined
          }
        >
          <select value={pvc.accessMode} onChange={(e) => setPvc({ accessMode: e.target.value })}>
            {ACCESS_MODES.map((m) => (
              <option key={m} value={m}>
                {ACCESS_MODE_LABELS[m] || m}
              </option>
            ))}
          </select>
        </Field>
      </div>
      {storageWarnings.length ? (
        <ul className="wizard-storage-warnings">
          {storageWarnings.map((warning, index) => (
            <li key={index} className="wizard-storage-warning">
              {warning}
            </li>
          ))}
        </ul>
      ) : null}
      <AdvancedStorageOptions
        advanced={advanced}
        setAdvanced={handleAdvancedChange}
        clusterNodes={clusterNodes}
        clusterNodesLoading={clusterNodesLoading}
        onMarkStorageEdit={onMarkStorageEdit}
      />
    </>
  );
}

export function StepBasics({ state, setState, clusterOptions, nameValidation }) {
  const basics = state.basics;
  const setBasics = (patch) => setState((s) => ({ ...s, basics: { ...s.basics, ...patch } }));
  const labelRows = Object.entries(basics.labels || {}).map(([key, value]) => ({ key, value }));

  return (
    <div className="wizard-step-panel">
      <WizardSectionHeader
        title="Application Basics"
        subtitle="Define identity, target cluster, and ownership metadata."
      />
      <div className="wizard-form-grid">
        <Field label="Application Name *" hint={nameValidation?.error || nameValidation?.warning}>
          <input
            value={basics.appName}
            onChange={(e) => setBasics({ appName: e.target.value })}
            placeholder="my-app"
            className={nameValidation?.error ? "input-error" : ""}
          />
        </Field>
        <Field label="Version">
          <input value={basics.version} onChange={(e) => setBasics({ version: e.target.value })} placeholder="1.0.0" />
        </Field>
        <Field label="Cluster *">
          <select value={basics.clusterId} onChange={(e) => setBasics({ clusterId: e.target.value })}>
            <option value="">Select cluster</option>
            {clusterOptions.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name || c.id}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Namespace *">
          <NamespaceSelect
            clusterId={basics.clusterId}
            value={basics.namespace}
            onChange={(e) => setBasics({ namespace: e.target.value })}
          />
        </Field>
        <Field label="Team / Owner">
          <input value={basics.ownerTeam} onChange={(e) => setBasics({ ownerTeam: e.target.value })} placeholder="platform-team" />
        </Field>
        <Field label="Environment">
          <select value={basics.environment} onChange={(e) => setBasics({ environment: e.target.value })}>
            <option value="">—</option>
            {ENVIRONMENT_OPTIONS.map((o) => (
              <option key={o} value={o}>{o}</option>
            ))}
          </select>
        </Field>
        <Field label="Criticality">
          <select value={basics.criticality} onChange={(e) => setBasics({ criticality: e.target.value })}>
            <option value="">—</option>
            {CRITICALITY_OPTIONS.map((o) => (
              <option key={o} value={o}>{o}</option>
            ))}
          </select>
        </Field>
        <Field label="Description" hint="Shown in application metadata">
          <textarea value={basics.description} onChange={(e) => setBasics({ description: e.target.value })} rows={3} />
        </Field>
      </div>
      <Field label="Labels">
        <KeyValueEditor
          items={labelRows.length ? labelRows : [{ key: "", value: "" }]}
          onChange={(rows) =>
            setBasics({
              labels: Object.fromEntries(rows.filter((r) => r.key).map((r) => [r.key, r.value])),
            })
          }
          keyPlaceholder="label key"
          valuePlaceholder="label value"
        />
      </Field>
    </div>
  );
}

export function StepWorkload({ state, setState }) {
  return (
    <div className="wizard-step-panel">
      <WizardSectionHeader
        title="Workload Type"
        subtitle="Select the primary Kubernetes resource kind for this application."
      />
      <div className="wizard-workload-grid">
        {WORKLOAD_TYPES.map((type) => (
          <button
            key={type}
            type="button"
            className={`wizard-workload-card${state.workloadType === type ? " is-selected" : ""}`}
            onClick={() => setState((s) => ({ ...s, workloadType: type }))}
          >
            <strong>{type}</strong>
            <span className="muted">{workloadDescription(type)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function workloadDescription(type) {
  const map = {
    Deployment: "Stateless replicated pods",
    StatefulSet: "Stable identity & storage",
    DaemonSet: "Pod on every node",
    Job: "Run to completion",
    CronJob: "Scheduled jobs",
    Service: "Network exposure",
    ConfigMap: "Configuration data",
    Secret: "Sensitive data",
    PersistentVolumeClaim: "Persistent storage",
    HorizontalPodAutoscaler: "Auto scaling",
    Ingress: "HTTP routing",
  };
  return map[type] || "";
}

export function StepContainers({ state, setState }) {
  if (!POD_WORKLOAD_TYPES.has(state.workloadType)) {
    return (
      <div className="wizard-step-panel">
        <p className="muted">Container configuration applies to pod-based workloads. Selected type: <strong>{state.workloadType}</strong></p>
      </div>
    );
  }

  const updateContainer = (index, patch) => {
    setState((s) => {
      const containers = s.containers.map((c, i) => (i === index ? { ...c, ...patch } : c));
      return { ...s, containers };
    });
  };

  const addContainer = () => {
    setState((s) => ({
      ...s,
      containers: [...s.containers, { name: `container-${s.containers.length + 1}`, image: "", tag: "latest", pullPolicy: "IfNotPresent", ports: [8080], command: [], args: [], workingDir: "" }],
    }));
  };

  const removeContainer = (index) => {
    setState((s) => ({ ...s, containers: s.containers.filter((_, i) => i !== index) }));
  };

  return (
    <div className="wizard-step-panel">
      <WizardSectionHeader
        title="Container Configuration"
        subtitle="Configure one or more containers per pod. Examples: nginx:latest, redis:7, postgres:16"
      />
      {state.containers.map((container, index) => (
        <div key={index} className="wizard-container-card card">
          <div className="wizard-container-card__header">
            <strong>Container {index + 1}</strong>
            {state.containers.length > 1 ? (
              <button type="button" className="btn-outline btn-sm" onClick={() => removeContainer(index)}>Remove</button>
            ) : null}
          </div>
          <div className="wizard-form-grid">
            <Field label="Name">
              <input value={container.name} onChange={(e) => updateContainer(index, { name: e.target.value })} />
            </Field>
            <Field label="Image *">
              <input value={container.image} onChange={(e) => updateContainer(index, { image: e.target.value })} placeholder="nginx" />
            </Field>
            <Field label="Tag">
              <input value={container.tag} onChange={(e) => updateContainer(index, { tag: e.target.value })} placeholder="latest" />
            </Field>
            <Field label="Pull Policy">
              <select value={container.pullPolicy} onChange={(e) => updateContainer(index, { pullPolicy: e.target.value })}>
                {PULL_POLICIES.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
            </Field>
            <Field label="Container Ports">
              <PortListEditor
                ports={container.ports || []}
                onChange={(ports) => updateContainer(index, { ports: ports.length ? ports : [8080] })}
              />
            </Field>
            <Field label="Working Directory">
              <input value={container.workingDir} onChange={(e) => updateContainer(index, { workingDir: e.target.value })} />
            </Field>
            <Field label="Command">
              <input
                value={(container.command || []).join(" ")}
                onChange={(e) => updateContainer(index, { command: e.target.value ? e.target.value.split(" ") : [] })}
                placeholder="/bin/sh -c"
              />
            </Field>
            <Field label="Arguments">
              <input
                value={(container.args || []).join(" ")}
                onChange={(e) => updateContainer(index, { args: e.target.value ? e.target.value.split(" ") : [] })}
              />
            </Field>
          </div>
        </div>
      ))}
      <button type="button" className="btn-outline" onClick={addContainer}>+ Add Container</button>
    </div>
  );
}

export function StepEnvironment({ state, setState }) {
  const env = state.environment;
  const setEnv = (patch) => setState((s) => ({ ...s, environment: { ...s.environment, ...patch } }));

  if (state.workloadType === "ConfigMap") {
    return (
      <div className="wizard-step-panel">
        <WizardSectionHeader title="ConfigMap Data" />
        <KeyValueEditor
          items={Object.entries(env.configMapData || {}).map(([key, value]) => ({ key, value }))}
          onChange={(rows) => setEnv({ configMapData: Object.fromEntries(rows.filter((r) => r.key).map((r) => [r.key, r.value])) })}
        />
      </div>
    );
  }

  if (state.workloadType === "Secret") {
    return (
      <div className="wizard-step-panel">
        <WizardSectionHeader title="Secret Data" />
        <KeyValueEditor
          items={Object.entries(env.secretData || {}).map(([key, value]) => ({ key, value }))}
          onChange={(rows) => setEnv({ secretData: Object.fromEntries(rows.filter((r) => r.key).map((r) => [r.key, r.value])) })}
        />
      </div>
    );
  }

  return (
    <div className="wizard-step-panel">
      <WizardSectionHeader
        title="Environment Configuration"
        subtitle="Optional configuration references. Empty rows are ready for your input."
      />
      <Field label="Environment Variables">
        <KeyValueEditor
          items={env.envVars.map((r) => ({ name: r.name, value: r.value }))}
          onChange={(rows) => setEnv({ envVars: rows.map((r) => ({ name: r.name ?? r.key ?? "", value: r.value ?? "" })) })}
          emptyRow={{ ...EMPTY_ENV_VAR }}
          keyPlaceholder="VAR_NAME"
        />
      </Field>
      <Field label="ConfigMap References (name)">
        <KeyValueEditor
          items={env.configMapRefs.map((r) => ({ name: r.name, value: (r.keys || []).join(",") }))}
          onChange={(rows) =>
            setEnv({
              configMapRefs: rows.map((r) => ({
                name: r.name ?? r.key ?? "",
                keys: r.value ? r.value.split(",").map((k) => k.trim()).filter(Boolean) : [],
              })),
            })
          }
          emptyRow={{ name: EMPTY_CONFIGMAP_REF.name, value: "" }}
          keyPlaceholder="configmap-name"
          valuePlaceholder="keys (optional, comma-separated)"
        />
      </Field>
      <Field label="Secret References (name)">
        <KeyValueEditor
          items={env.secretRefs.map((r) => ({ name: r.name, value: (r.keys || []).join(",") }))}
          onChange={(rows) =>
            setEnv({
              secretRefs: rows.map((r) => ({
                name: r.name ?? r.key ?? "",
                keys: r.value ? r.value.split(",").map((k) => k.trim()).filter(Boolean) : [],
              })),
            })
          }
          emptyRow={{ name: EMPTY_SECRET_REF.name, value: "" }}
          keyPlaceholder="secret-name"
          valuePlaceholder="keys (optional)"
        />
      </Field>
      <Field label="Mounted Configuration Files">
        <MountedFilesEditor
          items={env.mountedFiles}
          onChange={(rows) => setEnv({ mountedFiles: rows })}
        />
      </Field>
    </div>
  );
}

export function StepResources({ state, setState }) {
  const res = state.resources;
  const setRes = (patch) => setState((s) => ({ ...s, resources: { ...s.resources, ...patch } }));

  return (
    <div className="wizard-step-panel">
      <WizardSectionHeader
        title="Resource Limits & Requests"
        subtitle="Set CPU and memory requests and limits for your workload."
      />
      <div className="wizard-form-grid">
        <Field label="CPU Request" hint="e.g. 250m">
          <input value={res.cpuRequest} onChange={(e) => setRes({ cpuRequest: e.target.value })} />
        </Field>
        <Field label="CPU Limit" hint="e.g. 500m">
          <input value={res.cpuLimit} onChange={(e) => setRes({ cpuLimit: e.target.value })} />
        </Field>
        <Field label="Memory Request" hint="e.g. 512Mi">
          <input value={res.memoryRequest} onChange={(e) => setRes({ memoryRequest: e.target.value })} />
        </Field>
        <Field label="Memory Limit" hint="e.g. 1Gi">
          <input value={res.memoryLimit} onChange={(e) => setRes({ memoryLimit: e.target.value })} />
        </Field>
      </div>
    </div>
  );
}

export function StorageReadinessBanner({ readiness }) {
  if (!readiness) return null;
  const icons = {
    green: <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" /></svg>,
    yellow: <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fillRule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clipRule="evenodd" /></svg>,
    red: <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" /></svg>,
  };
  return (
    <div className={`wizard-storage-readiness wizard-storage-readiness--${readiness.level}`}>
      <span className="wizard-storage-readiness__icon" aria-hidden="true">
        {icons[readiness.level] || <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><circle cx="10" cy="10" r="4" /></svg>}
      </span>
      <span>{readiness.message}</span>
    </div>
  );
}

export function StepStorage({
  state,
  setState,
  storageClasses = [],
  storageClassesLoading = false,
  storageWarnings = [],
  clusterNodes = [],
  clusterNodesLoading = false,
}) {
  const storage = state.storage;
  const advanced = storage.advanced || { ...EMPTY_ADVANCED_STORAGE };
  const setStorage = (patch) => setState((s) => ({ ...s, storage: { ...s.storage, ...patch } }));
  const setPvc = (patch) => setStorage({ newPvc: { ...storage.newPvc, ...patch } });
  const setAdvanced = (nextAdvanced) => setStorage({ advanced: nextAdvanced });
  const markStorageEdit = (field) => {
    setStorage({
      storageEdits: { ...(storage.storageEdits || {}), [field]: true },
    });
  };
  const handlePvcModeChange = (pvcMode) => {
    if (pvcMode === "new") {
      setState((s) => ({
        ...s,
        storage: applyNewPvcStorageDefaults({ ...s.storage, pvcMode }),
      }));
      return;
    }
    setStorage({ pvcMode });
  };

  if (state.workloadType === "PersistentVolumeClaim") {
    const pvc = storage.newPvc;
    return (
      <div className="wizard-step-panel">
        <WizardSectionHeader
          title="Persistent Volume Claim"
          subtitle="Configure storage for a standalone PVC resource."
        />
        <PvcFields
          pvc={pvc}
          setPvc={setPvc}
          storageClasses={storageClasses}
          storageClassesLoading={storageClassesLoading}
          storageWarnings={storageWarnings}
          advanced={advanced}
          setAdvanced={setAdvanced}
          clusterNodes={clusterNodes}
          clusterNodesLoading={clusterNodesLoading}
          storageEdits={storage.storageEdits}
          onMarkStorageEdit={markStorageEdit}
        />
      </div>
    );
  }

  return (
    <div className="wizard-step-panel">
      <WizardSectionHeader title="Storage" subtitle="Persistent volumes and mount paths for your workload." />
      <Field label="PVC Mode" tooltip={STORAGE_TOOLTIPS.pvc}>
        <select value={storage.pvcMode} onChange={(e) => handlePvcModeChange(e.target.value)}>
          <option value="none">No persistent storage</option>
          <option value="existing">Use existing PVC</option>
          <option value="new">Create new PVC</option>
        </select>
      </Field>
      {storage.pvcMode === "existing" ? (
        <Field label="Existing PVC Name" tooltip={STORAGE_TOOLTIPS.pvc}>
          <input value={storage.existingPvc} onChange={(e) => setStorage({ existingPvc: e.target.value })} />
        </Field>
      ) : null}
      {storage.pvcMode === "new" ? (
        <PvcFields
          pvc={storage.newPvc}
          setPvc={setPvc}
          storageClasses={storageClasses}
          storageClassesLoading={storageClassesLoading}
          storageWarnings={storageWarnings}
          advanced={advanced}
          setAdvanced={setAdvanced}
          clusterNodes={clusterNodes}
          clusterNodesLoading={clusterNodesLoading}
          storageEdits={storage.storageEdits}
          onMarkStorageEdit={markStorageEdit}
        />
      ) : null}
      <Field label="Volume Mounts">
        <VolumeMountsEditor
          items={storage.volumeMounts}
          onChange={(rows) =>
            setStorage({
              volumeMounts: rows,
              storageEdits: { ...(storage.storageEdits || {}), volumeMount: true },
            })
          }
        />
      </Field>
    </div>
  );
}

export function StepNetworking({ state, setState }) {
  const net = state.networking;
  const setNet = (patch) => setState((s) => ({ ...s, networking: { ...s.networking, ...patch } }));
  const svc = net.service;
  const ing = net.ingress;

  return (
    <div className="wizard-step-panel">
      <WizardSectionHeader title="Networking" subtitle="Service and ingress exposure for your workload." />
      {POD_WORKLOAD_TYPES.has(state.workloadType) ? (
        <>
          <label className="wizard-checkbox">
            <input type="checkbox" checked={svc.enabled} onChange={(e) => setNet({ service: { ...svc, enabled: e.target.checked } })} />
            <strong>Create Service</strong>
          </label>
          {svc.enabled ? (
            <div className="wizard-form-grid">
              <Field label="Service Type">
                <select value={svc.type} onChange={(e) => setNet({ service: { ...svc, type: e.target.value } })}>
                  {SERVICE_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </Field>
              <Field label="Port"><input type="number" value={svc.port} onChange={(e) => setNet({ service: { ...svc, port: Number(e.target.value) } })} /></Field>
              <Field label="Target Port"><input type="number" value={svc.targetPort} onChange={(e) => setNet({ service: { ...svc, targetPort: Number(e.target.value) } })} /></Field>
              <Field label="Protocol"><input value={svc.protocol} onChange={(e) => setNet({ service: { ...svc, protocol: e.target.value } })} /></Field>
            </div>
          ) : null}
        </>
      ) : null}
      <label className="wizard-checkbox">
        <input type="checkbox" checked={ing.enabled} onChange={(e) => setNet({ ingress: { ...ing, enabled: e.target.checked } })} />
        Create Ingress
      </label>
      {ing.enabled ? (
        <div className="wizard-form-grid">
          <Field label="Host"><input value={ing.host} onChange={(e) => setNet({ ingress: { ...ing, host: e.target.value } })} placeholder="app.example.com" /></Field>
          <Field label="Path"><input value={ing.path} onChange={(e) => setNet({ ingress: { ...ing, path: e.target.value } })} /></Field>
          <label className="wizard-checkbox">
            <input type="checkbox" checked={ing.tlsEnabled} onChange={(e) => setNet({ ingress: { ...ing, tlsEnabled: e.target.checked } })} />
            TLS Enabled
          </label>
          {ing.tlsEnabled ? (
            <Field label="TLS Secret"><input value={ing.tlsSecret} onChange={(e) => setNet({ ingress: { ...ing, tlsSecret: e.target.value } })} /></Field>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function StepHealth({ state, setState }) {
  const probes = state.healthChecks;
  const chipTemplates = [
    { id: "http-web", label: "HTTP Web", readiness: { enabled: true, type: "http", path: "/", port: 80 }, liveness: { enabled: true, type: "http", path: "/", port: 80 } },
    { id: "tcp", label: "TCP Port", readiness: { enabled: true, type: "tcp", port: 8080 }, liveness: { enabled: true, type: "tcp", port: 8080 } },
  ];

  const applyChipTemplate = (tpl) => {
    setState((s) => ({
      ...s,
      healthChecks: {
        ...s.healthChecks,
        readiness: { ...s.healthChecks.readiness, ...tpl.readiness },
        liveness: { ...s.healthChecks.liveness, ...(tpl.liveness || {}) },
      },
    }));
  };

  const renderProbe = (key, label) => {
    const probe = probes[key];
    const setProbe = (patch) => setState((s) => ({ ...s, healthChecks: { ...s.healthChecks, [key]: { ...s.healthChecks[key], ...patch } } }));
    return (
      <div key={key} className="wizard-probe-card card">
        <label className="wizard-checkbox">
          <input type="checkbox" checked={probe.enabled} onChange={(e) => setProbe({ enabled: e.target.checked })} />
          <strong>{label}</strong>
        </label>
        {probe.enabled ? (
          <div className="wizard-form-grid">
            <Field label="Type">
              <select value={probe.type} onChange={(e) => setProbe({ type: e.target.value })}>
                <option value="http">HTTP</option>
                <option value="tcp">TCP</option>
                <option value="command">Command</option>
              </select>
            </Field>
            {probe.type === "http" ? (
              <>
                <Field label="Path"><input value={probe.path} onChange={(e) => setProbe({ path: e.target.value })} /></Field>
                <Field label="Port"><input type="number" value={probe.port} onChange={(e) => setProbe({ port: Number(e.target.value) })} /></Field>
              </>
            ) : null}
            {probe.type === "tcp" ? (
              <Field label="Port"><input type="number" value={probe.port} onChange={(e) => setProbe({ port: Number(e.target.value) })} /></Field>
            ) : null}
            {probe.type === "command" ? (
              <Field label="Command"><input value={probe.command} onChange={(e) => setProbe({ command: e.target.value })} /></Field>
            ) : null}
          </div>
        ) : null}
      </div>
    );
  };

  return (
    <div className="wizard-step-panel">
      <WizardSectionHeader title="Health Checks" subtitle="Readiness and liveness probes for reliable rollouts." />
      <div className="wizard-template-chips">
        {chipTemplates.map((t) => (
          <button key={t.id} type="button" className="btn-outline btn-sm" onClick={() => applyChipTemplate(t)}>{t.label}</button>
        ))}
      </div>
      {renderProbe("readiness", "Readiness Probe")}
      {renderProbe("liveness", "Liveness Probe")}
      {renderProbe("startup", "Startup Probe")}
    </div>
  );
}

export function StepScaling({ state, setState }) {
  const scaling = state.scaling;
  const setScaling = (patch) => setState((s) => ({ ...s, scaling: { ...s.scaling, ...patch } }));
  const hpa = scaling.hpa;

  return (
    <div className="wizard-step-panel">
      <WizardSectionHeader title="Scaling" subtitle="Replica count and optional autoscaling." />
      {state.workloadType === "CronJob" ? (
        <Field label="Cron Schedule"><input value={scaling.cronSchedule} onChange={(e) => setScaling({ cronSchedule: e.target.value })} placeholder="0 * * * *" /></Field>
      ) : null}
      {["Deployment", "StatefulSet"].includes(state.workloadType) ? (
        <>
          <Field label="Replica Count">
            <input type="number" min={0} max={50} value={scaling.replicas} onChange={(e) => setScaling({ replicas: Number(e.target.value) })} />
          </Field>
          <label className="wizard-checkbox">
            <input type="checkbox" checked={hpa.enabled} onChange={(e) => setScaling({ hpa: { ...hpa, enabled: e.target.checked } })} />
            Enable Horizontal Pod Autoscaler
          </label>
          {hpa.enabled ? (
            <div className="wizard-form-grid">
              <Field label="Min Replicas"><input type="number" value={hpa.minReplicas} onChange={(e) => setScaling({ hpa: { ...hpa, minReplicas: Number(e.target.value) } })} /></Field>
              <Field label="Max Replicas"><input type="number" value={hpa.maxReplicas} onChange={(e) => setScaling({ hpa: { ...hpa, maxReplicas: Number(e.target.value) } })} /></Field>
              <Field label="CPU Threshold %"><input type="number" value={hpa.cpuThreshold} onChange={(e) => setScaling({ hpa: { ...hpa, cpuThreshold: Number(e.target.value) } })} /></Field>
              <Field label="Memory Threshold %"><input type="number" value={hpa.memoryThreshold ?? ""} onChange={(e) => setScaling({ hpa: { ...hpa, memoryThreshold: e.target.value ? Number(e.target.value) : null } })} placeholder="optional" /></Field>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

export function StepValidation({ validation }) {
  if (!validation) return <p className="muted">Run validation to check prerequisites…</p>;
  return (
    <div className="wizard-step-panel">
      <WizardSectionHeader title="Prerequisite Validation" />
      <div className="wizard-validation-summary">
        <span className="wizard-validation-stat passed">✅ {validation.passed} passed</span>
        <span className="wizard-validation-stat warning">⚠ {validation.warnings} warnings</span>
        <span className="wizard-validation-stat failed">❌ {validation.failed} failed</span>
      </div>
      <ul className="wizard-validation-list">
        {(validation.checks || []).map((check, i) => (
          <li key={i} className={`wizard-validation-item status-${check.status}`}>
            <span className="wizard-validation-icon">
              {check.status === "passed" ? "✅" : check.status === "warning" ? "⚠" : "❌"}
            </span>
            <div>
              <strong>{check.category}</strong>: {check.message}
              {check.detail ? <div className="muted">{check.detail}</div> : null}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function StepTemplates({ templates, onSelect, loading }) {
  if (loading) return <p className="muted">Loading templates…</p>;
  const grouped = templates.reduce((acc, t) => {
    const cat = t.category || "General";
    if (!acc[cat]) acc[cat] = [];
    acc[cat].push(t);
    return acc;
  }, {});

  return (
    <div className="wizard-step-panel">
      <WizardSectionHeader
        title="Start from a Template"
        subtitle="Deploy common applications with pre-configured settings. You can customize every step after selecting."
      />
      {Object.entries(grouped).map(([category, items]) => (
        <div key={category} className="wizard-template-group">
          <h5>{category}</h5>
          <div className="wizard-template-grid">
            {items.map((t) => (
              <button key={t.id} type="button" className="wizard-template-card card" onClick={() => onSelect(t.id)}>
                <strong>{t.name}</strong>
                <span className="muted">{t.description}</span>
                <span className="wizard-template-badge">{t.workloadType}</span>
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
