import { useEffect, useState } from "react";
import {
  getMergeCheckPolicy,
  updateMergeCheckPolicy,
} from "../../api/mergeChecksApi.js";
import LoadingState from "../../components/common/LoadingState.jsx";
import QualityGateFields from "../../components/catalog/QualityGateFields.jsx";

/**
 * Settings → Merge checks: the installation's quality gate.
 *
 * This is the number the whole installation is judged by. Every service
 * inherits it unless it has explicitly been set to override, so raising the bar
 * here raises it everywhere at once — which is the point, and also why the
 * panel says so above the field rather than leaving it to be discovered.
 *
 * Blank is a real answer here too: a blank limit is no limit, and an
 * installation that has configured no gate does not have one. Nothing invents a
 * number, because a default invented in a settings page would start blocking
 * merges nobody agreed to block.
 */
export default function MergeChecksPolicyPanel({ canManage = false }) {
  const [policy, setPolicy] = useState(null);
  const [form, setForm] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let live = true;
    getMergeCheckPolicy()
      .then((data) => {
        if (!live) return;
        setPolicy(data);
        setForm({ ...data.gate, enabledByDefault: Boolean(data.enabledByDefault) });
      })
      .catch((err) => live && setError(err.message || "Could not load the quality gate."))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, []);

  const set = (key, value) => {
    setForm((prev) => ({ ...prev, [key]: value }));
    setSaved(false);
  };

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const data = await updateMergeCheckPolicy(form);
      setPolicy(data);
      setForm({ ...data.gate, enabledByDefault: Boolean(data.enabledByDefault) });
      setSaved(true);
    } catch (err) {
      setError(err.message || "Could not save the quality gate.");
    } finally {
      setSaving(false);
    }
  };

  if (loading || !form) {
    return <LoadingState label="Loading the quality gate..." />;
  }

  const total = policy.effectiveGate?.maxTotalProblems;

  return (
    <div className="settings-panel-body">
      {error && <p className="banner-message error">{error}</p>}

      <section className="settings-card" id="settings-merge-checks">
        <div className="settings-card-head">
          <h3>Quality gate</h3>
        </div>
        <p className="muted">
          {total === null || total === undefined ? (
            <>
              No limit is configured, so merge checks report their findings and block
              nothing. Set a limit below to turn the gate on across every service that
              inherits it.
            </>
          ) : (
            <>
              A pull request with more than <strong>{total}</strong>{" "}
              {total === 1 ? "problem" : "problems"} is blocked, across every service
              that has not set its own limits. A service can override this on its
              Merge Checks tab.
            </>
          )}
        </p>

        <QualityGateFields
          values={form}
          disabled={!canManage}
          onChange={set}
          scope="policy"
        />
      </section>

      <section className="settings-card">
        <div className="settings-card-head">
          <h3>New services</h3>
        </div>
        <label className="sg-mc-check">
          <input
            type="checkbox"
            checked={Boolean(form.enabledByDefault)}
            disabled={!canManage}
            onChange={(event) => set("enabledByDefault", event.target.checked)}
          />
          Switch merge checks on for a service the first time it is configured
        </label>
        <p className="field-hint">
          Off by default. A service still does nothing until its webhook is pointed at
          KubeSight, so this only decides what the switch on the tab starts at.
        </p>
      </section>

      {canManage && (
        <div className="modal-actions">
          {saved && <span className="muted">Saved.</span>}
          <button type="button" className="primary" disabled={saving} onClick={save}>
            {saving ? "Saving…" : "Save quality gate"}
          </button>
        </div>
      )}
    </div>
  );
}
