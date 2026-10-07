import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import "../styles/signal/promotions.css";
import "../styles/signal/timetable.css";
import { createDefaultLadder, getPromotionOverview, getPromotionSetup, getPromotionTimetable } from "../api/promotionsApi.js";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import AppPanel from "../components/promotions/AppPanel.jsx";
import ApplicationsView from "../components/promotions/ApplicationsView.jsx";
import LadderView from "../components/promotions/LadderView.jsx";
import ReleasesView from "../components/promotions/ReleasesView.jsx";
import ReviewDialog from "../components/promotions/ReviewDialog.jsx";
import TimetableView from "../components/promotions/TimetableView.jsx";
import { ModeIcon, PrIcon } from "../components/promotions/icons.jsx";
import { formatRelative, gateQueue, planRelease } from "../components/promotions/promotionModel.js";
import { fmtTime } from "../components/promotions/timetableModel.js";
import { useAuth } from "../context/AuthContext";
import { useRouteParam, useRouteQuery } from "../routes/RouterContext.jsx";

const REFRESH_MS = 45000;

/**
 * Promotions: releases leave each environment on a timetable.
 *
 * Timetable is the work — the departures board, the selected release, the
 * tracker and the release graph. Applications is the estate at a glance;
 * Releases is what moved and what the ladder stopped; Ladder is the setup,
 * schedules included.
 */
export default function PromotionsPage() {
  const { hasPermission } = useAuth();
  const canManage = hasPermission("promotions:manage");
  const canDeploy = hasPermission("apps:deploy");

  const [tab, setTab] = useRouteParam("tab", "timetable");
  const [search, setSearch] = useRouteQuery("q", "");
  const [appKey, setAppKey] = useRouteQuery("app", "");
  const [depKey, setDepKey] = useRouteQuery("dep", "");
  const [tracked, setTracked] = useRouteQuery("track", "");

  const [setup, setSetup] = useState(null);
  const [overview, setOverview] = useState(null);
  const [timetable, setTimetable] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [releasesKey, setReleasesKey] = useState(0);
  const [panelReview, setPanelReview] = useState(null);
  const [toast, setToast] = useState("");
  const toastTimer = useRef(null);

  const showToast = useCallback((message) => {
    setToast(message);
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(""), 3600);
  }, []);

  const load = useCallback(async ({ refresh = false } = {}) => {
    setError("");
    try {
      const data = await getPromotionSetup();
      setSetup(data);
      if (data.environments.length) {
        const [ov, tt] = await Promise.all([getPromotionOverview({ refresh }), getPromotionTimetable()]);
        setOverview(ov);
        setTimetable(tt);
      } else {
        setOverview(null);
        setTimetable(null);
      }
    } catch (err) {
      setError(err.message || "Could not load the promotion ladder.");
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    load().finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [load]);

  // The board is live: departures board, cut-offs and statuses move on their own.
  useEffect(() => {
    if (!setup?.environments?.length) return undefined;
    const id = setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, REFRESH_MS);
    return () => clearInterval(id);
  }, [setup, load]);

  const refresh = async () => {
    setRefreshing(true);
    await load({ refresh: true });
    setReleasesKey((k) => k + 1);
    setRefreshing(false);
  };

  const changed = useCallback(async () => {
    setReleasesKey((k) => k + 1);
    await load({ refresh: true });
  }, [load]);

  const startLadder = async () => {
    setCreating(true);
    setError("");
    try {
      await createDefaultLadder();
      await load();
      setTab("ladder");
    } catch (err) {
      setError(err.message);
    } finally {
      setCreating(false);
    }
  };

  const environments = overview?.environments || setup?.environments || [];
  const hasLadder = (setup?.environments || []).length > 0;
  const openApp = useMemo(() => overview?.apps.find((a) => a.key === appKey) || null, [overview, appKey]);
  const attention = timetable ? timetable.departures.filter((d) => ["approval", "exception", "held", "failed", "refused", "rejected"].includes(d.status)).length : 0;

  const tabs = [
    { key: "timetable", label: "Timetable", count: attention, tone: "wait" },
    { key: "applications", label: "Applications", count: overview?.summary.applications, muted: true },
    { key: "releases", label: "Releases" },
    { key: "ladder", label: "Ladder" },
  ];

  return (
    <div className="ops-page pr-root">
      <div className="pr-head">
        <div className="pr-head-copy">
          <h2>Promotions</h2>
          <p>
            {hasLadder ? (
              <>
                Every release climbs{" "}
                {environments.map((env, i) => (
                  <span key={env.id} className="pr-head-env">
                    {i > 0 && <PrIcon.Arrow />}
                    {env.name}
                  </span>
                ))}{" "}
                on a timetable. Eligible applications join automatically; you settle the exceptions.
              </>
            ) : (
              "Make every release climb the same ladder of environments, on a schedule."
            )}
          </p>
        </div>
        {hasLadder && timetable && (
          <div className="pr-head-actions">
            <span className="tt-clock" title={`Times in ${timetable.timezone}`}>
              <b>{fmtTime(timetable.now, timetable.timezone)}</b>
              <span>{timetable.timezone}</span>
            </span>
            <span className="pr-scan" title={overview ? new Date(overview.scannedAt).toLocaleString() : ""}>
              Scanned {overview ? formatRelative(overview.scannedAt) : "—"}
            </span>
            <button type="button" className="btn-outline pr-refresh" onClick={refresh} disabled={refreshing}>
              <PrIcon.Refresh />
              {refreshing ? "Scanning…" : "Rescan"}
            </button>
          </div>
        )}
      </div>

      {error && <ErrorBanner message={error} />}

      {loading ? (
        <LoadingState label="Reading the timetable…" />
      ) : !hasLadder ? (
        <Intro canManage={canManage} creating={creating} onStart={startLadder} />
      ) : (
        <>
          {overview?.errors?.length > 0 && (
            <div className="pr-callout pr-callout--warn" role="status">
              <PrIcon.Warn />
              <p>
                Could not read {overview.errors.map((e) => e.clusterId).join(", ")} — those namespaces are missing until the cluster answers.
              </p>
            </div>
          )}

          <nav className="pr-tabs" aria-label="Promotion views">
            {tabs.map((entry) => (
              <button
                key={entry.key}
                type="button"
                className={`btn-ghost pr-tab${tab === entry.key ? " is-on" : ""}`}
                aria-current={tab === entry.key ? "page" : undefined}
                onClick={() => setTab(entry.key)}
              >
                {entry.label}
                {entry.count > 0 && (
                  <span className={`pr-tab-count${entry.muted ? " is-muted" : ""}${entry.tone === "wait" ? " is-wait" : ""}`} title={entry.tone === "wait" ? "Releases that need attention" : undefined}>
                    {entry.count}
                  </span>
                )}
              </button>
            ))}
          </nav>

          {!overview || !timetable ? (
            <LoadingState label="Scanning the environments…" />
          ) : tab === "timetable" ? (
            <TimetableView
              overview={overview}
              timetable={timetable}
              selectedKey={depKey || ""}
              onSelect={(key) => setDepKey(key)}
              tracked={tracked || ""}
              onTrack={(value) => setTracked(value || null)}
              canDeploy={canDeploy}
              canManage={canManage}
              onChanged={changed}
              onOpenApp={(key) => setAppKey(key)}
              onOpenLadder={() => setTab("ladder")}
              onToast={showToast}
            />
          ) : tab === "applications" ? (
            <ApplicationsView overview={overview} search={search || ""} onSearch={(value) => setSearch(value || null)} onOpenApp={(key) => setAppKey(key)} />
          ) : tab === "releases" ? (
            <ReleasesView environments={overview.environments} refreshKey={releasesKey} />
          ) : (
            setup && <LadderView setup={setup} overview={overview} canManage={canManage} onChanged={() => load({ refresh: true })} />
          )}
        </>
      )}

      {openApp && overview && (
        <AppPanel
          app={openApp}
          overview={overview}
          canDeploy={canDeploy}
          onClose={() => setAppKey(null)}
          onPromote={(gateIdx, image) => {
            const rows = new Map(gateQueue([openApp], gateIdx).map((r) => [r.app.key, r]));
            const plan = planRelease(new Map([[openApp.key, { image }]]), rows, overview.clusters);
            setPanelReview({ plan, from: overview.environments[gateIdx], to: overview.environments[gateIdx + 1] });
          }}
        />
      )}
      {panelReview && (
        <ReviewDialog
          plan={panelReview.plan}
          from={panelReview.from}
          to={panelReview.to}
          clusters={overview?.clusters}
          onRemove={() => setPanelReview(null)}
          onClose={() => setPanelReview(null)}
          onDone={async () => {
            setPanelReview(null);
            await changed();
          }}
        />
      )}
      <div className={`tt-toast${toast ? " is-on" : ""}`} role="status" aria-live="polite">
        {toast}
      </div>
    </div>
  );
}

function Intro({ canManage, creating, onStart }) {
  const steps = [
    { name: "Dev", note: "any build", mode: "entry" },
    { name: "SIT", note: "every 2 h", mode: "enforce" },
    { name: "UAT", note: "Tue & Thu 14:00", mode: "enforce" },
    { name: "Pre-prod", note: "Fri 10:00", mode: "enforce" },
  ];
  return (
    <section className="pr-intro" aria-label="About promotions">
      <div className="pr-intro-copy">
        <span className="pr-eyebrow">Promotion timetable</span>
        <h3>Every release runs on a timetable</h3>
        <p>
          Each environment hands over to the next on a fixed schedule. Applications that are eligible by the cut-off join the release
          automatically — 150 applications, no checkboxes. KubeSight holds every deploy to the order, from the UI, CI, tickets, Helm or an agent.
        </p>
        <ul>
          <li>
            <PrIcon.Asterisk />
            <span>
              <b>Environments are namespaces.</b> Bind them by name, by pattern such as <code>*-sit</code>, or a whole cluster at a time.
            </span>
          </li>
          <li>
            <PrIcon.Train />
            <span>
              <b>Releases leave on a schedule.</b> At the cut-off every eligible application becomes one release with one approval; the rest ride
              the next one.
            </span>
          </li>
          <li>
            <PrIcon.Hand />
            <span>
              <b>Skipping is an exception</b>, with a written reason and someone else&apos;s approval. Rollbacks are never blocked.
            </span>
          </li>
        </ul>
        {canManage ? (
          <div className="pr-intro-actions">
            <button type="button" className="primary" onClick={onStart} disabled={creating}>
              <PrIcon.Ladder />
              {creating ? "Creating…" : "Start with Dev → SIT → UAT → Pre-prod"}
            </button>
            <span className="pr-muted">Then bind namespaces and set each hop&apos;s schedule.</span>
          </div>
        ) : (
          <p className="pr-muted">No ladder yet — ask an administrator to set one up.</p>
        )}
      </div>
      <div className="pr-intro-visual" aria-hidden="true">
        {steps.map((step, index) => (
          <div key={step.name} className="pr-intro-step" style={{ "--i": index }}>
            <span className="pr-intro-step-name">{step.name}</span>
            <span className={`pr-mode pr-mode--${step.mode}`}>
              <ModeIcon mode={step.mode} />
              {step.mode === "entry" ? "Entry" : "Enforce"}
            </span>
            <span className="pr-intro-step-note">{step.note}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
