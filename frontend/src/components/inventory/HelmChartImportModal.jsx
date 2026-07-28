import { useEffect, useState } from "react";

import {
  addHelmChartVersion,
  importHelmChartFromArchive,
  importHelmChartFromGit,
  importHelmChartFromYaml,
  readChartArchiveAsBase64,
} from "../../api/helmApi.js";
import HelmChartContents, { helmSourceLabel } from "./HelmChartContents.jsx";
import SearchableSelect from "../common/SearchableSelect.jsx";

const EMPTY_GIT = {
  repositoryUrl: "",
  ref: "",
  path: "",
  importType: "auto",
  username: "",
  token: "",
};

const EMPTY_ARCHIVE = { path: "", importType: "auto" };

// Mirrors MAX_IMPORT_BYTES on the backend so oversized uploads fail instantly.
const MAX_ARCHIVE_BYTES = 10 * 1024 * 1024;
const ARCHIVE_SUFFIXES = [".zip", ".tgz", ".tar.gz"];

function formatSize(bytes) {
  return bytes >= 1024 * 1024
    ? `${(bytes / (1024 * 1024)).toFixed(2)} MiB`
    : `${Math.max(1, Math.round(bytes / 1024))} KiB`;
}

export default function HelmChartImportModal({
  open,
  onClose,
  onImported,
  // When set, the upload becomes a new version of that existing chart instead
  // of a new catalog entry.
  targetTemplate = null,
}) {
  const [mode, setMode] = useState("yaml");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [yamlFiles, setYamlFiles] = useState([]);
  const [pastedYaml, setPastedYaml] = useState("");
  const [git, setGit] = useState(EMPTY_GIT);
  const [archive, setArchive] = useState(EMPTY_ARCHIVE);
  const [archiveFile, setArchiveFile] = useState(null);
  const [imported, setImported] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) {
      setMode("yaml");
      setName("");
      setDescription("");
      setYamlFiles([]);
      setPastedYaml("");
      setGit(EMPTY_GIT);
      setArchive(EMPTY_ARCHIVE);
      setArchiveFile(null);
      setImported(null);
      setBusy(false);
      setError("");
    }
  }, [open]);

  if (!open) return null;

  const versionMode = Boolean(targetTemplate?.id);
  const activeMode = versionMode ? "archive" : mode;

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      let result;
      if (activeMode === "yaml") {
        const files = await Promise.all(
          yamlFiles.map(async (file) => ({ name: file.name, content: await file.text() })),
        );
        if (pastedYaml.trim()) {
          files.push({ name: "pasted-manifests.yaml", content: pastedYaml });
        }
        if (!files.length) {
          throw new Error("Choose at least one YAML file or paste Kubernetes YAML.");
        }
        result = await importHelmChartFromYaml({ name, description, files });
      } else if (activeMode === "archive") {
        if (!archiveFile) {
          throw new Error("Choose a .zip, .tgz, or .tar.gz archive to import.");
        }
        if (!ARCHIVE_SUFFIXES.some((suffix) => archiveFile.name.toLowerCase().endsWith(suffix))) {
          throw new Error("Only .zip, .tgz, and .tar.gz archives are supported.");
        }
        if (archiveFile.size > MAX_ARCHIVE_BYTES) {
          throw new Error("The archive exceeds the 10 MiB import limit.");
        }
        const archivePayload = {
          filename: archiveFile.name,
          archiveBase64: await readChartArchiveAsBase64(archiveFile),
          ...archive,
        };
        result = versionMode
          ? await addHelmChartVersion(targetTemplate.id, archivePayload)
          : await importHelmChartFromArchive({ name, description, ...archivePayload });
      } else {
        result = await importHelmChartFromGit({
          name,
          description,
          ...git,
        });
      }
      // Clear credentials immediately after the request completes. They are
      // deliberately never returned by, or persisted in, the API.
      setGit((previous) => ({ ...previous, token: "" }));
      onImported?.(result);
      setImported(result);
    } catch (err) {
      setGit((previous) => ({ ...previous, token: "" }));
      setError(err.message || "Helm chart import failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel helm-chart-import-modal"
        role="dialog"
        aria-labelledby="helm-import-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <h3 id="helm-import-title">
              {imported
                ? versionMode
                  ? `Version ${imported.version} Added`
                  : "Chart Imported"
                : versionMode
                  ? `Add Version to ${targetTemplate.name}`
                  : "Import Helm Chart"}
            </h3>
            <p className="muted">
              {imported
                ? "KubeSight read the chart and stored it for reuse."
                : versionMode
                  ? `Upload another packaged chart. Its Chart.yaml version becomes the new version — ${targetTemplate.name} is currently v${targetTemplate.version}.`
                  : "Save a reusable chart from Kubernetes manifests, a chart archive, or Git."}
            </p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>

        {imported ? (
          <div className="helm-import-result">
            <p className="banner-message">
              {versionMode ? "Added" : "Imported"} <strong>{imported.name}</strong> v
              {imported.version} · {helmSourceLabel(imported)}
              {versionMode ? ` · ${imported.versionCount} versions stored` : ""}
            </p>
            <HelmChartContents template={imported} />
            {(imported.warnings || []).length ? (
              <details className="helm-chart-card__warnings" open>
                <summary>
                  {imported.warnings.length} import warning
                  {imported.warnings.length === 1 ? "" : "s"}
                </summary>
                <ul>
                  {imported.warnings.map((warning, index) => (
                    <li key={`import-warning-${index}`}>{warning}</li>
                  ))}
                </ul>
              </details>
            ) : null}
            <div className="modal-actions">
              <button
                type="button"
                className="btn-text"
                onClick={() => {
                  setImported(null);
                  setArchiveFile(null);
                }}
              >
                {versionMode ? "Add another version" : "Import another"}
              </button>
              <button type="button" className="btn-primary" onClick={onClose}>
                Done
              </button>
            </div>
          </div>
        ) : (
          <>
            {versionMode ? null : (
              <div className="helm-import-source-tabs" role="tablist" aria-label="Import source">
                <button
                  type="button"
                  className={mode === "yaml" ? "active" : ""}
                  onClick={() => {
                    setMode("yaml");
                    setError("");
                  }}
                >
                  Deployment YAMLs
                </button>
                <button
                  type="button"
                  className={mode === "archive" ? "active" : ""}
                  onClick={() => {
                    setMode("archive");
                    setError("");
                  }}
                >
                  Chart Archive
                </button>
                <button
                  type="button"
                  className={mode === "git" ? "active" : ""}
                  onClick={() => {
                    setMode("git");
                    setError("");
                  }}
                >
                  Git Repository
                </button>
              </div>
            )}

            <form className="add-app-form" onSubmit={submit}>
              {error ? <p className="banner-message error">{error}</p> : null}

              {versionMode ? null : (
                <div className="helm-form-grid">
                  <label>
                    Chart name <span className="muted">(optional)</span>
                    <input
                      value={name}
                      maxLength={120}
                      onChange={(event) => setName(event.target.value)}
                      placeholder="Derived from Chart.yaml or the first workload"
                    />
                  </label>
                  <label>
                    Description <span className="muted">(optional)</span>
                    <input
                      value={description}
                      maxLength={500}
                      onChange={(event) => setDescription(event.target.value)}
                    />
                  </label>
                </div>
              )}

              {activeMode === "yaml" ? (
                <>
                  <label>
                    Kubernetes YAML files
                    <input
                      type="file"
                      accept=".yaml,.yml,text/yaml,application/yaml"
                      multiple
                      onChange={(event) => setYamlFiles(Array.from(event.target.files || []))}
                    />
                  </label>
                  {yamlFiles.length ? (
                    <p className="helm-selected-files muted">
                      {yamlFiles.length} file{yamlFiles.length === 1 ? "" : "s"} selected:{" "}
                      {yamlFiles.map((file) => file.name).join(", ")}
                    </p>
                  ) : null}
                  <label>
                    Or paste YAML
                    <textarea
                      className="yaml-editor"
                      rows={8}
                      value={pastedYaml}
                      onChange={(event) => setPastedYaml(event.target.value)}
                      placeholder={"apiVersion: apps/v1\nkind: Deployment\n..."}
                    />
                  </label>
                  <p className="form-help">
                    Related resources become one chart. Repeated resources are treated as
                    environment variants: their differing values become configurable fields.
                  </p>
                </>
              ) : activeMode === "archive" ? (
                <>
                  <label>
                    Chart or manifests archive{" "}
                    <span className="muted">(.zip, .tgz, .tar.gz — up to 10 MiB)</span>
                    <input
                      type="file"
                      accept=".zip,.tgz,.tar.gz,.gz,application/zip,application/x-zip-compressed,application/gzip,application/x-gzip,application/x-compressed-tar"
                      onChange={(event) => {
                        setArchiveFile(event.target.files?.[0] || null);
                        setError("");
                      }}
                    />
                  </label>
                  {archiveFile ? (
                    <p className="helm-selected-files muted">
                      {archiveFile.name} · {formatSize(archiveFile.size)}
                    </p>
                  ) : null}
                  <div className="helm-form-grid">
                    <label>
                      Path inside archive <span className="muted">(optional)</span>
                      <input
                        value={archive.path}
                        onChange={(event) =>
                          setArchive((previous) => ({ ...previous, path: event.target.value }))
                        }
                        placeholder="charts/my-app"
                      />
                    </label>
                    <label>
                      Content type
                      <SearchableSelect
                        value={archive.importType}
                        onChange={(event) =>
                          setArchive((previous) => ({
                            ...previous,
                            importType: event.target.value,
                          }))
                        }
                      >
                        <option value="auto">Auto-detect Helm chart or raw YAML</option>
                        <option value="chart">Existing Helm chart</option>
                        <option value="yaml">Raw Kubernetes YAML</option>
                      </SearchableSelect>
                    </label>
                  </div>
                  <p className="form-help">
                    A packaged chart is detected by its Chart.yaml at any depth, so a
                    “helm package” tarball or a “Download ZIP” export works as-is. Templates
                    and values-&lt;environment&gt;.yaml files are read and listed after import,
                    and sensitive values are always stripped.
                    {versionMode
                      ? " The new version becomes the one deployed by default; earlier versions stay available."
                      : ""}
                  </p>
                </>
              ) : (
                <>
                  <label>
                    HTTPS repository URL
                    <input
                      required
                      type="url"
                      value={git.repositoryUrl}
                      onChange={(event) =>
                        setGit((previous) => ({ ...previous, repositoryUrl: event.target.value }))
                      }
                      placeholder="https://github.com/organization/repository.git"
                    />
                  </label>
                  <div className="helm-form-grid">
                    <label>
                      Branch or tag <span className="muted">(optional)</span>
                      <input
                        value={git.ref}
                        onChange={(event) =>
                          setGit((previous) => ({ ...previous, ref: event.target.value }))
                        }
                        placeholder="main"
                      />
                    </label>
                    <label>
                      Repository path <span className="muted">(optional)</span>
                      <input
                        value={git.path}
                        onChange={(event) =>
                          setGit((previous) => ({ ...previous, path: event.target.value }))
                        }
                        placeholder="deploy/helm/my-app"
                      />
                    </label>
                  </div>
                  <label>
                    Content type
                    <SearchableSelect
                      value={git.importType}
                      onChange={(event) =>
                        setGit((previous) => ({ ...previous, importType: event.target.value }))
                      }
                    >
                      <option value="auto">Auto-detect Helm chart or raw YAML</option>
                      <option value="chart">Existing Helm chart</option>
                      <option value="yaml">Raw Kubernetes YAML</option>
                    </SearchableSelect>
                  </label>
                  <div className="helm-form-grid">
                    <label>
                      Git username <span className="muted">(private repositories)</span>
                      <input
                        autoComplete="username"
                        value={git.username}
                        onChange={(event) =>
                          setGit((previous) => ({ ...previous, username: event.target.value }))
                        }
                        placeholder="oauth2 or your username"
                      />
                    </label>
                    <label>
                      Personal access token <span className="muted">(optional)</span>
                      <input
                        type="password"
                        autoComplete="new-password"
                        value={git.token}
                        onChange={(event) =>
                          setGit((previous) => ({ ...previous, token: event.target.value }))
                        }
                      />
                    </label>
                  </div>
                  <p className="form-help">
                    The token is used only for this clone request, cleared immediately, and never
                    saved in KubeSight.
                  </p>
                </>
              )}

              <div className="modal-actions">
                <button type="button" className="btn-text" onClick={onClose} disabled={busy}>
                  Cancel
                </button>
                <button type="submit" className="btn-primary" disabled={busy}>
                  {busy
                    ? versionMode
                      ? "Adding…"
                      : "Importing…"
                    : versionMode
                      ? "Add Version"
                      : "Import & Save Chart"}
                </button>
              </div>
            </form>
          </>
        )}
      </section>
    </div>
  );
}
