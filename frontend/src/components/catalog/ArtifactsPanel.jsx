import { useEffect, useState } from "react";
import { ciArtifactDownloadPath, listCiServiceArtifacts } from "../../api/ciApi.js";
import { getBaseUrl } from "../../api/client.js";
import EmptyState from "../common/EmptyState.jsx";
import LoadingState from "../common/LoadingState.jsx";
import { formatBytes, formatRelative, shortSha } from "./ciShared.jsx";

/**
 * Artifacts tab.
 *
 * A container image gets a Deploy action that hands its exact reference to the
 * existing KubeSight deploy flow — CI never deploys anything itself, so the
 * button is gated on the deploy permission, not on any CI permission.
 */
export default function ArtifactsPanel({ service, canDeploy, onDeploy, refreshToken }) {
  const [artifacts, setArtifacts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    listCiServiceArtifacts(service.id, { limit: 100 })
      .then((data) => {
        if (!cancelled) {
          setArtifacts(data.items || []);
          setError("");
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || "Could not load artifacts.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [service.id, refreshToken]);

  if (loading) return <LoadingState label="Loading artifacts…" />;

  return (
    <div className="sg-ci-panel">
      {error && <p className="banner-message error">{error}</p>}

      {artifacts.length === 0 ? (
        <EmptyState
          message="No artifacts yet."
          hint="Artifacts appear here once a build produces one — a jar, an image, a report."
        />
      ) : (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Artifact</th>
                <th>Type</th>
                <th>Version</th>
                <th>Commit</th>
                <th>Size</th>
                <th>Created</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {artifacts.map((artifact) => (
                <tr key={artifact.id}>
                  <td>
                    <strong>{artifact.name}</strong>
                    {artifact.uri && (
                      <div className="sg-ci-artifact-uri">
                        <code>{artifact.uri}</code>
                      </div>
                    )}
                    {artifact.digest && (
                      <div className="field-hint">
                        <code>{artifact.digest}</code>
                      </div>
                    )}
                  </td>
                  <td>
                    <span className="chip">{artifact.artifactType}</span>
                  </td>
                  <td>{artifact.version || "—"}</td>
                  <td>
                    <code>{shortSha(artifact.commitSha)}</code>
                    {artifact.branch && <div className="field-hint">{artifact.branch}</div>}
                  </td>
                  <td>{formatBytes(artifact.sizeBytes)}</td>
                  <td>{formatRelative(artifact.createdAt)}</td>
                  <td className="table-actions-cell">
                    {artifact.downloadable && (
                      <a
                        className="btn-outline btn-compact"
                        href={`${getBaseUrl()}${ciArtifactDownloadPath(artifact.id)}`}
                        download
                      >
                        Download
                      </a>
                    )}
                    {artifact.deployable && canDeploy && (
                      <button
                        type="button"
                        className="primary btn-compact"
                        onClick={() => onDeploy(artifact)}
                      >
                        Deploy
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
