export default function PageTitle({ title, subtitle, actionLabel, onAction }) {
  return (
    <header className="page-title">
      <div className="page-title-content">
        <div>
          <h2>{title}</h2>
          <p>{subtitle}</p>
        </div>
        {actionLabel ? (
          <button type="button" className="btn-outline page-title-action" onClick={onAction}>
            {actionLabel}
          </button>
        ) : null}
      </div>
    </header>
  );
}
