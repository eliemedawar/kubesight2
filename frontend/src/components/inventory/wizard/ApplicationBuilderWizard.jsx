import { useCallback, useEffect, useMemo, useState } from "react";
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
import { useAuth } from "../../../context/AuthContext.jsx";
import { formatAccessError, isAccessDeniedError } from "../../../utils/authz.js";
import { normalizeClusterOptions } from "../../../utils/clusterOptions.js";
import YamlPreviewPanel from "./YamlPreviewPanel.jsx";
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
  StepWorkload,
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

  const applyDeploy = async () => {
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
        return <StepBasics state={state} setState={setState} clusterOptions={clusterSelectOptions} nameValidation={nameValidation} />;
      case "workload":
        return <StepWorkload state={state} setState={setState} />;
      case "containers":
        return <StepContainers state={state} setState={setState} />;
      case "environment":
        return <StepEnvironment state={state} setState={setState} />;
      case "resources":
        return <StepResources state={state} setState={setState} />;
      case "storage":
        return <StepStorage state={state} setState={setState} />;
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
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">×</button>
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
                <button
                  type="button"
                  className="btn-primary"
                  onClick={applyDeploy}
                  disabled={busy || confirmation !== confirmationPhrase}
                >
                  {busy ? "Deploying…" : "Deploy Application"}
                </button>
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
