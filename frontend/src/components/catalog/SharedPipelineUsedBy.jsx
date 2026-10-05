import { useCallback, useEffect, useMemo, useState } from "react";
import "../../styles/signal/pipelineWorkspace.css";
import "../../styles/signal/sharedPipelines.css";
import { attachSharedPipeline, getSharedPipeline, listCiServices } from "../../api/ciApi.js";
import { buildRoute } from "../../routes/routeUrl.js";
import SearchableSelect from "../common/SearchableSelect.jsx";
import { StatusPill, applicationTypeLabel, formatRelative } from "./ciShared.jsx";
import { PlIcon } from "./pipeline/icons.jsx";

/**
 * "Used by": the CI services that build with this pipeline.
 *
 * Each one runs these stages against its own repository, Dockerfile and
 * registry, and keeps its builds in its own history — so the list links to the
 * service, not to builds here. Attaching from this side is the same action as
 * "Use a shared pipeline" on the service's Pipeline tab.
 */
export default function SharedPipelineUsedBy({ pipeline, canAttach, onChanged }) {
  const [detail, setDetail] = useState(null);
  const [services, setServices] = useState(null);
  const [picked, setPicked] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(() => {
    getSharedPipeline(pipeline.id)
      .then((data) => {
        setDetail(data);
        setError("");
      })
      .catch((err) => setError(err.message || "Could not load who uses this pipeline."));
  }, [pipeline.id]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!canAttach) return;
    listCiServices()
      .then((data) => setServices(data.items || []))
      .catch(() => setServices([]));
  }, [canAttach]);

  const usedBy = detail?.usedBy || [];
  const usingIds = useMemo(() => new Set(usedBy.map((item) => String(item.serviceId))), [usedBy]);
  const candidates = (services || []).filter((item) => !usingIds.has(String(item.id)));
  const pickedService = candidates.find((item) => String(item.id) === String(picked)) || null;

  const attach = async () => {
    if (!pickedService) return;
    const today = pickedService.sharedPipeline
      ? `the shared pipeline “${pickedService.sharedPipeline.name}”`
      : pickedService.usingDefaultPipeline
        ? "the KubeSight starter pipeline"
        : "its own pipeline";
    if (
      !window.confirm(
        `${pickedService.name} builds with ${today} today. From its next build it builds with “${pipeline.name}” instead. Its own stages are kept and come back if it stops using this one.`
      )
    ) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      await attachSharedPipeline(pickedService.id, pipeline.id);
      setNotice(`${pickedService.name} now builds with this pipeline.`);
      setPicked("");
      load();
      onChanged?.();
    } catch (err) {
      setError(err.message || "Could not attach the pipeline.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="pl-root sp-usedby">
      <header className="pl-top">
        <div className="pl-top-id">
          <span className="pl-top-glyph" aria-hidden="true">
            <PlIcon name="link" />
          </span>
          <div>
            <h3>Used by</h3>
            <p className="pl-top-sentence">
              {detail === null ? (
                "Loading…"
              ) : usedBy.length ? (
                <>
                  <b>{usedBy.length}</b> {usedBy.length === 1 ? "service builds" : "services build"} with this
                  pipeline. Each runs these stages on its own repository and keeps its own build history; a
                  change saved here reaches all of them from their next build.
                </>
              ) : (
                "No service builds with this pipeline yet. It still runs on its own."
              )}
            </p>
          </div>
        </div>
      </header>

      {error && (
        <div className="pl-banner is-error" role="alert">
          <PlIcon name="alert" />
          <p>{error}</p>
          <button type="button" className="btn-ghost pl-banner-close" aria-label="Dismiss" onClick={() => setError("")}>
            <PlIcon name="x" />
          </button>
        </div>
      )}
      {notice && (
        <div className="pl-banner is-ok" role="status">
          <PlIcon name="check" />
          <p>{notice}</p>
          <button type="button" className="btn-ghost pl-banner-close" aria-label="Dismiss" onClick={() => setNotice("")}>
            <PlIcon name="x" />
          </button>
        </div>
      )}

      {canAttach && (
        <section className="pl-panel sp-attach" aria-label="Use this pipeline on a service">
          <div className="sp-attach-copy">
            <strong>Use it on a CI service</strong>
            <small>The service keeps its repository, Dockerfile, registry, secrets and deployments.</small>
          </div>
          <div className="sp-attach-row">
            <SearchableSelect
              id="sp-attach-service"
              aria-label="CI service"
              value={picked}
              placeholder={services === null ? "Loading services…" : candidates.length ? "Pick a CI service…" : "Every service already uses it"}
              searchPlaceholder="Search services…"
              disabled={!candidates.length || busy}
              options={candidates.map((item) => ({
                value: String(item.id),
                label: (
                  <span className="pl-deploy-option">
                    <span>{item.name}</span>
                    <small>
                      {item.sharedPipeline
                        ? `uses ${item.sharedPipeline.name}`
                        : item.usingDefaultPipeline
                          ? "starter pipeline"
                          : `${item.pipelineStageCount} own stage${item.pipelineStageCount === 1 ? "" : "s"}`}
                    </small>
                  </span>
                ),
              }))}
              onChange={(event) => setPicked(event.target.value)}
            />
            <button type="button" className="primary btn-compact" disabled={!pickedService || busy} onClick={attach}>
              <PlIcon name="link" /> {busy ? "Attaching…" : "Use this pipeline"}
            </button>
          </div>
        </section>
      )}

      {usedBy.length > 0 && (
        <ul className="pl-panel sp-users">
          {usedBy.map((item) => (
            <li key={item.serviceId} className="sp-user">
              <span className="sp-user-glyph" aria-hidden="true">
                <PlIcon name="source" />
              </span>
              <div className="sp-user-copy">
                <a href={buildRoute({ key: "serviceDetail", params: { serviceId: String(item.serviceId), tab: "pipeline" } })}>
                  {item.serviceName}
                </a>
                <small>
                  {applicationTypeLabel(item.applicationType)} · <code>{item.serviceSlug}</code>
                  {item.serviceStatus !== "active" && ` · ${item.serviceStatus}`}
                </small>
              </div>
              <div className="sp-user-build">
                {item.latestBuild ? (
                  <>
                    <StatusPill status={item.latestBuild.status} />
                    <small>
                      #{item.latestBuild.number}
                      {item.latestBuild.sharedPipeline?.version
                        ? ` · v${item.latestBuild.sharedPipeline.version}`
                        : " · its own pipeline"}
                      {item.latestBuild.queuedAt ? ` · ${formatRelative(item.latestBuild.queuedAt)}` : ""}
                    </small>
                  </>
                ) : (
                  <small>Not built yet</small>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
