// Instant-paint placeholder that mirrors the Signal Overview layout so the page
// shows its structure (page header, KPI tiles, chart cards, list/feed cards)
// immediately on load instead of a blank "Loading…" screen. Swapped for the
// real dashboard once the summary lands — the matching layout keeps the
// transition from jumping.

function KpiSkeleton() {
  return (
    <div className="sg-kpi" aria-hidden="true">
      <span className="skeleton skeleton-text skeleton-text--sm" style={{ width: 90 }} />
      <div className="skeleton skeleton-text skeleton-text--xl" style={{ width: "50%", marginTop: 12 }} />
      <div className="skeleton skeleton-text skeleton-text--sm" style={{ width: "75%", marginTop: 10 }} />
    </div>
  );
}

function ChartCardSkeleton({ subtitle = 160, tall = false }) {
  return (
    <section className="ov-card" aria-hidden="true">
      <div className="ov-card-h">
        <span className="skeleton skeleton-text" style={{ width: 130, marginBottom: 0 }} />
        <span className="skeleton skeleton-text skeleton-text--sm" style={{ width: subtitle, marginBottom: 0 }} />
        <div className="ov-card-r">
          <span className="skeleton skeleton-text skeleton-text--sm" style={{ width: 90, marginBottom: 0 }} />
        </div>
      </div>
      <div className="ov-chart-wrap">
        <div className={`skeleton ov-chart-skeleton${tall ? " ov-chart-skeleton--tall" : ""}`} />
      </div>
    </section>
  );
}

function ListCardSkeleton({ rows = 5, title = 110 }) {
  return (
    <section className="ov-card" aria-hidden="true">
      <div className="ov-card-h">
        <span className="skeleton skeleton-text" style={{ width: title, marginBottom: 0 }} />
      </div>
      {Array.from({ length: rows }).map((_, i) => (
        <div className="skeleton-row" key={i}>
          <span className="skeleton skeleton-circle" style={{ width: 10, height: 10 }} />
          <span className="skeleton skeleton-text" style={{ width: "55%", marginBottom: 0 }} />
          <span className="skeleton skeleton-text skeleton-text--sm" style={{ width: 56, marginLeft: "auto", marginBottom: 0 }} />
        </div>
      ))}
    </section>
  );
}

export default function DashboardSkeleton() {
  return (
    <div className="ov-dash" role="status" aria-label="Loading dashboard">
      <header className="sg-ph" aria-hidden="true">
        <div>
          <div className="ov-title-line">
            <h1>Operations Dashboard</h1>
            <span className="skeleton skeleton-text" style={{ width: 78, height: 22, borderRadius: 999, marginBottom: 0 }} />
          </div>
          <div className="skeleton skeleton-text skeleton-text--sm" style={{ width: 280, marginTop: 8 }} />
        </div>
        <div className="sg-ph-actions">
          <span className="skeleton skeleton-text" style={{ width: 128, height: 28, borderRadius: 999, marginBottom: 0 }} />
          <span className="skeleton skeleton-text" style={{ width: 88, height: 32, borderRadius: 999, marginBottom: 0 }} />
        </div>
      </header>

      <div className="sg-kpi-grid">
        {Array.from({ length: 6 }).map((_, i) => (
          <KpiSkeleton key={i} />
        ))}
      </div>

      <div className="sg-row-2">
        <ChartCardSkeleton subtitle={170} tall />
        <ListCardSkeleton rows={6} title={100} />
      </div>

      <div className="sg-row-11">
        <ChartCardSkeleton subtitle={180} />
        <ChartCardSkeleton subtitle={150} />
      </div>

      <div className="sg-row-2">
        <ListCardSkeleton rows={5} title={120} />
        <ListCardSkeleton rows={4} title={110} />
      </div>
    </div>
  );
}
