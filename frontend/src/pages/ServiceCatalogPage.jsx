import { useCallback, useEffect, useRef, useState } from "react";
import { createCiService, listCiServices } from "../api/ciApi.js";
import { useAuth } from "../context/AuthContext";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import RunBuildModal from "../components/catalog/RunBuildModal.jsx";
import RunnersModal from "../components/catalog/RunnersModal.jsx";
import ServiceCard from "../components/catalog/ServiceCard.jsx";
import ServiceFormModal from "../components/catalog/ServiceFormModal.jsx";
import ServiceDetailPage from "./ServiceDetailPage.jsx";
import {
  APPLICATION_TYPES,
  PlusIcon,
  RunnerIcon,
  SearchIcon,
  isBuildActive,
} from "../components/catalog/ciShared.jsx";

const REFRESH_MS = 4000;

// Health-strip tiles ARE the filters (the Alerts pattern): each shows a live
// count and clicking it narrows the grid to exactly the cards it counted.
const TILES = [
  { key: "all", label: "Services", countKey: "total", tone: "" },
  { key: "building", label: "Building now", countKey: "building", tone: "run" },
  { key: "failing", label: "Failing", countKey: "failing", tone: "bad" },
  { key: "queued", label: "Queued", countKey: "queued", tone: "" },
  { key: "needsSetup", label: "Needs setup", countKey: "needsSetup", tone: "warn" },
];

const tileMatch = {
  all: () => true,
  building: (s) => s.latestBuild?.status === "running",
  failing: (s) => ["failed", "timeout"].includes(s.latestBuild?.status),
  queued: (s) => s.latestBuild?.status === "queued",
  needsSetup: (s) => !(s.sourceConfigured && s.pipelineConfigured),
};

/**
 * The CI Service Catalog — a build floor, not a list.
 *
 * The strip answers "is everything building?" before a single card is read;
 * cards are verdicts with the Run action in reach; anything incomplete carries
 * its own fix. Polls only while something is actually building or queued.
 */
export default function ServiceCatalogPage({ clusters = [] }) {
  const { hasPermission } = useAuth();
  const canView = hasPermission("ci_services:view");
  const canCreate = hasPermission("ci_services:create");
  const canRun = hasPermission("ci_builds:run");
  const canViewRunners = hasPermission("ci_runners:view");
  const canManageRunners = hasPermission("ci_runners:manage");

  const [services, setServices] = useState([]);
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [tile, setTile] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");
  // {serviceId, tab?, buildId?} — deep links from cards land on the right tab.
  const [opened, setOpened] = useState(null);
  const [creating, setCreating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const timerRef = useRef(null);

  const load = useCallback(async ({ background = false } = {}) => {
    if (!background) setLoading(true);
    try {
      const data = await listCiServices();
      setServices(data.items || []);
      setSummary(data.summary || null);
      setError("");
      return (data.items || []).some(
        (item) => item.latestBuild && isBuildActive(item.latestBuild.status)
      );
    } catch (err) {
      setError(err.message || "Could not load the service catalog.");
      return false;
    } finally {
      setLoading(false);
    }
  }, []);

  // Poll only while a build is in flight — a quiet catalog costs nothing.
  useEffect(() => {
    if (!canView || opened) return undefined;
    let cancelled = false;
    const tick = async (background) => {
      const active = await load({ background });
      if (cancelled || !active) return;
      timerRef.current = window.setTimeout(() => tick(true), REFRESH_MS);
    };
    tick(false);
    return () => {
      cancelled = true;
      window.clearTimeout(timerRef.current);
    };
  }, [canView, opened, load]);

  const handleCreate = async (payload) => {
    setSaving(true);
    setSaveError("");
    try {
      const created = await createCiService(payload);
      setCreating(false);
      // Straight into the new service: the next step is connecting its source.
      setOpened({ serviceId: created.id, tab: "source" });
    } catch (err) {
      setSaveError(err.message || "Could not register the service.");
    } finally {
      setSaving(false);
    }
  };

  // Run from a card opens the ref picker in place; only after the build has
  // actually started does the view jump into the service's Builds tab.
  const [runFor, setRunFor] = useState(null);
  const [showRunners, setShowRunners] = useState(false);

  if (!canView) return <AccessDeniedPage />;

  if (opened) {
    return (
      <ServiceDetailPage
        serviceId={opened.serviceId}
        initialTab={opened.tab}
        initialBuildId={opened.buildId}
        clusters={clusters}
        onBack={() => setOpened(null)}
        onDeleted={() => setOpened(null)}
      />
    );
  }

  const filtered = services.filter((service) => {
    if (!tileMatch[tile](service)) return false;
    if (typeFilter !== "all" && service.applicationType !== typeFilter) return false;
    if (!search) return true;
    const term = search.toLowerCase();
    return (
      service.name.toLowerCase().includes(term) ||
      (service.ownerTeam || "").toLowerCase().includes(term) ||
      (service.repositoryName || "").toLowerCase().includes(term)
    );
  });

  const subtitle = loading
    ? "Applications KubeSight builds — source, pipeline, builds, artifacts."
    : summary?.failing
    ? `${summary.failing} service${summary.failing === 1 ? "" : "s"} failing`
    : summary?.building
    ? `${summary.building} build${summary.building === 1 ? "" : "s"} running`
    : "All quiet — every service green.";

  return (
    <div className="ops-page">
      <div className="sg-ph">
        <div>
          <h2>Service Catalog</h2>
          <p className="sg-ph-sub">{subtitle}</p>
        </div>
        <div className="sg-ph-actions">
          {canViewRunners && (
            <button
              type="button"
              className="btn-outline sg-cat-new"
              onClick={() => setShowRunners(true)}
            >
              <RunnerIcon />
              Runners
            </button>
          )}
          {canCreate && (
            <button
              type="button"
              className="primary sg-cat-new"
              onClick={() => {
                setSaveError("");
                setCreating(true);
              }}
            >
              <PlusIcon />
              Register service
            </button>
          )}
        </div>
      </div>

      {error && <ErrorBanner message={error} />}

      {summary && (
        <div className="sg-ci-health" role="group" aria-label="Catalog health — click to filter">
          {TILES.map((entry) => (
            <button
              key={entry.key}
              type="button"
              className={`sg-ci-tile sg-ci-tile--${entry.tone}${
                tile === entry.key ? " is-on" : ""
              }`}
              aria-pressed={tile === entry.key}
              onClick={() => setTile(tile === entry.key ? "all" : entry.key)}
            >
              <b>{summary[entry.countKey] ?? 0}</b>
              <span>{entry.label}</span>
            </button>
          ))}
        </div>
      )}

      <div className="sg-cat-toolbar">
        <select
          className="sg-ci-type-filter"
          value={typeFilter}
          aria-label="Filter by application type"
          onChange={(event) => setTypeFilter(event.target.value)}
        >
          <option value="all">All types</option>
          {APPLICATION_TYPES.map((type) => (
            <option key={type.value} value={type.value}>
              {type.label}
            </option>
          ))}
        </select>
        <label className="sg-cat-search">
          <SearchIcon />
          <input
            type="search"
            placeholder="Search by name, team, or repository…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            aria-label="Search services"
          />
        </label>
      </div>

      {loading ? (
        <LoadingState label="Loading service catalog…" />
      ) : filtered.length === 0 ? (
        <EmptyState
          message={
            services.length
              ? tile !== "all"
                ? `No services match “${TILES.find((t) => t.key === tile)?.label}”.`
                : "No services match those filters."
              : "No services registered yet."
          }
          hint={
            services.length
              ? "Clear the filter or adjust the search."
              : "Register an application, connect its Bitbucket repository, and define how it builds."
          }
        />
      ) : (
        <div className="sg-card-grid">
          {filtered.map((service) => (
            <ServiceCard
              key={service.id}
              service={service}
              canRun={canRun}
              onOpen={(tab) =>
                setOpened({
                  serviceId: service.id,
                  tab: typeof tab === "string" ? tab : undefined,
                })
              }
              onOpenBuild={(buildId) =>
                setOpened({ serviceId: service.id, tab: "builds", buildId })
              }
              onRun={() => setRunFor(service)}
            />
          ))}
        </div>
      )}

      {creating && (
        <ServiceFormModal
          onClose={() => setCreating(false)}
          onSave={handleCreate}
          saving={saving}
          error={saveError}
        />
      )}

      {showRunners && (
        <RunnersModal canManage={canManageRunners} onClose={() => setShowRunners(false)} />
      )}

      {runFor && (
        <RunBuildModal
          service={runFor}
          onClose={() => setRunFor(null)}
          onStarted={(build) => {
            setRunFor(null);
            setOpened({ serviceId: runFor.id, tab: "builds", buildId: build.id });
          }}
        />
      )}
    </div>
  );
}
