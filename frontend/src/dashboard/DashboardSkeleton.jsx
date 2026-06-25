// Instant-paint placeholder that mirrors the OpsDashboard layout so the page
// shows its structure (KPI strip, chart panels, tables) immediately on load
// instead of a blank "Loading…" screen. Swapped for the real dashboard once the
// summary lands — the matching layout keeps the transition from jumping.

function KpiSkeleton() {
  return (
    <div className="ops-kpi" aria-hidden="true">
      <div className="ops-kpi-head">
        <span className="skeleton skeleton-text skeleton-text--sm" style={{ width: 70 }} />
      </div>
      <div className="skeleton skeleton-text skeleton-text--xl" style={{ width: "55%", marginTop: 10 }} />
      <div className="skeleton skeleton-text skeleton-text--sm" style={{ width: "80%" }} />
    </div>
  );
}

function ChartPanelSkeleton({ subtitle }) {
  return (
    <section className="ops-panel" aria-hidden="true">
      <div className="ops-panel-head">
        <div>
          <div className="skeleton skeleton-text" style={{ width: 140 }} />
          <div className="skeleton skeleton-text skeleton-text--sm" style={{ width: subtitle, marginTop: 6 }} />
        </div>
        <div className="skeleton skeleton-text skeleton-text--lg" style={{ width: 56 }} />
      </div>
      <div className="skeleton ops-chart-skeleton" />
    </section>
  );
}

export default function DashboardSkeleton() {
  return (
    <div className="ops-dash ops-dash--loading" role="status" aria-label="Loading dashboard">
      <div className="ops-titlerow">
        <div>
          <div className="ops-title-line">
            <h1 className="ops-title">Operations Dashboard</h1>
            <span className="skeleton skeleton-text" style={{ width: 78, height: 22, borderRadius: 999 }} />
          </div>
          <div className="skeleton skeleton-text skeleton-text--sm" style={{ width: 260, marginTop: 8 }} />
        </div>
      </div>

      <div className="ops-kpi-grid">
        {Array.from({ length: 6 }).map((_, i) => (
          <KpiSkeleton key={i} />
        ))}
      </div>

      <div className="ops-chart-row">
        <ChartPanelSkeleton subtitle={160} />
        <ChartPanelSkeleton subtitle={180} />
      </div>

      <section className="ops-panel" aria-hidden="true">
        <div className="ops-panel-head">
          <div>
            <div className="skeleton skeleton-text" style={{ width: 110 }} />
            <div className="skeleton skeleton-text skeleton-text--sm" style={{ width: 150, marginTop: 6 }} />
          </div>
        </div>
        <div className="skeleton ops-chart-skeleton" />
      </section>

      <div className="ops-grid ops-grid--wide-left">
        <section className="ops-panel ops-panel--flush" aria-hidden="true">
          <div className="ops-panel-bar">
            <div className="skeleton skeleton-text" style={{ width: 130 }} />
          </div>
          {Array.from({ length: 5 }).map((_, i) => (
            <div className="skeleton-row" key={i}>
              <span className="skeleton skeleton-text" style={{ width: "30%" }} />
              <span className="skeleton skeleton-text skeleton-text--sm" style={{ width: 60 }} />
            </div>
          ))}
        </section>
        <section className="ops-panel ops-panel--flush ops-panel--col" aria-hidden="true">
          <div className="ops-panel-bar">
            <div className="skeleton skeleton-text" style={{ width: 110 }} />
          </div>
          {Array.from({ length: 4 }).map((_, i) => (
            <div className="skeleton-row" key={i}>
              <span className="skeleton skeleton-circle" style={{ width: 10, height: 10 }} />
              <span className="skeleton skeleton-text" style={{ width: "70%" }} />
            </div>
          ))}
        </section>
      </div>
    </div>
  );
}
