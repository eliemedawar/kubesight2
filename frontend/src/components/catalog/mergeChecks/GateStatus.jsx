import { PlIcon } from "../pipeline/icons.jsx";

const STEP_ICON = {
  ok: "check",
  todo: "plus",
  warn: "alert",
  unknown: "alert",
  checking: "clock",
};

const STEP_WORD = {
  ok: "Done",
  todo: "To do",
  warn: "Needs attention",
  unknown: "Can't tell",
  checking: "Checking…",
};

/**
 * The first thing on the tab: whether a failed check actually stops a merge.
 *
 * Four things have to be true and three of them live in Bitbucket, so the
 * card asks rather than assumes, step by step — a red build status on a branch
 * nobody protected is decoration, and this is where that shows. The one
 * button that fixes steps two and three sits next to them.
 */
export default function GateStatus({
  status,
  branches,
  gateOpen,
  canEdit,
  setupBusy,
  setupDisabled,
  protectBranches,
  onProtectBranches,
  blockDirectPush,
  onBlockDirectPush,
  onSetup,
  onEnable,
  onGoToTab,
}) {
  const { steps, done, total, overall } = status;
  const branchText = branches.join(", ");
  const needsSetup = steps.some((step) => step.action === "setup");

  const headline =
    overall === "protected"
      ? {
          icon: "shield",
          title: `Merges into ${branchText} are protected`,
          text: "A pull request that fails the checks cannot be merged until it is fixed.",
        }
      : overall === "stuck"
        ? {
            icon: "alert",
            title: `Pull requests into ${branchText} are stuck`,
            text: "Bitbucket waits for a passing KubeSight check, and none is sent while merge checks are off. Switch them on, or remove the branch restriction.",
          }
        : {
            icon: "shield",
            title: `Not protecting merges into ${branchText} yet`,
            text: `${done} of ${total} done. Until every step is, a failing pull request can still be merged.`,
          };

  const act = (action) => {
    if (action === "setup") onSetup();
    else if (action === "enable") onEnable();
    else if (action === "source") onGoToTab?.("source");
    else if (action === "credential") onGoToTab?.("source");
  };

  const actionLabel = (action) =>
    ({
      setup: null,
      enable: "Switch on",
      source: "Open Source",
      credential: "Change credential",
    })[action];

  return (
    <section className={`mc-status is-${overall}`} aria-label="Is this protecting merges?">
      <header className="mc-status-head">
        <span className="mc-status-glyph" aria-hidden="true">
          <PlIcon name={headline.icon} />
        </span>
        <div>
          <h3>{headline.title}</h3>
          <p>{headline.text}</p>
        </div>
        <div className="mc-status-meter" aria-label={`${done} of ${total} steps done`}>
          {steps.map((step) => (
            <i key={step.key} className={`is-${step.state}`} />
          ))}
        </div>
      </header>

      <ol className="mc-steps">
        {steps.map((step, index) => (
          <li key={step.key} className={`mc-step is-${step.state}`}>
            <span className="mc-step-mark" aria-hidden="true">
              {step.state === "ok" ? <PlIcon name="check" /> : <span>{index + 1}</span>}
            </span>
            <div className="mc-step-copy">
              <span className="mc-step-state">
                <PlIcon name={STEP_ICON[step.state]} /> {STEP_WORD[step.state]}
              </span>
              <strong>{step.title}</strong>
              <p>{step.detail}</p>
              {canEdit && step.action && actionLabel(step.action) && (
                <button type="button" className="btn-outline btn-compact" onClick={() => act(step.action)}>
                  {actionLabel(step.action)}
                </button>
              )}
            </div>
          </li>
        ))}
      </ol>

      {canEdit && needsSetup && (
        <div className="mc-setup-cta">
          <div className="mc-setup-copy">
            <strong>Let KubeSight do the Bitbucket part</strong>
            <p>
              Creates the webhook with its secret and, if ticked, the branch restriction — with
              the credential this service already uses. Nothing is switched on.
            </p>
            <div className="mc-setup-options">
              <label className="pl-check">
                <input
                  type="checkbox"
                  checked={protectBranches}
                  onChange={(event) => onProtectBranches(event.target.checked)}
                />
                <span>
                  <strong>Block merging into {branchText} until KubeSight passes it</strong>
                </span>
              </label>
              <label className="pl-check">
                <input
                  type="checkbox"
                  checked={blockDirectPush}
                  disabled={!protectBranches}
                  onChange={(event) => onBlockDirectPush(event.target.checked)}
                />
                <span>
                  <strong>Also block direct pushes</strong>
                  <small>A pull request becomes the only way in — this changes how the team works.</small>
                </span>
              </label>
            </div>
          </div>
          <button type="button" className="primary" disabled={setupBusy || setupDisabled} onClick={onSetup}>
            <PlIcon name="link" />
            {setupBusy ? "Setting up…" : steps.find((s) => s.key === "webhook")?.state === "warn" ? "Resync with Bitbucket" : "Set up in Bitbucket"}
          </button>
        </div>
      )}

      {gateOpen && overall !== "stuck" && (
        <p className="mc-status-note">
          <PlIcon name="gauge" />
          <span>
            <strong>The quality gate has no limits,</strong> so the checks report what they find
            and every pull request passes. Set a limit under Quality gate for anything to be
            blocked.
          </span>
        </p>
      )}
    </section>
  );
}
