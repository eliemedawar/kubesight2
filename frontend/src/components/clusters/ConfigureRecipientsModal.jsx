import { useEffect, useMemo, useState } from "react";
import {
  getDeploymentRequestRecipients,
  updateDeploymentRequestRecipients,
} from "../../api";

const SOURCE_LABELS = {
  configuredGroups: "Selected approver groups",
  configuredGroupsAndEmails: "Selected groups + additional recipients",
  configured: "Additional recipients below",
  env: "DEPLOYMENT_REQUEST_RECIPIENTS environment variable",
  receiverGroup: '"Deployment Approvers" alert-routing group',
  allReceivers: "All enabled email receivers (Alert Routing)",
  smtpFrom: "SMTP from-address (fallback)",
  none: "No recipients — requests will not be emailed",
};

/**
 * Admin modal to configure who receives deployment-request emails. Approvers
 * are chosen from Alert Routing receiver groups, with an optional additional
 * email list, plus a required-approvals quorum (e.g. 3 of 5 must approve).
 */
export default function ConfigureRecipientsModal({ open, onClose, clusters = [] }) {
  const [config, setConfig] = useState(null);
  const [groupIds, setGroupIds] = useState([]);
  const [text, setText] = useState("");
  const [required, setRequired] = useState(1);
  // Per-cluster overrides keyed by cluster id; absent keys fall back to `required`.
  const [clusterApprovals, setClusterApprovals] = useState({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (!open) return;
    setError("");
    setNotice("");
    setLoading(true);
    getDeploymentRequestRecipients()
      .then((data) => {
        setConfig(data);
        setGroupIds(data.groupIds || []);
        setText((data.recipients || []).join("\n"));
        setRequired(data.requiredApprovals ?? 1);
        setClusterApprovals(data.clusterApprovals || {});
      })
      .catch((err) => setError(err.message || "Failed to load recipients."))
      .finally(() => setLoading(false));
  }, [open]);

  const parseEmails = (value) =>
    value
      .split(/[\n,;]+/)
      .map((entry) => entry.trim())
      .filter(Boolean);

  const availableGroups = config?.availableGroups || [];

  // Live preview of the approver pool from current selections.
  const poolEmails = useMemo(() => {
    const set = new Set();
    availableGroups
      .filter((g) => groupIds.includes(g.id))
      .forEach((g) => (g.emails || []).forEach((e) => set.add(e.toLowerCase())));
    parseEmails(text).forEach((e) => set.add(e.toLowerCase()));
    return [...set];
  }, [availableGroups, groupIds, text]);

  const poolSize = poolEmails.length;

  const toggleGroup = (id) => {
    setGroupIds((prev) =>
      prev.includes(id) ? prev.filter((g) => g !== id) : [...prev, id]
    );
  };

  // Normalize the cluster list to { id, name } pairs, de-duplicated by id.
  const clusterList = useMemo(() => {
    const seen = new Set();
    return (clusters || [])
      .map((cluster) => ({
        id: cluster.id || cluster.name,
        name: cluster.name || cluster.id,
      }))
      .filter((cluster) => {
        if (!cluster.id || seen.has(cluster.id)) return false;
        seen.add(cluster.id);
        return true;
      });
  }, [clusters]);

  const globalDefault = Math.max(0, Number.parseInt(required, 10) || 0);

  const setClusterApproval = (clusterId, value) =>
    setClusterApprovals((prev) => ({ ...prev, [clusterId]: value }));

  const handleSave = async () => {
    setSaving(true);
    setError("");
    setNotice("");
    try {
      // Only persist explicit overrides; clusters left at the default are omitted
      // so they continue to track the global "Required approvals" value.
      const clusterApprovalsPayload = {};
      Object.entries(clusterApprovals).forEach(([clusterId, value]) => {
        if (value === "" || value === null || value === undefined) return;
        clusterApprovalsPayload[clusterId] = Math.max(0, Number.parseInt(value, 10) || 0);
      });
      const updated = await updateDeploymentRequestRecipients({
        recipients: parseEmails(text),
        groupIds,
        requiredApprovals: globalDefault,
        clusterApprovals: clusterApprovalsPayload,
      });
      setConfig(updated);
      setGroupIds(updated.groupIds || []);
      setText((updated.recipients || []).join("\n"));
      setRequired(updated.requiredApprovals ?? 1);
      setClusterApprovals(updated.clusterApprovals || {});
      setNotice("Recipients saved.");
    } catch (err) {
      setError(err.message || "Failed to save recipients.");
    } finally {
      setSaving(false);
    }
  };

  if (!open) return null;

  const resolved = config?.resolvedRecipients || [];
  const effectiveMax = Math.max(1, poolSize || 1);

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Configure deployment request recipients"
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: "640px", width: "100%" }}
      >
        <header className="modal-header">
          <h3>Configure Request Recipients</h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" /></svg>
          </button>
        </header>

        {loading ? (
          <p className="muted">Loading…</p>
        ) : (
          <>
            <p className="muted" style={{ marginTop: 0 }}>
              Choose the approver groups from Alert Routing. Deployment requests are
              emailed to every enabled member, and approved once the required number
              of approvals is reached.
            </p>

            <div className="form-section">
              <h4>Approver groups</h4>
              {availableGroups.length === 0 ? (
                <p className="muted">
                  No receiver groups configured yet. Create one in Settings → Alert
                  Routing, or add individual recipients below.
                </p>
              ) : (
                <div style={{ display: "grid", gap: "var(--space-2)" }}>
                  {availableGroups.map((group) => (
                    <label
                      key={group.id}
                      className="checkbox-row"
                      style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}
                    >
                      <input
                        type="checkbox"
                        checked={groupIds.includes(group.id)}
                        onChange={() => toggleGroup(group.id)}
                        disabled={!group.enabled}
                      />
                      <span>
                        {group.name}{" "}
                        <span className="muted">
                          ({group.memberCount} member{group.memberCount === 1 ? "" : "s"}
                          {group.enabled ? "" : ", disabled"})
                        </span>
                      </span>
                    </label>
                  ))}
                </div>
              )}
            </div>

            <div className="form-grid" style={{ marginTop: "var(--space-3)" }}>
              <label className="form-grid__full">
                Additional recipients (optional)
                <textarea
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  rows={3}
                  placeholder={"extra-approver@company.com"}
                />
              </label>
              <label className="form-grid__full">
                Required approvals
                <input
                  type="number"
                  min={0}
                  max={effectiveMax}
                  value={required}
                  onChange={(e) => setRequired(e.target.value)}
                />
                <span className="muted" style={{ fontSize: "var(--font-size-sm)" }}>
                  {(Number.parseInt(required, 10) || 0) <= 0
                    ? "No approval required — requests are auto-approved and deploy immediately."
                    : `Require ${Math.min(Number.parseInt(required, 10) || 0, effectiveMax)} of ${poolSize} approver${
                        poolSize === 1 ? "" : "s"
                      } to approve each request.`}
                </span>
              </label>
            </div>

            <div className="form-section" style={{ marginTop: "var(--space-3)" }}>
              <h4>Per-cluster approvals</h4>
              <p className="muted" style={{ marginTop: 0, fontSize: "var(--font-size-sm)" }}>
                Override the number of approvals needed before a deploy is allowed on a
                specific cluster. Leave a cluster blank to use the global default of{" "}
                <strong>{globalDefault}</strong>. Set <strong>0</strong> to let that cluster
                deploy without approval.
              </p>
              {clusterList.length === 0 ? (
                <p className="muted">No clusters available.</p>
              ) : (
                <div style={{ display: "grid", gap: "var(--space-2)" }}>
                  {clusterList.map((cluster) => {
                    const override = clusterApprovals[cluster.id];
                    const hasOverride =
                      override !== "" && override !== null && override !== undefined;
                    return (
                      <label
                        key={cluster.id}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          gap: "var(--space-2)",
                        }}
                      >
                        <span>{cluster.name}</span>
                        <input
                          type="number"
                          min={0}
                          value={hasOverride ? override : ""}
                          placeholder={String(globalDefault)}
                          onChange={(e) => setClusterApproval(cluster.id, e.target.value)}
                          style={{ width: "96px" }}
                          aria-label={`Approvals needed for ${cluster.name}`}
                        />
                      </label>
                    );
                  })}
                </div>
              )}
            </div>

            {config ? (
              <p className="muted" style={{ fontSize: "var(--font-size-sm)" }}>
                Currently sending to:{" "}
                <strong>{resolved.length ? resolved.join(", ") : "nobody"}</strong>
                {" "}({SOURCE_LABELS[config.source] || config.source})
                {!config.smtpConfigured ? (
                  <>
                    <br />
                    <span style={{ color: "var(--color-warn, #fbbf24)" }}>
                      SMTP is not configured — emails will not be delivered until it is
                      set up in Settings → Alert Routing.
                    </span>
                  </>
                ) : null}
              </p>
            ) : null}

            {notice ? <p className="banner-message" role="status">{notice}</p> : null}
            {error ? <p className="banner-message error">{error}</p> : null}

            <div className="modal-actions">
              <button type="button" className="btn-text" onClick={onClose} disabled={saving}>
                Close
              </button>
              <button type="button" className="btn-primary" onClick={handleSave} disabled={saving}>
                {saving ? "Saving…" : "Save Recipients"}
              </button>
            </div>
          </>
        )}
      </section>
    </div>
  );
}
