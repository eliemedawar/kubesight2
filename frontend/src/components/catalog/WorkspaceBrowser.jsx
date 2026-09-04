import { useCallback, useEffect, useState } from "react";
import { getCiBuildWorkspace } from "../../api/ciApi.js";
import { formatBytes } from "./ciShared.jsx";

/**
 * The live build workspace, one directory at a time.
 *
 * Only exists while a stage is running: the workspace is an emptyDir that goes
 * away with the build pod. When it cannot be read the backend says why, and
 * that reason is shown as-is rather than being flattened into "empty" — an
 * empty directory and a vanished pod are very different answers.
 *
 * Listings only. File contents are deliberately not served: a workspace often
 * holds credentials a stage wrote for itself.
 */
export default function WorkspaceBrowser({ buildId, active }) {
  const [path, setPath] = useState("/workspace");
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(
    async (target) => {
      setLoading(true);
      try {
        const result = await getCiBuildWorkspace(buildId, target);
        setData(result);
        setPath(result.path);
        setError("");
      } catch (err) {
        setError(err.message || "The workspace could not be read.");
        setData(null);
      } finally {
        setLoading(false);
      }
    },
    [buildId]
  );

  useEffect(() => {
    load("/workspace");
  }, [load]);

  const entries = data?.entries || [];

  return (
    <section className="sg-ci-workspace">
      <div className="sg-ci-workspace-bar">
        <code className="sg-ci-workspace-path">{path}</code>
        <div className="sg-ci-workspace-actions">
          {data?.parent && (
            <button type="button" className="btn-outline btn-compact" onClick={() => load(data.parent)}>
              Up
            </button>
          )}
          <button
            type="button"
            className="btn-outline btn-compact"
            disabled={loading}
            onClick={() => load(path)}
          >
            {loading ? "Reading…" : "Refresh"}
          </button>
        </div>
      </div>

      {data?.stage && (
        <p className="muted sg-ci-workspace-note">
          Read from the running stage “{data.stage.name}”. Every stage shares this
          directory, so this is what the next stage will see.
        </p>
      )}

      {error ? (
        <p className="muted sg-ci-workspace-note">{error}</p>
      ) : loading && !data ? (
        <p className="muted sg-ci-workspace-note">Reading the workspace…</p>
      ) : entries.length === 0 ? (
        <p className="muted sg-ci-workspace-note">This directory is empty.</p>
      ) : (
        <ul className="sg-ci-workspace-list">
          {entries.map((entry) => (
            <li key={entry.name}>
              {entry.type === "dir" ? (
                <button
                  type="button"
                  className="sg-ci-workspace-entry is-dir"
                  onClick={() => load(`${path === "/" ? "" : path}/${entry.name}`)}
                >
                  <span className="sg-ci-workspace-name">{entry.name}/</span>
                </button>
              ) : (
                <div className="sg-ci-workspace-entry">
                  <span className="sg-ci-workspace-name">{entry.name}</span>
                  {/* Size is the point for a build: a 0-byte jar is a failure
                      that otherwise only shows up much later. */}
                  <span className={`sg-ci-workspace-size${entry.size === 0 ? " is-empty" : ""}`}>
                    {formatBytes(entry.size)}
                  </span>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {!active && !error && (
        <p className="muted sg-ci-workspace-note">
          This build has finished — its workspace was removed with the build pod.
        </p>
      )}
    </section>
  );
}
