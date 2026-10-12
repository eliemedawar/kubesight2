/**
 * Titled card. `actionDisabled` greys out the header action (pass the page's
 * busy flag so a slow action cannot be fired twice).
 */
export default function InfoCard({ title, children, actionLabel, onAction, actionDisabled = false }) {
  return (
    <section className="card info-card">
      <header>
        <h3>{title}</h3>
        {actionLabel ? (
          <button type="button" className="btn-outline" onClick={onAction} disabled={actionDisabled}>
            {actionLabel}
          </button>
        ) : null}
      </header>
      {/* .info-card has no padding of its own; this class carries it. */}
      <div className="info-card-body">{children}</div>
    </section>
  );
}
