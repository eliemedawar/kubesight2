import { deployOutcome } from "./pipeline/deployModel.js";

/**
 * What a Deploy stage did in this build, above its log: where it deployed,
 * which image replaced which, the change bundle it waited on, and whose rights
 * it used. The log has the step-by-step; this is the answer.
 */
export default function DeployStageSummary({ stage }) {
  const state = stage?.deploy;
  if (!state) return null;
  const outcome = deployOutcome(state);
  const target = state.target || {};
  const where = [target.clusterId, target.namespace, target.deploymentName].filter(Boolean).join(" / ");
  return (
    <section className={`sg-ci-deploy is-${outcome?.tone || "info"}`} aria-label="Deploy result">
      <header>
        <span className={`sg-ci-deploy-badge is-${outcome?.tone || "info"}`}>{outcome?.label}</span>
        {where && <code className="sg-ci-deploy-where">{where}</code>}
      </header>
      <dl>
        {state.image && (
          <>
            <dt>Image</dt>
            <dd>
              {state.previousImage && state.previousImage !== state.image ? (
                <>
                  <code>{state.previousImage}</code> <span aria-hidden="true">→</span>{" "}
                  <span className="sg-sr">replaced by </span>
                  <code>{state.image}</code>
                </>
              ) : (
                <code>{state.image}</code>
              )}
              {state.created ? <span className="chip">created</span> : null}
            </dd>
          </>
        )}
        {state.containerName && (
          <>
            <dt>Container</dt>
            <dd>
              <code>{state.containerName}</code>
            </dd>
          </>
        )}
        {state.bundleId && (
          <>
            <dt>Approval</dt>
            <dd>
              Change bundle #{state.bundleId}
              {state.phase === "waiting_approval" ? " — waiting for approval" : ""}
            </dd>
          </>
        )}
        {target.authorizedBy && (
          <>
            <dt>Deployed as</dt>
            <dd>{target.authorizedBy}</dd>
          </>
        )}
        {state.phase === "rolling_out" && state.lastDetail && (
          <>
            <dt>Rollout</dt>
            <dd>{state.lastDetail}</dd>
          </>
        )}
      </dl>
    </section>
  );
}
