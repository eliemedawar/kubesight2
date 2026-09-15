import { useEffect, useMemo, useState } from "react";
import {
  createCiService,
  listCiSourceCredentials,
  previewCiBranches,
  updateCiSource,
} from "../../api/ciApi.js";
import { getCiAssistAvailability } from "../../api/ciAssistApi.js";
import HermesAnalysisPanel from "./HermesAnalysisPanel.jsx";
import { APPLICATION_TYPES, CRITICALITIES } from "./ciShared.jsx";

/**
 * Register a service in one flow instead of three tabs.
 *
 * The first screen asks only what a person actually knows before they have
 * looked at anything: what this is called, where its code lives, and whether
 * they want Hermes to work the rest out. It deliberately does NOT ask for the
 * Java version, the Gradle version, the framework or the package manager —
 * those are answers, and answering them is the job the flow exists to do.
 *
 * The service row is created BEFORE the analysis runs, and that ordering is the
 * whole safety property: a failed analysis leaves a real, usable, manually
 * configurable service behind rather than losing the registration. Hermes can
 * never be the reason somebody cannot create a service.
 */

const STEPS = ["Service", "Configure"];

export default function RegisterServiceWizard({ onClose, onCreated, onOpenService }) {
  const [step, setStep] = useState(0);
  const [form, setForm] = useState({
    name: "",
    slug: "",
    description: "",
    ownerTeam: "",
    criticality: "medium",
    repositoryUrl: "",
    credentialProfileId: "",
    defaultBranch: "main",
    workingDirectory: "",
    // The one choice that matters on this screen.
    method: "hermes",
    // Manual mode only, and only once the user has asked for it.
    applicationType: "java_gradle",
  });
  const [credentials, setCredentials] = useState([]);
  // Branches read from the live repository as soon as there is a URL and a
  // credential to read them with. A typed branch that does not exist fails
  // much later, inside a checkout, which is a bad place to learn it.
  const [branches, setBranches] = useState([]);
  const [branchError, setBranchError] = useState("");
  const [loadingBranches, setLoadingBranches] = useState(false);
  const [availability, setAvailability] = useState(null);
  const [service, setService] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  useEffect(() => {
    listCiSourceCredentials()
      .then((data) => setCredentials(data.items || []))
      .catch(() => setCredentials([]));
    // Asked before the choice is offered: an installation without Hermes shows
    // manual configuration and the reason, never a control that fails.
    getCiAssistAvailability()
      .then(setAvailability)
      .catch(() => setAvailability({ available: false, reason: "Hermes could not be reached." }));
  }, []);

  const hermesAvailable = availability?.available;
  useEffect(() => {
    if (availability && !hermesAvailable && form.method === "hermes") {
      set("method", "manual");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [availability]);

  // Debounced: this runs while somebody types or pastes a URL, and the answer
  // is only interesting once they stop.
  useEffect(() => {
    const url = form.repositoryUrl.trim();
    if (!url || !form.credentialProfileId) {
      setBranches([]);
      setBranchError("");
      return undefined;
    }
    let live = true;
    setLoadingBranches(true);
    const timer = window.setTimeout(() => {
      previewCiBranches({
        repositoryUrl: url,
        credentialProfileId: form.credentialProfileId,
      })
        .then((data) => {
          if (!live) return;
          const items = data.items || [];
          setBranches(items);
          setBranchError("");
          // Land on the repository's own default when the current value is not
          // one of its branches — "main" is a guess, not an answer.
          const names = items.filter((i) => i.type === "branch").map((i) => i.value);
          if (names.length && !names.includes(form.defaultBranch)) {
            const preferred = ["main", "master", "develop"].find((n) => names.includes(n));
            set("defaultBranch", preferred || names[0]);
          }
        })
        .catch((err) => {
          if (!live) return;
          setBranches([]);
          // Non-fatal: the field stays typeable, because a listing outage must
          // not stop somebody registering a service.
          setBranchError(err.message || "Branches could not be listed.");
        })
        .finally(() => {
          if (live) setLoadingBranches(false);
        });
    }, 500);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form.repositoryUrl, form.credentialProfileId]);

  const branchOptions = branches.filter((item) => item.type === "branch");

  const derivedSlug = useMemo(
    () =>
      form.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "service",
    [form.name]
  );

  const wantsRepository = form.method === "hermes";
  const canContinue =
    form.name.trim() &&
    (!wantsRepository || (form.repositoryUrl.trim() && form.credentialProfileId));

  const createService = async () => {
    setSaving(true);
    setError("");
    try {
      // Identity first. If the source or the analysis fails after this, the
      // service still exists and is still configurable by hand.
      const created = await createCiService({
        name: form.name,
        slug: form.slug.trim(),
        description: form.description,
        ownerTeam: form.ownerTeam,
        criticality: form.criticality,
        // In Hermes mode the type is a placeholder that the accepted profile
        // replaces; asking for it up front is exactly what this flow removes.
        applicationType: form.method === "manual" ? form.applicationType : "generic",
      });

      let withSource = created;
      if (form.repositoryUrl.trim() && form.credentialProfileId) {
        withSource = await updateCiSource(created.id, {
          repositoryUrl: form.repositoryUrl.trim(),
          credentialProfileId: form.credentialProfileId,
          defaultBranch: form.defaultBranch || "main",
          workingDirectory: form.workingDirectory,
        });
      }
      setService(withSource);
      onCreated?.(withSource);

      if (form.method === "manual") {
        // Nothing left for this dialog to do: the service exists and the
        // detail page owns every remaining decision.
        onOpenService?.(withSource, "pipeline");
        return;
      }
      setStep(1);
    } catch (err) {
      setError(err.message || "The service could not be registered.");
    } finally {
      setSaving(false);
    }
  };

  const finishManually = () => {
    if (service) onOpenService?.(service, "pipeline");
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={step === 0 ? onClose : undefined}>
      <div
        className="modal-card sg-ci-wizard"
        role="dialog"
        aria-label="Register a service"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-card__header">
          <h3>Register a service</h3>
          <ol className="sg-ci-wizard-steps" aria-label="Progress">
            {STEPS.map((label, index) => (
              <li key={label} className={index === step ? "is-current" : index < step ? "is-done" : ""}>
                {label}
              </li>
            ))}
          </ol>
        </div>

        {error && <p className="banner-message error">{error}</p>}

        {step === 0 && (
          <div className="sg-ci-wizard-body">
            <section className="form-section">
              <h4>Service</h4>
              <div className="form-grid">
                <label className="form-grid__full">
                  Name *
                  <input
                    value={form.name}
                    maxLength={160}
                    placeholder="e.g. Payment Service"
                    onChange={(event) => set("name", event.target.value)}
                  />
                </label>
                <label className="form-grid__full">
                  Slug (build identifier)
                  <input
                    value={form.slug}
                    maxLength={180}
                    placeholder={derivedSlug}
                    onChange={(event) =>
                      set("slug", event.target.value.toLowerCase().replace(/[^a-z0-9-]+/g, "-"))
                    }
                  />
                  <span className="field-hint">
                    Names the pushed image (<code>{form.slug.trim() || derivedSlug}:&lt;tag&gt;</code>).
                    For ticket-driven deploys it must equal the Kubernetes deployment name.
                  </span>
                </label>
                <label className="form-grid__full">
                  Description
                  <textarea
                    rows={2}
                    style={{ resize: "vertical" }}
                    value={form.description}
                    onChange={(event) => set("description", event.target.value)}
                  />
                </label>
                <label>
                  Owner / team
                  <input
                    value={form.ownerTeam}
                    maxLength={255}
                    placeholder="e.g. Payments"
                    onChange={(event) => set("ownerTeam", event.target.value)}
                  />
                </label>
                <label>
                  Criticality
                  <select
                    value={form.criticality}
                    onChange={(event) => set("criticality", event.target.value)}
                  >
                    {CRITICALITIES.map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            </section>

            <section className="form-section">
              <h4>Repository</h4>
              <div className="form-grid">
                <label className="form-grid__full">
                  Repository URL {wantsRepository && "*"}
                  <input
                    value={form.repositoryUrl}
                    placeholder="https://bitbucket.org/workspace/repository"
                    onChange={(event) => set("repositoryUrl", event.target.value)}
                  />
                  <span className="field-hint">
                    Bitbucket Cloud over HTTPS. Credentials must not be part of the URL.
                  </span>
                </label>
                <label>
                  Credential profile {wantsRepository && "*"}
                  <select
                    value={form.credentialProfileId}
                    onChange={(event) => set("credentialProfileId", event.target.value)}
                  >
                    <option value="">Select a credential…</option>
                    {credentials.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                  {credentials.length === 0 && (
                    <span className="field-hint">
                      None yet — add one under a service's Source tab, then come back.
                    </span>
                  )}
                </label>
                <label>
                  Branch
                  {branchOptions.length > 0 ? (
                    <select
                      value={form.defaultBranch}
                      onChange={(event) => set("defaultBranch", event.target.value)}
                    >
                      {/* A value that is not in the list is still shown, so a
                          branch created seconds ago is never silently dropped. */}
                      {!branchOptions.some((item) => item.value === form.defaultBranch) &&
                        form.defaultBranch && (
                          <option value={form.defaultBranch}>{form.defaultBranch}</option>
                        )}
                      {branchOptions.map((item) => (
                        <option key={item.value} value={item.value}>
                          {item.value}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      value={form.defaultBranch}
                      onChange={(event) => set("defaultBranch", event.target.value)}
                    />
                  )}
                  <span className="field-hint">
                    {loadingBranches
                      ? "Reading branches from the repository…"
                      : branchError
                        ? `${branchError} Type the branch name instead.`
                        : branchOptions.length > 0
                          ? `${branchOptions.length} branches found.`
                          : "Listed once a repository and credential are set."}
                  </span>
                </label>
                <label className="form-grid__full">
                  Working directory
                  <input
                    value={form.workingDirectory}
                    placeholder="Leave empty unless this is a monorepo"
                    onChange={(event) => set("workingDirectory", event.target.value)}
                  />
                  <span className="field-hint">
                    For a monorepo, the subdirectory this service is built from.
                    Everything is read and run relative to it.
                  </span>
                </label>
              </div>
            </section>

            <section className="form-section">
              <h4>Configuration method</h4>
              <div className="sg-ci-wizard-method">
                <label className={form.method === "hermes" ? "is-selected" : ""}>
                  <input
                    type="radio"
                    name="method"
                    value="hermes"
                    checked={form.method === "hermes"}
                    disabled={!hermesAvailable}
                    onChange={() => set("method", "hermes")}
                  />
                  <span>
                    <strong>Analyze &amp; configure with Hermes</strong>
                    <em>
                      Reads the repository's build files, works out the language,
                      framework and build system, and proposes a complete pipeline.
                      You review it and fill in only what it could not know.
                    </em>
                    {!hermesAvailable && availability && (
                      <em className="sg-ci-wizard-unavailable">
                        Unavailable: {availability.reason}
                      </em>
                    )}
                  </span>
                </label>
                <label className={form.method === "manual" ? "is-selected" : ""}>
                  <input
                    type="radio"
                    name="method"
                    value="manual"
                    checked={form.method === "manual"}
                    onChange={() => set("method", "manual")}
                  />
                  <span>
                    <strong>Configure manually</strong>
                    <em>
                      Pick the application type and edit the pipeline yourself. The
                      full pipeline editor, exactly as before.
                    </em>
                  </span>
                </label>
              </div>

              {form.method === "manual" && (
                <div className="form-grid">
                  <label>
                    Application type
                    <select
                      value={form.applicationType}
                      onChange={(event) => set("applicationType", event.target.value)}
                    >
                      {APPLICATION_TYPES.filter((type) => !type.legacy).map((type) => (
                        <option key={type.value} value={type.value}>
                          {type.label}
                        </option>
                      ))}
                    </select>
                    <span className="field-hint">
                      Sets the starter pipeline and Dockerfile. All editable afterwards.
                    </span>
                  </label>
                </div>
              )}
            </section>
          </div>
        )}

        {step === 1 && service && (
          <div className="sg-ci-wizard-body">
            <p className="sg-ci-wizard-created">
              <strong>{service.name}</strong> is registered. Whatever happens next, it
              stays — you can always configure it by hand.
            </p>
            <HermesAnalysisPanel
              service={service}
              availability={availability}
              canEdit
              autoStart
              onAccepted={() => onOpenService?.(service, "pipeline")}
              onConfigureManually={finishManually}
            />
          </div>
        )}

        <div className="modal-actions">
          {step === 0 ? (
            <>
              <button type="button" className="btn-outline" onClick={onClose} disabled={saving}>
                Cancel
              </button>
              <button
                type="button"
                className="primary"
                onClick={createService}
                disabled={saving || !canContinue}
              >
                {saving
                  ? "Registering…"
                  : form.method === "hermes"
                    ? "Register & analyze"
                    : "Register service"}
              </button>
            </>
          ) : (
            <button type="button" className="btn-outline" onClick={finishManually}>
              Done — open the service
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
