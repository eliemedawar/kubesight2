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

// Variable-change runs skip the Jenkins build; image_check gates the RUNNING
// image (the change restarts pods) and the verify slot is the variable check.
const ENV_STEPS = [
  { key: "image_check", label: "Image check" },
  { key: "verify", label: "Variable check" },
  { key: "approval", label: "Approval" },
  { key: "deploy", label: "Apply" },
  { key: "pods", label: "Pod health" },
];

// Custom (non-cluster) environments deploy entirely through Jenkins — there is
// no image gate, no approval bundle, and no rollout to watch, so the k8s strip
// would be all skips plus a misleading "Pod health". Show only the stages that
// exist: the build IS the deploy. Runs whose environment maps to a registered
// Mobile Application get an extra chip for the binary handoff (run.mobileBuilds).
const CUSTOM_STEPS = [
  { key: "build", label: "Jenkins build" },
  { key: "deploy", label: "Deploy" },
];

// run.mobileBuilds.status → chip state + tooltip for the binary-ingest stage.
const MOBILE_CHIP = {
  fetching: ["run", "binary being pulled from Jenkins into Mobile Applications"],
  available: ["done", "binary stored — see the Mobile Applications tab"],
  failed: ["fail", "binary ingest failed — see the Mobile Applications tab"],
};

const STEP_CHIP_CLASS = {
  done: "sg-pstep--done",
  run: "sg-pstep--run",
  fail: "sg-pstep--fail",
  wait: "sg-pstep--wait",
  skip: "sg-pstep--wait sg-zh-pstep-skip",
};

const RUN_STATUS_PILL = {
  waiting: ["warn", "In queue"],
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

// "waiting" = queued behind another run on the same target; it's open (polls,
// cancellable, blocks a second Run on the ticket) even though nothing runs yet.
export const ACTIVE_RUN_STATUSES = new Set([
  "waiting",
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
  const custom = Boolean(run.customEnvironment);
  const stepList = custom ? CUSTOM_STEPS : run.changeType === "env_var" ? ENV_STEPS : STEPS;

  // Resolve each rendered stage to a chip state up front.
  const chips = stepList.map((step) => {
    let state = steps.get(step.key) || { status: "wait", detail: "" };
    // Runs that finished before the pod-health step existed have no
    // "pods" entry — show it as skipped rather than eternally waiting.
    if (step.key === "pods" && run.status === "deployed" && state.status === "wait") {
      state = { status: "skip", detail: "not checked (older run)" };
    }
    // Custom runs park their queue note on the (hidden) image_check slot —
    // surface it on the build chip while nothing has been triggered yet.
    if (custom && step.key === "build" && state.status === "wait") {
      const gate = steps.get("image_check");
      if (gate?.status === "wait" && gate.detail) {
        state = { status: "wait", detail: gate.detail };
      }
    }
    return { key: step.key, label: step.label, state };
  });

  // The binary handoff to Mobile Applications, when this run produced one.
  if (custom && run.mobileBuilds?.status && MOBILE_CHIP[run.mobileBuilds.status]) {
    const [status, detail] = MOBILE_CHIP[run.mobileBuilds.status];
    const count = run.mobileBuilds.count || 1;
    return renderChips([
      ...chips,
      {
        key: "mobile",
        label: count > 1 ? `Mobile binaries ×${count}` : "Mobile binary",
        state: { status, detail },
      },
    ]);
  }
  return renderChips(chips);
}

function renderChips(chips) {
  return (
    <div className="sg-pipe sg-zh-run-pipe">
      {chips.map(({ key, label, state }, index) => {
        const chip = (
          <span
            key={key}
            className={`sg-pstep ${STEP_CHIP_CLASS[state.status] || "sg-pstep--wait"}`}
            title={state.detail || label}
          >
            {state.status === "done" ? <IconCheck /> : null}
            {label}
            {state.status === "skip" ? " (skipped)" : ""}
          </span>
        );
        return index === 0 ? (
          chip
        ) : (
          <span key={key} className="sg-zh-pipe-seg">
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
          {run.customEnvironment ? (
            <span
              className="sg-tag"
              title="Custom environment — the Jenkins job owns the whole deploy; there is no cluster rollout to gate or watch."
            >
              custom env
            </span>
          ) : null}
          {run.changeType === "env_var" ? (
            <span className="sg-tag mono" title={`set ${run.variableName} to ${run.variableValue}`}>
              {run.variableName}={run.variableValue}
            </span>
          ) : (
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
          )}
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
