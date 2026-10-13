/** App profiles — the applications a ticket may ask for.
 *
 *  Hermes reads a ticket ("put the shop front end on the new cluster") and
 *  picks one of these by name; the chart, its version, its values and where it
 *  goes all come from here, never from the ticket — Hermes does not even see
 *  the values. A profile pins the chart version it was saved with. Nothing is
 *  installed before an approver approves.
 */

import { useEffect, useState } from "react";
import AppsPicker from "./AppsPicker.jsx";
import { Field } from "./common.jsx";
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
