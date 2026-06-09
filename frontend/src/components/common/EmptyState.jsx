export default function EmptyState({ message, hint }) {
  return (
    <section className="card empty-state-card">
      <p>{message}</p>
      {hint ? <p className="muted">{hint}</p> : null}
    </section>
  );
}
