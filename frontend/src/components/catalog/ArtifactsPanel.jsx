import { useCallback, useEffect, useState } from "react";
import {
  ciArtifactDownloadPath,
  deleteCiArtifact,
  getCiArtifactPolicy,
  listCiServiceArtifacts,
  purgeCiArtifacts,
} from "../../api/ciApi.js";
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
 *
 * Artifacts are also the one part of CI that grows without bound, so the
 * retention rule in force is stated here rather than left in a ConfigMap, next
 * to the manual cleanups. Deleting is per artifact or in bulk; container images
 * are excluded from both, because their bytes live in a registry and the row is
 * the only record of what was built.
 */
export default function ArtifactsPanel({
  service,
  canDeploy,
  canManage,
  onDeploy,
  refreshToken,
}) {
  const [artifacts, setArtifacts] = useState([]);
  const [policy, setPolicy] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    const [rows, rules] = await Promise.all([
      listCiServiceArtifacts(service.id, { limit: 100 }),
      // The policy is informational: a failure here must not hide the list.
      getCiArtifactPolicy(service.id).catch(() => null),
    ]);
    setArtifacts(rows.items || []);
    setPolicy(rules);
    setError("");
  }, [service.id]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    load()
      .catch((err) => {
        if (!cancelled) setError(err.message || "Could not load artifacts.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [load, refreshToken]);

  const run = async (key, action, describe) => {
    setBusy(key);
    setError("");
    setNotice("");
    try {
      const result = await action();
      await load();
      setNotice(describe(result));
    } catch (err) {
      setError(err.message || "That did not work.");
    } finally {
      setBusy("");
    }
  };

  const cleaned = (result) =>
    result.deleted === 0
      ? "Nothing to clean — no artifact was old enough."
      : `Removed ${result.deleted} artifact${result.deleted === 1 ? "" : "s"}, freeing ${formatBytes(
          result.freedBytes
        )}${result.keptRecent ? ` (kept ${result.keptRecent} from the newest build)` : ""}.`;

  if (loading) return <LoadingState label="Loading artifacts…" />;

  const days = policy?.retentionDays ?? 0;

  return (
    <div className="sg-ci-panel">
      {error && <p className="banner-message error">{error}</p>}
      {notice && <p className="banner-message">{notice}</p>}

      {policy && (
        <div className="sg-ci-retention">
          <p className="muted sg-ci-retention-rule">
            {days > 0 ? (
              <>
                Stored artifacts are removed after{" "}
                <strong>
                  {days} day{days === 1 ? "" : "s"}
                </strong>
                {policy.autoclean ? ", swept automatically" : " (automatic sweep off)"}.
                {policy.keepLastBuilds > 0 &&
                  ` The newest ${
                    policy.keepLastBuilds === 1 ? "build's" : `${policy.keepLastBuilds} builds'`
                  } artifacts are always kept, so there is always something to download.`}
              </>
            ) : (
              <>Artifacts are kept indefinitely — nothing expires on its own.</>
            )}{" "}
            Holding {policy.usage?.count || 0} file
            {(policy.usage?.count || 0) === 1 ? "" : "s"} ·{" "}
            {formatBytes(policy.usage?.bytes || 0)}
            {policy.registryOnly > 0 &&
              ` · ${policy.registryOnly} container image${
                policy.registryOnly === 1 ? "" : "s"
              } in the registry, not counted`}
            .
          </p>

          {canManage && (policy.usage?.count || 0) > 0 && (
            <div className="sg-ci-retention-actions">
              {days > 0 && (
                <button
                  type="button"
                  className="btn-outline btn-compact"
                  disabled={Boolean(busy)}
                  onClick={() =>
                    run("expire", () => purgeCiArtifacts({ serviceId: service.id }), cleaned)
                  }
                >
                  {busy === "expire" ? "Cleaning…" : `Clean older than ${days} day${
                    days === 1 ? "" : "s"
                  }`}
                </button>
              )}
              <button
                type="button"
                className="btn-outline btn-compact danger"
                disabled={Boolean(busy)}
                onClick={() => {
                  if (
                    !window.confirm(
                      `Delete every stored artifact for ${service.name}? Downloads stop ` +
                        "working for those builds. Container images in the registry are " +
                        "not affected."
                    )
                  )
                    return;
                  // keepLast 0 with no age bound is the only way to say "all of
                  // it" — the guard that protects the newest build is otherwise
                  // exactly what would leave files behind here.
                  run(
                    "all",
                    () =>
                      purgeCiArtifacts({
                        serviceId: service.id,
                        olderThanDays: 0,
                        keepLast: 0,
                      }),
                    cleaned
                  );
                }}
              >
                {busy === "all" ? "Deleting…" : "Delete all"}
              </button>
            </div>
          )}
        </div>
      )}

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
                    {canManage && (
                      <button
                        type="button"
                        className="btn-outline btn-compact danger"
                        disabled={Boolean(busy)}
                        title={
                          artifact.downloadable
                            ? "Delete this file"
                            : "Remove this record — the image stays in its registry"
                        }
                        onClick={() => {
                          if (
                            !window.confirm(
                              `Delete "${artifact.name}"?` +
                                (artifact.downloadable
                                  ? " The file is removed and can no longer be downloaded."
                                  : " The image itself stays in its registry; only KubeSight's record goes.")
                            )
                          )
                            return;
                          run(
                            `row-${artifact.id}`,
                            () => deleteCiArtifact(artifact.id),
                            () => `Deleted ${artifact.name}.`
                          );
                        }}
                      >
                        {busy === `row-${artifact.id}` ? "Deleting…" : "Delete"}
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
