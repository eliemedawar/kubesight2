import { PUBLISH_STEP_LABELS, storeUploadOutcome } from "./pipeline/storeUploadModel.js";

/**
 * What an App store upload stage did in this build: which file went where,
 * the Mobile Apps release and publish it became, and that publish's steps.
 * The log has the line-by-line; this is the answer, with a way to the release.
 */
export default function StoreUploadStageSummary({ stage }) {
  const state = stage?.storeUpload;
  if (!state) return null;
  const outcome = storeUploadOutcome(state);
  const target = state.target || {};
  const steps = state.steps || [];
  return (
    <section className={`sg-ci-deploy is-${outcome?.tone || "info"}`} aria-label="Store upload result">
      <header>
        <span className={`sg-ci-deploy-badge is-${outcome?.tone || "info"}`}>{outcome?.label}</span>
        {target.label && <code className="sg-ci-deploy-where">{target.label}</code>}
      </header>
      <dl>
        {(target.appName || target.appId) && (
          <>
            <dt>App</dt>
            <dd>
              {state.appId || target.appId ? (
                <a href={`#/mobile-apps/${state.appId || target.appId}`}>{target.appName || `App #${target.appId}`}</a>
              ) : (
                target.appName
              )}
            </dd>
          </>
        )}
        {state.artifactName && (
          <>
            <dt>File</dt>
            <dd>
              <code>{state.artifactName}</code>
              {state.signatureState === "unsigned" ? <span className="chip">unsigned</span> : null}
            </dd>
          </>
        )}
        {state.mobileBuildId && (
          <>
            <dt>Release</dt>
            <dd>
              #{state.mobileBuildId}
              {state.version ? ` · ${state.version}` : ""}
            </dd>
          </>
        )}
        {state.publishId && (
          <>
            <dt>Publish</dt>
            <dd>
              #{state.publishId} · {state.publishStatus || "queued"}
              {state.storeRef?.versionCode ? ` · versionCode ${state.storeRef.versionCode}` : ""}
            </dd>
          </>
        )}
        {target.authorizedBy && (
          <>
            <dt>Published as</dt>
            <dd>{target.authorizedBy}</dd>
          </>
        )}
      </dl>
      {steps.length > 0 && (
        <ol className="sg-ci-store-steps" aria-label="Publish steps">
          {steps.map((step) => (
            <li key={step.key} className={`is-${step.status || "wait"}`}>
              <strong>{PUBLISH_STEP_LABELS[step.key] || step.key}</strong>
              <span>{step.detail || step.status}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
