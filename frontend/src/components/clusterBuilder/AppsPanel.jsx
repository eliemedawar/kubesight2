/** The build's Inventory Helm charts on the build page.
 *
 *  ``BuildAppsCard`` lists them with where each stands — to install,
 *  installed, failed (with helm's words), waiting for the cluster's approval —
 *  and lets a chart that has not gone in be fixed or withdrawn. ``AppsPanel``
 *  is the day-two "Install apps" form: the wizard's picker, sent at once.
 */

import { useState } from "react";
import AppsPicker from "./AppsPicker.jsx";
import { Field, StatusPill } from "./common.jsx";
import {
  addClusterBuildApps,
  removeClusterBuildApp,
  updateClusterBuildApp,
} from "../../api/clusterBuildsApi.js";
import { APP_STATUS_LABELS, VALUES_MASK, appsError, appsPayload } from "../../utils/clusterApps.js";
import { confirmDialog } from "../../lib/dialogs.js";

const FIXABLE = ["pending", "failed", "rejected"];

function ValuesEditor({ build, app, notify, onChanged, onClose }) {
  const [text, setText] = useState(app.valuesYaml || "");
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      await updateClusterBuildApp(build.id, app.id, { valuesYaml: text });
      notify(`Values for ${app.releaseName} saved.`);
      onChanged();
      onClose();
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="sg-cb-app-edit">
      <Field
        label={`values.yaml for ${app.releaseName}`}
        htmlFor={`cb-app-edit-${app.id}`}
        hint={`${VALUES_MASK} keeps a saved password as it is. Retry the build to install with the new values.`}
      >
        <textarea
          id={`cb-app-edit-${app.id}`}
          className="sg-cb-input sg-cb-mono sg-cb-app-values"
          rows={12}
          spellCheck={false}
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
      </Field>
      <div className="sg-cb-actions">
        <button className="btn-ghost btn-sm" type="button" onClick={onClose}>Cancel</button>
        <button className="primary btn-sm" type="button" disabled={busy} onClick={save}>
          {busy ? "Saving…" : "Save values"}
        </button>
      </div>
    </div>
  );
}

export function BuildAppsCard({ build, canCreate, notify, onChanged }) {
  const [editing, setEditing] = useState(null);
  const [busy, setBusy] = useState(false);
  const apps = build.apps || [];
  if (!apps.length) return null;
  const running = ["building", "preflighting"].includes(build.status);

  const withdraw = async (app) => {
    if (!(await confirmDialog({
      title: "Withdraw chart",
      message: `Withdraw ${app.releaseName}? Nothing is removed from the cluster — the chart `
        + "is dropped from this build and will not be installed.",
      confirmLabel: "Withdraw",
      tone: "danger",
    }))) return;
    setBusy(true);
    try {
      await removeClusterBuildApp(build.id, app.id);
      onChanged();
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };

  const installed = apps.filter((app) => app.status === "installed").length;
  return (
    <div className="card sg-cb-card sg-cb-appcard">
      <div className="sg-cb-sect">
        <h2>Helm apps</h2>
        <span className="sg-cb-sect-right">
          {installed} of {apps.length} installed · in this order
        </span>
      </div>
      <ul className="sg-cb-app-list">
        {apps.map((app) => (
          <li key={app.id} className={`is-${app.status}`}>
            <div className="sg-cb-app-row">
              <StatusPill status={app.status}>{APP_STATUS_LABELS[app.status] || app.status}</StatusPill>
              <b className="sg-cb-mono">{app.releaseName}</b>
              <span className="muted">
                {app.namespace} · {app.chartName} {app.chartVersion}
                {app.valuesFile ? ` · from ${app.valuesFile}` : ""}
              </span>
              <span className="sg-cb-app-tools">
                {app.bundleId ? (
                  <a className="btn-ghost btn-sm" href="#/approvals">Change bundle #{app.bundleId}</a>
                ) : null}
                {canCreate && !running && FIXABLE.includes(app.status) ? (
                  <>
                    <button className="btn-ghost btn-sm" type="button" disabled={busy}
                            onClick={() => setEditing(editing === app.id ? null : app.id)}>
                      Edit values
                    </button>
                    <button className="btn-ghost btn-sm" type="button" disabled={busy}
                            onClick={() => withdraw(app)}>
                      Withdraw
                    </button>
                  </>
                ) : null}
              </span>
            </div>
            {app.error ? <p className="sg-cb-app-error">{app.error}</p> : null}
            {editing === app.id ? (
              <ValuesEditor
                build={build}
                app={app}
                notify={notify}
                onChanged={onChanged}
                onClose={() => setEditing(null)}
              />
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function AppsPanel({ build, canExecute, notify, onChanged, onClose }) {
  const [value, setValue] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const problem = value.length ? appsError(value, build.apps || []) : "";

  const apply = async () => {
    setBusy(true);
    setError("");
    try {
      const data = await addClusterBuildApps(build.id, appsPayload(value));
      const waiting = (data?.bundleIds || []).map((id) => `#${id}`).join(", ");
      notify(data?.pendingApproval
        ? `${build.name} needs approval for changes: sent for approval as change bundle ${waiting}. `
          + "The charts install once it is approved."
        : `Installing ${value.length} chart${value.length === 1 ? "" : "s"} on ${build.name}.`);
      onChanged();
      onClose();
    } catch (exception) {
      const message = exception.message || String(exception);
      setError(message);
      notify(message, true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card sg-cb-grow">
      <div className="sg-cb-sect">
        <h2>Install apps</h2>
        <span className="sg-cb-sect-right">
          <button className="btn-ghost btn-sm" type="button" onClick={onClose}>Close</button>
        </span>
      </div>
      {error ? <p className="sg-cb-grow-error" role="alert">{error}</p> : null}
      <p className="muted sg-cb-grow-lede">
        Helm charts from Inventory, installed on this running cluster in the order
        below. Each one is rendered with its values before anything is sent, and a
        release that already exists is left alone. If the cluster needs approval
        for changes, the charts wait for it as a change bundle.
      </p>
      <AppsPicker value={value} onChange={(next) => { setValue(next); setError(""); }}
                  installed={build.apps || []} notify={notify} />
      <div className="sg-cb-actions">
        {problem ? <span className="sg-cb-field-error">{problem}</span> : null}
        <button
          className="primary"
          type="button"
          disabled={!value.length || Boolean(problem) || busy || !canExecute}
          onClick={apply}
        >
          {busy
            ? "Checking and sending…"
            : value.length
              ? `Install ${value.length} chart${value.length === 1 ? "" : "s"} on ${build.name}`
              : "Add a chart to install"}
        </button>
        {!canExecute ? (
          <span className="muted">Installing charts needs the build-execute permission.</span>
        ) : null}
      </div>
    </div>
  );
}
