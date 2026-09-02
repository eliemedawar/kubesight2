import { useCallback, useEffect, useState } from "react";
import { listCiRunners, updateCiRunner } from "../../api/ciApi.js";
import { RUNNER_TYPES, StatusPill } from "./ciShared.jsx";

/**
 * The build fleet — where an operator turns a runner on.
 *
 * The Kubernetes runner ships disabled on purpose: it only works once
 * k8s/ci-runner.yaml is applied to the cluster. Somebody then has to say "the
 * cluster is ready" and there was previously no way to say it outside the API.
 *
 * Each row answers one question — will this runner take my build? — so the
 * reason it won't is stated on the row rather than left to be inferred from a
 * status word. Builtin runners derive their status from whether an adapter is
 * registered (scheduler.sync_builtin_runner_statuses), which is why enabling is
 * the only control they expose.
 */

const typeLabel = (value) =>
  RUNNER_TYPES.find((entry) => entry.value === value)?.label || value || "—";

/**
 * Why this runner would refuse work right now, or "" when it would take it.
 * Ordered by what an operator should fix first.
 */
function blockedReason(runner) {
  if (!runner.enabled) {
    return "Disabled — the engine will never assign a build here.";
  }
  if (runner.status === "offline") {
    return runner.isBuiltin
      ? "No adapter registered in this backend — builds cannot be dispatched."
      : "Offline — the agent has not checked in.";
  }
  if (runner.status === "draining") {
    return "Draining — finishing its current builds, taking no new ones.";
  }
  if (runner.maxConcurrent && runner.currentLoad >= runner.maxConcurrent) {
    return "At capacity — new builds queue until a slot frees.";
  }
  return "";
}

export default function RunnersModal({ canManage, onClose }) {
  const [runners, setRunners] = useState([]);
  const [meta, setMeta] = useState({ queueDepth: 0, eligible: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState(null);

  const load = useCallback(async () => {
    try {
      const data = await listCiRunners();
      setRunners(data.items || []);
      setMeta({ queueDepth: data.queueDepth || 0, eligible: data.eligible || 0 });
      setError("");
    } catch (err) {
      setError(err.message || "Could not load the runners.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Reload rather than patch in place: enabling a runner also moves the
  // eligible count and a builtin row's derived status.
  const save = async (runner, payload) => {
    setBusyId(runner.id);
    setError("");
    try {
      await updateCiRunner(runner.id, payload);
      await load();
    } catch (err) {
      setError(err.message || "Could not update the runner.");
    } finally {
      setBusyId(null);
    }
  };

  const summary = loading
    ? "Reading the fleet…"
    : `${meta.eligible} runner${meta.eligible === 1 ? "" : "s"} can take work` +
      (meta.queueDepth ? ` · ${meta.queueDepth} build(s) waiting` : "");

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal-card sg-ci-runners"
        role="dialog"
        aria-label="Build runners"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-card__header">
          <h3>Build runners</h3>
          <p className="muted">{summary}</p>
        </div>

        {error && <p className="banner-message error">{error}</p>}

        {loading ? (
          <p className="muted sg-ci-run-note">Loading…</p>
        ) : runners.length === 0 ? (
          <p className="muted sg-ci-run-note">No runners registered.</p>
        ) : (
          <ul className="sg-ci-runner-list">
            {runners.map((runner) => {
              const blocked = blockedReason(runner);
              const busy = busyId === runner.id;
              return (
                <li
                  key={runner.id}
                  className={`sg-ci-runner${runner.enabled ? "" : " is-off"}`}
                >
                  <div className="sg-ci-runner-head">
                    <span className="sg-ci-runner-name">{runner.name}</span>
                    <StatusPill status={runner.status} />
                    {runner.isBuiltin && <span className="chip">built-in</span>}
                    <span className="sg-ci-runner-type">{typeLabel(runner.runnerType)}</span>
                  </div>

                  {runner.description && (
                    <p className="sg-ci-runner-desc muted">{runner.description}</p>
                  )}

                  {blocked ? (
                    <p className="sg-ci-runner-why">{blocked}</p>
                  ) : (
                    <p className="sg-ci-runner-why is-ok">
                      Ready — {runner.currentLoad} of {runner.maxConcurrent} slots in use.
                    </p>
                  )}

                  {runner.lastError && (
                    <p className="sg-ci-runner-why">Last error: {runner.lastError}</p>
                  )}

                  {runner.capabilities?.length > 0 && (
                    <div className="sg-ci-runner-caps">
                      {runner.capabilities.map((cap) => (
                        <span key={cap} className="chip">
                          {cap}
                        </span>
                      ))}
                    </div>
                  )}

                  {canManage && (
                    <div className="sg-ci-runner-actions">
                      <button
                        type="button"
                        className={runner.enabled ? "btn-outline" : "primary"}
                        disabled={busy}
                        aria-pressed={runner.enabled}
                        onClick={() => save(runner, { enabled: !runner.enabled })}
                      >
                        {busy ? "Saving…" : runner.enabled ? "Disable" : "Enable"}
                      </button>
                      <label className="sg-ci-runner-slots">
                        Max concurrent
                        <input
                          type="number"
                          min="1"
                          max="100"
                          defaultValue={runner.maxConcurrent}
                          disabled={busy}
                          aria-label={`Maximum concurrent builds for ${runner.name}`}
                          onBlur={(event) => {
                            const next = Number(event.target.value);
                            if (!Number.isFinite(next) || next === runner.maxConcurrent) return;
                            save(runner, { maxConcurrent: next });
                          }}
                        />
                      </label>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
