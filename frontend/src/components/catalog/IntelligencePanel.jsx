import { useCallback, useEffect, useState } from "react";
import {
  enableCiServiceIntelligence,
  getCiServiceIntelligence,
  listBitbucketCredentialProfiles,
  requestApplicationAnalysis,
} from "../../api/applicationIntelligenceApi";
import LoadingState from "../common/LoadingState.jsx";
import { ApplicationIntelligenceWorkspace } from "../../pages/ApplicationIntelligencePage.jsx";

const MODES = ["Quick", "Deep", "Build Verified"];

/**
 * Application Intelligence for this service's repository.
 *
 * The repository, branch, credential and working directory are the ones on
 * the Source tab — there is nothing to register twice. The first analysis
 * creates the Intelligence application behind the scenes and links it here;
 * one registered earlier for the same repository is offered for linking so
 * its history is kept.
 */
export default function IntelligencePanel({ service, canManage, canAnalyze, onGoToTab }) {
  const [state, setState] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState("Quick");
  // Findings can open a fix pull request, which picks a credential.
  const [credentials, setCredentials] = useState([]);

  const load = useCallback(async () => {
    try {
      setState(await getCiServiceIntelligence(service.id));
      setError("");
    } catch (err) {
      setError(err.message || "Could not load the analysis.");
    }
  }, [service.id]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!canManage) return;
    listBitbucketCredentialProfiles()
      .then((data) => setCredentials(data.items || []))
      .catch(() => setCredentials([]));
  }, [canManage]);

  const enable = async ({ analyze }) => {
    setBusy(true);
    setError("");
    try {
      const next = await enableCiServiceIntelligence(service.id);
      if (analyze && next.application) {
        await requestApplicationAnalysis(next.application.id, {
          analysisMode: mode,
          revision: service.defaultBranch,
        });
      }
      // Remount the workspace so it opens on the run just started.
      setState(null);
      await load();
    } catch (err) {
      setError(err.message || "The analysis could not be started.");
    } finally {
      setBusy(false);
    }
  };

  if (!state) {
    return error
      ? <p className="banner-message error">{error}</p>
      : <LoadingState label="Loading analysis…" />;
  }

  const { application, linked, sourceConfigured } = state;

  if (!application) {
    return (
      <section className="sg-ci-assist">
        {error && <p className="banner-message error">{error}</p>}
        <div className="sg-ci-assist-start">
          <h4>Analyze this repository</h4>
          <p className="muted">
            Hermes reads {service.repositoryWorkspace
              ? <code>{service.repositoryWorkspace}/{service.repositoryName}</code>
              : "this service's repository"} in an isolated, read-only job and
            reports evidence-backed findings, its architecture and APIs, its
            configuration and how ready it is to deploy. It uses the repository
            and credential from the Source tab.
          </p>
          {!sourceConfigured ? (
            <button type="button" className="primary" onClick={() => onGoToTab("source")}>
              Connect a repository
            </button>
          ) : canManage && canAnalyze ? (
            <div className="sg-ci-assist-actions">
              <select
                value={mode}
                onChange={(event) => setMode(event.target.value)}
                aria-label="Analysis mode"
              >
                {MODES.map((value) => (
                  <option key={value} value={value}>{value}</option>
                ))}
              </select>
              <button
                type="button"
                className="primary"
                disabled={busy}
                onClick={() => enable({ analyze: true })}
              >
                {busy ? "Starting…" : "Analyze repository"}
              </button>
            </div>
          ) : (
            <p className="field-hint">
              Starting an analysis needs the Application Intelligence manage and
              analyze permissions.
            </p>
          )}
        </div>
      </section>
    );
  }

  return (
    <div className="sg-ci-intel">
      {error && <p className="banner-message error">{error}</p>}
      {!linked && (
        <p className="banner-message">
          An analysis of this repository already exists as “{application.name}”.
          {canManage ? (
            <>
              {" "}
              <button
                type="button"
                className="btn-outline btn-compact"
                disabled={busy}
                onClick={() => enable({ analyze: false })}
              >
                {busy ? "Linking…" : "Link it to this service"}
              </button>
            </>
          ) : null}
        </p>
      )}
      <ApplicationIntelligenceWorkspace
        key={application.id}
        applicationId={String(application.id)}
        credentials={credentials}
        canManage={canManage}
        canAnalyze={canAnalyze}
        embedded
      />
    </div>
  );
}
