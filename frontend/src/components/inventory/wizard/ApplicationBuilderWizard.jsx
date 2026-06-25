import { useCallback, useEffect, useMemo, useState } from "react";
import { listClusterNodes, listStorageClasses } from "../../../api/clustersApi.js";
import { getClusterDeployEligibility } from "../../../api/deploymentRequestsApi.js";
import {
  applyWizardDeploy,
  diffWizardDeploy,
  dryRunWizardDeploy,
  generateWizardManifests,
  getWizardTemplate,
  listWizardTemplates,
  validateWizardName,
  validateWizardPrerequisites,
} from "../../../api/inventoryApi.js";
import {
  getStorageReadiness,
  isCreatingPvc,
  validateStorageConfig,
} from "../../../lib/storageValidation.js";
import { useAuth } from "../../../context/AuthContext.jsx";
import { formatAccessError, isAccessDeniedError } from "../../../utils/authz.js";
import { normalizeClusterOptions } from "../../../utils/clusterOptions.js";
import YamlPreviewPanel from "./YamlPreviewPanel.jsx";
import AddToBundleButton from "../../changes/AddToBundleButton.jsx";
import {
  WIZARD_STEPS,
  applyTemplate,
  buildWizardPayload,
  createEmptyWizardState,
  POD_WORKLOAD_TYPES,
} from "./wizardDefaults.js";
import {
  StepBasics,
  StepContainers,
  StepEnvironment,
  StepHealth,
  StepNetworking,
  StepResources,
  StepScaling,
  StepStorage,
  StepTemplates,
  StepValidation,
  StorageReadinessBanner,
} from "./WizardStepPanels.jsx";

export default function ApplicationBuilderWizard({
  open,
  onClose,
  onSuccess,
  clusterOptions = [],
  defaultClusterId = "",
  initialState = null,
  showTemplatePicker = true,
}) {
  const { hasPermission } = useAuth();
  const canViewDiff = hasPermission("apps:diff");
  const clusterSelectOptions = normalizeClusterOptions(clusterOptions);
  const resolvedDefaultClusterId =
    defaultClusterId && clusterSelectOptions.some((c) => c.id === defaultClusterId)
      ? defaultClusterId
      : clusterSelectOptions[0]?.id || "";

  const [stepIndex, setStepIndex] = useState(showTemplatePicker ? -1 : 0);
  const [state, setState] = useState(() => createEmptyWizardState(resolvedDefaultClusterId));
  const [templates, setTemplates] = useState([]);
  const [templatesLoading, setTemplatesLoading] = useState(false);
  const [generatedYaml, setGeneratedYaml] = useState("");
  const [previousYaml, setPreviousYaml] = useState("");
  const [validation, setValidation] = useState(null);
  const [nameValidation, setNameValidation] = useState(null);
  const [preview, setPreview] = useState(null);
  const [deployDiff, setDeployDiff] = useState(null);
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [storageClasses, setStorageClasses] = useState([]);
  const [storageClassesLoading, setStorageClassesLoading] = useState(false);
  const [clusterNodes, setClusterNodes] = useState([]);
  const [clusterNodesLoading, setClusterNodesLoading] = useState(false);
  const [storageWarnings, setStorageWarnings] = useState([]);
  const [manifestWarnings, setManifestWarnings] = useState([]);
  const [eligibility, setEligibility] = useState(null);

  const currentStep = stepIndex >= 0 ? WIZARD_STEPS[stepIndex] : null;
  const payload = useMemo(() => buildWizardPayload(state), [state]);
  const confirmationPhrase = state.basics.namespace ? `APPLY ${state.basics.namespace}` : "";

  const reset = useCallback(() => {
    setStepIndex(showTemplatePicker ? -1 : 0);
    setState(initialState || createEmptyWizardState(resolvedDefaultClusterId));
    setGeneratedYaml("");
    setPreviousYaml("");
    setValidation(null);
    setNameValidation(null);
    setPreview(null);
    setDeployDiff(null);
    setConfirmation("");
    setError("");
    setStorageClasses([]);
    setClusterNodes([]);
    setStorageWarnings([]);
    setManifestWarnings([]);
    setEligibility(null);
  }, [initialState, resolvedDefaultClusterId, showTemplatePicker]);

  useEffect(() => {
    if (!open) return;
    reset();
  }, [open, reset]);

  useEffect(() => {
    if (!open || !showTemplatePicker) return;
    setTemplatesLoading(true);
    listWizardTemplates()
      .then(setTemplates)
      .catch(() => setTemplates([]))
      .finally(() => setTemplatesLoading(false));
  }, [open, showTemplatePicker]);

  useEffect(() => {
    const clusterId = state.basics.clusterId;
    if (!open || !clusterId) {
      setStorageClasses([]);
      return;
    }
    let cancelled = false;
    setStorageClassesLoading(true);
    listStorageClasses(clusterId)
      .then((items) => {
        if (!cancelled) setStorageClasses(Array.isArray(items) ? items : []);
      })
      .catch(() => {
        if (!cancelled) setStorageClasses([]);
      })
      .finally(() => {
        if (!cancelled) setStorageClassesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, state.basics.clusterId]);

  useEffect(() => {
    const clusterId = state.basics.clusterId;
    if (!open || !clusterId) {
      setClusterNodes([]);
      return;
    }
    let cancelled = false;
    setClusterNodesLoading(true);
    listClusterNodes(clusterId)
      .then((items) => {
        if (!cancelled) setClusterNodes(Array.isArray(items) ? items : []);
      })
      .catch(() => {
        if (!cancelled) setClusterNodes([]);
      })
      .finally(() => {
        if (!cancelled) setClusterNodesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, state.basics.clusterId]);

  // Gate the Deploy button on per-cluster approval requirements. The backend 403
  // remains the authoritative enforcement; this is an up-front hint only.
  useEffect(() => {
    const clusterId = state.basics.clusterId;
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
        // Errored/unknown check never blocks — let the backend gate enforce it.
        if (!cancelled) setEligibility(null);
      });
    return () => {
      cancelled = true;
    };
  }, [open, state.basics.clusterId]);

  useEffect(() => {
    if (!clusterNodes.length) return;
    const advanced = state.storage?.advanced;
    const usesLocalPv =
      advanced?.createManualPv && (advanced.storageType || "hostPath") === "local";
    if (!usesLocalPv) return;

    const readyNodes = clusterNodes.filter((node) => node.status === "Ready");
    const firstReady = readyNodes[0];
    if (!firstReady) return;

    setState((current) => {
      const currentAdvanced = current.storage?.advanced || {};
      const selected = currentAdvanced.nodeName;
      const selectedStillValid = readyNodes.some((node) => node.name === selected);
      if (selectedStillValid) return current;
      return {
        ...current,
        storage: {
          ...current.storage,
          advanced: { ...currentAdvanced, nodeName: firstReady.name },
        },
      };
    });
  }, [
    clusterNodes,
    state.storage?.advanced?.createManualPv,
    state.storage?.advanced?.storageType,
    state.basics.clusterId,
  ]);

  useEffect(() => {
    if (!storageClasses.length || !isCreatingPvc(state) || state.storage?.advanced?.createManualPv) return;
    const defaultSc = storageClasses.find((sc) => sc.default);
    if (!defaultSc) return;
    setState((current) => {
      const pvc = current.storage?.newPvc;
      if (pvc?.storageClass) return current;
      return {
        ...current,
        storage: {
          ...current.storage,
          newPvc: { ...pvc, storageClass: defaultSc.name },
        },
      };
    });
  }, [storageClasses, state.workloadType, state.storage?.pvcMode]);

  const storageValidation = useMemo(
    () => validateStorageConfig(state, storageClasses),
    [state, storageClasses],
  );
  const storageReadiness = useMemo(
    () => getStorageReadiness(state, storageClasses),
    [state, storageClasses],
  );

  useEffect(() => {
    if (!open || stepIndex < 0) return;
    const basics = state.basics;
    if (!basics.appName || !basics.namespace) return;
    const timer = setTimeout(() => {
      validateWizardName({ ...payload, workloadType: state.workloadType })
        .then((result) => setNameValidation(result))
        .catch(() => setNameValidation(null));
    }, 400);
    return () => clearTimeout(timer);
  }, [open, state.basics.appName, state.basics.namespace, state.basics.clusterId, state.workloadType, stepIndex, payload]);

  const selectTemplate = async (templateId) => {
    setBusy(true);
    setError("");
    try {
      const template = await getWizardTemplate(templateId);
      setState((s) => applyTemplate(s, template));
      setStepIndex(0);
    } catch (err) {
      setError(err.message || "Failed to load template");
    } finally {
      setBusy(false);
    }
  };

  const skipTemplates = () => setStepIndex(0);

  const generateYaml = async () => {
    const result = await generateWizardManifests(payload);
    setGeneratedYaml(result.yaml || "");
    setManifestWarnings(result.summary?.warnings || []);
    return result.yaml || "";
  };

  const runValidation = async () => {
    setBusy(true);
    setError("");
    try {
      const yaml = generatedYaml || (await generateYaml());
      setGeneratedYaml(yaml);
      const result = await validateWizardPrerequisites(payload);
      setValidation(result);
      if (result.yaml) setGeneratedYaml(result.yaml);
    } catch (err) {
      setError(err.message || "Validation failed");
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (!open || stepIndex < 0 || WIZARD_STEPS[stepIndex]?.key !== "validation") return;
    runValidation();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, stepIndex]);

  const runPreview = async () => {
    setBusy(true);
    setError("");
    try {
      const yaml = generatedYaml || (await generateYaml());
      setGeneratedYaml(yaml);
      const dryRun = await dryRunWizardDeploy(payload);
      if (canViewDiff) {
        try {
          const diffPayload = await diffWizardDeploy(payload);
          const content = String(diffPayload.diff || "").trim();
          setDeployDiff(content ? { content } : null);
        } catch (diffErr) {
          setDeployDiff({
            hint: isAccessDeniedError(diffErr.message)
              ? "Diff requires apps:diff permission."
              : formatAccessError(diffErr.message) || diffErr.message,
          });
        }
      }
      setPreview({ dryRun, yaml: dryRun.yaml || yaml });
    } catch (err) {
      setError(err.message || "Preview failed");
    } finally {
      setBusy(false);
    }
  };

  const runStorageValidation = () => {
    const result = validateStorageConfig(state, storageClasses);
    setStorageWarnings(result.warnings);
    if (result.errors.length) {
      setError(result.errors[0]);
      return false;
    }
    return true;
  };

  const applyDeploy = async () => {
    if (!runStorageValidation()) return;
    setBusy(true);
    setError("");
    try {
      const result = await applyWizardDeploy({
        ...payload,
        confirmation,
        yaml: state.advancedYamlEdit ? state.editedYaml : undefined,
      });
      onSuccess?.(result);
      onClose();
    } catch (err) {
      setError(err.message || "Deploy failed");
    } finally {
      setBusy(false);
    }
  };

  const canProceed = () => {
    if (stepIndex < 0) return true;
    const key = currentStep?.key;
    if (key === "basics") {
      return state.basics.appName && state.basics.clusterId && state.basics.namespace && !nameValidation?.error;
    }
    if (key === "containers" && POD_WORKLOAD_TYPES.has(state.workloadType)) {
      return state.containers.every((c) => c.image?.trim());
    }
    return true;
  };

  const goNext = async () => {
    setError("");
    if (currentStep?.key === "storage" && !runStorageValidation()) {
      return;
    }
    if (currentStep?.key === "review") {
      await runPreview();
      return;
    }
    if (stepIndex < WIZARD_STEPS.length - 1) {
      if (currentStep?.key === "scaling") {
        try {
          const yaml = await generateYaml();
          setGeneratedYaml(yaml);
        } catch (err) {
          setError(err.message);
          return;
        }
      }
      const nextIndex = stepIndex + 1;
      if (WIZARD_STEPS[nextIndex]?.key === "review" && !runStorageValidation()) {
        return;
      }
      if (WIZARD_STEPS[nextIndex]?.key === "review") {
        try {
          await generateYaml();
          await runPreview();
        } catch (err) {
          setError(err.message);
          return;
        }
      }
      setStepIndex(nextIndex);
    }
  };

  const goBack = () => {
    setError("");
    if (stepIndex === 0 && showTemplatePicker) {
      setStepIndex(-1);
    } else if (stepIndex > 0) {
      setStepIndex((i) => i - 1);
    }
  };

  if (!open) return null;

  const displayYaml = state.advancedYamlEdit ? state.editedYaml : generatedYaml;

  // Only block on a definitive result; in-flight/errored checks leave Deploy enabled.
  const approvalBlocked = Boolean(
    eligibility && eligibility.approvalRequired && !eligibility.hasActiveApproval,
  );
  const requiredApprovals = eligibility?.requiredApprovals;

  const renderStep = () => {
    if (stepIndex < 0) {
      return (
        <StepTemplates
          templates={templates}
          loading={templatesLoading}
          onSelect={selectTemplate}
        />
      );
    }
    switch (currentStep.key) {
      case "basics":
        return (
          <>
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
                This cluster requires an approved deployment request — request one from
                the Clusters tab
                {requiredApprovals
                  ? ` (needs ${requiredApprovals} approval${requiredApprovals === 1 ? "" : "s"})`
                  : ""}
                .
              </p>
            ) : null}
            <StepBasics state={state} setState={setState} clusterOptions={clusterSelectOptions} nameValidation={nameValidation} />
          </>
        );
      case "containers":
        return <StepContainers state={state} setState={setState} />;
      case "environment":
        return <StepEnvironment state={state} setState={setState} />;
      case "resources":
        return <StepResources state={state} setState={setState} />;
      case "storage":
        return (
          <StepStorage
            state={state}
            setState={setState}
            storageClasses={storageClasses}
            storageClassesLoading={storageClassesLoading}
            storageWarnings={storageWarnings.length ? storageWarnings : storageValidation.warnings}
            clusterNodes={clusterNodes}
            clusterNodesLoading={clusterNodesLoading}
          />
        );
      case "networking":
        return <StepNetworking state={state} setState={setState} />;
      case "health":
        return <StepHealth state={state} setState={setState} />;
      case "scaling":
        return <StepScaling state={state} setState={setState} />;
      case "validation":
        return <StepValidation validation={validation} />;
      case "review":
        return (
          <div className="wizard-step-panel">
            <h4>Review & Deploy</h4>
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
                This cluster requires an approved deployment request — request one from
                the Clusters tab
                {requiredApprovals
                  ? ` (needs ${requiredApprovals} approval${requiredApprovals === 1 ? "" : "s"})`
                  : ""}
                .
              </p>
            ) : null}
            <StorageReadinessBanner readiness={storageReadiness} />
            {manifestWarnings.length ? (
              <div className="wizard-hpa-warning" role="status">
                {manifestWarnings.map((warning) => (
                  <p key={warning} style={{ margin: 0 }}>⚠️ {warning}</p>
                ))}
              </div>
            ) : null}
            <Field label="Change Summary">
              <input
                value={state.changeSummary}
                onChange={(e) => setState((s) => ({ ...s, changeSummary: e.target.value }))}
                placeholder="Describe what changed in this deployment"
              />
            </Field>
            <label className="wizard-checkbox">
              <input
                type="checkbox"
                checked={state.advancedYamlEdit}
                onChange={(e) => {
                  const enabled = e.target.checked;
                  setState((s) => ({
                    ...s,
                    advancedYamlEdit: enabled,
                    editedYaml: enabled ? generatedYaml : s.editedYaml,
                  }));
                }}
              />
              Advanced Edit YAML (power users)
            </label>
            <YamlPreviewPanel
              yaml={displayYaml}
              readOnly={!state.advancedYamlEdit}
              onChange={(val) => setState((s) => ({ ...s, editedYaml: val }))}
              previousYaml={previousYaml}
              showCompare={Boolean(previousYaml)}
            />
            {deployDiff?.content ? (
              <pre className="wizard-yaml-panel__diff">{deployDiff.content}</pre>
            ) : deployDiff?.hint ? (
              <p className="muted">{deployDiff.hint}</p>
            ) : null}
            {preview?.dryRun ? (
              <p className="muted">Dry-run: {preview.dryRun.message || "Server-side validation passed."}</p>
            ) : null}
            <Field label={`Confirmation — type "${confirmationPhrase}"`}>
              <input value={confirmation} onChange={(e) => setConfirmation(e.target.value)} placeholder={confirmationPhrase} />
            </Field>
          </div>
        );
      default:
        return null;
    }
  };

  return (
    <div className="modal-overlay wizard-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel wizard-modal"
        role="dialog"
        aria-labelledby="app-builder-title"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="wizard-modal__header">
          <div>
            <h3 id="app-builder-title">Application Builder</h3>
            <p className="muted">Deploy Kubernetes applications without writing YAML manually</p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close"><svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" /></svg></button>
        </header>

        {stepIndex >= 0 ? (
          <nav className="wizard-stepper" aria-label="Deployment steps">
            {WIZARD_STEPS.map((step, index) => (
              <button
                key={step.key}
                type="button"
                className={`wizard-stepper__item${index === stepIndex ? " is-active" : ""}${index < stepIndex ? " is-complete" : ""}`}
                onClick={() => index <= stepIndex && setStepIndex(index)}
                disabled={index > stepIndex}
              >
                <span className="wizard-stepper__number">{step.number}</span>
                <span className="wizard-stepper__label">{step.label}</span>
              </button>
            ))}
          </nav>
        ) : null}

        <div className="wizard-modal__body">
          {error ? <p className="error-banner">{error}</p> : null}
          {renderStep()}
        </div>

        <footer className="wizard-modal__footer modal-actions">
          {stepIndex < 0 ? (
            <>
              <button type="button" className="btn-outline" onClick={onClose}>Cancel</button>
              <button type="button" className="btn-primary" onClick={skipTemplates}>Start from scratch</button>
            </>
          ) : (
            <>
              <button type="button" className="btn-outline" onClick={goBack} disabled={busy}>
                Back
              </button>
              {currentStep?.key === "validation" ? (
                <button type="button" className="btn-outline" onClick={runValidation} disabled={busy}>
                  Re-run checks
                </button>
              ) : null}
              {currentStep?.key === "review" ? (
                <>
                  <AddToBundleButton
                    className="btn-outline"
                    label="Add to Bundle"
                    disabled={busy}
                    buildDescriptor={async () => {
                      const yaml = displayYaml || generatedYaml || (await generateYaml());
                      return {
                        actionType: "create_from_template",
                        clusterId: state.basics.clusterId,
                        namespace: state.basics.namespace,
                        resourceKind: state.workloadType || "Deployment",
                        resourceName: state.basics.appName,
                        yaml,
                      };
                    }}
                    onAdded={onClose}
                  />
                  <button
                    type="button"
                    className="btn-primary"
                    onClick={applyDeploy}
                    disabled={busy || confirmation !== confirmationPhrase || approvalBlocked}
                  >
                    {busy ? "Deploying…" : "Deploy Application"}
                  </button>
                </>
              ) : (
                <button type="button" className="btn-primary" onClick={goNext} disabled={busy || !canProceed()}>
                  {currentStep?.key === "validation" ? "Continue to Review" : "Next"}
                </button>
              )}
            </>
          )}
        </footer>
      </section>
    </div>
  );
}

function Field({ label, children }) {
  return (
    <label className="wizard-field">
      <span className="wizard-field__label">{label}</span>
      {children}
    </label>
  );
}
