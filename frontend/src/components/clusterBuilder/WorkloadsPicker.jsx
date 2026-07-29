/** Pick namespaces or workloads out of a cluster that already runs them.
 *
 *  The same component serves the wizard's Workloads step and the day-two
 *  panel, because it is the same decision either way: which cluster, which
 *  registry to check against, what to copy — and then the answer to "will
 *  these actually start?", which is the image check.
 *
 *  The image check is advisory by design. A workload with no image in the
 *  chosen registry gets a Remove button and a plain sentence about what will
 *  happen if it is kept; it never disables the button that copies it.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Field } from "./common.jsx";
import {
  STORAGE_SOURCES,
  WORKLOAD_KINDS,
  emptyStorage,
  expandNamespaceSelection,
  imageStatusText,
  isWholeNamespaceSelected,
  removeWorkload,
  selectedInNamespace,
  setAllStorageDecisions,
  setStorageDecision,
  storageNeeds,
  storageRows,
  storageSummary,
  toggleWholeNamespace,
  toggleWorkload,
  workloadPlanVerdict,
  workloadSelectionSummary,
} from "../../utils/clusterBuilder.js";
import {
  listWorkloadNamespaces,
  listWorkloadSources,
  listWorkloadsInNamespace,
  planWorkloadCopy,
} from "../../api/clusterBuildsApi.js";

const KIND_SHORT = {
  Deployment: "Deploy",
  StatefulSet: "STS",
  DaemonSet: "DS",
  CronJob: "Cron",
};

function KindTag({ kind }) {
  return <span className="sg-cb-kindtag">{KIND_SHORT[kind] || kind}</span>;
}

/** One workload's image verdict, or nothing when no plan has been run yet. */
function ImageDot({ status }) {
  if (!status) return null;
  const tone = status === "ok" ? "is-ok"
    : status === "missing" ? "is-bad"
      : status === "unreachable" ? "is-warn" : "is-idle";
  return (
    <span className={`sg-cb-wl-dot ${tone}`} title={imageStatusText(status)}>
      <i />
      <span>{imageStatusText(status)}</span>
    </span>
  );
}

/** Where each copied PersistentVolumeClaim lands.
 *
 *  Per claim, not per copy: a migration usually wants its database's existing
 *  data and is perfectly happy for a cache to start empty. Only rendered once
 *  the plan reports claims — a copy without volumes never sees this.
 */
function StorageSection({ rows, storage, onChange }) {
  const needs = storageNeeds(rows);
  const keys = rows.map((row) => row.key);
  const set = (patch) => onChange({ ...storage, ...patch });

  return (
    <div className="sg-cb-wl-storage">
      <div className="sg-cb-wl-head">
        <span>
          {rows.length} volume claim{rows.length === 1 ? "" : "s"} come along
        </span>
        <span className="muted sg-cb-wl-count">{storageSummary(rows)}</span>
      </div>
      <p className="sg-cb-field-hint">
        A copied claim is only a request for storage — a freshly built cluster has
        nothing to bind it to. Every volume KubeSight creates keeps a{" "}
        <b>Retain</b> policy, so deleting a claim can never delete the data.
      </p>

      <div className="sg-cb-wl-setall">
        <label htmlFor="wl-setall">Set every claim to</label>
        <select
          id="wl-setall"
          className="sg-cb-input"
          value={storage.default || "none"}
          onChange={(event) => onChange(
            setAllStorageDecisions(storage, keys, event.target.value)
          )}
        >
          {STORAGE_SOURCES.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
        <span className="muted">
          {STORAGE_SOURCES.find((o) => o.value === (storage.default || "none"))?.hint}
        </span>
      </div>

      {needs.nfs ? (
        <div className="sg-cb-wl-nfs">
          <Field label="NFS server" htmlFor="wl-nfs-server">
            <input
              id="wl-nfs-server"
              className="sg-cb-input sg-cb-mono"
              placeholder="10.4.1.20"
              value={storage.nfsServer || ""}
              onChange={(event) => set({ nfsServer: event.target.value })}
            />
          </Field>
          <Field
            label="Export root"
            htmlFor="wl-nfs-root"
            hint="One directory per claim is created under it, as <namespace>/<claim>."
          >
            <input
              id="wl-nfs-root"
              className="sg-cb-input sg-cb-mono"
              placeholder="/exports/kubesight"
              value={storage.nfsExportRoot || ""}
              onChange={(event) => set({ nfsExportRoot: event.target.value })}
            />
          </Field>
          <Field
            label="Mount options"
            htmlFor="wl-nfs-opts"
            hint="Optional, comma separated — e.g. nfsvers=4.1,hard."
          >
            <input
              id="wl-nfs-opts"
              className="sg-cb-input sg-cb-mono"
              placeholder="none"
              value={storage.nfsMountOptions || ""}
              onChange={(event) => set({ nfsMountOptions: event.target.value })}
            />
          </Field>
        </div>
      ) : null}

      {needs.storageClass ? (
        <Field
          label="StorageClass name"
          htmlFor="wl-sc"
          hint="Must already exist in the new cluster. Claims that name their own are left alone."
        >
          <input
            id="wl-sc"
            className="sg-cb-input sg-cb-mono"
            placeholder="nfs-client"
            value={storage.storageClassName || ""}
            onChange={(event) => set({ storageClassName: event.target.value })}
          />
        </Field>
      ) : null}

      <div className="sg-cb-wl-claims">
        {rows.map((row) => (
          <div className={`sg-cb-wl-claim ${row.error ? "is-bad" : ""}`} key={row.key}>
            <span className="sg-cb-wl-claim-id">
              <span className="cn sg-cb-mono">{row.key}</span>
              <span className="cs">
                {row.capacity || "no size"}
                {row.accessModes?.length ? ` · ${row.accessModes.join(", ")}` : ""}
                {row.workloads?.length ? ` · ${row.workloads.join(", ")}` : ""}
              </span>
            </span>
            <select
              className="sg-cb-input"
              aria-label={`Destination for ${row.key}`}
              value={row.source}
              onChange={(event) => onChange(
                setStorageDecision(storage, row.key, { source: event.target.value })
              )}
            >
              {STORAGE_SOURCES.map((option) => (
                <option
                  key={option.value}
                  value={option.value}
                  disabled={option.value === "reuse" && !row.reusable}
                >
                  {option.label}
                  {option.value === "reuse" && !row.reusable ? " — unavailable" : ""}
                </option>
              ))}
            </select>
            <span className="sg-cb-wl-claim-target sg-cb-mono">
              {row.error
                ? <span className="sg-cb-field-error">{row.error}</span>
                : row.target || <span className="muted">nothing to bind to</span>}
            </span>
            {row.source === "reuse" ? (
              <label className="sg-cb-wl-ro">
                <input
                  type="checkbox"
                  checked={row.readOnly}
                  onChange={(event) => onChange(setStorageDecision(
                    storage, row.key, { readOnly: event.target.checked }
                  ))}
                />
                read-only
              </label>
            ) : row.reusable ? (
              <span className="muted sg-cb-wl-ro">
                has data at {row.sourceTarget}
              </span>
            ) : (
              <span className="muted sg-cb-wl-ro">
                {row.sourceKind ? `on ${row.sourceKind} over there` : "unbound over there"}
              </span>
            )}
          </div>
        ))}
      </div>

      {needs.reuse ? (
        <p className="sg-cb-topowarn">
          ⚠ Reused exports are mounted read-write by both clusters. That is what a
          migration wants once the old workload is stopped — and data corruption
          while it is still running.
        </p>
      ) : null}
    </div>
  );
}

export default function WorkloadsPicker({
  value,
  onChange,
  onPlanChange = null,
  notify = null,
  compact = false,
}) {
  const items = value.items || [];
  const [sources, setSources] = useState([]);
  const [registries, setRegistries] = useState([]);
  const [namespaces, setNamespaces] = useState([]);
  const [loadingNamespaces, setLoadingNamespaces] = useState(false);
  const [openNamespace, setOpenNamespace] = useState("");
  const [workloadsBy, setWorkloadsBy] = useState({});
  const [loadingNamespace, setLoadingNamespace] = useState("");
  const [plan, setPlan] = useState(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const [showSystem, setShowSystem] = useState(false);

  const fail = useCallback((message) => {
    setError(message);
    if (notify) notify(message, true);
  }, [notify]);

  const publishPlan = useCallback((next) => {
    setPlan(next);
    if (onPlanChange) onPlanChange(next);
  }, [onPlanChange]);

  useEffect(() => {
    let ignore = false;
    listWorkloadSources()
      .then((data) => {
        if (ignore) return;
        setSources(data.items || []);
        setRegistries(data.registries || []);
      })
      .catch((exception) => fail(exception.message || String(exception)));
    return () => { ignore = true; };
  }, [fail]);

  useEffect(() => {
    let ignore = false;
    if (!value.sourceClusterId) { setNamespaces([]); return undefined; }
    setLoadingNamespaces(true);
    setError("");
    listWorkloadNamespaces(value.sourceClusterId)
      .then((data) => { if (!ignore) setNamespaces(data.items || []); })
      .catch((exception) => {
        if (!ignore) {
          setNamespaces([]);
          fail(exception.message || String(exception));
        }
      })
      .finally(() => { if (!ignore) setLoadingNamespaces(false); });
    return () => { ignore = true; };
  }, [value.sourceClusterId, fail]);

  const openWorkloads = (namespace) => {
    if (openNamespace === namespace) { setOpenNamespace(""); return; }
    setOpenNamespace(namespace);
    if (workloadsBy[namespace]) return;
    setLoadingNamespace(namespace);
    listWorkloadsInNamespace(value.sourceClusterId, namespace)
      .then((data) => setWorkloadsBy((previous) => (
        { ...previous, [namespace]: data.items || [] }
      )))
      .catch((exception) => fail(exception.message || String(exception)))
      .finally(() => setLoadingNamespace(""));
  };

  /** Any edit invalidates the last image check — a stale verdict is worse than
      none, because it reads as a promise about what is selected now. */
  const setItems = (next) => {
    publishPlan(null);
    onChange({ ...value, items: next });
  };

  const pickSource = (clusterId) => {
    const source = sources.find((row) => String(row.id) === String(clusterId));
    publishPlan(null);
    setOpenNamespace("");
    setWorkloadsBy({});
    onChange({
      ...value,
      sourceClusterId: clusterId,
      sourceClusterName: source?.name || "",
      items: [],
    });
  };

  const runCheck = async () => {
    setChecking(true);
    setError("");
    try {
      publishPlan(await planWorkloadCopy({
        sourceClusterId: value.sourceClusterId,
        sourceClusterName: value.sourceClusterName || "",
        registryConnectionId: value.registryConnectionId || undefined,
        storage: value.storage || emptyStorage(),
        items,
      }));
    } catch (exception) {
      publishPlan(null);
      fail(exception.message || String(exception));
    } finally {
      setChecking(false);
    }
  };

  const verdict = useMemo(() => (plan ? workloadPlanVerdict(plan) : null), [plan]);
  // Recomputed locally as the destinations change: the claim list itself is
  // what the server answered, and that does not move when a decision does.
  const claimRows = useMemo(
    () => storageRows(plan, value.storage || emptyStorage()),
    [plan, value.storage]
  );
  const statusByKey = useMemo(() => {
    const map = {};
    for (const workload of plan?.workloads || []) {
      map[`${workload.namespace}/${workload.kind}/${workload.name}`] = workload.imageStatus;
    }
    return map;
  }, [plan]);

  const visibleNamespaces = namespaces.filter((row) => showSystem || !row.system);
  const systemCount = namespaces.filter((row) => row.system).length;
  const liveSources = sources.filter((row) => row.live);

  return (
    <div className={`sg-cb-wl ${compact ? "is-compact" : ""}`}>
      <div className="sg-cb-wl-top">
        <Field
          label="Copy from"
          htmlFor="wl-source"
          hint={liveSources.length
            ? "Any cluster KubeSight can reach. Nothing in it is changed — this only reads."
            : "No cluster has a live API connection, so there is nothing to copy from yet."}
        >
          <select
            id="wl-source"
            className="sg-cb-input"
            value={value.sourceClusterId || ""}
            onChange={(event) => pickSource(event.target.value)}
          >
            <option value="">Select a cluster…</option>
            {sources.map((row) => (
              <option key={row.id} value={row.id} disabled={!row.live}>
                {row.name}{row.live ? "" : " — not connected"}
              </option>
            ))}
          </select>
        </Field>

        <Field
          label="Check images against"
          htmlFor="wl-registry"
          hint="Every image the copied workloads run is looked up in this registry."
        >
          <select
            id="wl-registry"
            className="sg-cb-input"
            value={value.registryConnectionId || ""}
            onChange={(event) => {
              publishPlan(null);
              onChange({
                ...value,
                registryConnectionId: event.target.value ? Number(event.target.value) : null,
              });
            }}
          >
            <option value="">
              {registries.length ? "No check — copy as-is" : "No registry is linked"}
            </option>
            {registries.map((row) => (
              <option key={row.id} value={row.id}>
                {row.name}{row.host ? ` · ${row.host}` : ""}
              </option>
            ))}
          </select>
        </Field>
      </div>

      {error ? <p className="sg-cb-grow-error" role="alert">{error}</p> : null}

      {value.sourceClusterId ? (
        <>
          <div className="sg-cb-wl-head">
            <span>
              {loadingNamespaces
                ? "Reading namespaces…"
                : `${visibleNamespaces.length} namespace${
                  visibleNamespaces.length === 1 ? "" : "s"} with workloads`}
            </span>
            {systemCount ? (
              <button
                type="button"
                className="sg-cb-fchip"
                aria-pressed={showSystem}
                onClick={() => setShowSystem(!showSystem)}
              >
                {showSystem ? "Hide" : "Show"} {systemCount} system namespace
                {systemCount === 1 ? "" : "s"}
              </button>
            ) : null}
            <span className="muted sg-cb-wl-count">{workloadSelectionSummary(items)}</span>
          </div>

          <div className="sg-cb-wl-list">
            {visibleNamespaces.map((row) => {
              const whole = isWholeNamespaceSelected(items, row.name);
              const picked = selectedInNamespace(items, row.name);
              const open = openNamespace === row.name;
              const workloads = workloadsBy[row.name] || [];
              return (
                <div
                  className={`sg-cb-wl-ns ${whole ? "is-whole" : ""} ${picked.length ? "is-part" : ""}`}
                  key={row.name}
                >
                  <div className="sg-cb-wl-nsrow">
                    <label className="sg-cb-wl-nspick">
                      <input
                        type="checkbox"
                        checked={whole}
                        onChange={() => setItems(toggleWholeNamespace(items, row.name))}
                      />
                      <span>
                        <span className="nn sg-cb-mono">{row.name}</span>
                        <span className="ns">
                          {WORKLOAD_KINDS
                            .filter((kind) => row.counts?.[kind])
                            .map((kind) => `${row.counts[kind]} ${KIND_SHORT[kind]}`)
                            .join(" · ") || "no workloads"}
                          {row.system ? " · system namespace" : ""}
                        </span>
                      </span>
                    </label>
                    <span className="sg-cb-wl-nsright">
                      {whole ? (
                        <span className="sg-cb-wl-tag">whole namespace</span>
                      ) : picked.length ? (
                        <span className="sg-cb-wl-tag is-part">
                          {picked.length} of {row.total} picked
                        </span>
                      ) : null}
                      <button
                        type="button"
                        className="btn-ghost btn-sm"
                        aria-expanded={open}
                        onClick={() => openWorkloads(row.name)}
                        disabled={!row.total}
                      >
                        {open ? "Close" : "Pick individually"}
                      </button>
                    </span>
                  </div>

                  {open ? (
                    <div className="sg-cb-wl-items">
                      {loadingNamespace === row.name ? (
                        <p className="muted">Reading {row.name}…</p>
                      ) : workloads.length ? workloads.map((workload) => {
                        const key = `${workload.namespace}/${workload.kind}/${workload.name}`;
                        const selected = whole || items.some(
                          (item) => item.namespace === workload.namespace
                            && item.kind === workload.kind
                            && item.name === workload.name
                        );
                        return (
                          <label className="sg-cb-wl-item" key={key}>
                            <input
                              type="checkbox"
                              checked={selected}
                              onChange={() => setItems(toggleWorkload(
                                // Unticking a row of a whole-namespace
                                // selection has to mean "all but this one".
                                expandNamespaceSelection(items, row.name, workloads),
                                workload,
                              ))}
                            />
                            <KindTag kind={workload.kind} />
                            <span className="wn sg-cb-mono">{workload.name}</span>
                            <span className="wi sg-cb-mono">
                              {(workload.images || []).join(", ") || "no image"}
                            </span>
                            <ImageDot status={statusByKey[key]} />
                          </label>
                        );
                      }) : (
                        <p className="muted">
                          Nothing copyable in {row.name}.
                        </p>
                      )}
                      {whole ? (
                        <p className="sg-cb-field-hint">
                          The whole namespace is selected, so everything here is
                          ticked. Untick one and the selection becomes the rest,
                          listed workload by workload.
                        </p>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              );
            })}
            {!visibleNamespaces.length && !loadingNamespaces ? (
              <p className="muted sg-cb-vm-empty">
                {namespaces.length
                  ? "Only system namespaces have workloads here."
                  : "No namespace in that cluster has a Deployment, StatefulSet, "
                    + "DaemonSet or CronJob."}
              </p>
            ) : null}
          </div>

          <div className="sg-cb-wl-check">
            <button
              className="btn-outline"
              type="button"
              disabled={!items.length || checking}
              onClick={runCheck}
            >
              {checking ? "Checking…" : plan ? "Re-check" : "Check what comes over"}
            </button>
            {verdict ? (
              <span className={`sg-cb-grow-verdict is-${
                verdict.tone === "good" ? "pass" : verdict.tone === "warn" ? "warn" : "plain"}`}
              >
                {verdict.headline}
              </span>
            ) : items.length ? (
              <span className="muted">
                Resolves what each workload needs and looks up every image.
              </span>
            ) : null}
          </div>

          {verdict ? (
            <div className="sg-cb-wl-plan">
              <div className="sg-cb-facts">
                <div className="sg-cb-fact">
                  <div className="k">Workloads</div>
                  <div className="v">{verdict.total}</div>
                </div>
                <div className="sg-cb-fact">
                  <div className="k">Comes with them</div>
                  <div className="v">{verdict.supportCount} config object(s)</div>
                </div>
                <div className="sg-cb-fact">
                  <div className="k">Images</div>
                  <div className="v">
                    {verdict.checked
                      ? `${verdict.imageCount - verdict.missingImageCount} of ${verdict.imageCount} in the registry`
                      : `${verdict.imageCount}, not checked`}
                  </div>
                </div>
              </div>

              {verdict.missing.length ? (
                <div className="sg-cb-wl-missing">
                  <div className="sg-cb-wl-missing-head">
                    <b>
                      {verdict.missing.length === 1
                        ? "1 workload has no image in that registry"
                        : `${verdict.missing.length} workloads have no image in that registry`}
                    </b>
                    <span>
                      {verdict.missing.length === 1 ? "It" : "They"} will be created
                      and {verdict.missing.length === 1 ? "its pod" : "their pods"} will
                      sit in ImagePullBackOff until the image is pushed. Remove
                      {verdict.missing.length === 1 ? " it" : " them"}, or keep
                      {verdict.missing.length === 1 ? " it" : " them"} and acknowledge it.
                    </span>
                  </div>
                  {verdict.missing.map((workload) => (
                    <div
                      className="sg-cb-entry"
                      key={`${workload.namespace}/${workload.kind}/${workload.name}`}
                    >
                      <span className="sg-cb-entry-id">
                        <span className="en">
                          <KindTag kind={workload.kind} />
                          <span className="sg-cb-mono">
                            {workload.namespace}/{workload.name}
                          </span>
                        </span>
                        <span className="ea sg-cb-mono">
                          {(workload.missingImages || []).join(", ")}
                        </span>
                      </span>
                      <span className="sg-cb-entry-right">
                        <button
                          className="btn-ghost btn-sm"
                          type="button"
                          onClick={() => setItems(removeWorkload(items, workload))}
                        >
                          Remove
                        </button>
                      </span>
                    </div>
                  ))}
                </div>
              ) : null}

              {claimRows.length ? (
                <StorageSection
                  rows={claimRows}
                  storage={value.storage || emptyStorage()}
                  onChange={(storage) => onChange({ ...value, storage })}
                />
              ) : null}

              {verdict.unreachable.length ? (
                <p className="sg-cb-topowarn">
                  ⚠ The registry did not answer for{" "}
                  {verdict.unreachable.length} workload
                  {verdict.unreachable.length === 1 ? "" : "s"} — the images may
                  well exist; nothing was proved either way.
                </p>
              ) : null}

              {verdict.skipped.length ? (
                <p className="sg-cb-topowarn">
                  ⚠ {verdict.skipped.length} selection
                  {verdict.skipped.length === 1 ? "" : "s"} no longer exist in the
                  source cluster and will be skipped: {verdict.skipped.join(", ")}.
                </p>
              ) : null}

              {verdict.warnings.map((warning) => (
                <p className="sg-cb-topowarn" key={warning}>⚠ {warning}</p>
              ))}
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
