import { useCallback, useEffect, useRef, useState } from "react";
import { listCiServices } from "../api/ciApi.js";
import { useAuth } from "../context/AuthContext";
import { useRouter } from "../routes/RouterContext.jsx";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import RunBuildModal from "../components/catalog/RunBuildModal.jsx";
import RunnersModal from "../components/catalog/RunnersModal.jsx";
import ServiceCard from "../components/catalog/ServiceCard.jsx";
import RegisterServiceWizard from "../components/catalog/RegisterServiceWizard.jsx";
import ServiceDetailPage from "./ServiceDetailPage.jsx";
import {
  APPLICATION_TYPES,
  PlusIcon,
  RunnerIcon,
  SearchIcon,
  isBuildActive,
} from "../components/catalog/ciShared.jsx";

const REFRESH_MS = 4000;
// A poll that fails is almost always a blip -- the backend rolling, a reset
// connection -- not an outage. Back off instead of giving up, and stay quiet
// until the failures start to look like a pattern rather than a hiccup.
const RETRY_MS = 8000;
const MAX_RETRY_MS = 60000;
const FAILURES_BEFORE_BANNER = 3;
const retryDelay = (failures) =>
  Math.min(RETRY_MS * 2 ** Math.max(0, failures - 1), MAX_RETRY_MS);

// `fetch` rejects with a bare "Failed to fetch" when the request never reached
// the backend at all. That is the browser's wording, not ours, and it tells the
// reader nothing -- anything that carries a status came from the API and says
// something useful, so only the native strings get replaced.
const NATIVE_FETCH_FAILURES = new Set([
  "Failed to fetch",
  "Load failed",
  "NetworkError when attempting to fetch resource.",
]);
const describeError = (err) =>
  !err?.status && NATIVE_FETCH_FAILURES.has(err?.message)
    ? "Lost contact with the backend. Retrying..."
    : err?.message || "Could not load the service catalog.";

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
  // The open service is an address: /service-catalog/:serviceId/:tab?build=
  // Back closes it, and a link reproduces the exact tab and build.
  const { route, params: routeParams, query: routeQuery, navigate } = useRouter();
  const opened =
    route.key === "serviceDetail"
      ? {
          serviceId: routeParams.serviceId,
          tab: routeParams.tab,
          buildId: routeQuery.build || undefined,
        }
      : null;
  const setOpened = useCallback(
    (next) => {
      if (!next) {
        navigate({ key: "serviceCatalog" });
        return;
      }
      navigate({
        key: "serviceDetail",
        params: { serviceId: String(next.serviceId), tab: next.tab || "overview" },
        query: next.buildId ? { build: String(next.buildId) } : {},
      });
    },
    [navigate]
  );
  const [creating, setCreating] = useState(false);
  const timerRef = useRef(null);

  const failuresRef = useRef(0);

  const load = useCallback(async ({ background = false } = {}) => {
    if (!background) setLoading(true);
    try {
      const data = await listCiServices();
      setServices(data.items || []);
      setSummary(data.summary || null);
      failuresRef.current = 0;
      setError("");
      return {
        ok: true,
        active: (data.items || []).some(
          (item) => item.latestBuild && isBuildActive(item.latestBuild.status)
        ),
      };
    } catch (err) {
      failuresRef.current += 1;
      // A background tick that misses once while the grid is already on screen
      // is not worth a banner: the next one almost always repaints it.
      if (!background || failuresRef.current >= FAILURES_BEFORE_BANNER) {
        setError(describeError(err));
      }
      return { ok: false, active: false };
    } finally {
      setLoading(false);
    }
  }, []);

  // Poll only while a build is in flight — a quiet catalog costs nothing.
  useEffect(() => {
    if (!canView || opened) return undefined;
    let cancelled = false;
    failuresRef.current = 0;
    const tick = async (background) => {
      const { ok, active } = await load({ background });
      if (cancelled) return;
      // A failed poll must never be read as "nothing is building". That used to
      // end the chain on the first blip, freezing the strip and leaving a stale
      // banner over live data with nothing left running to clear it.
      if (!ok) {
        timerRef.current = window.setTimeout(
          () => tick(true),
          retryDelay(failuresRef.current)
        );
        return;
      }
      if (!active) return;
      timerRef.current = window.setTimeout(() => tick(true), REFRESH_MS);
    };
    tick(false);
    return () => {
      cancelled = true;
      window.clearTimeout(timerRef.current);
    };
  }, [canView, opened, load]);

  // Registration is a flow now, and the wizard owns it: identity, repository,
  // and the choice between letting Hermes work the rest out or doing it by
  // hand. It creates the service itself so that a failed analysis still leaves
  // a real, usable service behind — the catalog only has to refresh and, when
  // the wizard says so, land on it.
  const handleRegistered = () => {
    load({ background: true });
  };

  const handleWizardDone = (service, tab) => {
    setCreating(false);
    setOpened({ serviceId: service.id, tab: tab || "pipeline" });
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
              onClick={() => setCreating(true)}
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
        <RegisterServiceWizard
          onClose={() => {
            setCreating(false);
            load({ background: true });
          }}
          onCreated={handleRegistered}
          onOpenService={handleWizardDone}
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
