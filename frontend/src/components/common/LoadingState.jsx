export default function LoadingState({ label = "Loading data...", hint }) {
  return (
    <div className="loading-state" role="status" aria-live="polite">
      <p className="muted">{label}</p>
      {hint ? <p className="muted loading-state-hint">{hint}</p> : null}
    </div>
  );
}
