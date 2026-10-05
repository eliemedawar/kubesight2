import { useCallback, useEffect, useRef, useState } from "react";
import "../styles/signal/sharedPipelines.css";
import { createSharedPipeline, listCiServices, listSharedPipelines } from "../api/ciApi.js";
import { useAuth } from "../context/AuthContext";
import { useRouter } from "../routes/RouterContext.jsx";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import RunBuildModal from "../components/catalog/RunBuildModal.jsx";
import ServiceDetailPage from "./ServiceDetailPage.jsx";
import {
  APPLICATION_TYPES,
  FAILURES_BEFORE_BANNER,
  PlayIcon,
  PlusIcon,
  RepoIcon,
  SearchIcon,
  Sparkline,
  StatusPill,
  describeError,
  formatDuration,
  formatRelative,
  isBuildActive,
  retryDelay,
} from "../components/catalog/ciShared.jsx";
import { PlIcon } from "../components/catalog/pipeline/icons.jsx";

const REFRESH_MS = 4000;

const TILES = [
  { key: "all", label: "Pipelines", countKey: "total", tone: "" },
  { key: "shared", label: "Used by services", countKey: "shared", tone: "" },
  { key: "running", label: "Running now", countKey: "running", tone: "run" },
  { key: "failing", label: "Failing", countKey: "failing", tone: "bad" },
];

const tileMatch = {
  all: () => true,
  shared: (item) => item.usedByCount > 0,
  running: (item) => ["running", "queued"].includes(item.latestBuild?.status),
  failing: (item) => ["failed", "timeout"].includes(item.latestBuild?.status),
};

/**
 * The Pipelines page: pipelines that live outside any CI service.
 *
 * One list for both of their lives — a pipeline runs on its own (a job with no
 * application behind it, a repository only if it needs one), and CI services
 * can build with it instead of their own stages. Each card says which: its last
 * run, and how many services use it.
 */
export default function PipelinesPage() {
  const { hasPermission } = useAuth();
  const canView = hasPermission("ci_pipelines:view");
  const canCreate = hasPermission("ci_services:create") && hasPermission("ci_pipelines:edit");
  const canRun = hasPermission("ci_builds:run");

  const [items, setItems] = useState([]);
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [tile, setTile] = useState("all");
  const [creating, setCreating] = useState(false);
  const [runFor, setRunFor] = useState(null);
  const timerRef = useRef(null);
  const failuresRef = useRef(0);

  const { route, params, query, navigate } = useRouter();
  const opened =
    route.key === "pipelineDetail"
      ? { id: params.pipelineId, tab: params.tab, buildId: query.build || undefined }
      : null;
  const open = useCallback(
    (next) => {
      if (!next) {
        navigate({ key: "pipelines" });
        return;
      }
      navigate({
        key: "pipelineDetail",
        params: { pipelineId: String(next.id), tab: next.tab || "pipeline" },
        query: next.buildId ? { build: String(next.buildId) } : {},
      });
    },
    [navigate]
  );

  const load = useCallback(async ({ background = false } = {}) => {
    if (!background) setLoading(true);
    try {
      const data = await listSharedPipelines();
      setItems(data.items || []);
      setSummary(data.summary || null);
      failuresRef.current = 0;
      setError("");
      return {
        ok: true,
        active: (data.items || []).some((item) => item.latestBuild && isBuildActive(item.latestBuild.status)),
      };
    } catch (err) {
      failuresRef.current += 1;
      if (!background || failuresRef.current >= FAILURES_BEFORE_BANNER) {
        setError(describeError(err, "Could not load the pipelines."));
      }
      return { ok: false, active: false };
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!canView || opened) return undefined;
    let cancelled = false;
    failuresRef.current = 0;
    const tick = async (background) => {
      const { ok, active } = await load({ background });
      if (cancelled) return;
      if (!ok) {
        timerRef.current = window.setTimeout(() => tick(true), retryDelay(failuresRef.current));
        return;
      }
      if (active) timerRef.current = window.setTimeout(() => tick(true), REFRESH_MS);
    };
    tick(false);
    return () => {
      cancelled = true;
      window.clearTimeout(timerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canView, Boolean(opened), load]);

  if (!canView) return <AccessDeniedPage />;

  if (opened) {
    return (
      <ServiceDetailPage
        variant="pipeline"
        serviceId={opened.id}
        initialTab={opened.tab}
        initialBuildId={opened.buildId}
        onBack={() => open(null)}
        onDeleted={() => open(null)}
      />
    );
  }

  const term = search.trim().toLowerCase();
  const filtered = items.filter(
    (item) =>
      tileMatch[tile](item) &&
      (!term ||
        item.name.toLowerCase().includes(term) ||
        (item.description || "").toLowerCase().includes(term) ||
        (item.stageNames || []).some((name) => name.toLowerCase().includes(term)))
  );

  return (
    <div className="ops-page">
      <div className="sg-ph">
        <div>
          <h2>Pipelines</h2>
          <p className="sg-ph-sub">
            Pipelines that live outside any CI service. Run one on its own, or let services build with it.
          </p>
        </div>
        <div className="sg-ph-actions">
          {canCreate && (
            <button type="button" className="primary sg-cat-new" onClick={() => setCreating(true)}>
              <PlusIcon />
              New pipeline
            </button>
          )}
        </div>
      </div>

      {error && <ErrorBanner message={error} />}

      {summary && items.length > 0 && (
        <div className="sg-ci-health" role="group" aria-label="Pipelines — click to filter">
          {TILES.map((entry) => (
            <button
              key={entry.key}
              type="button"
              className={`sg-ci-tile sg-ci-tile--${entry.tone}${tile === entry.key ? " is-on" : ""}`}
              aria-pressed={tile === entry.key}
              onClick={() => setTile(tile === entry.key ? "all" : entry.key)}
            >
              <b>{summary[entry.countKey] ?? 0}</b>
              <span>{entry.label}</span>
            </button>
          ))}
        </div>
      )}

      {items.length > 0 && (
        <div className="sg-cat-toolbar">
          <label className="sg-cat-search">
            <SearchIcon />
            <input
              type="search"
              placeholder="Search by name, description or stage…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              aria-label="Search pipelines"
            />
          </label>
        </div>
      )}

      {loading ? (
        <LoadingState label="Loading pipelines…" />
      ) : items.length === 0 ? (
        <section className="sp-intro" aria-label="About pipelines">
          <div className="sp-intro-copy">
            <h3>No pipelines yet</h3>
            <p>A pipeline here belongs to no service, so you can use it two ways:</p>
            <ul>
              <li>
                <PlIcon name="forward" />
                <span>
                  <strong>Run it on its own.</strong> A job with no application behind it, such as a nightly
                  backup, a cleanup or a release train. It has its own builds, schedules and secrets. It only
                  needs a repository if it checks code out.
                </span>
              </li>
              <li>
                <PlIcon name="link" />
                <span>
                  <strong>Share it.</strong> Write the stages once and let CI services build with them. Each
                  service still builds its own repository and keeps its own history. A change here reaches every
                  service from its next build.
                </span>
              </li>
            </ul>
            {canCreate && (
              <button type="button" className="primary sg-cat-new" onClick={() => setCreating(true)}>
                <PlusIcon />
                New pipeline
              </button>
            )}
          </div>
        </section>
      ) : filtered.length === 0 ? (
        <EmptyState message="No pipelines match." hint="Clear the filter or adjust the search." />
      ) : (
        <div className="sg-card-grid">
          {filtered.map((item) => (
            <PipelineCard
              key={item.id}
              item={item}
              canRun={canRun}
              onOpen={(tab) => open({ id: item.id, tab: typeof tab === "string" ? tab : undefined })}
              onOpenBuild={(buildId) => open({ id: item.id, tab: "builds", buildId })}
              onRun={() => setRunFor(item)}
            />
          ))}
        </div>
      )}

      {creating && (
        <NewPipelineModal
          onClose={() => setCreating(false)}
          onCreated={(created) => {
            setCreating(false);
            open({ id: created.id, tab: "pipeline" });
          }}
        />
      )}

      {runFor && (
        <RunBuildModal
          service={runFor}
          onClose={() => setRunFor(null)}
          onStarted={(build) => {
            setRunFor(null);
            open({ id: runFor.id, tab: "builds", buildId: build.id });
          }}
        />
      )}
    </div>
  );
}

function PipelineCard({ item, canRun, onOpen, onOpenBuild, onRun }) {
  const build = item.latestBuild;
  const active = Boolean(build && isBuildActive(build.status));
  const stages = item.stageNames || [];
  const empty = stages.length === 0;
  const readyToRun = canRun && !empty && item.status === "active";

  const onKeyDown = (event) => {
    if (event.target !== event.currentTarget) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onOpen();
    }
    if ((event.key === "r" || event.key === "R") && readyToRun && !active) {
      event.preventDefault();
      onRun();
    }
  };

  return (
    <article
      className={`sg-ccard sg-ccard--clickable sg-ci-card sp-card${empty ? " sg-ci-card--setup" : ""}`}
      role="button"
      tabIndex={0}
      onClick={() => onOpen()}
      onKeyDown={onKeyDown}
      aria-label={`Open pipeline ${item.name}`}
    >
      <header>
        <span className="sg-ico sg-ico--muted sp-card-ico">
          <PlIcon name="stages" />
        </span>
        <div className="sg-ci-card-id">
          <b>{item.name}</b>
          <span className="sg-ccard-sub">{item.description || "No description"}</span>
        </div>
        {build ? (
          <StatusPill status={build.status}>{`#${build.number} ${build.status}`}</StatusPill>
        ) : (
          <span className="status-pill unknown">never run</span>
        )}
      </header>

      <ol className="sp-card-stages" aria-label="Stages">
        {stages.slice(0, 5).map((name, index) => (
          <li key={`${name}-${index}`}>{name}</li>
        ))}
        {stages.length > 5 && <li className="is-more">+{stages.length - 5}</li>}
        {empty && <li className="is-empty">No stages yet</li>}
      </ol>

      <div className="sg-ci-card-build">
        <Sparkline statuses={item.recentBuildStatuses} />
        {build ? (
          <button
            type="button"
            className={`sg-ci-verdict sg-ci-verdict--${build.status}`}
            onClick={(event) => {
              event.stopPropagation();
              onOpenBuild(build.id);
            }}
            title={`Open run #${build.number}`}
          >
            {build.status === "running"
              ? build.currentStage
                ? `stage ${build.stageProgress} · ${build.currentStage}`
                : "starting…"
              : (build.status === "failed" || build.status === "timeout") && build.failedStage
                ? `failed in ${build.failedStage} · ${formatRelative(build.finishedAt)}`
                : `${build.status} ${formatRelative(build.finishedAt || build.queuedAt)}`}
          </button>
        ) : (
          <span className="muted sg-ci-verdict-text">Created {formatRelative(item.createdAt)}</span>
        )}
        {build?.durationSeconds != null && <span className="sg-ci-duration">{formatDuration(build.durationSeconds)}</span>}
      </div>

      <footer>
        <button
          type="button"
          className={`btn-ghost sg-tag sp-tag${item.usedByCount ? " is-shared" : ""}`}
          onClick={(event) => {
            event.stopPropagation();
            onOpen("usedBy");
          }}
          title="Which CI services build with it"
        >
          {item.usedByCount
            ? `Used by ${item.usedByCount} service${item.usedByCount === 1 ? "" : "s"}`
            : "Not used by a service"}
        </button>
        <span className="sg-tag sp-tag" title={item.repositoryUrl || "Runs without a repository"}>
          <RepoIcon />
          {item.sourceConfigured ? `${item.repositoryWorkspace}/${item.repositoryName}` : "No repository"}
        </span>
        {empty && (
          <button
            type="button"
            className="sg-tag sg-ci-tag--todo sg-ci-tag--action"
            onClick={(event) => {
              event.stopPropagation();
              onOpen("pipeline");
            }}
          >
            Stages needed → Add
          </button>
        )}
        {item.status !== "active" && <span className="sg-tag sg-ci-tag--todo">turned off</span>}
        {readyToRun && (
          <button
            type="button"
            className="sg-ci-card-run"
            disabled={active}
            title={active ? `Run #${build.number} is ${build.status}` : "Run it now (R)"}
            onClick={(event) => {
              event.stopPropagation();
              if (!active) onRun();
            }}
          >
            <PlayIcon />
            Run
          </button>
        )}
      </footer>
    </article>
  );
}

const START_OPTIONS = [
  { value: "empty", label: "Empty", hint: "Add the stages yourself." },
  { value: "template", label: "A starter", hint: "The stages KubeSight uses for an application type." },
  { value: "service", label: "A CI service's pipeline", hint: "A copy of what a service builds with today." },
];

function NewPipelineModal({ onClose, onCreated }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [start, setStart] = useState("empty");
  const [applicationType, setApplicationType] = useState("java_gradle");
  const [serviceId, setServiceId] = useState("");
  const [services, setServices] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (start !== "service" || services !== null) return;
    listCiServices()
      .then((data) => setServices(data.items || []))
      .catch(() => setServices([]));
  }, [start, services]);

  const canSave = name.trim() && (start !== "service" || serviceId) && !saving;

  const submit = async (event) => {
    event?.preventDefault();
    if (!canSave) return;
    setSaving(true);
    setError("");
    try {
      const created = await createSharedPipeline({
        name: name.trim(),
        description: description.trim(),
        applicationType: start === "template" ? applicationType : "generic",
        startFrom:
          start === "template"
            ? { type: "template", applicationType }
            : start === "service"
              ? { type: "service", serviceId: Number(serviceId) }
              : { type: "empty" },
      });
      onCreated(created);
    } catch (err) {
      setError(err.message || "Could not create the pipeline.");
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <form
        className="modal-card sp-new"
        role="dialog"
        aria-label="New pipeline"
        onClick={(event) => event.stopPropagation()}
        onSubmit={submit}
      >
        <div className="modal-card__header">
          <h3>New pipeline</h3>
          <p className="muted">It belongs to no service. Run it on its own, or let services build with it.</p>
        </div>

        {error && <p className="banner-message error">{error}</p>}

        <div className="form-grid">
          <label className="form-grid__full">
            Name
            <input
              value={name}
              maxLength={160}
              placeholder="Java Gradle standard"
              autoFocus
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label className="form-grid__full">
            Description
            <input
              value={description}
              maxLength={2000}
              placeholder="What it does, and who should use it"
              onChange={(event) => setDescription(event.target.value)}
            />
          </label>
        </div>

        <fieldset className="sp-start">
          <legend>Start from</legend>
          {START_OPTIONS.map((option) => (
            <label key={option.value} className={`sp-start-option${start === option.value ? " is-on" : ""}`}>
              <input
                type="radio"
                name="sp-start"
                value={option.value}
                checked={start === option.value}
                onChange={() => setStart(option.value)}
              />
              <span>
                <strong>{option.label}</strong>
                <small>{option.hint}</small>
              </span>
            </label>
          ))}
        </fieldset>

        {start === "template" && (
          <label className="sp-start-detail">
            Application type
            <select value={applicationType} onChange={(event) => setApplicationType(event.target.value)}>
              {APPLICATION_TYPES.filter((type) => !type.legacy).map((type) => (
                <option key={type.value} value={type.value}>
                  {type.label}
                </option>
              ))}
            </select>
          </label>
        )}
        {start === "service" && (
          <label className="sp-start-detail">
            CI service
            <select value={serviceId} onChange={(event) => setServiceId(event.target.value)}>
              <option value="">{services === null ? "Loading…" : "Pick a service…"}</option>
              {(services || []).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                  {item.sharedPipeline ? ` (uses ${item.sharedPipeline.name})` : ""}
                </option>
              ))}
            </select>
            <span className="field-hint">
              Its stages, build inputs and post actions are copied. The service itself is not changed. To make it
              use the new pipeline, open the Used by tab afterwards.
            </span>
          </label>
        )}

        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button type="submit" className="primary sg-cat-new" disabled={!canSave}>
            <PlusIcon />
            {saving ? "Creating…" : "Create pipeline"}
          </button>
        </div>
      </form>
    </div>
  );
}
