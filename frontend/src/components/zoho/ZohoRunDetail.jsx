import { IconCheck } from "./icons.jsx";

// Display order + labels for the run step chips (mirrors the backend STEP_KEYS).
const STEPS = [
  { key: "image_check", label: "Image check" },
  { key: "build", label: "Build" },
  { key: "verify", label: "Verify" },
  { key: "approval", label: "Approval" },
  { key: "deploy", label: "Deploy" },
  { key: "pods", label: "Pod health" },
];

const STEP_CHIP_CLASS = {
  done: "sg-pstep--done",
  run: "sg-pstep--run",
  fail: "sg-pstep--fail",
  wait: "sg-pstep--wait",
  skip: "sg-pstep--wait sg-zh-pstep-skip",
};

const RUN_STATUS_PILL = {
  queued: ["info", "Queued"],
  checking_image: ["info", "Checking image"],
  building: ["info", "Building"],
  verifying_image: ["info", "Verifying"],
  awaiting_approval: ["warn", "Awaiting approval"],
  verifying_rollout: ["info", "Rolling out"],
  deployed: ["ok", "Deployed"],
  failed: ["danger", "Failed"],
  cancelled: ["muted", "Cancelled"],
};

export const ACTIVE_RUN_STATUSES = new Set([
  "queued",
  "checking_image",
  "building",
  "verifying_image",
  "awaiting_approval",
  "verifying_rollout",
]);

export function RunStatusPill({ status }) {
  const [tone, label] = RUN_STATUS_PILL[status] || ["muted", status || "—"];
  return <span className={`status-pill ${tone}`}>{label}</span>;
}

export function RunPipeline({ run }) {
  const steps = new Map((run.steps || []).map((s) => [s.key, s]));
  return (
    <div className="sg-pipe sg-zh-run-pipe">
      {STEPS.map((step, index) => {
        let state = steps.get(step.key) || { status: "wait", detail: "" };
        // Runs that finished before the pod-health step existed have no
        // "pods" entry — show it as skipped rather than eternally waiting.
        if (step.key === "pods" && run.status === "deployed" && state.status === "wait") {
          state = { status: "skip", detail: "not checked (older run)" };
        }
        const chip = (
          <span
            key={step.key}
            className={`sg-pstep ${STEP_CHIP_CLASS[state.status] || "sg-pstep--wait"}`}
            title={state.detail || step.label}
          >
            {state.status === "done" ? <IconCheck /> : null}
            {step.label}
            {state.status === "skip" ? " (skipped)" : ""}
          </span>
        );
        return index === 0 ? (
          chip
        ) : (
          <span key={step.key} className="sg-zh-pipe-seg">
            <span className="sg-pline" />
            {chip}
          </span>
        );
      })}
    </div>
  );
}

// Full run block: header (id, target, tag, status, cancel), pipeline, links, error.
// Used inside expanded ticket rows and the orphan-runs list.
export function RunDetail({ run, canManage, cancelling, onCancel, showHead = true }) {
  const active = ACTIVE_RUN_STATUSES.has(run.status);
  return (
    <div className="sg-zh-run">
      {showHead ? (
        <div className="sg-zh-run-head">
          <b>{run.ticketNumber || `run #${run.id}`}</b>
          <span className="mono sg-zh-run-target">{run.deploymentName}</span>
          <span className="sg-tag">{run.namespace}</span>
          <span
            className="sg-tag"
            title={
              run.ticketTag && run.ticketTag !== run.imageTag
                ? `ticket tag: ${run.ticketTag}`
                : undefined
            }
          >
            {run.imageTag}
          </span>
          {run.auto ? <span className="sg-zh-count">auto</span> : null}
          <span className="sg-zh-run-spacer" />
          <span className="sg-zh-htime">
            {run.createdAt ? new Date(run.createdAt).toLocaleString() : ""}
          </span>
          <RunStatusPill status={run.status} />
          {active && canManage ? (
            <button
              type="button"
              className="btn-ghost sg-zh-run-cancel"
              onClick={() => onCancel(run)}
              disabled={cancelling}
            >
              Cancel
            </button>
          ) : null}
        </div>
      ) : null}

      <RunPipeline run={run} />

      {run.jenkinsBuildUrl ? (
        <p className="sg-zh-fhint">
          Router build{" "}
          <a href={run.jenkinsBuildUrl} target="_blank" rel="noreferrer">
            #{run.jenkinsBuildNumber}
          </a>
          {run.bundleId ? <> · Change bundle #{run.bundleId} ({run.bundleStatus || "…"})</> : null}
        </p>
      ) : run.bundleId ? (
        <p className="sg-zh-fhint">
          Change bundle #{run.bundleId} ({run.bundleStatus || "…"})
        </p>
      ) : null}
      {run.error ? <p className="sg-zh-inline-error">{run.error}</p> : null}
    </div>
  );
}
