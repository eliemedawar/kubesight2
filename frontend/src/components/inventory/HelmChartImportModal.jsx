import { useEffect, useState } from "react";

import {
  importHelmChartFromGit,
  importHelmChartFromYaml,
} from "../../api/helmApi.js";
import SearchableSelect from "../common/SearchableSelect.jsx";

const EMPTY_GIT = {
  repositoryUrl: "",
  ref: "",
  path: "",
  importType: "auto",
  username: "",
  token: "",
};

export default function HelmChartImportModal({ open, onClose, onImported }) {
  const [mode, setMode] = useState("yaml");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [yamlFiles, setYamlFiles] = useState([]);
  const [pastedYaml, setPastedYaml] = useState("");
  const [git, setGit] = useState(EMPTY_GIT);
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
      setBusy(false);
      setError("");
    }
  }, [open]);

  if (!open) return null;

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      let result;
      if (mode === "yaml") {
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
      onClose();
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
            <h3 id="helm-import-title">Import Helm Chart</h3>
            <p className="muted">
              Save a reusable chart from Kubernetes manifests or Git.
            </p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>

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
            className={mode === "git" ? "active" : ""}
            onClick={() => {
              setMode("git");
              setError("");
            }}
          >
            Git Repository
          </button>
        </div>

        <form className="add-app-form" onSubmit={submit}>
          {error ? <p className="banner-message error">{error}</p> : null}

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

          {mode === "yaml" ? (
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
              {busy ? "Importing…" : "Import & Save Chart"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
