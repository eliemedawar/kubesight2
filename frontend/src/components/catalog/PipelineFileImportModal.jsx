import { useEffect, useMemo, useState } from "react";
import { importCiPipeline, previewCiPipelineImport } from "../../api/ciApi.js";
import { listClusters } from "../../api/clustersApi.js";
import SearchableSelect from "../common/SearchableSelect.jsx";
import YamlFileButton from "../common/YamlFileButton.jsx";
import { CheckIcon, formatRelative, useEscapeToClose } from "./ciShared.jsx";

/**
 * Import a pipeline exported from another KubeSight (UAT → prod) onto this one.
 *
 * Read the file, then answer what cannot travel between installations: which
 * cluster here each Deploy stage goes to, and the values of the secrets this
 * service does not have yet. Unlike a Jenkinsfile draft this saves at once —
 * the secrets are created with it, in one step that either all happens or
 * none of it does.
 */
export default function PipelineFileImportModal({ pipeline, service, onImported, onClose }) {
  const [content, setContent] = useState("");
  const [fileName, setFileName] = useState("");
  const [preview, setPreview] = useState(null);
  const [clusters, setClusters] = useState([]);
  const [clusterMap, setClusterMap] = useState({});
  const [secretValues, setSecretValues] = useState({});
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");

  useEscapeToClose(onClose, !working);

  useEffect(() => {
    let cancelled = false;
    listClusters()
      .then((result) => !cancelled && setClusters(result?.items || []))
      .catch(() => !cancelled && setClusters([]));
    return () => {
      cancelled = true;
    };
  }, []);

  const read = async (text, name) => {
    setContent(text);
    setFileName(name || "");
    setPreview(null);
    setError("");
    setWorking(true);
    try {
      const data = await previewCiPipelineImport(pipeline.id, text);
      setPreview(data);
      setSecretValues({});
      // The same cluster id here, or else one with the same name.
      setClusterMap(
        Object.fromEntries(
          (data.clusters || []).map((item) => {
            const match =
              clusters.find((cluster) => cluster.id === item.id) ||
              clusters.find((cluster) => (cluster.name || "").toLowerCase() === (item.name || "").toLowerCase());
            return [item.id, match?.id || ""];
          })
        )
      );
    } catch (err) {
      setError(err.message || "The file could not be read.");
    } finally {
      setWorking(false);
    }
  };

  const missingSecrets = useMemo(() => (preview?.secrets || []).filter((item) => item.missing), [preview]);
  const unmapped = (preview?.clusters || []).filter((item) => !clusterMap[item.id]);
  const emptySecrets = missingSecrets.filter((item) => !String(secretValues[item.name] || "").trim());

  const submit = async () => {
    setWorking(true);
    setError("");
    try {
      const result = await importCiPipeline(pipeline.id, { content, clusterMap, secretValues });
      onImported(result);
    } catch (err) {
      setError(err.message || "The import was refused.");
      setWorking(false);
    }
  };

  const source = preview?.source || {};
  const shared = preview?.sharedPipeline;

  return (
    <div className="modal-backdrop" role="presentation" onClick={() => !working && onClose()}>
      <div
        className="modal-card sg-ci-import sg-ci-transfer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="ci-transfer-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-card__header">
          <h3 id="ci-transfer-title">Import a pipeline file</h3>
          <p className="muted">
            A file exported from another KubeSight — from UAT to prod, say. It replaces the stages, build
            inputs and post actions of {service.name}&apos;s <strong>{pipeline.name}</strong> pipeline.
          </p>
        </div>

        <div className="sg-ci-transfer-pick">
          <YamlFileButton className="btn-outline btn-compact" disabled={working} onLoad={read} onError={setError}>
            {fileName ? "Choose another file" : "Choose the .pipeline.yaml file"}
          </YamlFileButton>
          {fileName && <code>{fileName}</code>}
          {working && !preview && <span className="muted">Reading…</span>}
        </div>

        {error && (
          <p className="banner-message error" role="alert">
            {error}
          </p>
        )}

        {preview && (
          <section className="sg-ci-import-review" aria-live="polite">
            <p className="sg-ci-import-summary">
              From <strong>{source.serviceName || source.service || "a service"}</strong>
              {source.pipeline ? <> · pipeline {source.pipeline}</> : null}
              {source.version ? <> · version {source.version}</> : null}
              {preview.exportedAt ? <> · exported {formatRelative(preview.exportedAt)}</> : null}
              {preview.exportedBy ? <> by {preview.exportedBy}</> : null}
            </p>

            {shared?.found && (
              <p className="sg-ci-transfer-note">
                <CheckIcon /> It builds with the shared pipeline <strong>{shared.name}</strong>, which is on this
                KubeSight too — {service.name} will build with that one.
              </p>
            )}

            {(preview.warnings || []).length > 0 && (
              <ul className="sg-ci-import-notes">
                {preview.warnings.map((warning) => (
                  <li key={warning} className="is-warning">
                    <span className="sg-ci-import-note-level">Check</span>
                    <span>{warning}</span>
                  </li>
                ))}
              </ul>
            )}

            {(preview.clusters || []).length > 0 && (
              <div className="sg-ci-import-group">
                <h4>Where its Deploy stages go here</h4>
                <ul className="sg-ci-transfer-rows">
                  {preview.clusters.map((item) => (
                    <li key={item.id}>
                      <span>
                        <strong>{item.name}</strong>
                        {item.name !== item.id ? <small> ({item.id})</small> : null}
                        <span aria-hidden="true"> →</span>
                      </span>
                      <SearchableSelect
                        aria-label={`Cluster here for ${item.name}`}
                        value={clusterMap[item.id] || ""}
                        placeholder="Pick a cluster…"
                        options={clusters.map((cluster) => ({ value: cluster.id, label: cluster.name || cluster.id }))}
                        onChange={(event) => setClusterMap((current) => ({ ...current, [item.id]: event.target.value }))}
                      />
                    </li>
                  ))}
                </ul>
                <p className="muted">Namespaces, deployments and releases are kept as they were in the file.</p>
              </div>
            )}

            {(preview.secrets || []).length > 0 && (
              <div className="sg-ci-import-group">
                <h4>Secrets it uses</h4>
                <ul className="sg-ci-transfer-rows">
                  {preview.secrets.map((item) => (
                    <li key={item.name}>
                      <code>{item.name}</code>
                      {item.missing ? (
                        <input
                          type="password"
                          autoComplete="new-password"
                          aria-label={`Value of ${item.name}`}
                          placeholder="Its value here — or leave empty to set it later"
                          value={secretValues[item.name] || ""}
                          onChange={(event) =>
                            setSecretValues((current) => ({ ...current, [item.name]: event.target.value }))
                          }
                        />
                      ) : (
                        <span className="is-ready">
                          <CheckIcon /> already set here ({item.scope === "global" ? "global" : "this service"})
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
                <p className="muted">Values never travel in the file. Missing ones are created on {service.name}.</p>
              </div>
            )}
            {!shared?.found && (
              <details className="sg-ci-import-group sg-ci-transfer-stages">
                <summary>
                  {preview.stages.length} stage{preview.stages.length === 1 ? "" : "s"}
                  {preview.parameterCount ? ` · ${preview.parameterCount} build inputs` : ""}
                  {preview.postActionCount ? ` · ${preview.postActionCount} post actions` : ""}
                  <span className="muted"> — replace the {preview.target?.stageCount || 0} here</span>
                </summary>
                <ol className="sg-ci-import-stages">
                  {preview.stages.map((stage, index) => (
                    <li key={`${stage.name}-${index}`}>
                      <span className="sg-ci-stage-index">{index + 1}</span>
                      <span className="sg-ci-import-stage-copy">
                        <strong>{stage.name}</strong>
                        <small>{stage.stageType.replace(/_/g, " ")}</small>
                      </span>
                    </li>
                  ))}
                </ol>
              </details>
            )}
          </section>
        )}

        <div className="modal-actions sg-ci-import-actions">
          <span className="muted">
            {preview
              ? unmapped.length
                ? `Pick the cluster for ${unmapped.map((item) => item.name).join(", ")}.`
                : `Saves immediately as version ${(preview.target?.version || 0) + 1} of ${pipeline.name}`
                  + (emptySecrets.length
                    ? `; ${emptySecrets.map((item) => item.name).join(", ")} will be created empty — set ${emptySecrets.length === 1 ? "it" : "them"} under Settings → Secrets before building.`
                    : ". Deploy stages will deploy as you.")
              : ""}
          </span>
          <button type="button" className="btn-outline" onClick={onClose} disabled={working}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            disabled={!preview || working || unmapped.length > 0}
            onClick={submit}
          >
            {working && preview ? "Importing…" : "Import and save"}
          </button>
        </div>
      </div>
    </div>
  );
}
