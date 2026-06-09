export default function InfoCard({ title, children, actionLabel, onAction }) {
  return (
    <section className="card info-card">
      <header>
        <h3>{title}</h3>
        {actionLabel ? (
          <button type="button" className="btn-outline" onClick={onAction}>
            {actionLabel}
          </button>
        ) : null}
      </header>
      <div>{children}</div>
    </section>
  );
}
