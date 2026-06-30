import { useEffect, useState } from "react";
import {
  applyDeployYaml,
  diffDeployYaml,
  dryRunDeployYaml,
  validateDeployYaml,
} from "../../api/inventoryApi.js";
import { getResourceYaml } from "../../api/clustersApi.js";
import { getClusterDeployEligibility } from "../../api/deploymentRequestsApi.js";
import { usePermission } from "../../hooks/usePermission.js";
import { formatAccessError, isAccessDeniedError } from "../../utils/authz.js";
import YamlPreviewPanel from "../inventory/wizard/YamlPreviewPanel.jsx";
import AddToBundleButton from "../changes/AddToBundleButton.jsx";

// kubectl kind -> human label / YAML Kind casing for the editable resource kinds.
const KIND_META = {
  deployment: { label: "deployment", yamlKind: "Deployment", actionType: "edit_deployment" },
  configmap: { label: "config map", yamlKind: "ConfigMap", actionType: "edit_configmap" },
  secret: { label: "secret", yamlKind: "Secret", actionType: "edit_secret" },
};

function metaForKind(kind) {
  return KIND_META[kind] || { label: kind || "resource", yamlKind: kind || "Resource", actionType: "edit_resource" };
}

function describeDeployDiffError(err) {
  const message = err?.message || "Diff preview is unavailable.";
  if (isAccessDeniedError(message)) {
    return "Diff preview requires the apps:diff permission. You can still apply after confirming below.";
  }
  const lowered = message.toLowerCase();
  if (
    lowered.includes("executable file not found") ||
    lowered.includes('failed to run "diff"') ||
    lowered.includes("diff utility")
  ) {
    return message;
  }
  return formatAccessError(message, { suppressAccessDenied: false }) || message;
}

export default function EditResourceModal({
  open,
  clusterId,
  namespace,
  kind = "deployment",
  resourceName,
  onClose,
  onSuccess,
}) {
  const { hasPermission } = usePermission();
  const canViewDeployDiff = hasPermission("apps:diff");
  const meta = metaForKind(kind);

  const [step, setStep] = useState("edit");
  const [yaml, setYaml] = useState("");
  const [loading, setLoading] = useState(false);
  const [preview, setPreview] = useState(null);
  /** @type {null | { content: string } | { hint: string }} */
  const [deployDiff, setDeployDiff] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [eligibility, setEligibility] = useState(null);

  // Editing applies through the deploy pipeline, which a cluster may gate on an
  // approved deployment request. Mirror the deploy wizard so non-admins see the
  // requirement up front instead of only hitting a 403 at apply time.
  useEffect(() => {
    if (!open || !clusterId) {
      setEligibility(null);
      return undefined;
    }
    let cancelled = false;
    setEligibility(null);
    getClusterDeployEligibility(clusterId)
      .then((res) => {
        if (!cancelled) setEligibility(res || null);
      })
      .catch(() => {
        // An errored check is treated as "unknown" — the backend gate still
        // enforces approval on the actual apply.
        if (!cancelled) setEligibility(null);
      });
    return () => {
      cancelled = true;
    };
  }, [open, clusterId]);

  useEffect(() => {
    if (!open) {
      setStep("edit");
      setYaml("");
      setPreview(null);
      setDeployDiff(null);
      setError("");
      setBusy(false);
      setLoading(false);
      return undefined;
    }
    if (!clusterId || !namespace || !resourceName) {
      return undefined;
    }
    let cancelled = false;
    setLoading(true);
    setError("");
    getResourceYaml({
      clusterId,
      namespace,
      kind,
      name: resourceName,
    })
      .then((payload) => {
        if (!cancelled) setYaml(payload.yaml || payload.output || "");
      })
      .catch((err) => {
        if (!cancelled) {
          setError(formatAccessError(err.message) || err.message || "Failed to load YAML");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, clusterId, namespace, kind, resourceName]);

  if (!open) return null;

  // Only block when the check is definitive (approval required, none active).
  // In-flight/errored checks never disable the buttons — the backend 403 is the
  // authoritative fallback surfaced in `error`.
  const approvalBlocked = Boolean(
    eligibility && eligibility.approvalRequired && !eligibility.hasActiveApproval,
  );
  const requiredApprovals = eligibility?.requiredApprovals;

  const runPreview = async () => {
    setBusy(true);
    setError("");
    try {
      const validation = await validateDeployYaml({ clusterId, namespace, yaml });
      const dryRun = await dryRunDeployYaml({ clusterId, namespace, yaml });
      if (!canViewDeployDiff) {
        setDeployDiff({
          hint: "Diff preview requires the apps:diff permission. You can still apply after confirming below.",
        });
      } else {
        try {
          const diffPayload = await diffDeployYaml({ clusterId, namespace, yaml });
          const content = String(diffPayload.diff || "").trim();
          setDeployDiff(content ? { content } : null);
        } catch (diffError) {
          setDeployDiff({ hint: describeDeployDiffError(diffError) });
        }
      }
      setPreview({ validation, dryRun });
      setStep("confirm");
    } catch (err) {
      setError(formatAccessError(err.message) || err.message || "Validation failed");
    } finally {
      setBusy(false);
    }
  };

  const applyYaml = async () => {
    setBusy(true);
    setError("");
    try {
      await applyDeployYaml({
        clusterId,
        namespace,
        yaml,
        // Only Deployments register an inventory app; the backend ignores this
        // for other kinds, but we leave it unset to keep intent clear.
        deploymentName: kind === "deployment" ? resourceName : "",
      });
      onSuccess?.();
      onClose();
    } catch (err) {
      setError(formatAccessError(err.message) || err.message || "Apply failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel modal-card--wide edit-deployment-modal"
        role="dialog"
        aria-labelledby="edit-resource-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <h3 id="edit-resource-title">Edit &amp; apply — {resourceName}</h3>
            <p className="muted">
              Edit the {meta.label} YAML and apply it through the deploy pipeline.
            </p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" /></svg>
          </button>
        </header>

        {error ? <p className="banner-message error">{error}</p> : null}

        {approvalBlocked ? (
          <p
            className="error-banner"
            style={{
              background: "#fee2e2",
              border: "1px solid #dc2626",
              color: "#b91c1c",
              fontWeight: 600,
              padding: "0.75rem 1rem",
              borderRadius: "8px",
            }}
          >
            This cluster requires an approved deployment request before applying changes —
            request one from the Clusters tab
            {requiredApprovals
              ? ` (needs ${requiredApprovals} approval${requiredApprovals === 1 ? "" : "s"})`
              : ""}
            .
          </p>
        ) : null}

        {step === "edit" ? (
          loading ? (
            <p className="muted">Loading…</p>
          ) : (
            <div className="deploy-preview">
              <YamlPreviewPanel
                yaml={yaml}
                readOnly={false}
                onChange={(value) => setYaml(value)}
              />
              <div className="modal-actions">
                <button type="button" className="btn-text" onClick={onClose}>
                  Cancel
                </button>
                <AddToBundleButton
                  className="btn-secondary"
                  label="Add to Bundle"
                  disabled={busy || !yaml.trim()}
                  descriptor={{
                    actionType: meta.actionType,
                    clusterId,
                    clusterName: clusterId,
                    namespace,
                    resourceKind: meta.yamlKind,
                    resourceName,
                    yaml,
                  }}
                  onAdded={onClose}
                />
                <button
                  type="button"
                  className="btn-primary"
                  disabled={busy || !yaml.trim() || approvalBlocked}
                  onClick={runPreview}
                >
                  {busy ? "Validating..." : "Validate & Preview"}
                </button>
              </div>
            </div>
          )
        ) : null}

        {step === "confirm" && preview ? (
          <div className="deploy-preview">
            <h4>Resources to create/update</h4>
            <ul>
              {(preview.dryRun?.resources || preview.validation?.resources || []).map((res) => (
                <li key={`${res.kind}-${res.name}`}>
                  {res.kind} {res.name}
                </li>
              ))}
            </ul>
            {(preview.validation?.warnings || []).length ? (
              <div className="banner-message warn">
                {(preview.validation.warnings || []).join("; ")}
              </div>
            ) : null}
            {deployDiff?.content ? (
              <>
                <h4>Diff</h4>
                <p className="muted deploy-diff-caption">
                  Changes compared to what is already running in the cluster.
                </p>
                <pre className="yaml-preview">{deployDiff.content}</pre>
              </>
            ) : null}
            {deployDiff?.hint ? (
              <p className="muted deploy-diff-hint">{deployDiff.hint}</p>
            ) : null}
            <div className="modal-actions">
              <button type="button" className="btn-text" onClick={() => setStep("edit")}>
                Back
              </button>
              <button
                type="button"
                className="btn-primary"
                disabled={busy || approvalBlocked}
                onClick={applyYaml}
              >
                {busy ? "Applying..." : "Apply to Cluster"}
              </button>
            </div>
          </div>
        ) : null}
      </section>
    </div>
  );
}
