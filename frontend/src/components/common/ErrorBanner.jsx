import { formatAccessError } from "../../utils/authz";

export default function ErrorBanner({
  message,
  className = "banner-message error",
  suppressAccessDenied = true,
  onDismiss,
}) {
  const text = formatAccessError(message, { suppressAccessDenied });
  if (!text) {
    return null;
  }
  if (!onDismiss) {
    return <p className={className}>{text}</p>;
  }
  return (
    <p className={`${className} banner-message--dismissible`} role="alert">
      <span className="banner-message__text">{text}</span>
      <button
        type="button"
        className="btn-ghost banner-message__dismiss"
        onClick={onDismiss}
        aria-label="Dismiss message"
        title="Dismiss"
      >
        <svg width="12" height="12" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" />
        </svg>
      </button>
    </p>
  );
}
