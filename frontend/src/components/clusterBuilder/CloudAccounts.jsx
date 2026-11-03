/** Sources → Cloud accounts: the keys KubeSight creates machines and clusters
 *  with on Oracle Cloud, AWS and Google Cloud.
 *
 *  Each cloud publishes the fields its form asks for (``providers[].fields``),
 *  so this form is drawn from them rather than hard-coding three. Secrets are
 *  never sent back: editing an account shows "stored" and a blank field keeps
 *  what is there.
 */

import { useState } from "react";
import { Field } from "./common.jsx";
import { freshness, timeAgo } from "../../utils/clusterBuilder.js";
import { accountFormError, accountPayload, accountState, providerLabel } from "../../utils/cloudProvisioning.js";
import {
  createCloudAccount,
  deleteCloudAccount,
  testCloudAccount,
  updateCloudAccount,
} from "../../api/clusterBuildsApi.js";
import { confirmDialog } from "../../lib/dialogs.js";

const REGION_HINTS = {
  oci: "me-dubai-1, eu-frankfurt-1 …",
  aws: "eu-west-1, me-central-1 …",
  gcp: "europe-west1, me-central1 …",
};

function emptyForm(provider) {
  return { name: "", provider, region: "", values: {}, caPem: "" };
}

function formFrom(account) {
  return {
    name: account.name,
    provider: account.provider,
    region: account.region || "",
    values: { ...(account.settings || {}) },
    caPem: "",
  };
}

function Freshness({ account }) {
  const state = accountState(account);
  if (state === "failed") {
    return <span className="sg-cb-fresh is-bad">last test failed {timeAgo(account.lastTestAt)}</span>;
  }
  const fresh = freshness(account.lastTestAt);
  if (fresh.never) return <span className="sg-cb-fresh is-stale">never tested</span>;
  return <span className={`sg-cb-fresh ${fresh.stale ? "is-stale" : ""}`}>{fresh.text}</span>;
}

function AccountForm({ providers, initial, editing, secretsSet = {}, busy, onSave, onClose }) {
  const [form, setForm] = useState(initial);
  const provider = providers.find((item) => item.id === form.provider) || null;
  const fields = provider?.fields || [];
  const error = accountFormError(form, fields, { editing, secretsSet });
  const setValue = (key, value) => setForm({ ...form, values: { ...form.values, [key]: value } });

  return (
    <div className="sg-cb-qgrid sg-cb-addform">
      {!editing ? (
        <Field label="Cloud" htmlFor="ca-provider">
          <div className="sg-cb-seg" role="group" aria-label="Cloud" id="ca-provider">
            {providers.map((item) => (
              <button key={item.id} type="button" aria-pressed={form.provider === item.id}
                      onClick={() => setForm({ ...emptyForm(item.id), name: form.name, region: "" })}>
                {item.label}
              </button>
            ))}
          </div>
        </Field>
      ) : null}
      <Field label="Name" htmlFor="ca-name" hint="How builds and Sources refer to this account.">
        <input id="ca-name" className="sg-cb-input" value={form.name}
               onChange={(e) => setForm({ ...form, name: e.target.value })} />
      </Field>
      <Field label="Default region" htmlFor="ca-region" hint={REGION_HINTS[form.provider] || ""}>
        <input id="ca-region" className="sg-cb-input sg-cb-mono" value={form.region}
               onChange={(e) => setForm({ ...form, region: e.target.value })} />
      </Field>
      {fields.map((item) => {
        const id = `ca-${item.key}`;
        const stored = item.secret && editing && secretsSet[item.key];
        const hint = [item.help, stored ? "Stored — leave blank to keep it." : ""].filter(Boolean).join(" ");
        const value = form.values[item.key] ?? "";
        return (
          <Field key={item.key} label={`${item.label}${item.required ? "" : " (optional)"}`} htmlFor={id} hint={hint}>
            {item.kind === "textarea" ? (
              <textarea id={id} className={`sg-cb-input sg-cb-mono ${item.secret ? "sg-cb-secret" : ""}`} rows={5}
                        value={value} placeholder={stored ? "stored" : item.placeholder || ""}
                        spellCheck={false} autoComplete="off"
                        onChange={(e) => setValue(item.key, e.target.value)} />
            ) : (
              <input id={id} type={item.secret ? "password" : "text"} className="sg-cb-input sg-cb-mono"
                     value={value} placeholder={stored ? "stored" : item.placeholder || ""} autoComplete="off"
                     onChange={(e) => setValue(item.key, e.target.value)} />
            )}
          </Field>
        );
      })}
      <details className="sg-cb-adv sg-cb-span">
        <summary>
          TLS inspection
          <span className="cv">only when a proxy re-signs the cloud&apos;s certificates</span>
        </summary>
        <div className="sg-cb-adv-body">
          <Field label="Extra CA certificate (PEM)" htmlFor="ca-pem"
                 hint={editing ? "Leave blank to keep the one stored, if any." : "The proxy's CA, trusted for this account's calls and OpenTofu's."}>
            <textarea id="ca-pem" className="sg-cb-input sg-cb-mono" rows={4} spellCheck={false}
                      value={form.caPem} placeholder="-----BEGIN CERTIFICATE-----"
                      onChange={(e) => setForm({ ...form, caPem: e.target.value })} />
          </Field>
        </div>
      </details>
      <div className="sg-cb-actions sg-cb-span">
        {error ? <span className="muted">{error}</span> : null}
        <button className="btn-ghost" type="button" onClick={onClose}>Cancel</button>
        <button className="primary" type="button" disabled={busy || Boolean(error)}
                onClick={() => onSave(accountPayload(form, fields))}>
          {busy ? "Saving…" : editing ? "Save changes" : "Save account"}
        </button>
      </div>
    </div>
  );
}

/** ``adding`` / ``setAdding`` belong to the Sources tab, whose Group header holds the Add button. */
export default function CloudAccounts({ accounts = [], providers = [], simulated = false, adding, setAdding, notify, reloadInfra }) {
  const [editingId, setEditingId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(null);

  const run = async (fn, after) => {
    setBusy(true);
    try {
      const result = await fn();
      await reloadInfra();
      if (after) after(result);
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (account) => {
    if (!(await confirmDialog({
      title: "Remove cloud account",
      message: `Remove the ${providerLabel(account.provider)} account "${account.name}"? Its stored keys are deleted. This cannot be undone.`,
      confirmLabel: "Remove",
      tone: "danger",
    }))) return;
    run(() => deleteCloudAccount(account.id), () => notify(`Removed ${account.name}.`));
  };

  const test = async (account) => {
    setTesting(account.id);
    try {
      const result = await testCloudAccount(account.id);
      notify(result.ok ? `${account.name}: ${result.message}` : result.message || `${account.name} refused the keys.`, !result.ok);
      await reloadInfra();
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setTesting(null);
    }
  };

  return (
    <>
      {simulated ? (
        <p className="muted sg-cb-pv-acct-msg">Demo mode: clouds answer with sample regions and networks, and nothing is created.</p>
      ) : null}
      {adding && providers.length ? (
        <AccountForm
          key="new"
          providers={providers}
          initial={emptyForm(providers[0].id)}
          editing={false}
          busy={busy}
          onClose={() => setAdding(false)}
          onSave={(payload) => run(() => createCloudAccount(payload), () => {
            setAdding(false);
            notify(`${payload.name} saved. Test it next.`);
          })}
        />
      ) : null}
      {accounts.length ? accounts.map((account) => (
        <div className="sg-cb-pv-vc" key={account.id}>
          <div className={`sg-cb-entry ${accountState(account) === "failed" ? "is-bad" : ""}`}>
            <span className="sg-cb-entry-id">
              <span className="en">
                <span className={`sg-cb-cloudbadge is-${account.provider}`}>{account.providerLabel || providerLabel(account.provider)}</span>
                {" "}{account.name}
              </span>
              <span className="ea sg-cb-mono">
                {[account.region, account.identity].filter(Boolean).join(" · ")}
                {account.inUse ? ` · ${account.inUse} build${account.inUse === 1 ? "" : "s"}` : ""}
              </span>
            </span>
            <span className="sg-cb-entry-right">
              <Freshness account={account} />
              <button className="btn-ghost btn-sm" type="button" disabled={testing !== null} onClick={() => test(account)}>
                {testing === account.id ? "Testing…" : "Test now"}
              </button>
              <button className="btn-ghost btn-sm" type="button" disabled={busy}
                      onClick={() => setEditingId(editingId === account.id ? null : account.id)}>
                {editingId === account.id ? "Close" : "Edit"}
              </button>
              <button className="btn-ghost btn-sm" type="button" disabled={busy || account.inUse > 0}
                      title={account.inUse ? "Builds still use this account." : undefined}
                      onClick={() => remove(account)}>
                Remove
              </button>
            </span>
          </div>
          {accountState(account) === "failed" && account.lastTestMessage ? (
            <p className="sg-cb-pv-acct-msg is-bad">{account.lastTestMessage}</p>
          ) : null}
          {editingId === account.id ? (
            <AccountForm
              key={`edit-${account.id}`}
              providers={providers}
              initial={formFrom(account)}
              editing
              secretsSet={account.secretsSet || {}}
              busy={busy}
              onClose={() => setEditingId(null)}
              onSave={(payload) => run(() => updateCloudAccount(account.id, payload), () => {
                setEditingId(null);
                notify(`${payload.name} saved.`);
              })}
            />
          ) : null}
        </div>
      )) : (
        !adding ? (
          <p className="muted">
            No cloud accounts yet. Add one to create VMs or managed clusters on Oracle Cloud, AWS or Google Cloud.
          </p>
        ) : null
      )}
    </>
  );
}
