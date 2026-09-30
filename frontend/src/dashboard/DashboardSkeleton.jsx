// Instant-paint placeholder with the dashboard's own shape (header, attention
// strip, four vitals, node table, two lists) so nothing jumps when the summary
// lands.

export default function DashboardSkeleton() {
  return (
    <div className="db-root" aria-busy="true" aria-label="Loading dashboard">
      <header className="sg-ph db-head" aria-hidden="true">
        <div>
          <span className="skeleton skeleton-text skeleton-text--xl" style={{ width: 260 }} />
          <span className="skeleton skeleton-text skeleton-text--sm" style={{ width: 320, marginTop: 8 }} />
        </div>
      </header>
      <span className="skeleton db-skel-block db-skel-attn" aria-hidden="true" />
      <div className="db-vitals" aria-hidden="true">
        {[0, 1, 2].map((i) => (
          <span key={i} className="skeleton db-skel-block db-skel-tile" />
        ))}
        <span className="skeleton db-skel-block db-skel-tile db-tile--capacity" />
      </div>
      <span className="skeleton db-skel-block db-skel-table" aria-hidden="true" />
      <div className="db-row-2" aria-hidden="true">
        <span className="skeleton db-skel-block db-skel-list" />
        <span className="skeleton db-skel-block db-skel-list" />
      </div>
    </div>
  );
}
