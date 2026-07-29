import { useEffect, useState } from "react";

import {
  addHelmChartVersion,
  discoverHelmGitPaths,
  discoverHelmGitRefs,
  discoverNamespaceResources,
  importHelmChartFromArchive,
  importHelmChartFromGit,
  importHelmChartFromNamespace,
  importHelmChartFromYaml,
  readChartArchiveAsBase64,
} from "../../api/helmApi.js";
import { listClusters, listNamespacesByCluster } from "../../api/clustersApi.js";
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

const EMPTY_NAMESPACE = { clusterId: "", namespace: "" };

// Identity of a live object inside the scan result; also the checkbox key.
function resourceKey(resource) {
  return `${resource.kind}/${resource.name}`;
}

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
  const [gitRefs, setGitRefs] = useState([]);
  const [gitPaths, setGitPaths] = useState([]);
  const [gitDiscoveryBusy, setGitDiscoveryBusy] = useState("");
  const [gitDiscoveryError, setGitDiscoveryError] = useState("");
  const [gitPathsTruncated, setGitPathsTruncated] = useState(false);
  const [source, setSource] = useState(EMPTY_NAMESPACE);
  const [clusters, setClusters] = useState([]);
  const [namespaces, setNamespaces] = useState([]);
  const [scan, setScan] = useState(null);
  const [selectedResources, setSelectedResources] = useState(() => new Set());
  const [scanBusy, setScanBusy] = useState("");
  const [scanError, setScanError] = useState("");
  const [resourceFilter, setResourceFilter] = useState("");

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
      setGitRefs([]);
      setGitPaths([]);
      setGitDiscoveryBusy("");
      setGitDiscoveryError("");
      setGitPathsTruncated(false);
      setSource(EMPTY_NAMESPACE);
      setNamespaces([]);
      setScan(null);
      setSelectedResources(new Set());
      setScanBusy("");
      setScanError("");
      setResourceFilter("");
    }
  }, [open]);

  // Clusters are loaded once the namespace tab is opened, not on every mount.
  useEffect(() => {
    if (!open || mode !== "namespace" || clusters.length) return;
    let cancelled = false;
    listClusters()
      .then((result) => {
        if (!cancelled) setClusters(result?.items || []);
      })
      .catch((err) => {
        if (!cancelled) setScanError(err.message || "Unable to load clusters.");
      });
    return () => {
      cancelled = true;
    };
  }, [open, mode, clusters.length]);

  if (!open) return null;

  const versionMode = Boolean(targetTemplate?.id);
  const activeMode = versionMode ? "archive" : mode;

  const updateGitConnectionField = (field, value) => {
    setGit((previous) => ({
      ...previous,
      [field]: value,
      ref: "",
      path: "",
    }));
    setGitRefs([]);
    setGitPaths([]);
    setGitPathsTruncated(false);
    setGitDiscoveryError("");
  };

  const loadGitPaths = async (ref, connection = git) => {
    setGitDiscoveryBusy("paths");
    setGitDiscoveryError("");
    setGitPaths([]);
    setGitPathsTruncated(false);
    try {
      const result = await discoverHelmGitPaths({ ...connection, ref });
      setGitPaths(result.paths || []);
      setGitPathsTruncated(Boolean(result.truncated));
    } catch (err) {
      setGitDiscoveryError(err.message || "Unable to load repository paths.");
    } finally {
      setGitDiscoveryBusy("");
    }
  };

  const loadGitRepository = async () => {
    if (!git.repositoryUrl.trim()) {
      setGitDiscoveryError("Enter the HTTPS repository URL first.");
      return;
    }
    setGitDiscoveryBusy("refs");
    setGitDiscoveryError("");
    setGitRefs([]);
    setGitPaths([]);
    setGitPathsTruncated(false);
    try {
      const result = await discoverHelmGitRefs(git);
      const refs = result.refs || [];
      const selectedRef =
        result.defaultRef && refs.some((item) => item.name === result.defaultRef)
          ? result.defaultRef
          : refs[0]?.name || "";
      setGitRefs(refs);
      setGit((previous) => ({ ...previous, ref: selectedRef, path: "" }));
      if (selectedRef) {
        await loadGitPaths(selectedRef, git);
      }
    } catch (err) {
      setGitDiscoveryError(err.message || "Unable to load repository branches and tags.");
      setGitDiscoveryBusy("");
    }
  };

  const selectSourceCluster = async (clusterId) => {
    setSource({ clusterId, namespace: "" });
    setNamespaces([]);
    setScan(null);
    setSelectedResources(new Set());
    setScanError("");
    if (!clusterId) return;
    setScanBusy("namespaces");
    try {
      const result = await listNamespacesByCluster(clusterId, { lite: true });
      setNamespaces((result?.items || []).map((item) => item.name || item).filter(Boolean));
    } catch (err) {
      setScanError(err.message || "Unable to load namespaces for this cluster.");
    } finally {
      setScanBusy("");
    }
  };

  const scanNamespace = async () => {
    if (!source.clusterId || !source.namespace) {
      setScanError("Choose a cluster and a namespace first.");
      return;
    }
    setScanBusy("scan");
    setScanError("");
    setScan(null);
    try {
      const result = await discoverNamespaceResources(source);
      setScan(result);
      // Everything importable starts ticked; the operator unticks what should
      // stay out of the chart.
      setSelectedResources(
        new Set(
          (result?.resources || [])
            .filter((resource) => !resource.skipped)
            .map((resource) => resourceKey(resource)),
        ),
      );
    } catch (err) {
      setScanError(err.message || "Unable to read this namespace.");
    } finally {
      setScanBusy("");
    }
  };

  const toggleResource = (resource) => {
    setSelectedResources((previous) => {
      const next = new Set(previous);
      const key = resourceKey(resource);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const importableResources = (scan?.resources || []).filter((resource) => !resource.skipped);
  const skippedResources = (scan?.resources || []).filter((resource) => resource.skipped);
  // A busy namespace is imported one application at a time, so every bulk
  // action works on what the filter currently shows, not on all 128 objects.
  const filterTerm = resourceFilter.trim().toLowerCase();
  const visibleResources = filterTerm
    ? importableResources.filter((resource) =>
        `${resource.kind}/${resource.name}`.toLowerCase().includes(filterTerm),
      )
    : importableResources;
  const allVisibleSelected =
    visibleResources.length > 0 &&
    visibleResources.every((resource) => selectedResources.has(resourceKey(resource)));

  const setSelection = (resources, selected) => {
    setSelectedResources((previous) => {
      const next = new Set(previous);
      resources.forEach((resource) => {
        if (selected) next.add(resourceKey(resource));
        else next.delete(resourceKey(resource));
      });
      return next;
    });
  };

  // The backend already orders resources workloads-first; grouping keeps that
  // order while making a 128-object namespace readable.
  const groupedResources = visibleResources.reduce((groups, resource) => {
    const bucket = groups.get(resource.kind) || [];
    bucket.push(resource);
    groups.set(resource.kind, bucket);
    return groups;
  }, new Map());

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
      } else if (activeMode === "namespace") {
        if (!scan) {
          throw new Error("Scan the namespace first, then choose what to import.");
        }
        const chosen = importableResources.filter((resource) =>
          selectedResources.has(resourceKey(resource)),
        );
        if (!chosen.length) {
          throw new Error("Select at least one resource to import.");
        }
        result = await importHelmChartFromNamespace({
          name,
          description,
          ...source,
          resources: chosen.map((resource) => ({
            kind: resource.kind,
            name: resource.name,
          })),
        });
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
                  : "Save a reusable chart from Kubernetes manifests, a chart archive, Git, or a live namespace."}
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
                <button
                  type="button"
                  className={mode === "namespace" ? "active" : ""}
                  onClick={() => {
                    setMode("namespace");
                    setError("");
                  }}
                >
                  Cluster Namespace
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
              ) : activeMode === "namespace" ? (
                <>
                  <div className="helm-form-grid">
                    <label>
                      Cluster
                      <SearchableSelect
                        value={source.clusterId}
                        placeholder="Select a cluster"
                        searchPlaceholder="Search clusters…"
                        onChange={(event) => selectSourceCluster(event.target.value)}
                        options={clusters.map((item) => ({
                          value: item.id,
                          label: item.name || item.id,
                        }))}
                      />
                    </label>
                    <label>
                      Namespace
                      <SearchableSelect
                        value={source.namespace}
                        disabled={!namespaces.length || scanBusy === "namespaces"}
                        placeholder={
                          scanBusy === "namespaces"
                            ? "Loading namespaces…"
                            : source.clusterId
                              ? "Select a namespace"
                              : "Select a cluster first"
                        }
                        searchPlaceholder="Search namespaces…"
                        onChange={(event) => {
                          const namespace = event.target.value;
                          setSource((previous) => ({ ...previous, namespace }));
                          setScan(null);
                          setSelectedResources(new Set());
                        }}
                        options={namespaces.map((item) => ({ value: item, label: item }))}
                      />
                    </label>
                  </div>
                  <div className="helm-git-discovery">
                    <button
                      type="button"
                      className="btn-outline"
                      disabled={Boolean(scanBusy) || !source.clusterId || !source.namespace}
                      onClick={scanNamespace}
                    >
                      {scanBusy === "scan"
                        ? "Reading namespace…"
                        : scan
                          ? "Rescan namespace"
                          : "Scan namespace"}
                    </button>
                    {scan ? (
                      <span className="muted">
                        {importableResources.length} importable object
                        {importableResources.length === 1 ? "" : "s"}
                        {skippedResources.length
                          ? ` · ${skippedResources.length} cluster-managed skipped`
                          : ""}
                      </span>
                    ) : null}
                  </div>
                  {scanError ? <p className="banner-message error">{scanError}</p> : null}
                  {(scan?.warnings || []).map((warning, index) => (
                    <p className="form-help" key={`scan-warning-${index}`}>
                      {warning}
                    </p>
                  ))}

                  {scan && importableResources.length ? (
                    <div className="helm-namespace-results">
                      <header>
                        <span>
                          {selectedResources.size} of {importableResources.length} selected
                        </span>
                        <input
                          className="helm-namespace-filter"
                          type="search"
                          value={resourceFilter}
                          placeholder="Filter by name or kind…"
                          onChange={(event) => setResourceFilter(event.target.value)}
                        />
                        <button
                          type="button"
                          className="btn-text"
                          disabled={!visibleResources.length}
                          onClick={() => setSelection(visibleResources, !allVisibleSelected)}
                        >
                          {allVisibleSelected ? "Clear" : "Select"}
                          {filterTerm ? " shown" : " all"}
                        </button>
                      </header>
                      {filterTerm && !visibleResources.length ? (
                        <p className="helm-namespace-empty muted">
                          No importable object matches “{resourceFilter}”.
                        </p>
                      ) : null}
                      <ul className="helm-namespace-groups">
                        {Array.from(groupedResources.entries()).map(([kind, items]) => (
                          <li key={kind}>
                            <p className="helm-namespace-kind">
                              {kind} <span className="muted">({items.length})</span>
                              <button
                                type="button"
                                className="btn-text"
                                onClick={() =>
                                  setSelection(
                                    items,
                                    !items.every((item) =>
                                      selectedResources.has(resourceKey(item)),
                                    ),
                                  )
                                }
                              >
                                {items.every((item) => selectedResources.has(resourceKey(item)))
                                  ? "none"
                                  : "all"}
                              </button>
                            </p>
                            <ul>
                              {items.map((resource) => (
                                <li key={resourceKey(resource)}>
                                  <label className="helm-namespace-item">
                                    <input
                                      type="checkbox"
                                      checked={selectedResources.has(resourceKey(resource))}
                                      onChange={() => toggleResource(resource)}
                                    />
                                    <span>{resource.name}</span>
                                  </label>
                                </li>
                              ))}
                            </ul>
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : null}

                  {skippedResources.length ? (
                    <details className="helm-namespace-skipped">
                      <summary>
                        {skippedResources.length} object
                        {skippedResources.length === 1 ? "" : "s"} left out
                      </summary>
                      <ul>
                        {skippedResources.map((resource) => (
                          <li key={resourceKey(resource)}>
                            <code>
                              {resource.kind}/{resource.name}
                            </code>{" "}
                            <span className="muted">· {resource.skipped}</span>
                          </li>
                        ))}
                      </ul>
                    </details>
                  ) : null}

                  <p className="form-help">
                    Live objects are stripped of everything the cluster generated — status,
                    UIDs, revisions, cluster IPs and node ports — before becoming templates.
                    Images, replicas, env values, ports, hosts and storage sizes turn into
                    configurable chart values. Secret keys are kept but their values are never
                    read into the chart: each one becomes a required field to fill at deploy
                    time.
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
                        updateGitConnectionField("repositoryUrl", event.target.value)
                      }
                      placeholder="https://github.com/organization/repository.git"
                    />
                  </label>
                  <div className="helm-form-grid">
                    <label>
                      Git username <span className="muted">(private repositories)</span>
                      <input
                        autoComplete="username"
                        value={git.username}
                        onChange={(event) =>
                          updateGitConnectionField("username", event.target.value)
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
                          updateGitConnectionField("token", event.target.value)
                        }
                      />
                    </label>
                  </div>
                  <div className="helm-git-discovery">
                    <button
                      type="button"
                      className="btn-outline"
                      disabled={Boolean(gitDiscoveryBusy) || !git.repositoryUrl.trim()}
                      onClick={loadGitRepository}
                    >
                      {gitDiscoveryBusy === "refs"
                        ? "Loading branches & tags…"
                        : gitDiscoveryBusy === "paths"
                          ? "Loading repository paths…"
                          : gitRefs.length
                            ? "Reload repository"
                            : "Connect & load repository"}
                    </button>
                    {gitRefs.length ? (
                      <span className="muted">
                        {gitRefs.length} branch/tag reference
                        {gitRefs.length === 1 ? "" : "s"} found
                      </span>
                    ) : null}
                  </div>
                  {gitDiscoveryError ? (
                    <p className="banner-message error">{gitDiscoveryError}</p>
                  ) : null}
                  <div className="helm-form-grid">
                    <label>
                      Branch or tag
                      <SearchableSelect
                        value={git.ref}
                        disabled={!gitRefs.length || Boolean(gitDiscoveryBusy)}
                        placeholder={
                          gitDiscoveryBusy === "refs"
                            ? "Loading branches and tags…"
                            : "Connect to load branches and tags"
                        }
                        searchPlaceholder="Search branches and tags…"
                        onChange={(event) => {
                          const ref = event.target.value;
                          setGit((previous) => ({ ...previous, ref, path: "" }));
                          loadGitPaths(ref);
                        }}
                        options={gitRefs.map((item) => ({
                          value: item.name,
                          label: item.label,
                        }))}
                      />
                    </label>
                    <label>
                      Repository path
                      <SearchableSelect
                        value={git.path}
                        disabled={!gitPaths.length || Boolean(gitDiscoveryBusy)}
                        placeholder={
                          gitDiscoveryBusy === "paths"
                            ? "Loading repository paths…"
                            : "Select a repository path"
                        }
                        searchPlaceholder="Search repository paths…"
                        onChange={(event) =>
                          setGit((previous) => ({ ...previous, path: event.target.value }))
                        }
                        options={gitPaths.map((item) => ({
                          value: item.path,
                          label: item.label,
                        }))}
                      />
                    </label>
                  </div>
                  {gitPathsTruncated ? (
                    <p className="form-help">
                      Showing the first 2,000 repository directories. Narrow the repository if the
                      required path is not listed.
                    </p>
                  ) : null}
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
                  <p className="form-help">
                    Credentials are sent only for repository requests, never saved in KubeSight,
                    and cleared when the import completes or this dialog closes.
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
