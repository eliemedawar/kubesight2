import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getMergeCheckEnforcement,
  getServiceMergeChecks,
  listMergeChecks,
  redeliverMergeCheck,
  revealMergeCheckSecret,
  rotateMergeCheckSecret,
  saveServiceMergeChecks,
} from "../../api/mergeChecksApi.js";
import LoadingState from "../common/LoadingState.jsx";
import QualityGateFields, { TOOLS } from "./QualityGateFields.jsx";
import { CheckIcon, StatusPill, formatRelative, shortSha } from "./ciShared.jsx";

const EVENTS = [
  ["pullrequest:created", "Pull request opened"],
  ["pullrequest:updated", "New commits pushed to it"],
  ["pullrequest:approved", "Somebody approved it"],
  ["pullrequest:fulfilled", "It was merged"],
];

// A script travels as a list of lines and edits as one blob of text; this is
// the seam between the two, named so neither direction has a bare "\n" buried
// in an expression.
const NEWLINE = "\n";

const VERDICT_LABEL = {
  allowed: "Merge allowed",
  blocked: "Merge blocked",
  unknown: "Not checked",
};

/**
 * Merge Checks tab: the gate between a pull request and a merge.
 *
 * The shape of the page follows the shape of the job. You configure the
 * webhook, choose the checks, set the number, and then watch what pull requests
 * it decided — in that order, because that is the order somebody setting this
 * up works in and the order they read it in afterwards.
 *
 * Two things are stated on the page rather than assumed, because the feature
 * silently does nothing without them and both live in Bitbucket:
 *
 *   1. the webhook has to be pointed here, with the secret;
 *   2. the branch has to REQUIRE successful builds before merging.
 *
 * KubeSight reports a verdict. Bitbucket is what enforces it — nothing here can
 * stand between a developer and the Merge button, and pretending otherwise
 * would be the most dangerous thing this panel could do.
 */
export default function MergeChecksPanel({ service, canEdit, canView = true }) {
  const [config, setConfig] = useState(null);
  const [form, setForm] = useState(null);
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [secret, setSecret] = useState("");
  const [busyCheckId, setBusyCheckId] = useState(null);
  const [showCommands, setShowCommands] = useState("");
  // Unsaved script text, per tool. Kept apart from `form` because a script is
  // saved on its own button rather than with the rest of the configuration —
  // it is long enough that losing it to an unrelated save would sting.
  const [drafts, setDrafts] = useState({});
  const [enforcement, setEnforcement] = useState(null);

  const toForm = (data) => ({
    enabled: Boolean(data.enabled),
    tools: [...(data.tools || [])],
    events: [...(data.events || [])],
    targetBranches: (data.targetBranches || []).join("\n"),
    statusKey: data.statusKey || "KUBESIGHT-MERGE",
    postComment: data.postComment !== false,
    gateMode: data.gateMode || "inherit",
    ...(data.override || {}),
  });

  const load = useCallback(async () => {
    try {
      const data = await getServiceMergeChecks(service.id);
      setConfig(data);
      setForm(toForm(data));
      // The server's scripts are the truth again; anything still in a draft
      // has either just been saved or been reset.
      setDrafts({});
      setError("");
    } catch (err) {
      setError(err.message || "Could not load the merge check configuration.");
    } finally {
      setLoading(false);
    }
  }, [service.id]);

  const loadRuns = useCallback(async () => {
    try {
      const data = await listMergeChecks(service.id, { limit: 25 });
      setRuns(data.items || []);
    } catch {
      /* The table is secondary; the configuration above still works. */
    }
  }, [service.id]);

  const loadEnforcement = useCallback(async () => {
    try {
      setEnforcement(await getMergeCheckEnforcement(service.id));
    } catch {
      // "We could not ask Bitbucket" is itself an answer the banner renders.
      setEnforcement({ known: false, enforced: false, reason: "" });
    }
  }, [service.id]);

  useEffect(() => {
    load();
    loadRuns();
    loadEnforcement();
  }, [load, loadRuns, loadEnforcement]);

  // A check that is still running settles within seconds of its build ending,
  // so the table refreshes itself while anything is in flight and stops as soon
  // as nothing is.
  const hasRunning = runs.some((run) => run.state === "queued" || run.state === "running");
  useEffect(() => {
    if (!hasRunning) return undefined;
    const timer = setInterval(loadRuns, 5000);
    return () => clearInterval(timer);
  }, [hasRunning, loadRuns]);

  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  const toggleIn = (key, value) =>
    setForm((prev) => {
      const current = new Set(prev[key] || []);
      if (current.has(value)) current.delete(value);
      else current.add(value);
      return { ...prev, [key]: [...current] };
    });

  const save = async (overrides = {}) => {
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const payload = {
        ...form,
        ...overrides,
        targetBranches: String(overrides.targetBranches ?? form.targetBranches)
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean),
      };
      const data = await saveServiceMergeChecks(service.id, payload);
      setConfig(data);
      setForm(toForm(data));
      setNotice("Saved.");
    } catch (err) {
      setError(err.message || "Could not save the merge check configuration.");
    } finally {
      setSaving(false);
    }
  };

  const reveal = async () => {
    try {
      const data = await revealMergeCheckSecret(service.id);
      setSecret(data.secret);
    } catch (err) {
      setError(err.message || "Could not read the webhook secret.");
    }
  };

  const rotate = async () => {
    if (
      !window.confirm(
        "Generate a new secret? The current one stops working immediately, and " +
          "Bitbucket will be rejected until the webhook is updated."
      )
    ) {
      return;
    }
    try {
      const data = await rotateMergeCheckSecret(service.id);
      setSecret(data.secret);
      setNotice(data.message);
      load();
    } catch (err) {
      setError(err.message || "Could not rotate the webhook secret.");
    }
  };

  const copy = (text, what) => {
    navigator.clipboard?.writeText(text).then(
      () => setNotice(`${what} copied.`),
      () => setNotice(`Select and copy the ${what.toLowerCase()} by hand.`)
    );
  };

  const redeliver = async (checkId) => {
    setBusyCheckId(checkId);
    setError("");
    try {
      await redeliverMergeCheck(checkId);
      setNotice("The verdict was sent again.");
      loadRuns();
    } catch (err) {
      setError(err.message || "The verdict could not be delivered.");
    } finally {
      setBusyCheckId(null);
    }
  };

  const saveScript = (tool, text) =>
    save({ customCommands: { [tool]: text } });

  const resetScript = (tool) => {
    setDrafts((prev) => {
      const next = { ...prev };
      delete next[tool];
      return next;
    });
    // An empty override is how the backend is told "use the generated script";
    // it never stores an empty script, because a stage with no commands would
    // fail validation rather than fall back.
    save({ customCommands: { [tool]: [] } });
  };

  const scriptsByTool = useMemo(() => {
    const map = {};
    (config?.checkScripts || []).forEach((item) => {
      map[item.tool] = item;
    });
    return map;
  }, [config]);

  if (loading || !form) {
    return <LoadingState label="Loading merge checks..." />;
  }

  const blockers = [];
  if (!config.sourceReady) {
    blockers.push("Connect a repository and credential on the Source tab.");
  } else if (!config.canReportVerdict?.ok) {
    blockers.push(config.canReportVerdict.reason);
  }
  (config.unconfiguredEnvironments || []).forEach((item) => {
    blockers.push(
      `No build image is configured for ${item.label}. Set the ${item.environment} ` +
        "image before relying on this check."
    );
  });

  return (
    <div className="sg-ci-panel">
      {error && <p className="banner-message error">{error}</p>}
      {notice && !error && <p className="banner-message ok">{notice}</p>}

      {/* ── What this does, and whether it is on ─────────────────────── */}
      <section className="form-section">
        <header className="sg-mc-head">
          <div>
            <h4>Merge checks</h4>
            <p className="muted">
              When a pull request is opened, KubeSight runs the checks below against
              its source commit and reports a verdict back to Bitbucket as a build
              status. Bitbucket blocks the merge — see the setup steps below.
            </p>
          </div>
          <label className="sg-mc-switch">
            <input
              type="checkbox"
              checked={form.enabled}
              disabled={!canEdit || saving}
              onChange={(event) => save({ enabled: event.target.checked })}
            />
            <span>{form.enabled ? "On" : "Off"}</span>
          </label>
        </header>

        {blockers.length > 0 && (
          <ul className="sg-mc-blockers">
            {blockers.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        )}
      </section>

      {/* ── The webhook ─────────────────────────────────────────────── */}
      <section className="form-section">
        <h4>Webhook</h4>
        <p className="muted">
          Add this in Bitbucket under <strong>Repository settings → Webhooks</strong>,
          with the secret in a header called <code>X-KubeSight-Secret</code>. If the
          webhook form cannot set headers, append <code>?secret=…</code> to the URL
          instead.
        </p>

        <label className="form-grid__full">
          URL
          <div className="sg-mc-copyrow">
            <input
              readOnly
              value={config.webhookUrl || config.webhookPath}
              onFocus={(event) => event.target.select()}
            />
            <button
              type="button"
              onClick={() => copy(config.webhookUrl || config.webhookPath, "URL")}
            >
              Copy
            </button>
          </div>
          {!config.webhookUrl && (
            <span className="field-hint">
              This installation has no public address configured, so only the path is
              shown. Set <code>PUBLIC_BASE_URL</code> to have the full URL here and in
              the link on every build status.
            </span>
          )}
        </label>

        <label className="form-grid__full">
          Secret
          <div className="sg-mc-copyrow">
            <input
              readOnly
              type={secret ? "text" : "password"}
              value={secret || "••••••••••••••••••••••••"}
              onFocus={(event) => event.target.select()}
            />
            {canEdit && !secret && (
              <button type="button" onClick={reveal}>
                Reveal
              </button>
            )}
            {secret && (
              <button type="button" onClick={() => copy(secret, "Secret")}>
                Copy
              </button>
            )}
            {canEdit && (
              <button type="button" className="btn-danger-ghost" onClick={rotate}>
                Rotate
              </button>
            )}
          </div>
          <span className="field-hint">
            A request without this secret is refused. Revealing it is recorded in the
            audit log.
          </span>
        </label>

        <div className="form-grid">
          <fieldset className="form-grid__full sg-mc-choices">
            <legend>Run the checks when</legend>
            {EVENTS.map(([value, label]) => (
              <label key={value} className="sg-mc-check">
                <input
                  type="checkbox"
                  checked={form.events.includes(value)}
                  disabled={!canEdit}
                  onChange={() => toggleIn("events", value)}
                />
                {label}
              </label>
            ))}
            <span className="field-hint">
              Keep “new commits pushed” on — without it a pull request is judged by
              its first commit and a fix never clears the gate.
            </span>
          </fieldset>

          <label>
            Only for merges into
            <textarea
              rows={3}
              value={form.targetBranches}
              disabled={!canEdit}
              placeholder={"main\nrelease/*"}
              onChange={(event) => set("targetBranches", event.target.value)}
            />
            <span className="field-hint">
              One pattern per line. Empty means every branch.
            </span>
          </label>

          <label>
            Build status key
            <input
              value={form.statusKey}
              disabled={!canEdit}
              onChange={(event) => set("statusKey", event.target.value)}
            />
            <span className="field-hint">
              The name Bitbucket files the status under, and the one you select in the
              branch restriction. Re-running replaces the status with this key.
            </span>
          </label>

          <label className="sg-mc-check form-grid__full">
            <input
              type="checkbox"
              checked={form.postComment}
              disabled={!canEdit}
              onChange={(event) => set("postComment", event.target.checked)}
            />
            Also comment on the pull request explaining the verdict
          </label>
        </div>
      </section>

      {/* ── The checks ──────────────────────────────────────────────── */}
      <section className="form-section">
        <h4>Checks</h4>
        <p className="muted">
          Each check runs as a stage of a pipeline KubeSight generates. The script
          is a starting point — edit it for an unusual layout and the edit is kept,
          including when the checks or the severity floors change. The one line an
          edit must keep is the <code>##kubesight-metric</code> one: without it the
          gate reads the check as “not run”, not as “clean”.
        </p>
        <div className="sg-mc-tools">
          {TOOLS.map(({ key, label, hint }) => {
            const script = scriptsByTool[key];
            const open = showCommands === key;
            // The saved script, as one editable blob, unless there is an
            // unsaved draft for this tool.
            const savedText = (script?.commands || []).join(NEWLINE);
            const draftValue = drafts[key] !== undefined ? drafts[key] : savedText;
            const edited = Boolean(script) && draftValue !== savedText;
            return (
              <div
                className={`sg-mc-tool${open ? " is-open" : ""}`}
                key={key}
              >
                <label className="sg-mc-check">
                  <input
                    type="checkbox"
                    checked={form.tools.includes(key)}
                    disabled={!canEdit}
                    onChange={() => toggleIn("tools", key)}
                  />
                  <strong>{label}</strong>
                  {script?.customized && (
                    <span className="sg-mc-tag" title="This script has been edited">
                      edited
                    </span>
                  )}
                </label>
                <p className="muted">{hint}</p>
                {script && (
                  <>
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => setShowCommands(open ? "" : key)}
                    >
                      {open ? "Hide" : canEdit ? "Edit what it runs" : "Show what it runs"}
                    </button>
                    {open && (
                      <div className="sg-mc-script">
                        <p className="field-hint">
                          Runs in <code>{script.image || "the runner's own image"}</code>{" "}
                          from the repository root, under <code>set -e</code>. Keep the{" "}
                          <code>##kubesight-metric</code> line — it is what the gate reads.
                        </p>
                        <textarea
                          className="sg-mc-commands"
                          rows={18}
                          spellCheck={false}
                          value={draftValue}
                          readOnly={!canEdit}
                          onChange={(event) =>
                            setDrafts((prev) => ({ ...prev, [key]: event.target.value }))
                          }
                        />
                        {canEdit && (
                          <div className="sg-mc-script-actions">
                            {edited && <span className="muted">Unsaved changes.</span>}
                            <button
                              type="button"
                              disabled={saving || !script.customized}
                              title={
                                script.customized
                                  ? "Put the generated script back"
                                  : "This is already the generated script"
                              }
                              onClick={() => resetScript(key, script)}
                            >
                              Reset to default
                            </button>
                            <button
                              type="button"
                              className="primary"
                              disabled={saving || !edited}
                              onClick={() => saveScript(key, draftValue)}
                            >
                              {saving ? "Saving…" : "Save script"}
                            </button>
                          </div>
                        )}
                      </div>
                    )}
                  </>
                )}
              </div>
            );
          })}
        </div>
      </section>

      {/* ── The gate ────────────────────────────────────────────────── */}
      <section className="form-section">
        <h4>Quality gate</h4>
        <div className="sg-mc-gatemode">
          <label className="sg-mc-check">
            <input
              type="radio"
              name="gateMode"
              checked={form.gateMode === "inherit"}
              disabled={!canEdit}
              onChange={() => set("gateMode", "inherit")}
            />
            Use the installation policy
          </label>
          <label className="sg-mc-check">
            <input
              type="radio"
              name="gateMode"
              checked={form.gateMode === "override"}
              disabled={!canEdit}
              onChange={() => set("gateMode", "override")}
            />
            Set this service's own limits
          </label>
        </div>

        {form.gateMode === "inherit" ? (
          <GateSummary gate={config.effectiveGate} />
        ) : (
          <QualityGateFields
            values={form}
            disabled={!canEdit}
            onChange={(key, value) => set(key, value)}
            inheritedFrom={config.effectiveGate}
          />
        )}
      </section>

      {canEdit && (
        <div className="modal-actions">
          <button type="button" className="primary" disabled={saving} onClick={() => save()}>
            {saving ? "Saving…" : "Save merge checks"}
          </button>
        </div>
      )}

      {/* ── Finish it in Bitbucket ──────────────────────────────────── */}
      <section className="form-section sg-mc-finish">
        <h4>Finishing this in Bitbucket</h4>
        <p className="muted">
          KubeSight reports a verdict; it cannot itself prevent a merge. Until the
          branch requires the build to pass, a blocked pull request shows a red check
          and can still be merged.
        </p>
        <EnforcementBanner enforcement={enforcement} statusKey={form.statusKey} />
        <ol className="sg-mc-steps">
          <li>
            <strong>Repository settings → Webhooks → Add webhook.</strong> Point it at
            the URL above and tick the pull request events you chose.
          </li>
          <li>
            <strong>Repository settings → Branch restrictions.</strong> On{" "}
            {config.targetBranches?.length
              ? config.targetBranches.join(", ")
              : "the branches you protect"}
            , enable <em>Require successful builds before merging</em> and require the{" "}
            <code>{form.statusKey}</code> status.
          </li>
          <li>
            The credential this service uses needs write access — reporting a verdict
            writes a build status.
          </li>
        </ol>
      </section>

      {/* ── What it decided ─────────────────────────────────────────── */}
      <section className="form-section">
        <h4>Recent pull requests</h4>
        {runs.length === 0 ? (
          <p className="muted">
            Nothing yet. The first pull request into a watched branch will appear here.
          </p>
        ) : (
          <div className="table-wrap">
            <table className="data-table sg-mc-table">
              <thead>
                <tr>
                  <th>Pull request</th>
                  <th>Into</th>
                  <th>Commit</th>
                  <th>Problems</th>
                  <th>Verdict</th>
                  <th>Reported</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id}>
                    <td>
                      {run.pullRequestUrl ? (
                        <a href={run.pullRequestUrl} target="_blank" rel="noreferrer">
                          {run.title || `#${run.pullRequestId}`}
                        </a>
                      ) : (
                        run.title || `#${run.pullRequestId}`
                      )}
                      <span className="muted sg-mc-sub">
                        {run.author ? `${run.author} · ` : ""}
                        {formatRelative(run.createdAt)}
                      </span>
                    </td>
                    <td>{run.destinationBranch || "—"}</td>
                    <td className="mono">{shortSha(run.commitSha)}</td>
                    <td>
                      {run.totalProblems === null || run.totalProblems === undefined
                        ? "—"
                        : run.totalProblems}
                      {run.gate?.maxTotalProblems !== null &&
                        run.gate?.maxTotalProblems !== undefined && (
                          <span className="muted"> / {run.gate.maxTotalProblems}</span>
                        )}
                    </td>
                    {/* Both carry a sentence under a pill, so both are the
                        columns that have to be bounded — an unbounded one
                        squeezes every other column to nothing. */}
                    <td className="sg-mc-cell-wide">
                      <StatusPill status={run.verdict || run.state}>
                        {VERDICT_LABEL[run.verdict] || run.state}
                      </StatusPill>
                      {(run.reasons || []).length > 0 && (
                        <span className="muted sg-mc-sub" title={run.reasons.join("\n")}>
                          {run.reasons[0]}
                        </span>
                      )}
                    </td>
                    <td className="sg-mc-cell-wide">
                      <StatusPill status={run.deliveryState}>
                        {run.deliveryState}
                      </StatusPill>
                      {run.deliveryError && (
                        <span className="muted sg-mc-sub" title={run.deliveryError}>
                          {run.deliveryError}
                        </span>
                      )}
                    </td>
                    <td className="sg-mc-actions">
                      {canEdit && run.verdict && run.deliveryState !== "delivered" && (
                        <button
                          type="button"
                          disabled={busyCheckId === run.id}
                          onClick={() => redeliver(run.id)}
                        >
                          {busyCheckId === run.id ? "Sending…" : "Send again"}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

/**
 * Whether the merge is REALLY blocked — asked of Bitbucket, not assumed.
 *
 * The single most useful thing on this tab. Everything else can be perfect and
 * the gate still not gate: a red build status on a branch with no restriction
 * is decoration. So the page says which of the two it is, in Bitbucket's own
 * words, rather than leaving somebody to believe the steps below were enough.
 */
function EnforcementBanner({ enforcement, statusKey }) {
  if (!enforcement) {
    return <p className="muted sg-mc-enforce">Checking Bitbucket…</p>;
  }
  if (!enforcement.known) {
    return (
      <p className="sg-mc-enforce sg-mc-enforce--unknown">
        KubeSight could not read this repository's branch restrictions, so it cannot
        say whether a failed check would stop a merge.
        {enforcement.reason ? ` ${enforcement.reason}` : ""}
      </p>
    );
  }
  if (enforcement.enforced) {
    const where = (enforcement.covered || []).join(", ");
    return (
      <p className="sg-mc-enforce sg-mc-enforce--ok">
        <strong>Enforced.</strong> Bitbucket requires a passing build
        {where ? ` on ${where}` : ""}, so a blocked check stops the merge.
      </p>
    );
  }
  const missing = (enforcement.uncovered || []).join(", ");
  return (
    <p className="sg-mc-enforce sg-mc-enforce--warn">
      <strong>Not enforced yet.</strong>{" "}
      {(enforcement.restrictions || []).length === 0
        ? "This repository has no branch restriction requiring a passing build, so a blocked pull request can still be merged."
        : `Nothing requires a passing build on ${missing || "the branches this gate watches"}, so a blocked pull request into ${missing || "them"} can still be merged.`}{" "}
      Add the restriction below and require the <code>{statusKey}</code> status.
    </p>
  );
}

/** What an inheriting service is actually judged against, in one line each. */
function GateSummary({ gate }) {
  if (!gate) return null;
  const cap = (value) => (value === null || value === undefined ? "no limit" : value);
  return (
    <ul className="sg-mc-summary">
      <li>
        <CheckIcon /> At most <strong>{cap(gate.maxTotalProblems)}</strong> problems in
        total.
      </li>
      {TOOLS.map(({ key, label, capKey }) =>
        gate[capKey] === null || gate[capKey] === undefined ? null : (
          <li key={key}>
            <CheckIcon /> At most <strong>{gate[capKey]}</strong> from {label}.
          </li>
        )
      )}
      <li>
        <CheckIcon /> ESLint warnings{" "}
        {gate.eslintCountWarnings ? "count as problems" : "do not count"}; SonarQube
        counts <strong>{gate.sonarMinSeverity}</strong> and above; Dependency-Check
        counts <strong>{gate.dependencyMinSeverity}</strong> and above.
      </li>
      <li>
        <CheckIcon /> A check that cannot run{" "}
        {gate.blockOnToolError ? "blocks the merge" : "is a warning only"}.
      </li>
    </ul>
  );
}
