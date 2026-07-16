/**
 * Shared canvas for the pre-auth screens (login + first-login onboarding):
 * the Signal "aperture" ring field, the card, and the trust strip pinned to
 * the bottom of the screen. Styles live in styles/signal/login.css.
 */
export default function AuthShell({ children, wide = false }) {
  const host = typeof window !== "undefined" ? window.location.host : "";
  return (
    <div className="sg-lg-screen">
      <div className="sg-lg-aperture" aria-hidden="true">
        <span className="sg-lg-halo" />
        <span className="sg-lg-ring sg-lg-ring--1" />
        <span className="sg-lg-ring sg-lg-ring--2" />
        <span className="sg-lg-ring sg-lg-ring--3" />
        <svg className="sg-lg-sweep" viewBox="0 0 640 640">
          <circle cx="320" cy="320" r="318" />
        </svg>
      </div>
      <div className={wide ? "sg-lg-card sg-lg-card--wide" : "sg-lg-card"}>{children}</div>
      <div className="sg-lg-trust">
        {host ? (
          <>
            <span className="sg-lg-trust-host">{host}</span>
            <span className="sg-lg-trust-sep" />
          </>
        ) : null}
        <span>Every sign-in attempt is audit-logged</span>
      </div>
    </div>
  );
}

export function AuthError({ children }) {
  if (!children) {
    return null;
  }
  return (
    <p className="sg-lg-error" role="alert">
      <svg
        width="15"
        height="15"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="9.5" />
        <path d="M12 7.5v5.5" />
        <path d="M12 16.5h.01" />
      </svg>
      <span>{children}</span>
    </p>
  );
}
