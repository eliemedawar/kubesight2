/** Inventory Helm charts for a cluster: which chart, which version, where its
 *  values start (the chart's defaults or one of its values-*.yaml), the whole
 *  values.yaml on top, and the namespace and release it goes in as.
 *
 *  Shared by the wizard's Apps step and the day-two "Install apps" panel.
 *  Charts install in list order. The values editor opens with the chart's own
 *  values; once you type in it, picking another values file asks before
 *  replacing what you wrote. Passwords the stored chart blanked are listed
 *  until they appear in the text — preflight is the real check.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Field } from "./common.jsx";
import { getHelmChartTemplate, listHelmChartTemplates } from "../../api/helmApi.js";
import { getAppStartingValues } from "../../api/clusterBuildsApi.js";
import {
  VALUES_MASK,
  moveItem,
  newAppItem,
  releaseNameFor,
  unfilledRequired,
} from "../../utils/clusterApps.js";
import { confirmDialog } from "../../lib/dialogs.js";

/** values-*.yaml files of one chart version, cached per slug@version. */
function useValuesFiles(charts) {
  const [byKey, setByKey] = useState({});
  const asked = useRef(new Set());
  const filesFor = (slug, version) => {
    const chart = charts.find((entry) => entry.id === slug);
    if (!chart) return [];
    if (!version || version === chart.version) return chart.chart?.valuesFiles || [];
    return byKey[`${slug}@${version}`] || [];
  };
  const ensure = (slug, version) => {
    const chart = charts.find((entry) => entry.id === slug);
    const key = `${slug}@${version}`;
    if (!chart || !version || version === chart.version || asked.current.has(key)) return;
    asked.current.add(key);
    getHelmChartTemplate(slug, version)
      .then((detail) => setByKey((prev) => ({ ...prev, [key]: detail.chart?.valuesFiles || [] })))
      .catch(() => setByKey((prev) => ({ ...prev, [key]: [] })));
  };
  return { filesFor, ensure };
}

function AppCard({
  item, index, count, charts, filesFor, ensureFiles, onChange, onMove, onRemove, notify, single,
}) {
  const [required, setRequired] = useState([]);
  const [loading, setLoading] = useState(false);
  const chart = charts.find((entry) => entry.id === item.chartSlug);
  const versions = chart?.versions?.length
    ? chart.versions.map((entry) => entry.version)
    : chart ? [chart.version] : [];
  const valuesFiles = filesFor(item.chartSlug, item.chartVersion);
  const missing = unfilledRequired(item.valuesYaml, required);
  const masked = item.valuesYaml.includes(VALUES_MASK);

  useEffect(() => {
    if (item.chartSlug && item.chartVersion) ensureFiles(item.chartSlug, item.chartVersion);
  }, [item.chartSlug, item.chartVersion]); // eslint-disable-line react-hooks/exhaustive-deps

  // The editor's starting point follows the chart, version and values file —
  // until the person has typed in it.
  useEffect(() => {
    if (!item.chartSlug) return undefined;
    let cancelled = false;
    setLoading(true);
    getAppStartingValues(item.chartSlug, item.chartVersion, item.valuesFile)
      .then((data) => {
        if (cancelled) return;
        setRequired(data.requiredInputs || []);
        if (!item.valuesEdited) onChange({ valuesYaml: data.valuesYaml || "" });
      })
      .catch((error) => { if (!cancelled) notify(error.message || String(error), true); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [item.chartSlug, item.chartVersion, item.valuesFile]); // eslint-disable-line react-hooks/exhaustive-deps

  const pickStart = async (patch) => {
    if (
      item.valuesEdited
      && !(await confirmDialog({
        title: "Replace your values?",
        message: "Starting from another chart, version or values file replaces what "
          + "you wrote in values.yaml.",
        confirmLabel: "Replace",
        cancelLabel: "Keep my values",
      }))
    ) {
      onChange(patch);
      return;
    }
    onChange({ ...patch, valuesEdited: false });
  };

  const id = (field) => `cb-app-${item.key}-${field}`;
  return (
    <div className="sg-cb-app">
      <div className="sg-cb-app-head">
        <span className="sg-cb-app-n">{index + 1}</span>
        <b>{item.releaseName || "New chart"}</b>
        <span className="muted">
          {chart ? `${chart.name} ${item.chartVersion}` : "pick a chart"}
          {item.namespace ? ` · ${item.namespace}` : ""}
        </span>
        {single ? null : <span className="sg-cb-app-tools">
          <button className="btn-ghost btn-sm" type="button" disabled={index === 0}
                  onClick={() => onMove(-1)} aria-label="Install earlier">↑</button>
          <button className="btn-ghost btn-sm" type="button" disabled={index === count - 1}
                  onClick={() => onMove(1)} aria-label="Install later">↓</button>
          <button className="btn-ghost btn-sm" type="button" onClick={onRemove}>Remove</button>
        </span>}
      </div>

      <div className="sg-cb-app-grid">
        <Field label="Helm chart" htmlFor={id("chart")}>
          <select
            id={id("chart")}
            className="sg-cb-input"
            value={item.chartSlug}
            onChange={(event) => {
              const next = charts.find((entry) => entry.id === event.target.value);
              const followsChart = !item.releaseName
                || item.releaseName === releaseNameFor(item.chartSlug);
              pickStart({
                chartSlug: next?.id || "",
                chartVersion: next?.version || "",
                valuesFile: "",
                ...(followsChart ? { releaseName: releaseNameFor(next?.id) } : {}),
              });
            }}
          >
            <option value="">Pick a chart…</option>
            {charts.map((entry) => (
              <option key={entry.id} value={entry.id}>{entry.name}</option>
            ))}
          </select>
        </Field>
        <Field label="Version" htmlFor={id("version")}>
          <select
            id={id("version")}
            className="sg-cb-input"
            value={item.chartVersion}
            disabled={!chart}
            onChange={(event) => pickStart({ chartVersion: event.target.value, valuesFile: "" })}
          >
            {versions.map((version) => (
              <option key={version} value={version}>
                {version}{chart && version === chart.version ? " (current)" : ""}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Start values from" htmlFor={id("file")}>
          <select
            id={id("file")}
            className="sg-cb-input"
            value={item.valuesFile}
            disabled={!chart}
            onChange={(event) => pickStart({ valuesFile: event.target.value })}
          >
            <option value="">Chart defaults (values.yaml)</option>
            {valuesFiles.map((file) => (
              <option key={file.path} value={file.path}>
                {file.path}{file.environment ? ` — ${file.environment}` : ""}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Namespace" htmlFor={id("ns")}>
          <input
            id={id("ns")}
            className="sg-cb-input sg-cb-mono"
            value={item.namespace}
            placeholder="e.g. payments"
            spellCheck={false}
            onChange={(event) => onChange({ namespace: event.target.value.trim() })}
          />
        </Field>
        <Field label="Release name" htmlFor={id("release")}>
          <input
            id={id("release")}
            className="sg-cb-input sg-cb-mono"
            value={item.releaseName}
            spellCheck={false}
            onChange={(event) => onChange({ releaseName: event.target.value.trim().toLowerCase() })}
          />
        </Field>
      </div>

      <Field
        label="values.yaml"
        htmlFor={id("values")}
        hint={masked
          ? `${VALUES_MASK} keeps a saved password as it is; type over it to change it.`
          : "Your whole values file. It is layered over the chart's defaults and the file "
            + "above, kept encrypted, and deleted once the chart is installed."}
      >
        <textarea
          id={id("values")}
          className="sg-cb-input sg-cb-mono sg-cb-app-values"
          rows={14}
          spellCheck={false}
          value={loading && !item.valuesEdited ? "" : item.valuesYaml}
          placeholder={loading ? "Loading the chart's values…" : "key: value"}
          onChange={(event) => onChange({ valuesYaml: event.target.value, valuesEdited: true })}
        />
      </Field>
      {missing.length ? (
        <p className="sg-cb-app-missing">
          <b>Still to fill in:</b>{" "}
          {missing.map((input) => (
            <span className="sg-cb-mono" key={input.path}>{input.path}</span>
          ))}
          <span className="muted"> — the stored chart never keeps these.</span>
        </p>
      ) : null}
    </div>
  );
}

/** ``single``: exactly one chart (an app profile) — no add, reorder or remove. */
export default function AppsPicker({ value, onChange, installed = [], notify, single = false }) {
  const [charts, setCharts] = useState(null);
  const [loadError, setLoadError] = useState("");
  const { filesFor, ensure } = useValuesFiles(charts || []);

  useEffect(() => {
    let cancelled = false;
    listHelmChartTemplates()
      .then((data) => {
        if (cancelled) return;
        const items = Array.isArray(data) ? data : data?.items || [];
        setCharts([...items].sort((a, b) => String(a.name).localeCompare(String(b.name))));
      })
      .catch((error) => { if (!cancelled) setLoadError(error.message || String(error)); });
    return () => { cancelled = true; };
  }, []);

  // Values load asynchronously: an update must apply to the list as it is
  // then, not as it was when the request left.
  const latest = useRef(value);
  latest.current = value;
  const update = (key, patch) => {
    onChange(latest.current.map((item) => (item.key === key ? { ...item, ...patch } : item)));
  };
  const installedRows = useMemo(() => installed.filter((app) => app.status === "installed"), [installed]);

  if (loadError) {
    return <p className="sg-cb-field-error">The Helm charts could not be listed: {loadError}</p>;
  }
  if (charts === null) return <p className="muted">Loading Helm charts…</p>;

  return (
    <div className="sg-cb-apps">
      {installedRows.length ? (
        <ul className="sg-cb-app-installed">
          {installedRows.map((app) => (
            <li key={app.id}>
              <span className="sg-cb-pill is-ok">Installed</span>
              <b className="sg-cb-mono">{app.releaseName}</b>
              <span className="muted">{app.namespace} · {app.chartName} {app.chartVersion}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {value.map((item, index) => (
        <AppCard
          key={item.key}
          item={item}
          index={index}
          count={value.length}
          charts={charts}
          filesFor={filesFor}
          ensureFiles={ensure}
          notify={notify}
          onChange={(patch) => update(item.key, patch)}
          onMove={(delta) => onChange(moveItem(value, index, delta))}
          onRemove={() => onChange(value.filter((_, i) => i !== index))}
          single={single}
        />
      ))}

      {single ? null : charts.length ? (
        <button
          className="btn-outline sg-cb-app-add"
          type="button"
          onClick={() => onChange([...value, newAppItem(null)])}
        >
          + Add a Helm chart
        </button>
      ) : (
        <p className="muted">
          There are no charts in Inventory → Helm Charts yet. Import one there first.
        </p>
      )}
    </div>
  );
}
