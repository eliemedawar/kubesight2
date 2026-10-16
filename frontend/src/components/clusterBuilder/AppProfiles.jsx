/** App profiles — the applications a ticket may ask for.
 *
 *  Hermes reads a ticket ("put the shop front end on the new cluster") and
 *  picks one of these by name; the chart, its version, its values and where it
 *  goes all come from here, never from the ticket — Hermes does not even see
 *  the values. A profile pins the chart version it was saved with. Nothing is
 *  installed before an approver approves.
 *
 *  A profile may let a ticket name another of the chart's values files ("with
 *  the UAT values"). Then only what was changed in values.yaml here (the
 *  passwords, usually) goes on top of the file the ticket names.
 */

import { useEffect, useState } from "react";
import AppsPicker from "./AppsPicker.jsx";
import { Field } from "./common.jsx";
import { getHelmChartTemplate } from "../../api/helmApi.js";
import {
  createAppProfile,
  deleteAppProfile,
  listAppProfiles,
  updateAppProfile,
} from "../../api/appProfilesApi.js";
import { appsError, appsPayload, newAppItem } from "../../utils/clusterApps.js";
import { confirmDialog } from "../../lib/dialogs.js";

function formFromProfile(row) {
  return {
    id: row?.id || null,
    name: row?.name || "",
    description: row?.description || "",
    enabled: row ? row.enabled : true,
    allowedValuesFiles: row?.allowedValuesFiles || [],
    app: row
      ? {
        key: `profile-${row.id}`,
        chartSlug: row.chartSlug,
        chartVersion: row.chartVersion,
        valuesFile: row.valuesFile || "",
        namespace: row.namespace,
        releaseName: row.releaseName,
        valuesYaml: row.valuesYaml || "",
        valuesEdited: true,
      }
      : newAppItem(null),
  };
}

/** The chart's other values files a ticket may name instead of the profile's own. */
function TicketValuesFiles({ app, allowed, onChange }) {
  const [files, setFiles] = useState([]);
  useEffect(() => {
    if (!app.chartSlug) { setFiles([]); return undefined; }
    let cancelled = false;
    getHelmChartTemplate(app.chartSlug, app.chartVersion)
      .then((detail) => { if (!cancelled) setFiles(detail.chart?.valuesFiles || []); })
      .catch(() => { if (!cancelled) setFiles([]); });
    return () => { cancelled = true; };
  }, [app.chartSlug, app.chartVersion]);

  const others = files.filter((file) => file.path !== app.valuesFile);
  if (!others.length) return null;
  const toggle = (path, on) => onChange(on ? [...allowed, path] : allowed.filter((p) => p !== path));
  return (
    <div className="sg-cb-field">
      <span className="sg-cb-field-label">A ticket may pick instead</span>
      <span className="sg-cb-field-hint">
        Hermes installs with {app.valuesFile || "the chart defaults"} unless the ticket names one of
        these. Only what you changed in values.yaml above goes on top of the file it names.
      </span>
      {others.map((file) => (
        <label className="sg-cb-inlinecheck" key={file.path}>
          <input type="checkbox" checked={allowed.includes(file.path)}
                 onChange={(e) => toggle(file.path, e.target.checked)} />
          <span className="sg-cb-mono">{file.path}</span>
          {file.environment ? <span className="muted"> — {file.environment}</span> : null}
        </label>
      ))}
    </div>
  );
}

function ProfileEditor({ initial, notify, onClose, onSaved }) {
  const [form, setForm] = useState(initial);
  const [busy, setBusy] = useState(false);
  const problem = !form.name.trim() ? "Give the profile a name Hermes can copy." : appsError([form.app]);

  const save = async () => {
    setBusy(true);
    try {
      const [app] = appsPayload([form.app]);
      const payload = {
        name: form.name.trim(),
        description: form.description,
        enabled: form.enabled,
        chartSlug: app.chartSlug,
        chartVersion: app.chartVersion,
        valuesFile: app.valuesFile,
        namespace: app.namespace,
        releaseName: app.releaseName,
        valuesYaml: app.valuesYaml,
        // A file that is now the profile's own, or gone from the chart, drops out.
        allowedValuesFiles: form.allowedValuesFiles.filter((path) => path !== app.valuesFile),
      };
      if (form.id) await updateAppProfile(form.id, payload);
      else await createAppProfile(payload);
      notify(`App profile ${payload.name} saved.`);
      onSaved();
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="sg-cb-vmprofile-edit">
      <div className="sg-cb-qgrid sg-cb-addform">
        <Field label="Name Hermes copies" htmlFor="app-profile-name">
          <input id="app-profile-name" className="sg-cb-input" value={form.name}
                 placeholder="e.g. Shop front (UAT)"
                 onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </Field>
        <Field label="What it is, for Hermes to choose by" htmlFor="app-profile-desc">
          <input id="app-profile-desc" className="sg-cb-input" value={form.description}
                 placeholder="The shop front end, with UAT settings"
                 onChange={(e) => setForm({ ...form, description: e.target.value })} />
        </Field>
      </div>
      <AppsPicker
        single
        value={[form.app]}
        onChange={(next) => setForm((previous) => ({ ...previous, app: next[0] || previous.app }))}
        notify={notify}
      />
      <TicketValuesFiles
        app={form.app}
        allowed={form.allowedValuesFiles}
        onChange={(allowedValuesFiles) => setForm((previous) => ({ ...previous, allowedValuesFiles }))}
      />
      <div className="sg-cb-actions">
        {problem ? <span className="sg-cb-field-error">{problem}</span> : null}
        <button className="btn-ghost" type="button" onClick={onClose}>Cancel</button>
        <button className="primary" type="button" disabled={busy || Boolean(problem)} onClick={save}>
          {busy ? "Saving…" : "Save profile"}
        </button>
      </div>
    </div>
  );
}

export default function AppProfiles({ notify, vmRequestsEnabled = true }) {
  const [items, setItems] = useState(null);
  const [editing, setEditing] = useState(null);

  const load = () => listAppProfiles()
    .then((data) => setItems(data.items || []))
    .catch((error) => { setItems([]); notify(error.message || String(error), true); });
  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = async (row) => {
    try {
      await updateAppProfile(row.id, { enabled: !row.enabled });
      load();
    } catch (error) {
      notify(error.message || String(error), true);
    }
  };
  const remove = async (row) => {
    if (!(await confirmDialog({
      title: "Remove app profile",
      message: `Remove ${row.name}? Hermes can no longer ask for it. Nothing installed is touched.`,
      confirmLabel: "Remove",
      tone: "danger",
    }))) return;
    try {
      await deleteAppProfile(row.id);
      load();
    } catch (error) {
      notify(error.message || String(error), true);
    }
  };

  if (items === null) return <p className="muted">Loading…</p>;
  return (
    <div className="sg-cb-vmprofiles">
      {!vmRequestsEnabled ? (
        <p className="sg-cb-warn-text">
          Hermes is offered these only while VM requests are switched on (Settings → Integrations →
          Hermes ticket agent); the same approvers decide both.
        </p>
      ) : null}
      {items.map((row) => (
        editing?.id === row.id ? (
          <ProfileEditor key={row.id} initial={editing} notify={notify}
                         onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />
        ) : (
          <div className={`sg-cb-entry ${row.enabled ? "" : "is-off"}`} key={row.id}>
            <span className="sg-cb-entry-id">
              <span className="en">{row.name}</span>
              <span className="ea sg-cb-mono">
                {row.chartName} {row.chartVersion}{row.valuesFile ? ` · ${row.valuesFile}` : ""}
                {" → "}{row.releaseName} in {row.namespace}
              </span>
              {row.allowedValuesFiles?.length ? (
                <span className="ea">A ticket may pick: {row.allowedValuesFiles.join(", ")}</span>
              ) : null}
              {row.description ? <span className="ea">{row.description}</span> : null}
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
        <p className="muted">No app profiles yet. Until there is one, Hermes parks app tickets as Impediment.</p>
      ) : null}
      {editing && !editing.id ? (
        <ProfileEditor initial={editing} notify={notify}
                       onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />
      ) : !editing ? (
        <div className="sg-cb-actions">
          <button className="btn-outline" type="button" onClick={() => setEditing(formFromProfile(null))}>
            Add an app profile
          </button>
        </div>
      ) : null}
    </div>
  );
}
