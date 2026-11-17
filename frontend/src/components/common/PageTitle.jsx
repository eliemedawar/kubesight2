export default function PageTitle({ title, subtitle, actionLabel, onAction, actions = null }) {
  const actionButton = actionLabel ? (
    <button type="button" className="primary page-title-action" onClick={onAction}>
      {actionLabel}
    </button>
  ) : null;
  return (
    <header className="page-title">
      <div className="page-title-content">
        <div>
          <h2>{title}</h2>
          <p>{subtitle}</p>
        </div>
        {actions ? (
          <div className="page-title-actions">
            {actions}
            {actionButton}
          </div>
        ) : (
          actionButton
        )}
      </div>
    </header>
  );
}
