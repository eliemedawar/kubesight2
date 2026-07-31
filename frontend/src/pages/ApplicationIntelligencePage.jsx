import { useCallback, useEffect, useMemo, useState } from "react";

import {
  cancelApplicationAnalysis,
  collectApplicationRuntime,
  compareApplicationAnalyses,
  createBitbucketCredentialProfile,
  createApplicationPullRequest,
  createIntelligenceApplication,
  deleteBitbucketCredentialProfile,
  downloadApplicationArtifact,
  downloadApplicationNetworkPolicy,
  downloadFindingPatch,
  getApplicationAnalysis,
  getApplicationConfiguration,
  getApplicationRuntime,
  getApplicationTopology,
  getIntelligenceApplication,
  listApplicationFindings,
  listApplicationArtifacts,
  listApplicationPullRequests,
  listBitbucketCredentialProfiles,
  listBitbucketRepositoryDockerfiles,
  listBitbucketRepositoryRevisions,
  listIntelligenceApplications,
  requestApplicationAnalysis,
  testHermesConnection,
  updateBitbucketCredentialProfile,
  updateApplicationFinding,
} from "../api/applicationIntelligenceApi";
import { listPickerWorkloads } from "../api/applicationServicesApi";
import { listNamespacesByCluster } from "../api/clustersApi";
import EmptyState from "../components/common/EmptyState";
import LoadingState from "../components/common/LoadingState";
import PageTitle from "../components/common/PageTitle";
import SearchableSelect from "../components/common/SearchableSelect";
import TopologyViewer from "../components/common/TopologyViewer";
import {
  FINDING_STATUSES,
  SEVERITY_ORDER,
  coverageTone,
  formatTimestamp,
  humanizeKey,
  isAnalysisActive,
  normalizeDropdownNames,
  producedResult,
  riskLevelTone,
  severityTone,
  shortCommit,
  sortFindings,
  toDetailRows,
  validateApplicationForm,
} from "../utils/applicationIntelligence";
import "./ApplicationIntelligencePage.css";

const EMPTY_FORM = {
  name: "",
  description: "",
  repositoryWorkspace: "",
  repositoryUrl: "",
  defaultBranch: "main",
  revision: "",
  credentialProfileId: "",
  repositorySubdirectory: "",
  dockerfilePath: "",
  mappedClusterId: "",
  mappedNamespace: "",
  mappedWorkloadKind: "Deployment",
  mappedWorkloadName: "",
  analysisMode: "Quick",
};
const TABS = [
  "Overview",
  "Findings",
  "Architecture",
  "APIs",
  "Configuration",
  "Container & build",
  "Deployment",
  "History",
];
const STATUS_TONE = {
  Completed: "pass",
  Passed: "pass",
  "Completed With Warnings": "warning",
  Failed: "fail",
  Cancelled: "pending",
  Unavailable: "warning",
};
const CREDENTIAL_TYPE_OPTIONS = [
  { value: "api_token", label: "Atlassian API token" },
  { value: "repository_access_token", label: "Repository access token" },
  { value: "project_access_token", label: "Project access token" },
  { value: "workspace_access_token", label: "Workspace access token" },
  { value: "oauth", label: "OAuth token" },
];
const ANALYSIS_MODE_OPTIONS = [
  { value: "Quick", label: "Quick — metadata and lightweight source analysis" },
  { value: "Deep", label: "Deep — full static, topology, security, and Hermes review" },
  {
    value: "Build Verified",
    label: "Build Verified — isolated build, tests, scanners, and Hermes review",
  },
];
const WORKLOAD_KIND_OPTIONS = [
  { value: "Deployment", label: "Deployment" },
  { value: "StatefulSet", label: "StatefulSet" },
  { value: "DaemonSet", label: "DaemonSet" },
  { value: "Pod", label: "Pod" },
];
const FINDING_STATUS_OPTIONS = FINDING_STATUSES.map((value) => ({ value, label: value }));
const HTTP_METHOD_ORDER = ["GET", "POST", "PUT", "PATCH", "DELETE"];

function isCompleteBitbucketRepositoryUrl(value) {
  try {
    const parsed = new URL(String(value || "").trim());
    const parts = parsed.pathname.split("/").filter(Boolean);
    return (
      parsed.protocol === "https:"
      && ["bitbucket.org", "www.bitbucket.org"].includes(parsed.hostname.toLowerCase())
      && parts.length === 2
      && !parsed.username
      && !parsed.password
      && !parsed.search
      && !parsed.hash
    );
  } catch {
    return false;
  }
}

/* ---------------------------------------------------------------- primitives */

function Status({ status, fallback = "Not analyzed" }) {
  return (
    <span className={`status-badge status-badge--${STATUS_TONE[status] || "pending"}`}>
      {status || fallback}
    </span>
  );
}

function RiskBadge({ level }) {
  return (
    <span className={`status-badge status-badge--${riskLevelTone(level)}`}>
      {level === "None" ? "No open findings" : `${level || "Unknown"} risk`}
    </span>
  );
}

function SeverityChip({ severity, count }) {
  if (!count) return null;
  return (
    <span className={`ai-sev ai-sev--${String(severity).toLowerCase()}`} title={`${count} ${severity}`}>
      <b>{count}</b> {severity.slice(0, 4)}
    </span>
  );
}

function SeverityChips({ counts, empty = "None" }) {
  const shown = SEVERITY_ORDER.filter((severity) => counts?.[severity]);
  if (!shown.length) return <span className="muted">{empty}</span>;
  return (
    <span className="ai-sev-row">
      {shown.map((severity) => (
        <SeverityChip key={severity} severity={severity} count={counts[severity]} />
      ))}
    </span>
  );
}

function Section({ title, description, actions, children, className = "" }) {
  return (
    <section className={`card ai-section ${className}`}>
      {title ? (
        <header className="ai-section__head">
          <div>
            <h3>{title}</h3>
            {description ? <p>{description}</p> : null}
          </div>
          {actions ? <div className="ai-inline-actions">{actions}</div> : null}
        </header>
      ) : null}
      {children}
    </section>
  );
}

/** Render one structured value without ever falling back to a JSON dump. */
function DetailValue({ value }) {
  if (Array.isArray(value)) {
    return (
      <ul className="ai-detail-list">
        {value.map((item, index) => (
          <li key={index}>
            {item && typeof item === "object" ? <DetailValue value={item} /> : String(item)}
          </li>
        ))}
      </ul>
    );
  }
  if (value && typeof value === "object") {
    return <DetailGrid source={value} compact />;
  }
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

function DetailGrid({ source, compact = false, empty = "No evidence recorded." }) {
  const rows = toDetailRows(source);
  if (!rows.length) return <p className="muted">{empty}</p>;
  return (
    <dl className={`ai-detail-grid${compact ? " ai-detail-grid--compact" : ""}`}>
      {rows.map((row) => (
        <div key={row.key}>
          <dt>{row.label}</dt>
          <dd><DetailValue value={row.value} /></dd>
        </div>
      ))}
    </dl>
  );
}

function BulletList({ items, tone = "" }) {
  if (!items?.length) return null;
  return (
    <ul className={`ai-bullets ${tone}`}>
      {items.map((item, index) => <li key={index}>{String(item)}</li>)}
    </ul>
  );
}

function Field({ label, error, hint, children }) {
  return (
    <label className="ai-field">
      <span>{label}</span>
      {children}
      {hint && !error ? <small className="ai-field__hint">{hint}</small> : null}
      {error ? <small className="ai-field__error">{error}</small> : null}
    </label>
  );
}

/** Client-side filter box for the long inventory tables. */
function FilterBox({ value, onChange, placeholder, count, noun }) {
  return (
    <div className="ai-filter-box">
      <input value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} />
      <span className="muted">{count} {noun}</span>
    </div>
  );
}

/* ------------------------------------------------------------------- modals */

function CredentialPanel({ credential, onSaved, onCancel }) {
  const editing = Boolean(credential?.id);
  const [form, setForm] = useState({
    name: credential?.name || "",
    credentialType: credential?.credentialType || "api_token",
    principal: credential?.principal || "",
    token: "",
    readOnly: credential?.readOnly !== false,
  });
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const submit = async (event) => {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      const saved = editing
        ? await updateBitbucketCredentialProfile(credential.id, form)
        : await createBitbucketCredentialProfile(form);
      setForm((current) => ({ ...current, token: "" }));
      onSaved(saved);
    } catch (err) {
      setError(err.message || "Credential profile could not be created.");
    } finally {
      setSaving(false);
    }
  };
  return (
    <form className="ai-credential card" onSubmit={submit}>
      <div className="modal-header">
        <div>
          <h3>{editing ? "Edit" : "Add"} Bitbucket credential</h3>
          <p>Tokens are encrypted server-side and never returned to this browser.</p>
        </div>
      </div>
      {error ? <p className="banner-message error">{error}</p> : null}
      <div className="ai-form-grid">
        <Field label="Profile name">
          <input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </Field>
        <Field label="Credential type">
          <SearchableSelect
            value={form.credentialType}
            onChange={(e) => setForm({ ...form, credentialType: e.target.value })}
            options={CREDENTIAL_TYPE_OPTIONS}
            searchThreshold={10}
            aria-label="Credential type"
          />
        </Field>
        <Field
          label="Credential purpose"
          hint="Analysis only ever uses a read-only profile."
        >
          <SearchableSelect
            value={form.readOnly ? "read" : "write"}
            onChange={(e) => setForm({ ...form, readOnly: e.target.value === "read" })}
            options={[
              { value: "read", label: "Read-only analysis" },
              { value: "write", label: "Pull-request creation" },
            ]}
            searchThreshold={10}
            aria-label="Credential purpose"
          />
        </Field>
        <Field
          label={form.credentialType === "api_token" ? "Atlassian account email" : "Principal (optional)"}
        >
          <input
            required={form.credentialType === "api_token"}
            type={form.credentialType === "api_token" ? "email" : "text"}
            autoComplete={form.credentialType === "api_token" ? "email" : "off"}
            placeholder={form.credentialType === "api_token" ? "you@company.com" : ""}
            value={form.principal}
            onChange={(e) => setForm({ ...form, principal: e.target.value })}
          />
        </Field>
        <Field label={editing ? "Token (leave blank to keep current)" : "Token"}>
          <input
            required={!editing}
            type="password"
            autoComplete="new-password"
            value={form.token}
            onChange={(e) => setForm({ ...form, token: e.target.value })}
          />
        </Field>
      </div>
      <div className="modal-actions">
        <button type="button" className="btn-outline" onClick={onCancel}>Cancel</button>
        <button type="submit" className="primary" disabled={saving}>
          {saving ? "Saving…" : "Save credential"}
        </button>
      </div>
    </form>
  );
}

function AnalyzeModal({
  credentials,
  clusters,
  canManage,
  onCredentialRequested,
  onCredentialEditRequested,
  onCredentialDeleted,
  onClose,
  onComplete,
}) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [errors, setErrors] = useState({});
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [namespaces, setNamespaces] = useState([]);
  const [workloads, setWorkloads] = useState([]);
  const [namespacesLoading, setNamespacesLoading] = useState(false);
  const [workloadsLoading, setWorkloadsLoading] = useState(false);
  const [mappingError, setMappingError] = useState("");
  const [revisionOptions, setRevisionOptions] = useState([]);
  const [dockerfileOptions, setDockerfileOptions] = useState([]);
  const [revisionsLoading, setRevisionsLoading] = useState(false);
  const [dockerfilesLoading, setDockerfilesLoading] = useState(false);
  const [repositoryOptionsError, setRepositoryOptionsError] = useState("");
  const [credentialDeleting, setCredentialDeleting] = useState(false);
  const [hermesTest, setHermesTest] = useState(null);
  const [hermesTesting, setHermesTesting] = useState(false);

  const testHermes = async () => {
    setHermesTesting(true);
    setHermesTest(null);
    try {
      const result = await testHermesConnection();
      setHermesTest({
        ok: true,
        message: `Hermes connected (${result.model}, ${result.latencyMs} ms).`,
      });
    } catch (err) {
      setHermesTest({ ok: false, message: err.message || "Hermes connection test failed." });
    } finally {
      setHermesTesting(false);
    }
  };

  const deleteSelectedCredential = async () => {
    const selected = credentials.find((item) => String(item.id) === String(form.credentialProfileId));
    if (!selected) return;
    const confirmed = window.confirm(
      `Delete credential profile "${selected.name}"?\n\n`
      + "Its encrypted token will be permanently removed. Profiles already used "
      + "by an application cannot be deleted.",
    );
    if (!confirmed) return;
    setCredentialDeleting(true);
    setError("");
    try {
      await deleteBitbucketCredentialProfile(selected.id);
      updateCredential("");
      onCredentialDeleted(selected.id);
    } catch (err) {
      setError(err.message || "The credential profile could not be deleted.");
    } finally {
      setCredentialDeleting(false);
    }
  };

  const editSelectedCredential = () => {
    const selected = credentials.find((item) => String(item.id) === String(form.credentialProfileId));
    if (selected) onCredentialEditRequested(selected);
  };

  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const updateRepositoryUrl = (value) => {
    setForm((current) => ({ ...current, repositoryUrl: value, revision: "", dockerfilePath: "" }));
    setRevisionOptions([]);
    setDockerfileOptions([]);
    setRepositoryOptionsError("");
  };
  const updateCredential = (value) => {
    setForm((current) => ({
      ...current,
      credentialProfileId: value,
      revision: "",
      dockerfilePath: "",
    }));
    setRevisionOptions([]);
    setDockerfileOptions([]);
    setRepositoryOptionsError("");
  };
  const updateRevision = (value) => {
    const selectedRevision = revisionOptions.find((item) => item.value === value);
    setForm((current) => ({
      ...current,
      revision: value,
      defaultBranch: selectedRevision?.type === "branch" ? value : current.defaultBranch,
      dockerfilePath: "",
    }));
    setDockerfileOptions([]);
    setRepositoryOptionsError("");
  };
  const updateCluster = (value) => {
    setForm((current) => ({
      ...current,
      mappedClusterId: value,
      mappedNamespace: "",
      mappedWorkloadName: "",
    }));
    setNamespaces([]);
    setWorkloads([]);
    setNamespacesLoading(false);
    setWorkloadsLoading(false);
    setMappingError("");
  };
  const updateNamespace = (value) => {
    setForm((current) => ({ ...current, mappedNamespace: value, mappedWorkloadName: "" }));
    setWorkloads([]);
    setWorkloadsLoading(false);
    setMappingError("");
  };
  const updateWorkloadKind = (value) => {
    setForm((current) => ({ ...current, mappedWorkloadKind: value, mappedWorkloadName: "" }));
    setWorkloads([]);
    setWorkloadsLoading(false);
    setMappingError("");
  };

  useEffect(() => {
    if (!form.mappedClusterId) return undefined;
    let cancelled = false;
    setNamespacesLoading(true);
    setMappingError("");
    listNamespacesByCluster(form.mappedClusterId, { lite: true })
      .then((response) => {
        if (!cancelled) setNamespaces(normalizeDropdownNames(response));
      })
      .catch((err) => {
        if (!cancelled) {
          setNamespaces([]);
          setMappingError(err.message || "Namespaces could not be loaded.");
        }
      })
      .finally(() => {
        if (!cancelled) setNamespacesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [form.mappedClusterId]);

  useEffect(() => {
    if (!form.mappedClusterId || !form.mappedNamespace) return undefined;
    let cancelled = false;
    setWorkloadsLoading(true);
    setMappingError("");
    listPickerWorkloads(
      form.mappedClusterId,
      form.mappedNamespace,
      form.mappedWorkloadKind.toLowerCase(),
    )
      .then((response) => {
        if (!cancelled) setWorkloads(normalizeDropdownNames(response));
      })
      .catch((err) => {
        if (!cancelled) {
          setWorkloads([]);
          setMappingError(err.message || "Workloads could not be loaded.");
        }
      })
      .finally(() => {
        if (!cancelled) setWorkloadsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [form.mappedClusterId, form.mappedNamespace, form.mappedWorkloadKind]);

  useEffect(() => {
    if (!isCompleteBitbucketRepositoryUrl(form.repositoryUrl) || !form.credentialProfileId) {
      setRevisionOptions([]);
      setRevisionsLoading(false);
      return undefined;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      setRevisionsLoading(true);
      setRepositoryOptionsError("");
      listBitbucketRepositoryRevisions({
        repositoryUrl: form.repositoryUrl.trim(),
        credentialProfileId: Number(form.credentialProfileId),
      })
        .then((response) => {
          if (!cancelled) {
            const items = response.items || [];
            setRevisionOptions(items);
            setForm((current) => {
              if (current.revision) return current;
              const preferred = items.find(
                (item) => item.type === "branch" && item.value === "main",
              ) || items.find(
                (item) => item.type === "branch" && item.value === "master",
              ) || items.find((item) => item.type === "branch");
              return preferred
                ? { ...current, revision: preferred.value, defaultBranch: preferred.value }
                : current;
            });
          }
        })
        .catch((err) => {
          if (!cancelled) {
            setRevisionOptions([]);
            setRepositoryOptionsError(
              err.message || "Branches, tags, and commits could not be loaded.",
            );
          }
        })
        .finally(() => {
          if (!cancelled) setRevisionsLoading(false);
        });
    }, 450);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [form.repositoryUrl, form.credentialProfileId]);

  useEffect(() => {
    if (
      !isCompleteBitbucketRepositoryUrl(form.repositoryUrl)
      || !form.credentialProfileId
      || !form.revision.trim()
    ) {
      setDockerfileOptions([]);
      setDockerfilesLoading(false);
      return undefined;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      setDockerfilesLoading(true);
      setRepositoryOptionsError("");
      listBitbucketRepositoryDockerfiles({
        repositoryUrl: form.repositoryUrl.trim(),
        credentialProfileId: Number(form.credentialProfileId),
        revision: form.revision.trim(),
      })
        .then((response) => {
          if (!cancelled) setDockerfileOptions(response.items || []);
        })
        .catch((err) => {
          if (!cancelled) {
            setDockerfileOptions([]);
            setRepositoryOptionsError(err.message || "Dockerfile paths could not be loaded.");
          }
        })
        .finally(() => {
          if (!cancelled) setDockerfilesLoading(false);
        });
    }, 450);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [form.repositoryUrl, form.credentialProfileId, form.revision]);

  const credentialOptions = [
    { value: "", label: "Select read-only profile…" },
    ...credentials.map((item) => ({ value: item.id, label: item.name })),
  ];
  const clusterOptions = [
    { value: "", label: "No cluster mapping" },
    ...(clusters || []).map((cluster) => ({
      value: cluster.id,
      label: cluster.name || cluster.id,
    })),
  ];
  const namespacePlaceholder = !form.mappedClusterId
    ? "Select a cluster first"
    : namespacesLoading
      ? "Loading namespaces…"
      : namespaces.length
        ? "Select namespace…"
        : "No namespaces available";
  const namespaceOptions = [
    { value: "", label: namespacePlaceholder },
    ...namespaces.map((name) => ({ value: name, label: name })),
  ];
  const workloadPlaceholder = !form.mappedNamespace
    ? "Select a namespace first"
    : workloadsLoading
      ? `Loading ${form.mappedWorkloadKind.toLowerCase()}s…`
      : workloads.length
        ? `Select ${form.mappedWorkloadKind.toLowerCase()}…`
        : `No ${form.mappedWorkloadKind.toLowerCase()}s available`;
  const workloadOptions = [
    { value: "", label: workloadPlaceholder },
    ...workloads.map((name) => ({ value: name, label: name })),
  ];
  const repositoryMetadataReady = (
    isCompleteBitbucketRepositoryUrl(form.repositoryUrl) && Boolean(form.credentialProfileId)
  );
  const revisionPlaceholder = !repositoryMetadataReady
    ? "Select repository and credential first"
    : revisionsLoading
      ? "Loading branches, tags, and commits…"
      : revisionOptions.length
        ? "Select branch, tag, or commit…"
        : "Search or enter a revision";
  const revisionSelectOptions = [
    { value: "", label: revisionPlaceholder },
    ...revisionOptions,
  ];
  const dockerfilePlaceholder = !repositoryMetadataReady || !form.revision
    ? "Select a revision first"
    : dockerfilesLoading
      ? "Loading Dockerfiles…"
      : dockerfileOptions.length
        ? "Auto-detect or select a Dockerfile…"
        : "Auto-detect or enter a path";
  const dockerfileSelectOptions = [
    { value: "", label: dockerfilePlaceholder },
    ...dockerfileOptions,
  ];

  const submit = async (event) => {
    event.preventDefault();
    const nextErrors = validateApplicationForm(form);
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length) return;
    setSaving(true);
    setError("");
    try {
      const application = await createIntelligenceApplication({
        ...form,
        credentialProfileId: Number(form.credentialProfileId),
      });
      const analysis = await requestApplicationAnalysis(application.id, {
        revision: form.revision || form.defaultBranch,
        analysisMode: form.analysisMode,
      });
      onComplete(application, analysis);
    } catch (err) {
      setError(err.message || "The analysis could not be queued.");
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-label="Analyze application">
      <form className="modal-panel ai-modal" onSubmit={submit}>
        <div className="modal-header">
          <div>
            <h3>Analyze application</h3>
            <p>Checkout and scanners run in an isolated, non-privileged job. Repository content is treated as untrusted data.</p>
          </div>
          <button type="button" className="modal-close" aria-label="Close" onClick={onClose}>×</button>
        </div>
        {error ? <p className="banner-message error">{error}</p> : null}

        <fieldset className="ai-fieldset">
          <legend>Repository</legend>
          <div className="ai-form-grid">
            <Field label="Microservice name" error={errors.name}>
              <input value={form.name} onChange={(e) => update("name", e.target.value)} />
            </Field>
            <Field label="Bitbucket workspace or project (optional)">
              <input value={form.repositoryWorkspace} onChange={(e) => update("repositoryWorkspace", e.target.value)} />
            </Field>
            <Field label="Repository URL" error={errors.repositoryUrl}>
              <input
                placeholder="https://bitbucket.org/workspace/repository"
                value={form.repositoryUrl}
                onChange={(e) => updateRepositoryUrl(e.target.value)}
              />
            </Field>
            <Field label="Bitbucket credential profile" error={errors.credentialProfileId}>
              <div className="ai-field-control">
                <SearchableSelect
                  value={form.credentialProfileId}
                  onChange={(e) => updateCredential(e.target.value)}
                  options={credentialOptions}
                  disabled={credentialDeleting}
                  searchPlaceholder="Search credential profiles…"
                  aria-label="Bitbucket credential profile"
                />
                {canManage ? (
                  <button
                    type="button"
                    className="btn-outline"
                    onClick={editSelectedCredential}
                    disabled={!form.credentialProfileId || credentialDeleting}
                  >
                    Edit
                  </button>
                ) : null}
                {canManage ? (
                  <button
                    type="button"
                    className="btn-outline ai-delete-profile"
                    onClick={deleteSelectedCredential}
                    disabled={!form.credentialProfileId || credentialDeleting}
                  >
                    {credentialDeleting ? "Deleting…" : "Delete"}
                  </button>
                ) : null}
              </div>
            </Field>
            <Field label="Branch, tag, or commit">
              <SearchableSelect
                value={form.revision}
                onChange={(e) => updateRevision(e.target.value)}
                options={revisionSelectOptions}
                disabled={!repositoryMetadataReady || revisionsLoading}
                allowCustom
                customOptionLabel={(value) => `Use custom revision "${value}"`}
                searchPlaceholder="Search branches, tags, commits, or enter a ref…"
                aria-label="Branch, tag, or commit"
              />
            </Field>
            <Field label="Monorepo subdirectory (optional)" error={errors.repositorySubdirectory}>
              <input value={form.repositorySubdirectory} onChange={(e) => update("repositorySubdirectory", e.target.value)} />
            </Field>
          </div>
        </fieldset>

        <fieldset className="ai-fieldset">
          <legend>Analysis</legend>
          <div className="ai-form-grid">
            <Field label="Analysis mode">
              <SearchableSelect
                value={form.analysisMode}
                onChange={(e) => update("analysisMode", e.target.value)}
                options={ANALYSIS_MODE_OPTIONS}
                searchThreshold={10}
                aria-label="Analysis mode"
              />
            </Field>
            <Field label="Dockerfile path (optional)" error={errors.dockerfilePath}>
              <SearchableSelect
                value={form.dockerfilePath}
                onChange={(e) => update("dockerfilePath", e.target.value)}
                options={dockerfileSelectOptions}
                disabled={!repositoryMetadataReady || !form.revision || dockerfilesLoading}
                allowCustom
                customOptionLabel={(value) => `Use custom path "${value}"`}
                searchPlaceholder="Search Dockerfiles or enter a path…"
                aria-label="Dockerfile path"
              />
            </Field>
            <Field label="Description (optional)">
              <textarea rows="2" value={form.description} onChange={(e) => update("description", e.target.value)} />
            </Field>
          </div>
        </fieldset>

        <fieldset className="ai-fieldset">
          <legend>Runtime mapping (optional)</legend>
          <p className="ai-fieldset__note">
            Required only to compare source against a live workload later. Leaving it empty never affects source analysis.
          </p>
          <div className="ai-form-grid">
            <Field label="Kubernetes cluster">
              <SearchableSelect
                value={form.mappedClusterId}
                onChange={(e) => updateCluster(e.target.value)}
                options={clusterOptions}
                searchPlaceholder="Search clusters…"
                aria-label="Kubernetes cluster mapping"
              />
            </Field>
            <Field label="Namespace">
              <SearchableSelect
                value={form.mappedNamespace}
                onChange={(e) => updateNamespace(e.target.value)}
                options={namespaceOptions}
                disabled={!form.mappedClusterId || namespacesLoading || !namespaces.length}
                searchPlaceholder="Search namespaces…"
                aria-label="Namespace"
              />
            </Field>
            <Field label="Workload kind">
              <SearchableSelect
                value={form.mappedWorkloadKind}
                onChange={(e) => updateWorkloadKind(e.target.value)}
                options={WORKLOAD_KIND_OPTIONS}
                disabled={!form.mappedClusterId}
                searchThreshold={10}
                aria-label="Workload kind"
              />
            </Field>
            <Field label={`${form.mappedWorkloadKind} name`}>
              <SearchableSelect
                value={form.mappedWorkloadName}
                onChange={(e) => update("mappedWorkloadName", e.target.value)}
                options={workloadOptions}
                disabled={!form.mappedNamespace || workloadsLoading || !workloads.length}
                searchPlaceholder={`Search ${form.mappedWorkloadKind.toLowerCase()}s…`}
                aria-label={`${form.mappedWorkloadKind} name`}
              />
            </Field>
          </div>
        </fieldset>

        {repositoryOptionsError ? <p className="ai-mapping-error">{repositoryOptionsError}</p> : null}
        {mappingError ? <p className="ai-mapping-error">{mappingError}</p> : null}
        {hermesTest ? (
          <p className={`banner-message ${hermesTest.ok ? "" : "error"}`}>{hermesTest.message}</p>
        ) : null}
        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={testHermes} disabled={hermesTesting}>
            {hermesTesting ? "Testing Hermes…" : "Test Hermes"}
          </button>
          {canManage ? <button type="button" className="btn-outline" onClick={onCredentialRequested}>Add credential</button> : null}
          <button type="button" className="btn-outline" onClick={onClose}>Cancel</button>
          <button type="submit" className="primary" disabled={saving || !credentials.length}>
            {saving ? "Queuing…" : "Create and analyze"}
          </button>
        </div>
      </form>
    </div>
  );
}

/* ------------------------------------------------------------------ findings */

function FindingCard({ finding, canManage, onStatusChanged, selected, onSelected }) {
  const [status, setStatus] = useState(finding.status);
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  useEffect(() => setStatus(finding.status), [finding.status]);
  const saveStatus = async () => {
    setSaving(true);
    setError("");
    try {
      const updated = await updateApplicationFinding(finding.id, { status, reason });
      onStatusChanged(updated);
      setReason("");
    } catch (err) {
      setError(err.message || "Finding status could not be updated.");
    } finally {
      setSaving(false);
    }
  };
  const location = [
    finding.filePath,
    finding.startLine ? `line ${finding.startLine}` : null,
  ].filter(Boolean).join(" · ");
  const latestChange = finding.statusHistory?.at(-1);
  return (
    <article className={`card ai-finding ai-finding--${String(finding.severity).toLowerCase()}`}>
      <button type="button" className="ai-finding__head" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
        <span className={`ai-sev ai-sev--${String(finding.severity).toLowerCase()}`}>{finding.severity}</span>
        <span className="ai-finding__title">{finding.title}</span>
        <span className="ai-finding__meta">
          {finding.category} · {finding.confidence} confidence · {finding.scannerSource}
        </span>
        <span className={`status-badge status-badge--${finding.status === "Open" ? "pending" : "pass"}`}>
          {finding.status}
        </span>
        <span className="ai-chevron" aria-hidden="true">{open ? "▾" : "▸"}</span>
      </button>
      <p className="ai-finding__description">{finding.description}</p>
      {location ? <p className="ai-finding__location"><code>{location}</code></p> : null}
      {open ? (
        <div className="ai-finding__body">
          {finding.evidence ? (
            <div className="ai-evidence-note">
              <strong>Evidence</strong>
              <p>{finding.evidence}</p>
            </div>
          ) : (
            <div className="ai-evidence-note ai-evidence-note--missing">
              <strong>Evidence</strong>
              <p>No source observation was recorded for this finding. Treat it as a lead, not a confirmed defect.</p>
            </div>
          )}
          {finding.impact ? (
            <div><strong>Impact</strong><p>{finding.impact}</p></div>
          ) : null}
          {finding.recommendation ? (
            <div><strong>Recommendation</strong><p>{finding.recommendation}</p></div>
          ) : null}
          {finding.cwe || finding.cve ? (
            <p className="ai-finding__refs">
              {finding.cwe ? <code>{finding.cwe}</code> : null}
              {finding.cve ? <code>{finding.cve}</code> : null}
            </p>
          ) : null}
          {latestChange ? (
            <small className="muted">
              Last workflow change: {latestChange.previousStatus} → {latestChange.status} by {latestChange.changedBy}
              {latestChange.reason ? ` — ${latestChange.reason}` : ""}
            </small>
          ) : null}
        </div>
      ) : null}
      {finding.hasSuggestedPatch ? (
        <div className="ai-inline-actions">
          <button type="button" className="btn-outline" onClick={() => downloadFindingPatch(finding)}>
            Download suggested patch
          </button>
          {canManage ? (
            <label className="ai-checkbox">
              <input type="checkbox" checked={selected} onChange={(event) => onSelected(event.target.checked)} />
              Include in pull request
            </label>
          ) : null}
        </div>
      ) : null}
      {canManage ? (
        <div className="ai-finding-workflow">
          <SearchableSelect
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            options={FINDING_STATUS_OPTIONS}
            searchThreshold={10}
            aria-label={`Status for ${finding.title}`}
          />
          {["Risk Accepted", "False Positive"].includes(status) ? (
            <input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Required reason" />
          ) : null}
          <button type="button" className="btn-outline" disabled={saving || status === finding.status} onClick={saveStatus}>
            {saving ? "Saving…" : "Update status"}
          </button>
        </div>
      ) : null}
      {error ? <p className="ai-field__error">{error}</p> : null}
    </article>
  );
}

function Findings({
  items,
  filters,
  onFilters,
  canManage,
  credentials,
  analysis,
  pullRequests,
  onStatusChanged,
  onPullRequestCreated,
}) {
  const [selectedIds, setSelectedIds] = useState([]);
  const [writeCredentialId, setWriteCredentialId] = useState("");
  const [branchName, setBranchName] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const writeCredentials = credentials.filter((item) => item.enabled && !item.readOnly);
  const ordered = useMemo(() => sortFindings(items), [items]);
  const coverage = analysis?.evidenceCoverage;
  const createPullRequest = async () => {
    setCreating(true);
    setError("");
    try {
      const created = await createApplicationPullRequest(analysis.id, {
        findingIds: selectedIds,
        credentialProfileId: writeCredentialId,
        branchName,
        title,
        description,
      });
      onPullRequestCreated(created);
      setSelectedIds([]);
    } catch (err) {
      setError(err.message || "Pull request could not be requested.");
    } finally {
      setCreating(false);
    }
  };
  return (
    <section className="ai-stack">
      {coverage?.unavailable?.length ? (
        <p className="ai-caveat">
          <strong>Partial coverage.</strong> {coverage.unavailable.join(", ")}
          {coverage.unavailable.length === 1 ? " did" : " did"} not run, so this list contains no
          {" "}deterministic scanner results from {coverage.unavailable.length === 1 ? "it" : "them"}.
        </p>
      ) : null}
      <div className="ai-filters">
        {["severity", "confidence", "status"].map((key) => (
          <SearchableSelect
            key={key}
            value={filters[key]}
            onChange={(e) => onFilters({ ...filters, [key]: e.target.value })}
            options={[
              { value: "", label: `All ${key}` },
              ...(key === "severity"
                ? SEVERITY_ORDER
                : key === "confidence"
                  ? ["Confirmed", "High", "Medium", "Low", "Informational"]
                  : FINDING_STATUSES
              ).map((value) => ({ value, label: value })),
            ]}
            searchThreshold={10}
            aria-label={`Filter by ${key}`}
          />
        ))}
        <input placeholder="File path" value={filters.file} onChange={(e) => onFilters({ ...filters, file: e.target.value })} />
      </div>
      {!ordered.length ? <EmptyState title="No findings match these filters" /> : (
        <div className="ai-finding-list">
          {ordered.map((finding) => (
            <FindingCard
              key={finding.id}
              finding={finding}
              canManage={canManage}
              onStatusChanged={onStatusChanged}
              selected={selectedIds.includes(finding.id)}
              onSelected={(checked) => setSelectedIds((current) => (
                checked
                  ? [...new Set([...current, finding.id])]
                  : current.filter((id) => id !== finding.id)
              ))}
            />
          ))}
        </div>
      )}
      {canManage && selectedIds.length ? (
        <Section
          title="Create guarded Bitbucket pull request"
          description="A separate write-enabled credential is used only after isolated patch and build validation. KubeSight never pushes to the default branch."
        >
          <div className="ai-form-grid">
            <Field label="Write-enabled credential">
              <SearchableSelect
                value={writeCredentialId}
                onChange={(event) => setWriteCredentialId(event.target.value)}
                options={[
                  { value: "", label: "Select pull-request credential…" },
                  ...writeCredentials.map((item) => ({ value: item.id, label: item.name })),
                ]}
                searchThreshold={10}
              />
            </Field>
            <Field label="Source branch (optional)">
              <input value={branchName} onChange={(event) => setBranchName(event.target.value)} placeholder={`kubesight/analysis-${analysis.id}-…`} />
            </Field>
            <Field label="Pull-request title (optional)">
              <input value={title} onChange={(event) => setTitle(event.target.value)} />
            </Field>
            <Field label="Description (optional)">
              <textarea rows="2" value={description} onChange={(event) => setDescription(event.target.value)} />
            </Field>
          </div>
          {!writeCredentials.length ? <p className="banner-message">Add a credential with purpose “Pull-request creation” first.</p> : null}
          {error ? <p className="banner-message error">{error}</p> : null}
          <button type="button" className="primary" disabled={creating || !writeCredentialId} onClick={createPullRequest}>
            {creating ? "Requesting…" : `Validate and create PR (${selectedIds.length})`}
          </button>
        </Section>
      ) : null}
      {pullRequests.length ? (
        <Section title="Pull-request workflow">
          <div className="ai-table-wrap">
            <table className="data-table">
              <thead><tr><th>Status</th><th>Branch</th><th>Validation</th><th /></tr></thead>
              <tbody>
                {pullRequests.map((item) => (
                  <tr key={item.id}>
                    <td><Status status={item.status} /></td>
                    <td><code>{item.branchName}</code></td>
                    <td>
                      {item.validationSummary?.status || "Validation pending"}
                      {item.safeErrorMessage ? <small className="ai-field__error">{item.safeErrorMessage}</small> : null}
                    </td>
                    <td>
                      {item.providerUrl
                        ? <a href={item.providerUrl} target="_blank" rel="noreferrer">Open in Bitbucket</a>
                        : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      ) : null}
    </section>
  );
}

/* -------------------------------------------------------------- architecture */

function RuntimeBadge({ value }) {
  const normalized = String(value || "").toLowerCase();
  const tone = ["pass", "matched", "completed"].includes(normalized)
    ? "pass"
    : ["fail", "missing", "unexpected"].includes(normalized)
      ? "fail"
      : "warning";
  return <span className={`status-badge status-badge--${tone}`}>{value || "Unknown"}</span>;
}

/**
 * Wire label for one communication edge.
 *
 * Ports are only ever shown when the analysis actually recorded one — a
 * protocol's conventional default is never filled in, because a guessed port
 * on a diagram reads exactly like an observed one.
 */
function edgeWireLabel(protocol, port) {
  const wire = String(protocol || "").trim();
  if (wire && port) return `${wire.toUpperCase()} :${port}`;
  if (port) return `:${port}`;
  return wire ? wire.toUpperCase() : "";
}

/** Adapt source-topology rows to the shared TopologyViewer node/edge shape. */
function toSourceGraph(topology) {
  const nodes = (topology?.nodes || []).map((node, index) => ({
    id: node.id,
    name: node.id,
    type: node.type || "",
    kind: index === 0 ? "application" : "dependency",
  }));
  const known = new Set(nodes.map((node) => node.name));
  const edges = (topology?.edges || [])
    .filter((edge) => known.has(edge.source) && known.has(edge.destination))
    .map((edge) => ({
      id: edge.id,
      sourceNodeId: edge.source,
      targetNodeId: edge.destination,
      protocol: edgeWireLabel(edge.protocol, edge.port) || edge.destinationType || "",
      description: [edge.confidence, edge.required ? "required" : "optional"]
        .filter(Boolean).join(" · "),
    }));
  return { nodes, edges };
}

/** Adapt the runtime snapshot topology to the same viewer shape. */
function toRuntimeGraph(topology) {
  const nodes = (topology?.nodes || []).map((node) => ({
    id: node.id,
    name: node.label || node.id,
    type: node.type || "",
  }));
  const known = new Set(nodes.map((node) => String(node.id)));
  const edges = (topology?.edges || [])
    .filter((edge) => known.has(String(edge.source)) && known.has(String(edge.destination)))
    .map((edge) => ({
      id: edge.id,
      sourceNodeId: edge.source,
      targetNodeId: edge.destination,
      protocol: edgeWireLabel(edge.protocol, edge.port) || edge.relation || "",
      description: [edge.evidenceState, edge.confidence].filter(Boolean).join(" · "),
    }));
  return { nodes, edges };
}

/**
 * Why a wire fact is absent matters as much as the fact itself. An address
 * supplied by a property at deploy time is a different situation from one that
 * was never found, so name the property when we know it.
 */
function AbsentValue({ configurationKey }) {
  if (configurationKey) {
    // The property name is already spelled out in the endpoint column, so keep
    // the cell scannable and put the full key in the tooltip.
    return (
      <span className="ai-externalized" title={`Supplied at runtime by ${configurationKey}`}>
        externalized
      </span>
    );
  }
  return <span className="muted">not stated</span>;
}

function CommunicationsTable({ edges, onSelect, selectedId }) {
  const missingPorts = edges.filter((edge) => !edge.port);
  const externalized = missingPorts.filter((edge) => edge.configurationKey).length;
  return (
    <>
      <div className="ai-table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>Destination</th><th>Type</th><th>Protocol</th><th>Port</th>
              <th>Direction</th><th>Endpoint</th><th>Confidence</th>
            </tr>
          </thead>
          <tbody>
            {edges.map((edge) => (
              <tr
                key={edge.id}
                className={edge.id === selectedId ? "is-current" : ""}
                onClick={() => onSelect(edge.id)}
                style={{ cursor: "pointer" }}
              >
                <td><strong>{edge.destination}</strong></td>
                <td>{edge.destinationType || "—"}</td>
                <td>
                  {edge.protocol
                    ? edge.protocol.toUpperCase()
                    : <AbsentValue configurationKey={edge.configurationKey} />}
                </td>
                <td>
                  {edge.port
                    ? <code>{edge.port}</code>
                    : <AbsentValue configurationKey={edge.configurationKey} />}
                </td>
                <td>{edge.direction || "Outbound"}</td>
                <td className="ai-cell-path">{edge.endpoint ? <code>{edge.endpoint}</code> : "—"}</td>
                <td>{edge.confidence}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {missingPorts.length ? (
        <p className="ai-trust-note">
          {missingPorts.length} of {edges.length} dependencies record no port.
          {externalized
            ? ` ${externalized} resolve their address from a property at deploy time — the value lives in the config server or environment, not the repository.`
            : ""}
          {" "}Ports appear only where a literal value was found in the repository; a protocol's default
          port is never assumed.
        </p>
      ) : null}
    </>
  );
}

function Architecture({ summary, topology, runtime }) {
  const [selectedId, setSelectedId] = useState(null);
  const edges = topology?.edges || [];
  const selected = edges.find((edge) => edge.id === selectedId) || null;
  const sourceGraph = useMemo(() => toSourceGraph(topology), [topology]);
  const runtimeGraph = useMemo(() => toRuntimeGraph(runtime?.topology), [runtime?.topology]);
  const selectByNode = (node) => {
    const edge = edges.find((item) => item.destination === node.name || item.destination === node.id);
    setSelectedId(edge?.id ?? null);
  };
  return (
    <div className="ai-stack">
      {summary && Object.keys(summary).length ? (
        <Section title="Architecture summary" description="Hermes narrative, derived from the supplied source evidence.">
          <DetailGrid source={summary} />
        </Section>
      ) : null}

      <Section
        title="Source-inferred communications"
        description="Every edge is backed by a source observation. Select a node or row to read it."
      >
        {!edges.length ? <EmptyState title="No outbound dependencies were inferred" /> : (
          <>
            <div className="ai-graph">
              <div className="ai-graph__canvas">
                <TopologyViewer
                  nodes={sourceGraph.nodes}
                  edges={sourceGraph.edges}
                  fillWidth
                  zoomable
                  // One service fanning out to N dependencies: a column per
                  // rank keeps every edge in the gutter. The default vertical
                  // layout wraps past five peers, dropping the sixth under a
                  // sibling and routing its edge through the row above.
                  layoutDirection="horizontal"
                  onNodeClick={selectByNode}
                  nodeClickable={(node) => node.kind !== "application"}
                  emptyMessage="No communications were inferred from source."
                />
              </div>
              <aside className="ai-evidence">
                <h4>Edge evidence</h4>
                {selected ? (
                  <>
                    <strong>{selected.sourceLabel || selected.source} → {selected.destination}</strong>
                    <p>{selected.evidence || "No source snippet was stored for this edge."}</p>
                    <dl className="ai-detail-grid ai-detail-grid--compact">
                      <div><dt>Type</dt><dd>{selected.destinationType || "Unknown"}</dd></div>
                      <div>
                        <dt>Protocol</dt>
                        <dd>{selected.protocol || <AbsentValue configurationKey={selected.configurationKey} />}</dd>
                      </div>
                      <div>
                        <dt>Port</dt>
                        <dd>{selected.port || <AbsentValue configurationKey={selected.configurationKey} />}</dd>
                      </div>
                      {selected.configurationKey ? (
                        <div>
                          <dt>Address from</dt>
                          <dd><code>{selected.configurationKey}</code></dd>
                        </div>
                      ) : null}
                      <div><dt>Confidence</dt><dd>{selected.confidence}</dd></div>
                      <div><dt>Evidence state</dt><dd>{selected.evidenceState}</dd></div>
                      <div><dt>Required</dt><dd>{selected.required ? "Yes" : "No"}</dd></div>
                      {selected.filePath ? (
                        <div><dt>Location</dt><dd><code>{selected.filePath}{selected.lineNumber ? `:${selected.lineNumber}` : ""}</code></dd></div>
                      ) : null}
                    </dl>
                  </>
                ) : <p className="muted">Select a dependency to inspect its redacted evidence.</p>}
              </aside>
            </div>
            <CommunicationsTable edges={edges} onSelect={setSelectedId} selectedId={selectedId} />
          </>
        )}
      </Section>

      <Section
        title="Runtime-observed topology"
        description="Read-only evidence from the mapped workload, Services, and Ingresses. Ports here are observed, not inferred."
        actions={<RuntimeBadge value={runtime?.status || "Not Collected"} />}
      >
        {runtime?.status === "Completed" && runtimeGraph.nodes.length ? (
          <div className="ai-graph__canvas">
            <TopologyViewer
              nodes={runtimeGraph.nodes}
              edges={runtimeGraph.edges}
              fillWidth
              zoomable
              layoutDirection="horizontal"
              emptyMessage="The runtime snapshot recorded no topology."
            />
          </div>
        ) : (
          <p className="muted">
            Collect runtime evidence from the Deployment tab to populate this view. Source edges stay
            labelled “Source Inferred” until then.
          </p>
        )}
      </Section>
    </div>
  );
}

/* ------------------------------------------------------------------ overview */

function Overview({ analysis, application, artifacts }) {
  const result = analysis?.result || {};
  const profile = { ...(result.application_profile || {}) };
  const build = profile.build_verification;
  delete profile.build_verification;
  const risk = result.risk_summary || {};
  // Only KubeSight's own build stage may speak to build outcome. Analyses
  // recorded before that rule was enforced can still carry the model's claim.
  const { build_verified: _ignoredBuildClaim, ...operational } = result.operational_readiness || {};
  const posture = analysis?.posture;
  const coverage = analysis?.evidenceCoverage;
  const sourceCoverage = analysis?.sourceCoverage;
  const limitations = result.limitations || [];
  const warnings = analysis?.warnings || [];
  const assessed = producedResult(analysis?.status);
  return (
    <div className="ai-stack">
      {!assessed ? (
        <p className="ai-caveat">
          <strong>This run did not complete.</strong> It produced no posture, findings, or evidence —
          the absence of results here says nothing about the repository.
        </p>
      ) : null}
      <div className="ai-verdict">
        <article className="ai-verdict__card">
          <span>Risk level</span>
          {assessed
            ? <RiskBadge level={posture?.riskLevel} />
            : <span className="status-badge status-badge--pending">Not assessed</span>}
          <small>Derived from open findings, not from a model score.</small>
        </article>
        <article className="ai-verdict__card">
          <span>Open findings</span>
          {assessed
            ? <SeverityChips counts={posture?.openBySeverity} empty="None open" />
            : <span className="muted">—</span>}
          <small>{assessed ? `${posture?.total || 0} recorded in total` : "Analysis stopped before findings were recorded."}</small>
        </article>
        <article className="ai-verdict__card">
          <span>Scanner coverage</span>
          {assessed
            ? (
              <span className={`status-badge status-badge--${coverageTone(coverage?.label)}`}>
                {coverage?.label || "Unknown"}
              </span>
            )
            : <span className="muted">—</span>}
          <small>
            {!assessed
              ? "No scanners reported."
              : coverage?.available?.length
                ? `Ran: ${coverage.available.join(", ")}`
                : "No deterministic scanner produced evidence."}
          </small>
        </article>
        <article className="ai-verdict__card">
          <span>Source reviewed</span>
          {assessed && sourceCoverage
            ? (
              <span className={`status-badge status-badge--${
                sourceCoverage.reviewedPercent >= 80
                  ? "pass"
                  : sourceCoverage.reviewedPercent >= 40 ? "warning" : "fail"
              }`}>
                {sourceCoverage.reviewedPercent}% of files
              </span>
            )
            : <span className="muted">—</span>}
          <small>
            {assessed && sourceCoverage
              ? `Hermes read ${sourceCoverage.selectedFiles} of ${sourceCoverage.eligibleFiles} analyzable files (${analysis?.analysisMode} caps the slice at ${sourceCoverage.fileLimit}).`
              : "Not recorded for this run."}
          </small>
        </article>
        <article className="ai-verdict__card">
          <span>Build verification</span>
          {analysis?.buildVerificationStatus
            ? <Status status={analysis.buildVerificationStatus} />
            : <span className="status-badge status-badge--pending">Not run</span>}
          <small>
            {analysis?.analysisMode === "Build Verified"
              ? "Credential-free isolated build and test stage."
              : "Run the Build Verified mode to execute build and tests."}
          </small>
        </article>
      </div>

      {assessed && sourceCoverage && sourceCoverage.reviewedPercent < 100 ? (
        <p className="ai-caveat">
          <strong>Hermes read part of the repository, not all of it.</strong> {analysis.analysisMode} mode
          sends at most {sourceCoverage.fileLimit} files, so {sourceCoverage.selectedFiles} of{" "}
          {sourceCoverage.eligibleFiles} analyzable files were reviewed
          {sourceCoverage.truncatedFiles ? `, ${sourceCoverage.truncatedFiles} of them truncated` : ""}.
          Configuration, manifests, entrypoints, clients, and controllers are prioritized, but anything
          outside that slice is unexamined — its absence from the findings means nothing.
          {analysis.analysisMode === "Quick" ? " Deep mode triples the slice." : ""}
        </p>
      ) : null}
      {assessed && coverage?.unavailable?.length ? (
        <p className="ai-caveat">
          <strong>Read this result as incomplete.</strong> {coverage.unavailable.join(", ")} did not run in
          the analysis image, so dependency, CVE, and container-lint evidence is absent rather than clean.
        </p>
      ) : null}

      <div className="ai-two-col">
        <Section title="Service profile" description="What the repository is, from source evidence.">
          <DetailGrid source={profile} />
        </Section>
        <Section title="Risk summary" description="Hermes assessment, shown separately from the counted posture above.">
          {risk.overall_risk ? (
            <p className="ai-inline-fact"><span>Hermes overall risk</span><strong>{risk.overall_risk}</strong></p>
          ) : null}
          {risk.primary_risks?.length ? (
            <>
              <h4>Primary risks</h4>
              <BulletList items={risk.primary_risks} tone="ai-bullets--risk" />
            </>
          ) : null}
          {risk.positive_controls?.length ? (
            <>
              <h4>Positive controls</h4>
              <BulletList items={risk.positive_controls} tone="ai-bullets--ok" />
            </>
          ) : null}
          {!risk.overall_risk && !risk.primary_risks?.length ? <p className="muted">No risk summary was returned.</p> : null}
        </Section>
      </div>

      {operational.readiness_gaps?.length || toDetailRows(operational).length ? (
        <Section title="Operational readiness" description="Source-side signals only. Runtime state lives in the Deployment tab.">
          <DetailGrid source={operational} />
        </Section>
      ) : null}

      {build ? (
        <Section title="Build verification" description="KubeSight's own credential-free build stage. This is the authoritative build result.">
          <div className="ai-inline-facts">
            <p className="ai-inline-fact"><span>Status</span><Status status={build.status} /></p>
            <p className="ai-inline-fact"><span>Network</span><strong>{build.networkPolicy || "Unknown"}</strong></p>
            <p className="ai-inline-fact"><span>Credentials exposed</span><strong>{build.credentialExposure || "None"}</strong></p>
          </div>
          {(build.commands || []).map((command, index) => (
            <div key={index} className="ai-command">
              <div className="ai-command__head">
                <Status status={command.status} />
                <strong>{command.label || "Command"}</strong>
                <code>{(command.command || []).join(" ")}</code>
                {command.exitCode != null ? <small>exit {command.exitCode}</small> : null}
              </div>
              {command.output ? <pre>{command.output}</pre> : null}
            </div>
          ))}
        </Section>
      ) : null}

      <Section
        title="Scope and limitations"
        description="What this analysis did not or could not establish."
      >
        {warnings.length ? (
          <>
            <h4>Warnings from this run</h4>
            <BulletList items={warnings} tone="ai-bullets--risk" />
          </>
        ) : null}
        {limitations.length ? (
          <>
            <h4>Declared limitations</h4>
            <BulletList items={limitations} />
          </>
        ) : null}
        {!warnings.length && !limitations.length ? <p className="muted">No warnings or limitations were recorded.</p> : null}
        <p className="ai-provenance">
          Requested by <strong>{analysis?.requestedBy || "—"}</strong> · executed by{" "}
          <strong>{analysis?.executedBy || "hermes-agent"}</strong> · mode {analysis?.analysisMode || "—"} ·
          model {analysis?.hermesModel || "—"} ({analysis?.hermesPromptVersion || "unknown prompt"}) ·
          workspace cleanup {analysis?.workspaceCleanupStatus || "Unknown"}
        </p>
      </Section>

      {artifacts.length ? (
        <Section title="Reviewable artifacts" description="Downloads only. Nothing here is applied to the repository or a cluster.">
          <div className="ai-artifacts">
            {artifacts.map((artifact) => (
              <button key={artifact.id} type="button" className="btn-outline" onClick={() => downloadApplicationArtifact(artifact)}>
                {artifact.artifactType}
                <small>{artifact.filename}</small>
              </button>
            ))}
          </div>
        </Section>
      ) : null}

      {application?.description ? (
        <Section title="Description"><p>{application.description}</p></Section>
      ) : null}
    </div>
  );
}

/* ---------------------------------------------------------------- inventories */

const DIRECTION_TABS = [
  { value: "Inbound", label: "Exposed by this service" },
  { value: "Outbound", label: "Called by this service" },
  { value: "", label: "Direction not determined" },
];

function ApiInventory({ items, coverage }) {
  const [query, setQuery] = useState("");
  const [direction, setDirection] = useState("Inbound");
  const byDirection = useMemo(() => {
    const groups = { Inbound: [], Outbound: [], "": [] };
    for (const item of items || []) {
      const key = item.direction === "Outbound" || item.direction === "Inbound" ? item.direction : "";
      groups[key].push(item);
    }
    return groups;
  }, [items]);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const rows = (byDirection[direction] || []).filter((item) => (
      !needle
      || String(item.path || "").toLowerCase().includes(needle)
      || String(item.method || "").toLowerCase().includes(needle)
      || String(item.file || "").toLowerCase().includes(needle)
    ));
    return rows.sort((left, right) => (
      String(left.path || "").localeCompare(String(right.path || ""))
      || HTTP_METHOD_ORDER.indexOf(left.method) - HTTP_METHOD_ORDER.indexOf(right.method)
    ));
  }, [byDirection, direction, query]);
  const availableTabs = DIRECTION_TABS.filter((tab) => byDirection[tab.value]?.length);
  useEffect(() => {
    // Older analyses stored no direction; land on whichever group has rows.
    if (!byDirection[direction]?.length && availableTabs.length) {
      setDirection(availableTabs[0].value);
    }
  }, [byDirection, direction, availableTabs]);
  if (!items?.length) {
    return (
      <EmptyState
        title="No HTTP routes were inventoried"
        description={
          coverage?.label === "Hermes only"
            ? "Deterministic route extraction depends on scanners that did not run in this image."
            : "No route definitions were found in the supplied source evidence."
        }
      />
    );
  }
  return (
    <Section
      title="HTTP route inventory"
      description="Deterministic source matches merged with Hermes-identified routes and deduplicated. Routes a service calls are listed separately from routes it serves."
    >
      <div className="ai-segmented" role="tablist">
        {availableTabs.map((tab) => (
          <button
            key={tab.value || "unknown"}
            type="button"
            role="tab"
            aria-selected={direction === tab.value}
            className={direction === tab.value ? "is-active" : ""}
            onClick={() => setDirection(tab.value)}
          >
            {tab.label}
            <span>{byDirection[tab.value].length}</span>
          </button>
        ))}
      </div>
      {direction === "" ? (
        <p className="ai-caveat">
          These routes were recorded before KubeSight distinguished served routes from consumed ones.
          Re-analyze to classify them.
        </p>
      ) : null}
      <FilterBox
        value={query}
        onChange={setQuery}
        placeholder="Filter by path, method, or controller…"
        count={filtered.length}
        noun={filtered.length === 1 ? "route" : "routes"}
      />
      <div className="ai-table-wrap ai-table-wrap--tall">
        <table className="data-table">
          <thead><tr><th>Method</th><th>Path</th><th>Source</th><th>Confidence</th></tr></thead>
          <tbody>
            {filtered.map((item, index) => (
              <tr key={`${item.method}-${item.path}-${index}`}>
                <td><span className={`ai-method ai-method--${String(item.method || "").toLowerCase()}`}>{item.method || "—"}</span></td>
                <td><code>{item.path || "—"}</code></td>
                <td className="ai-cell-path">
                  <code>{item.file || "Unknown file"}{item.line ? `:${item.line}` : ""}</code>
                </td>
                <td>{item.confidence || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function Configuration({ configuration }) {
  const [query, setQuery] = useState("");
  const items = configuration?.items || [];
  const secrets = configuration?.secretRequirements || [];
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return items.filter((item) => (
      !needle
      || String(item.name || "").toLowerCase().includes(needle)
      || String(item.value || "").toLowerCase().includes(needle)
      || String(item.source || "").toLowerCase().includes(needle)
    ));
  }, [items, query]);
  return (
    <div className="ai-stack">
      <Section
        title="Configuration inventory"
        description="Declared configuration observed in source. Secret values are redacted before storage."
      >
        {!items.length ? <p className="muted">No configuration was inventoried.</p> : (
          <>
            <FilterBox
              value={query}
              onChange={setQuery}
              placeholder="Filter configuration…"
              count={filtered.length}
              noun={filtered.length === 1 ? "entry" : "entries"}
            />
            <div className="ai-table-wrap">
              <table className="data-table">
                <thead><tr><th>Name</th><th>Value</th><th>Source</th><th>Confidence</th></tr></thead>
                <tbody>
                  {filtered.map((item, index) => (
                    <tr key={`${item.name}-${index}`}>
                      <td><strong>{item.name}</strong></td>
                      <td className="ai-cell-wrap">{item.value == null ? "—" : String(item.value)}</td>
                      <td className="ai-cell-path"><code>{item.source || "—"}</code></td>
                      <td>{item.confidence || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Section>
      <Section
        title="Secret requirements"
        description="What the service needs at runtime. KubeSight never reads or stores the values themselves."
      >
        {!secrets.length ? <p className="muted">No secret requirements were identified.</p> : (
          <div className="ai-table-wrap">
            <table className="data-table">
              <thead><tr><th>Secret</th><th>Required for</th><th>Evidence</th><th>Confidence</th></tr></thead>
              <tbody>
                {secrets.map((item, index) => (
                  <tr key={`${item.name}-${index}`}>
                    <td><strong>{item.name}</strong>{item.note ? <small>{item.note}</small> : null}</td>
                    <td>{item.required_for || item.requiredFor || "—"}</td>
                    <td className="ai-cell-wrap">{item.source || "—"}</td>
                    <td>{item.confidence || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}

function ContainerAndBuild({ analysis, dependencies, artifacts }) {
  const docker = analysis?.result?.docker_analysis || {};
  const coverage = analysis?.evidenceCoverage;
  const proposed = docker.hardened_dockerfile || docker.proposed_dockerfile;
  const diff = docker.diff || docker.dockerfile_diff;
  const facts = {
    base_image: docker.base_image,
    runtime: docker.runtime,
    user: docker.user,
    entrypoint: docker.entrypoint,
    exposed_ports: docker.exposed_ports,
    dockerfiles: docker.dockerfiles,
  };
  const sbomArtifacts = artifacts.filter((item) => item.artifactType.includes("SBOM"));
  return (
    <div className="ai-stack">
      <Section title="Container image" description="Read from the analyzed Dockerfile.">
        <DetailGrid source={facts} empty="No Dockerfile was analyzed." />
        {docker.confirmed_issues?.length ? (
          <>
            <h4>Confirmed issues</h4>
            <BulletList items={docker.confirmed_issues} tone="ai-bullets--risk" />
          </>
        ) : null}
        {docker.missing_evidence?.length ? (
          <>
            <h4>Not present in the Dockerfile</h4>
            <BulletList items={docker.missing_evidence} />
          </>
        ) : null}
      </Section>

      {proposed ? (
        <Section
          title="Hardened Dockerfile proposal"
          description="Review-only. KubeSight never writes this to the repository."
        >
          <pre className="ai-code">{proposed}</pre>
        </Section>
      ) : null}
      {diff ? (
        <Section title="Reviewable diff"><pre className="ai-code">{diff}</pre></Section>
      ) : null}

      <Section
        title="Dependencies"
        description="Resolved from the repository manifests by the SBOM scanner."
        actions={sbomArtifacts.map((artifact) => (
          <button key={artifact.id} type="button" className="btn-outline" onClick={() => downloadApplicationArtifact(artifact)}>
            Download {artifact.artifactType}
          </button>
        ))}
      >
        {!dependencies?.length ? (
          <p className="ai-caveat">
            {coverage?.unavailable?.includes("Syft") || coverage?.unavailable?.includes("Trivy")
              ? "No dependency inventory was produced because the SBOM and vulnerability scanners are not installed in the analysis image. This is missing data, not an empty dependency tree."
              : "No dependencies were resolved from the repository manifests."}
          </p>
        ) : (
          <div className="ai-table-wrap ai-table-wrap--tall">
            <table className="data-table">
              <thead><tr><th>Name</th><th>Version</th><th>Ecosystem</th><th>License</th><th>Source</th><th>Vulnerable</th></tr></thead>
              <tbody>
                {dependencies.map((item) => (
                  <tr key={item.id}>
                    <td><strong>{item.name}</strong></td>
                    <td><code>{item.version || "—"}</code></td>
                    <td>{item.ecosystem || "—"}</td>
                    <td>{item.license || "—"}</td>
                    <td className="ai-cell-path"><code>{item.sourceFile || "—"}</code></td>
                    <td>{item.vulnerable ? <span className="status-badge status-badge--fail">Yes</span> : "No"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section title="Scanner runs" description="Which deterministic tools produced the evidence above.">
        {!(analysis?.scannerRuns || []).length ? <p className="muted">No scanners were invoked for this mode.</p> : (
          <div className="ai-table-wrap">
            <table className="data-table">
              <thead><tr><th>Scanner</th><th>Result</th><th>Version</th><th>Detail</th></tr></thead>
              <tbody>
                {analysis.scannerRuns.map((run, index) => (
                  <tr key={`${run.name}-${index}`}>
                    <td><strong>{run.name}</strong></td>
                    <td>
                      {typeof run.exitStatus === "number"
                        ? <span className={`status-badge status-badge--${run.exitStatus === 0 ? "pass" : "warning"}`}>Ran (exit {run.exitStatus})</span>
                        : <span className="status-badge status-badge--fail">{humanizeKey(run.exitStatus) || "Unavailable"}</span>}
                    </td>
                    <td>{run.version || "—"}</td>
                    <td className="ai-cell-wrap">{run.warning || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}

/* -------------------------------------------------------- deployment readiness */

function DeploymentReadiness({
  sourceRecommendations,
  runtime,
  runtimeError,
  runtimeLoading,
  canAnalyze,
  analysisId,
  onCollect,
}) {
  const comparison = runtime?.comparison || [];
  const gates = runtime?.readinessGates || [];
  const policy = runtime?.networkPolicy || {};
  const mapping = runtime?.mapping || {};
  const clusterId = runtime?.clusterId || mapping.clusterId;
  const namespace = runtime?.namespace || mapping.namespace;
  const workloadName = runtime?.workloadName || mapping.workloadName;
  const workloadKind = runtime?.workloadKind || mapping.workloadKind;
  const mapped = Boolean(clusterId && namespace && workloadName);
  return (
    <div className="ai-stack">
      <Section
        title="Mapped workload"
        description={mapped
          ? `${clusterId} / ${namespace} / ${workloadKind} ${workloadName}`
          : "No cluster, namespace, and workload are mapped to this application yet."}
        actions={
          <>
            <RuntimeBadge value={runtime?.status || "Not Collected"} />
            {canAnalyze && mapped ? (
              <button type="button" className="primary" disabled={runtimeLoading} onClick={onCollect}>
                {runtimeLoading ? "Collecting…" : "Refresh runtime evidence"}
              </button>
            ) : null}
          </>
        }
      >
        {runtime?.createdAt ? (
          <p className="muted">Observed {formatTimestamp(runtime.createdAt)} by {runtime.collectedBy}</p>
        ) : null}
        {!mapped ? (
          <p className="muted">Map one in the analyze dialog to compare source against a live workload.</p>
        ) : null}
        <p className="ai-trust-note">
          Runtime collection is read-only and permission-scoped. Secret values, ConfigMap values,
          cluster credentials, and Kubernetes tokens are never retained or sent to Hermes.
        </p>
      </Section>
      {runtimeError ? <p className="banner-message error">{runtimeError}</p> : null}

      {gates.length ? (
        <Section title="Deployment-readiness gates">
          <div className="ai-gates">
            {gates.map((gate) => (
              <article key={gate.id} className="ai-gate">
                <RuntimeBadge value={gate.status} />
                <strong>{gate.title}</strong>
                <p>{gate.evidence}</p>
                {gate.recommendation ? <small>{gate.recommendation}</small> : null}
              </article>
            ))}
          </div>
        </Section>
      ) : null}

      {comparison.length ? (
        <Section title="Source-to-runtime comparison">
          <div className="ai-table-wrap">
            <table className="data-table">
              <thead><tr><th>Category</th><th>Status</th><th>Source</th><th>Runtime</th><th>Detail</th></tr></thead>
              <tbody>
                {comparison.map((item) => (
                  <tr key={item.category}>
                    <td>
                      <strong>{item.category}</strong>
                      <small>{item.sourceEvidenceState} ↔ {item.runtimeEvidenceState}</small>
                    </td>
                    <td><RuntimeBadge value={item.status} /></td>
                    <td className="ai-cell-wrap"><DetailValue value={item.source} /></td>
                    <td className="ai-cell-wrap"><DetailValue value={item.runtime} /></td>
                    <td className="ai-cell-wrap">{item.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      ) : null}

      {policy.status ? (
        <Section
          title="NetworkPolicy recommendation"
          description="Review-only proposal. KubeSight does not apply this resource."
          actions={policy.yaml ? (
            <button type="button" className="btn-outline" onClick={() => downloadApplicationNetworkPolicy(analysisId)}>
              Download YAML
            </button>
          ) : null}
        >
          <BulletList items={policy.limitations} />
          {policy.yaml ? <pre className="ai-code">{policy.yaml}</pre> : <p className="muted">{policy.reason}</p>}
        </Section>
      ) : null}

      <Section
        title="Source-derived Kubernetes recommendations"
        description="Generated from source evidence alone, before any runtime observation."
      >
        <DetailGrid source={sourceRecommendations} empty="No recommendations were produced." />
      </Section>
    </div>
  );
}

/* ------------------------------------------------------------------- history */

function DeltaCell({ delta }) {
  if (!delta) return <span className="muted">no change</span>;
  return (
    <span className={`ai-delta ai-delta--${delta > 0 ? "worse" : "better"}`}>
      {delta > 0 ? `+${delta}` : delta}
    </span>
  );
}

function History({ application, analysis, onOpenAnalysis }) {
  const [baselineId, setBaselineId] = useState("");
  const [comparison, setComparison] = useState(null);
  const [comparisonError, setComparisonError] = useState("");
  const [comparing, setComparing] = useState(false);
  const runs = application.analyses || [];
  const compare = async () => {
    setComparing(true);
    setComparisonError("");
    try {
      setComparison(await compareApplicationAnalyses(analysis.id, baselineId));
    } catch (err) {
      setComparisonError(err.message || "Analyses could not be compared.");
    } finally {
      setComparing(false);
    }
  };
  return (
    <div className="ai-stack">
      <Section
        title="Compare runs"
        description="Severity movement and finding churn between this run and an earlier one."
      >
        <div className="ai-comparison-controls">
          <SearchableSelect
            value={baselineId}
            onChange={(event) => { setBaselineId(event.target.value); setComparison(null); }}
            options={[
              { value: "", label: "Select baseline analysis…" },
              ...runs
                .filter((item) => item.id !== analysis?.id)
                .map((item) => ({
                  value: item.id,
                  label: `${shortCommit(item.commitSha) || "pending"} · ${formatTimestamp(item.createdAt)}`,
                })),
            ]}
            searchThreshold={10}
          />
          <button type="button" className="btn-outline" disabled={!baselineId || comparing} onClick={compare}>
            {comparing ? "Comparing…" : "Compare with current"}
          </button>
        </div>
        {comparisonError ? <p className="banner-message error">{comparisonError}</p> : null}
        {comparison ? (
          <div className="ai-comparison">
            <p className="ai-inline-fact">
              <span>Risk level</span>
              <strong>{comparison.riskLevel?.baseline} → {comparison.riskLevel?.current}</strong>
            </p>
            <div className="ai-table-wrap">
              <table className="data-table">
                <thead><tr><th>Severity</th><th>Baseline</th><th>Current</th><th>Change</th></tr></thead>
                <tbody>
                  {SEVERITY_ORDER.map((severity) => {
                    const row = comparison.severityDeltas?.[severity];
                    if (!row || (!row.baseline && !row.current)) return null;
                    return (
                      <tr key={severity}>
                        <td><span className={`ai-sev ai-sev--${severity.toLowerCase()}`}>{severity}</span></td>
                        <td>{row.baseline}</td>
                        <td>{row.current}</td>
                        <td><DeltaCell delta={row.delta} /></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="ai-two-col">
              <div>
                <h4>New findings ({comparison.findings?.new?.length || 0})</h4>
                <BulletList
                  items={(comparison.findings?.new || []).map((item) => `${item.severity} · ${item.title}`)}
                  tone="ai-bullets--risk"
                />
              </div>
              <div>
                <h4>Resolved findings ({comparison.findings?.resolved?.length || 0})</h4>
                <BulletList
                  items={(comparison.findings?.resolved || []).map((item) => `${item.severity} · ${item.title}`)}
                  tone="ai-bullets--ok"
                />
              </div>
            </div>
            {comparison.dependencies?.changed?.length ? (
              <>
                <h4>Changed dependencies</h4>
                <BulletList
                  items={comparison.dependencies.changed.map(
                    (item) => `${item.name}: ${item.beforeVersion} → ${item.afterVersion}`,
                  )}
                />
              </>
            ) : null}
          </div>
        ) : null}
      </Section>

      <Section title="All runs">
        <div className="ai-table-wrap">
          <table className="data-table">
            <thead>
              <tr><th>Commit</th><th>Started</th><th>Mode</th><th>Status</th><th>Risk</th><th>Open findings</th><th>Requested by</th><th /></tr>
            </thead>
            <tbody>
              {runs.map((item) => (
                <tr key={item.id} className={item.id === analysis?.id ? "is-current" : ""}>
                  <td><code>{shortCommit(item.commitSha) || "pending"}</code></td>
                  <td>{formatTimestamp(item.createdAt)}</td>
                  <td>{item.analysisMode}</td>
                  <td><Status status={item.status} /></td>
                  {producedResult(item.status) ? (
                    <>
                      <td><RiskBadge level={item.posture?.riskLevel} /></td>
                      <td><SeverityChips counts={item.posture?.openBySeverity} /></td>
                    </>
                  ) : (
                    <td className="muted" colSpan={2}>Not assessed</td>
                  )}
                  <td>{item.requestedBy}</td>
                  <td>
                    {item.id === analysis?.id
                      ? <span className="muted">Viewing</span>
                      : <button type="button" className="btn-outline" onClick={() => onOpenAnalysis(item.id)}>Open</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}

/* -------------------------------------------------------------------- detail */

function ApplicationDetail({
  application,
  analysis,
  findings,
  topology,
  runtime,
  runtimeError,
  runtimeLoading,
  configuration,
  artifacts,
  pullRequests,
  credentials,
  filters,
  onFilters,
  onFindingChanged,
  onPullRequestCreated,
  onBack,
  onCancel,
  canManage,
  canAnalyze,
  onAnalyze,
  onOpenAnalysis,
  onCollectRuntime,
}) {
  const [tab, setTab] = useState("Overview");
  const [rerunMode, setRerunMode] = useState("Quick");
  // The detail view mounts before the analysis resolves, so adopt the last
  // mode used once it is known rather than silently offering Quick.
  useEffect(() => {
    if (analysis?.analysisMode) setRerunMode(analysis.analysisMode);
  }, [analysis?.analysisMode]);
  const result = analysis?.result || {};
  const dependencyItems = analysis?.dependencies || [];
  const commitLabel = shortCommit(analysis?.commitSha)
    || (["Failed", "Cancelled"].includes(analysis?.status) ? "commit unavailable" : "commit pending");
  const active = isAnalysisActive(analysis?.status);
  return (
    <div className="ai-detail">
      <button type="button" className="btn-ghost ai-back" onClick={onBack}>← All applications</button>
      <header className="ai-detail__head">
        <div>
          <h2>{application.name}</h2>
          <p className="ai-detail__sub">
            <code>{application.repositoryWorkspace}/{application.repositoryName}</code>
            <span>·</span>
            <span>{analysis?.branch || application.defaultBranch}</span>
            <span>·</span>
            <code>{commitLabel}</code>
            {analysis?.createdAt ? <><span>·</span><span>{formatTimestamp(analysis.createdAt)}</span></> : null}
          </p>
        </div>
        <div className="ai-actions">
          <Status status={analysis?.status} />
          {active ? <button type="button" className="btn-outline" onClick={onCancel}>Cancel analysis</button> : null}
          {canAnalyze && !active ? (
            <>
              <SearchableSelect
                value={rerunMode}
                onChange={(event) => setRerunMode(event.target.value)}
                options={[
                  { value: "Quick", label: "Quick" },
                  { value: "Deep", label: "Deep" },
                  { value: "Build Verified", label: "Build Verified" },
                ]}
                searchThreshold={10}
                aria-label="Rerun analysis mode"
              />
              <button type="button" className="primary" onClick={() => onAnalyze(rerunMode)}>
                Re-analyze
              </button>
            </>
          ) : null}
        </div>
      </header>

      {active ? (
        <div className="card ai-progress">
          <div>
            <strong>{analysis.currentStage || analysis.status}</strong>
            <span>{analysis.progressPercent || 0}%</span>
          </div>
          <progress max="100" value={analysis.progressPercent || 0} />
          <small>You can leave this page; the isolated job continues to run.</small>
        </div>
      ) : null}
      {analysis?.safeErrorMessage ? (
        <p className={`banner-message ${analysis.status === "Failed" ? "error" : ""}`}>
          {analysis.safeErrorMessage}
        </p>
      ) : null}

      {!analysis ? (
        <EmptyState title="No analysis has run for this application yet" />
      ) : (
        <>
          <nav className="ai-tabs" aria-label="Analysis results">
            {TABS.map((item) => (
              <button key={item} type="button" className={tab === item ? "active" : ""} onClick={() => setTab(item)}>
                {item}
                {item === "Findings" && analysis.posture?.openTotal
                  ? <span className="ai-tab-count">{analysis.posture.openTotal}</span>
                  : null}
              </button>
            ))}
          </nav>

          {tab === "Overview" ? (
            <Overview analysis={analysis} application={application} artifacts={artifacts} />
          ) : null}
          {tab === "Findings" ? (
            <Findings
              items={findings}
              filters={filters}
              onFilters={onFilters}
              canManage={canManage}
              credentials={credentials}
              analysis={analysis}
              pullRequests={pullRequests}
              onStatusChanged={onFindingChanged}
              onPullRequestCreated={onPullRequestCreated}
            />
          ) : null}
          {tab === "Architecture" ? (
            <Architecture
              summary={result.architecture_summary}
              topology={topology}
              runtime={runtime}
            />
          ) : null}
          {tab === "APIs" ? (
            <ApiInventory items={result.api_inventory} coverage={analysis.evidenceCoverage} />
          ) : null}
          {tab === "Configuration" ? <Configuration configuration={configuration} /> : null}
          {tab === "Container & build" ? (
            <ContainerAndBuild analysis={analysis} dependencies={dependencyItems} artifacts={artifacts} />
          ) : null}
          {tab === "Deployment" ? (
            <DeploymentReadiness
              sourceRecommendations={result.kubernetes_recommendations}
              runtime={runtime}
              runtimeError={runtimeError}
              runtimeLoading={runtimeLoading}
              canAnalyze={canAnalyze}
              analysisId={analysis.id}
              onCollect={onCollectRuntime}
            />
          ) : null}
          {tab === "History" ? (
            <History application={application} analysis={analysis} onOpenAnalysis={onOpenAnalysis} />
          ) : null}
        </>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------------- page */

export default function ApplicationIntelligencePage({ clusters = [], canManage, canAnalyze }) {
  const [applications, setApplications] = useState([]);
  const [credentials, setCredentials] = useState([]);
  const [selected, setSelected] = useState(null);
  const [analysis, setAnalysis] = useState(null);
  const [findings, setFindings] = useState([]);
  const [topology, setTopology] = useState({ nodes: [], edges: [] });
  const [runtime, setRuntime] = useState({ status: "Not Collected" });
  const [runtimeError, setRuntimeError] = useState("");
  const [runtimeLoading, setRuntimeLoading] = useState(false);
  const [configuration, setConfiguration] = useState({ items: [], secretRequirements: [] });
  const [artifacts, setArtifacts] = useState([]);
  const [pullRequests, setPullRequests] = useState([]);
  const [filters, setFilters] = useState({ severity: "", confidence: "", status: "", file: "" });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [modal, setModal] = useState(false);
  const [credentialPanel, setCredentialPanel] = useState(null);

  const loadList = useCallback(async () => {
    try {
      const data = await listIntelligenceApplications({ perPage: 100 });
      setApplications(data.items || []);
    } catch (err) {
      setError(err.message || "Applications could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, []);
  const loadCredentials = useCallback(async () => {
    if (!canManage) return;
    try {
      const data = await listBitbucketCredentialProfiles();
      setCredentials(data.items || []);
    } catch (err) {
      setError(err.message || "Credential profiles could not be loaded.");
    }
  }, [canManage]);

  useEffect(() => { loadList(); loadCredentials(); }, [loadList, loadCredentials]);

  const loadDetail = useCallback(async (applicationId, analysisId) => {
    const app = await getIntelligenceApplication(applicationId);
    const chosenId = analysisId || app.analyses?.[0]?.id;
    setSelected(app);
    if (!chosenId) {
      setAnalysis(null);
      setFindings([]);
      setTopology({ nodes: [], edges: [] });
      setRuntime({ status: "Not Collected" });
      setRuntimeError("");
      setConfiguration({ items: [], secretRequirements: [] });
      setArtifacts([]);
      setPullRequests([]);
      return;
    }
    const [analysisData, topologyData, configurationData, artifactData, pullRequestData] = await Promise.all([
      getApplicationAnalysis(chosenId),
      getApplicationTopology(chosenId),
      getApplicationConfiguration(chosenId),
      listApplicationArtifacts(chosenId),
      listApplicationPullRequests(chosenId),
    ]);
    setAnalysis(analysisData);
    setTopology(topologyData);
    setConfiguration(configurationData);
    setArtifacts(artifactData.items || []);
    setPullRequests(pullRequestData.items || []);
    try {
      setRuntime(await getApplicationRuntime(chosenId));
      setRuntimeError("");
    } catch (err) {
      setRuntime({ status: "Unavailable" });
      setRuntimeError(err.message || "Runtime evidence could not be loaded.");
    }
  }, []);

  useEffect(() => {
    if (!analysis?.id) return undefined;
    let cancelled = false;
    const load = async () => {
      try {
        const data = await listApplicationFindings(analysis.id, { ...filters, perPage: 100 });
        if (!cancelled) setFindings(data.items || []);
      } catch { /* detail error is already surfaced by its analysis state */ }
    };
    load();
    return () => { cancelled = true; };
  }, [analysis?.id, filters]);

  useEffect(() => {
    if (!analysis?.id || !isAnalysisActive(analysis.status)) return undefined;
    const timer = window.setInterval(async () => {
      try {
        const next = await getApplicationAnalysis(analysis.id);
        setAnalysis(next);
        if (!isAnalysisActive(next.status)) {
          await loadDetail(next.applicationId, next.id);
          await loadList();
        }
      } catch { /* polling resumes on next interval */ }
    }, 5000);
    return () => window.clearInterval(timer);
  }, [analysis?.id, analysis?.status, loadDetail, loadList]);

  useEffect(() => {
    if (!analysis?.id || !pullRequests.some((item) => item.status === "Queued")) return undefined;
    const timer = window.setInterval(async () => {
      try {
        const data = await listApplicationPullRequests(analysis.id);
        setPullRequests(data.items || []);
      } catch { /* polling resumes on next interval */ }
    }, 5000);
    return () => window.clearInterval(timer);
  }, [analysis?.id, pullRequests]);

  const latestById = useMemo(
    () => Object.fromEntries(applications.map((item) => [item.id, item.latestAnalysis])),
    [applications],
  );

  if (loading) return <LoadingState label="Loading Application Intelligence…" />;
  return (
    <div className="ai-page">
      {!selected ? (
        <>
          <PageTitle
            title="Application Intelligence"
            subtitle="Evidence-backed source, container, and deployment analysis for Bitbucket microservices."
            actionLabel={canAnalyze && canManage ? "Analyze application" : undefined}
            onAction={() => setModal(true)}
          />
          {error ? <p className="banner-message error">{error}</p> : null}
          {!applications.length ? (
            <EmptyState
              title="No applications analyzed yet"
              description="Register a Bitbucket repository to begin a bounded, read-only analysis."
            />
          ) : (
            <div className="card ai-table-wrap">
              <table className="data-table ai-list-table">
                <thead>
                  <tr>
                    <th>Application</th>
                    <th>Status</th>
                    <th>Risk</th>
                    <th>Open findings</th>
                    <th>Evidence</th>
                    <th>Repository</th>
                    <th>Revision</th>
                    <th>Last run</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {applications.map((item) => {
                    const latest = latestById[item.id];
                    const coverage = latest?.evidenceCoverage;
                    return (
                      <tr key={item.id}>
                        <td>
                          <strong>{item.name}</strong>
                          {item.description ? <small>{item.description}</small> : null}
                        </td>
                        <td><Status status={latest?.status} /></td>
                        {/* A run that never finished has no posture to report. */}
                        {producedResult(latest?.status) ? (
                          <>
                            <td><RiskBadge level={latest.posture?.riskLevel} /></td>
                            <td><SeverityChips counts={latest.posture?.openBySeverity} empty="None" /></td>
                            <td>
                              {coverage
                                ? <span className={`status-badge status-badge--${coverageTone(coverage.label)}`}>{coverage.label}</span>
                                : <span className="muted">—</span>}
                            </td>
                          </>
                        ) : (
                          <td className="muted" colSpan={3}>Not assessed</td>
                        )}
                        <td className="ai-cell-path"><code>{item.repositoryWorkspace}/{item.repositoryName}</code></td>
                        <td>
                          {latest?.branch || item.defaultBranch}
                          <small>
                            {shortCommit(latest?.commitSha)
                              || (latest?.status === "Failed" ? "unavailable" : "pending")}
                          </small>
                        </td>
                        <td>{formatTimestamp(latest?.createdAt)}</td>
                        <td><button type="button" className="btn-outline" onClick={() => loadDetail(item.id)}>Open</button></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      ) : (
        <ApplicationDetail
          application={selected}
          analysis={analysis}
          findings={findings}
          topology={topology}
          runtime={runtime}
          runtimeError={runtimeError}
          runtimeLoading={runtimeLoading}
          configuration={configuration}
          artifacts={artifacts}
          pullRequests={pullRequests}
          credentials={credentials}
          filters={filters}
          onFilters={setFilters}
          onFindingChanged={(updated) => {
            setFindings((items) => items.map((item) => (item.id === updated.id ? updated : item)));
          }}
          onPullRequestCreated={(created) => {
            setPullRequests((items) => [created, ...items]);
          }}
          onBack={() => { setSelected(null); setAnalysis(null); }}
          onCancel={async () => {
            if (!analysis?.id) return;
            const next = await cancelApplicationAnalysis(analysis.id);
            setAnalysis(next);
            loadList();
          }}
          canManage={canManage}
          canAnalyze={canAnalyze}
          onAnalyze={async (analysisMode) => {
            const next = await requestApplicationAnalysis(selected.id, {
              analysisMode,
              revision: selected.defaultBranch,
            });
            await loadList();
            await loadDetail(selected.id, next.id);
          }}
          onOpenAnalysis={(analysisId) => loadDetail(selected.id, analysisId)}
          onCollectRuntime={async () => {
            if (!analysis?.id) return;
            setRuntimeLoading(true);
            setRuntimeError("");
            try {
              setRuntime(await collectApplicationRuntime(analysis.id));
            } catch (err) {
              setRuntimeError(err.message || "Runtime evidence could not be collected.");
            } finally {
              setRuntimeLoading(false);
            }
          }}
        />
      )}
      {modal ? (
        <AnalyzeModal
          credentials={credentials.filter((item) => item.enabled && item.readOnly)}
          clusters={clusters}
          canManage={canManage}
          onCredentialRequested={() => {
            setModal(false);
            setCredentialPanel({ credential: null });
          }}
          onCredentialEditRequested={(credential) => {
            setModal(false);
            setCredentialPanel({ credential });
          }}
          onCredentialDeleted={(credentialId) => {
            setCredentials((items) => items.filter((item) => item.id !== credentialId));
          }}
          onClose={() => setModal(false)}
          onComplete={async (application, createdAnalysis) => {
            setModal(false);
            await loadList();
            await loadDetail(application.id, createdAnalysis.id);
          }}
        />
      ) : null}
      {credentialPanel ? (
        <div className="modal-overlay" role="dialog" aria-modal="true" aria-label="Bitbucket credential">
          <CredentialPanel
            credential={credentialPanel.credential}
            onCancel={() => { setCredentialPanel(null); setModal(true); }}
            onSaved={(saved) => {
              setCredentials((items) => {
                const exists = items.some((item) => item.id === saved.id);
                return exists
                  ? items.map((item) => (item.id === saved.id ? saved : item))
                  : [...items, saved];
              });
              setCredentialPanel(null);
              setModal(true);
            }}
          />
        </div>
      ) : null}
    </div>
  );
}
