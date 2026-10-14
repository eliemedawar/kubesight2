/** Upgrade Center — every cluster's Kubernetes version, support clock and upgrade.
 *
 *  Fleet: where each cluster sits on the release line and its next step.
 *  In progress: every upgrade waiting for approval, scheduled or running.
 *  History: every run with its report.
 *  #/upgrade/cluster/<id>: one cluster — target, readiness, plan and
 *  approval, the live run; or the guided runbook when KubeSight does not run
 *  the upgrade itself.
 *
 *  The page loads its own data (nothing in App.jsx): the fleet is one request
 *  that reads every cluster in parallel on the server, refreshed every 30 s
 *  while the tab is visible.
 */

import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import "../styles/signal/upgradeCenter.css";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import FleetView from "../components/upgrade/FleetView.jsx";
import { getUpgradeFleet } from "../api/upgradesApi.js";
import { usePermission } from "../hooks/usePermission.js";
import { setVisibleInterval } from "../lib/visibleInterval";
import { useRouter } from "../routes/RouterContext.jsx";
import { IconRefresh, timeAgo } from "../components/upgrade/upgradeUi.jsx";

const ClusterWorkspace = lazy(() => import("../components/upgrade/ClusterWorkspace.jsx"));
const InProgressView = lazy(() => import("../components/upgrade/InProgressView.jsx"));
const HistoryView = lazy(() => import("../components/upgrade/HistoryView.jsx"));

const TABS = [
  { key: "fleet", label: "Fleet" },
  { key: "progress", label: "In progress" },
  { key: "history", label: "History" },
];
const FLEET_POLL_MS = 30000;

export default function UpgradeCenterPage() {
  const { route, navigate } = useRouter();
  const { hasPermission, isAdmin } = usePermission();
  const clusterId = route.key === "upgradeCluster" ? route.params?.clusterId : null;
  const tab = route.key === "upgrade" ? route.params?.tab || "fleet" : null;

  const [fleet, setFleet] = useState(null);
  const [fleetError, setFleetError] = useState("");
  const [loading, setLoading] = useState(false);
  const loadSeq = useRef(0);

  const loadFleet = useCallback(async () => {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      const data = await getUpgradeFleet();
      if (seq === loadSeq.current) {
        setFleet(data);
        setFleetError("");
      }
    } catch (error) {
      if (seq === loadSeq.current) setFleetError(error.message);
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }, []);

  // The fleet feeds the Fleet and In-progress tabs; a workspace loads its own.
  const needsFleet = !clusterId && (tab === "fleet" || tab === "progress");
  useEffect(() => {
    if (!needsFleet) return undefined;
    loadFleet();
    return setVisibleInterval(loadFleet, FLEET_POLL_MS);
  }, [needsFleet, loadFleet]);

  const openCluster = useCallback(
    (id, query) => navigate({ key: "upgradeCluster", params: { clusterId: String(id) }, query: query || {} }),
    [navigate]
  );
  const openTab = (key) => navigate({ key: "upgrade", params: { tab: key } });

  if (clusterId) {
    return (
      <div className="uc-root ops-page">
        <Suspense fallback={<div className="uc-loading">Loading cluster…</div>}>
          <ClusterWorkspace
            key={clusterId}
            clusterId={clusterId}
            focusJobId={route.query?.job || null}
            canStart={hasPermission("upgrades:start")}
            isAdmin={isAdmin}
            onBack={() => openTab("fleet")}
          />
        </Suspense>
      </div>
    );
  }

  const inProgress = (fleet?.clusters || []).filter((row) => row.openJob).length;

  return (
    <div className="uc-root ops-page">
      <header className="uc-pagehead">
        <div className="uc-pagehead__title">
          <span className="uc-eyebrow">Infrastructure</span>
          <h1 className="uc-display uc-h1">Upgrade Center</h1>
        </div>
        <div className="uc-pagehead__actions">
          {fleet?.generatedAt && tab !== "history" ? (
            <span className="uc-sub" aria-live="polite">Updated {timeAgo(fleet.generatedAt)}</span>
          ) : null}
          {tab !== "history" ? (
            <button type="button" className="btn-outline uc-btn-sm" onClick={loadFleet} disabled={loading}>
              <IconRefresh /> {loading ? "Refreshing…" : "Refresh"}
            </button>
          ) : null}
        </div>
        <nav className="uc-tabs" aria-label="Upgrade Center sections">
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              className={`uc-tab${tab === t.key ? " is-on" : ""}`}
              aria-current={tab === t.key ? "page" : undefined}
              onClick={() => openTab(t.key)}
            >
              {t.label}
              {t.key === "progress" && inProgress ? <span className="uc-count">{inProgress}</span> : null}
            </button>
          ))}
        </nav>
      </header>

      {fleetError && tab !== "history" ? <ErrorBanner message={fleetError} /> : null}

      {tab === "fleet" ? (
        fleet ? (
          <FleetView data={fleet} onOpenCluster={openCluster} />
        ) : !fleetError ? (
          <FleetSkeleton />
        ) : null
      ) : null}

      <Suspense fallback={<div className="uc-loading">Loading…</div>}>
        {tab === "progress" ? (
          <InProgressView fleet={fleet} loading={!fleet && !fleetError} onOpenCluster={openCluster} />
        ) : null}
        {tab === "history" ? <HistoryView onOpenCluster={openCluster} /> : null}
      </Suspense>
    </div>
  );
}

function FleetSkeleton() {
  return (
    <div className="uc-skeleton" aria-busy="true" aria-label="Loading the fleet">
      <div className="uc-skel uc-skel--title" />
      <div className="uc-skel-row">
        {[0, 1, 2, 3, 4].map((i) => (
          <div key={i} className="uc-skel uc-skel--tile" />
        ))}
      </div>
      <div className="uc-skel uc-skel--lane" />
      <div className="uc-skel uc-skel--table" />
    </div>
  );
}
