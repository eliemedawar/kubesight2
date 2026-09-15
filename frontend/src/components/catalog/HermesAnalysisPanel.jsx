import { useCallback, useEffect, useRef, useState } from "react";
import {
  acceptCiAnalysis,
  cancelCiAnalysis,
  getCiServiceAnalysis,
  startCiAnalysis,
  updateCiApplicationProfile,
} from "../../api/ciAssistApi.js";
import ApplicationProfileCard from "./ApplicationProfileCard.jsx";
import GeneratedPipelineReview from "./GeneratedPipelineReview.jsx";
import RequiredConfigForm from "./RequiredConfigForm.jsx";

/**
 * Watching an analysis, and deciding what to do with what it found.
 *
 * Progress is reported as the step the worker is actually on, not as a
 * percentage invented to fill a bar. The backend names each step as it starts
 * it, so "Reading build configuration" is true when it says so — and when a
 * step takes a while, the honest thing is for the label to sit still.
 *
 * There are four outcomes and each has a different screen, because they need
 * different decisions:
 *
 *   analyzed  a profile and a valid pipeline — review, fill the gaps, create
 *   partial   a profile and a pipeline KubeSight refused — see why, or go manual
 *   failed    nothing — retry, or go manual
 *   (none)    never analyzed — offer to
 *
 * None of them is a dead end. Manual configuration is one click from every one.
 */

const POLL_MS = 1500;
const ACTIVE = new Set(["queued", "analyzing"]);

export default function HermesAnalysisPanel({
  service,
  availability,
  canEdit,
  onAccepted,
  onConfigureManually,
  autoStart = false,
}) {
  const [state, setState] = useState(null);
  const [inputs, setInputs] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const timer = useRef(null);
  const started = useRef(false);

  const analysis = state?.latestAnalysis || null;
  const active = analysis && ACTIVE.has(analysis.state);

  const load = useCallback(async () => {
    try {
      const data = await getCiServiceAnalysis(service.id);
      setState(data);
      return data;
    } catch (err) {
      setError(err.message || "Could not load the analysis.");
      return null;
    }
  }, [service.id]);

  useEffect(() => {
    load();
  }, [load]);

  // Poll only while something is running. A finished analysis is a static row.
  useEffect(() => {
    if (!active) return undefined;
    let cancelled = false;
    const tick = async () => {
      const data = await load();
      if (cancelled) return;
      if (data?.latestAnalysis && ACTIVE.has(data.latestAnalysis.state)) {
        timer.current = window.setTimeout(tick, POLL_MS);
      }
    };
    timer.current = window.setTimeout(tick, POLL_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer.current);
    };
  }, [active, load]);

  const begin = useCallback(
    async (payload = {}) => {
      setBusy(true);
      setError("");
      try {
        await startCiAnalysis(service.id, payload);
        await load();
      } catch (err) {
        setError(err.message || "The analysis could not be started.");
      } finally {
        setBusy(false);
      }
    },
    [service.id, load]
  );

  // The wizard hands this panel a service that has just been connected and
  // wants the analysis under way without a second click.
  useEffect(() => {
    if (!autoStart || started.current || !availability?.available) return;
    if (state === null) return;
    if (state.latestAnalysis) return;
    started.current = true;
    begin();
  }, [autoStart, availability, state, begin]);

  const saveProfileChanges = async (changes) => {
    setBusy(true);
    setError("");
    try {
      await updateCiApplicationProfile(service.id, { overrides: changes });
      await load();
    } catch (err) {
      setError(err.message || "Those changes could not be saved.");
    } finally {
      setBusy(false);
    }
  };

  const regenerate = async () => {
    // Built around the profile as it now stands, including anything the user
    // corrected — that is the point of recording overrides.
    await begin({ applicationProfile: state?.applicationProfile || analysis?.applicationProfile });
  };

  const accept = async () => {
    setBusy(true);
    setError("");
    try {
      const result = await acceptCiAnalysis(analysis.id, { inputs });
      onAccepted?.(result);
    } catch (err) {
      setError(err.message || "The pipeline could not be saved.");
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    try {
      await cancelCiAnalysis(analysis.id);
      await load();
    } catch (err) {
      setError(err.message || "The analysis could not be cancelled.");
    }
  };

  if (!availability?.available) {
    return (
      <section className="sg-ci-assist sg-ci-assist--off">
        <h4>Automatic configuration is unavailable</h4>
        <p className="muted">{availability?.reason || "Hermes is not configured."}</p>
        {onConfigureManually && (
          <button type="button" className="primary" onClick={onConfigureManually}>
            Configure manually
          </button>
        )}
      </section>
    );
  }

  const requiredInputs = analysis?.requiredInputs || [];
  const outstanding = requiredInputs.filter(
    (item) => item.required !== false && !String(inputs[item.name] || "").trim()
  );

  return (
    <section className="sg-ci-assist">
      {error && <p className="banner-message error">{error}</p>}

      {/* ---------------------------------------------------- not analyzed */}
      {!analysis && (
        <div className="sg-ci-assist-start">
          <h4>Configure automatically with Hermes</h4>
          <p className="muted">
            Hermes reads this repository's build files and proposes a complete
            KubeSight pipeline. Nothing is saved until you approve it.
          </p>
          <button
            type="button"
            className="primary"
            disabled={busy || !canEdit || !service.sourceConfigured}
            onClick={() => begin()}
            title={
              service.sourceConfigured
                ? "Read the repository and propose a pipeline"
                : "Connect a repository first"
            }
          >
            {busy ? "Starting…" : "Analyze repository"}
          </button>
          {onConfigureManually && (
            <button type="button" className="btn-outline" onClick={onConfigureManually}>
              Configure manually
            </button>
          )}
        </div>
      )}

      {/* -------------------------------------------------------- running */}
      {active && (
        <div className="sg-ci-assist-progress" role="status" aria-live="polite">
          <div className="sg-ci-assist-bar">
            <span style={{ width: `${Math.max(4, analysis.progressPercent || 0)}%` }} />
          </div>
          <p>
            <strong>{analysis.currentStage || "Working…"}</strong>
          </p>
          <p className="muted">
            Reading {service.repositoryWorkspace}/{service.repositoryName} at{" "}
            {analysis.revision}. This usually takes under a minute.
          </p>
          {canEdit && (
            <button type="button" className="btn-outline btn-compact" onClick={stop}>
              Cancel analysis
            </button>
          )}
        </div>
      )}

      {/* --------------------------------------------------------- failed */}
      {analysis?.state === "failed" && (
        <div className="sg-ci-assist-failed">
          <h4>Hermes could not complete the analysis</h4>
          <p>{analysis.error || "The analysis did not finish."}</p>
          {analysis.failureStage && (
            <p className="muted">It stopped at: {analysis.failureStage}.</p>
          )}
          <div className="sg-ci-assist-actions">
            <button
              type="button"
              className="primary"
              disabled={busy || !canEdit}
              onClick={() => begin()}
            >
              Retry analysis
            </button>
            {onConfigureManually && (
              <button type="button" className="btn-outline" onClick={onConfigureManually}>
                Configure manually
              </button>
            )}
          </div>
        </div>
      )}

      {analysis?.state === "cancelled" && (
        <div className="sg-ci-assist-failed">
          <h4>Analysis cancelled</h4>
          <div className="sg-ci-assist-actions">
            <button type="button" className="primary" disabled={busy} onClick={() => begin()}>
              Start again
            </button>
            {onConfigureManually && (
              <button type="button" className="btn-outline" onClick={onConfigureManually}>
                Configure manually
              </button>
            )}
          </div>
        </div>
      )}

      {/* ------------------------------------------- analyzed and partial */}
      {(analysis?.state === "analyzed" || analysis?.state === "partial") && (
        <>
          <ApplicationProfileCard
            profile={analysis.applicationProfile}
            editable={canEdit}
            onChange={saveProfileChanges}
            onRegenerate={canEdit ? regenerate : undefined}
            regenerating={busy}
          />

          {analysis.warnings?.length > 0 && (
            <ul className="sg-ci-assist-warnings">
              {analysis.warnings.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          )}

          <GeneratedPipelineReview
            pipeline={analysis.generatedPipeline}
            validation={analysis.validation}
          />

          {analysis.state === "analyzed" && (
            <>
              <RequiredConfigForm
                items={requiredInputs}
                values={inputs}
                onChange={setInputs}
                disabled={busy || !canEdit}
              />
              <div className="sg-ci-assist-actions">
                <button
                  type="button"
                  className="primary"
                  disabled={busy || !canEdit || outstanding.length > 0}
                  onClick={accept}
                  title={
                    outstanding.length
                      ? `Still needed: ${outstanding.map((i) => i.label || i.name).join(", ")}`
                      : "Save this as the service's pipeline"
                  }
                >
                  {busy ? "Saving…" : "Save pipeline"}
                </button>
                {onConfigureManually && (
                  <button type="button" className="btn-outline" onClick={onConfigureManually}>
                    Configure manually instead
                  </button>
                )}
              </div>
              <p className="field-hint">
                This saves a normal KubeSight pipeline. Builds run on the existing
                engine and never call Hermes again.
              </p>
            </>
          )}

          {analysis.state === "partial" && (
            <div className="sg-ci-assist-actions">
              <button
                type="button"
                className="primary"
                disabled={busy || !canEdit}
                onClick={() => begin()}
              >
                Try again
              </button>
              {onConfigureManually && (
                <button type="button" className="btn-outline" onClick={onConfigureManually}>
                  Configure manually
                </button>
              )}
            </div>
          )}

          {/* What was actually read, and how many rounds it took. Cheap to
              show and the first thing anybody asks when a result looks off. */}
          {(analysis.evidenceCoverage || analysis.attempts?.length > 0) && (
            <p className="sg-ci-assist-provenance">
              {analysis.evidenceCoverage?.filesRead != null && (
                <>
                  Read {analysis.evidenceCoverage.filesRead} build files of{" "}
                  {analysis.evidenceCoverage.filesInTree} in the repository.{" "}
                </>
              )}
              {analysis.attempts?.length > 1 && (
                <>
                  Corrected {analysis.attempts.length - 1}{" "}
                  {analysis.attempts.length === 2 ? "time" : "times"}
                  {/* The outcome decides the verb. Saying "before KubeSight
                      accepted it" under a refusal is the sentence contradicting
                      the verdict directly above it. */}
                  {analysis.state === "analyzed"
                    ? " before KubeSight accepted it."
                    : ", and KubeSight still could not accept it."}{" "}
                </>
              )}
              {analysis.evidenceCoverage?.treeTruncated && (
                <>The repository is large and was only partly listed. </>
              )}
            </p>
          )}
        </>
      )}
    </section>
  );
}
