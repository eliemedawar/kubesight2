import { useEffect, useState } from "react";
import { useAuth } from "../context/AuthContext";
import BrandMark from "../components/BrandMark.jsx";
import AuthShell, { AuthError } from "../components/auth/AuthShell.jsx";
import OtpInput from "../components/auth/OtpInput.jsx";
import PasswordField from "../components/auth/PasswordField.jsx";

const HOLD_RING_RADIUS = 40;
const HOLD_RING_CIRCUMFERENCE = 2 * Math.PI * HOLD_RING_RADIUS;

const initialsOf = (name = "") =>
  name
    .split(/[.\s_@-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0].toUpperCase())
    .join("") || "?";

const formatSeconds = (total) => {
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${seconds < 10 ? "0" : ""}${seconds}`;
};

/** Countdown scene for a temporary lock: a draining ring with mm:ss inside.
 * The button re-enables itself the moment the hold lifts. */
function HoldScene({ lock, onDone }) {
  const total = Math.max(lock.retryAfterSeconds || 0, 1);
  const [left, setLeft] = useState(lock.retryAfterSeconds || 0);

  useEffect(() => {
    setLeft(lock.retryAfterSeconds || 0);
    const timer = setInterval(() => {
      setLeft((prev) => (prev > 0 ? prev - 1 : 0));
    }, 1000);
    return () => clearInterval(timer);
  }, [lock]);

  const over = left <= 0;
  const reasonCopy =
    lock.reason === "failed_mfa"
      ? "Too many incorrect verification codes."
      : "Too many password attempts.";

  return (
    <div className="sg-lg-scene">
      <div className="sg-lg-holdring">
        <svg width="92" height="92" viewBox="0 0 92 92" aria-hidden="true">
          <circle className="sg-lg-hold-base" cx="46" cy="46" r={HOLD_RING_RADIUS} />
          <circle
            className="sg-lg-hold-arc"
            cx="46"
            cy="46"
            r={HOLD_RING_RADIUS}
            strokeDasharray={HOLD_RING_CIRCUMFERENCE}
            strokeDashoffset={HOLD_RING_CIRCUMFERENCE * (1 - left / total)}
          />
        </svg>
        <span className="sg-lg-holdtime">{formatSeconds(left)}</span>
      </div>
      <h2 className="sg-lg-title sg-lg-center">
        {over ? "You can try again" : "Sign-in is on hold"}
      </h2>
      <p className="sg-lg-sub sg-lg-center" aria-live="polite">
        {over
          ? "The hold has lifted. Your next attempt starts with a clean slate."
          : `${reasonCopy} The hold lifts automatically — nothing to do but wait.`}
      </p>
      <button type="button" className="primary sg-lg-submit" disabled={!over} onClick={onDone}>
        Try again
      </button>
      <p className="sg-lg-hint">
        Three holds in 24 hours will lock the account until an administrator unlocks it.
      </p>
    </div>
  );
}

/** Terminal lock state: only an administrator can re-enable sign-in. */
function AdminLockedScene({ onBack }) {
  return (
    <div className="sg-lg-scene">
      <div className="sg-lg-lockbadge">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="4.5" y="10.5" width="15" height="9.5" rx="2.5" />
          <path d="M8 10.5V7.8a4 4 0 0 1 8 0v2.7" />
        </svg>
      </div>
      <h2 className="sg-lg-title sg-lg-center">This account is locked</h2>
      <p className="sg-lg-sub sg-lg-center">
        To protect the account, sign-in stays off until a KubeSight administrator unlocks it.
      </p>
      <p className="sg-lg-note">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
          <circle cx="12" cy="12" r="9.5" />
          <path d="M12 11v5.5" />
          <path d="M12 7.5h.01" />
        </svg>
        <span>
          Ask an administrator to open <b>Users → your account → Unlock</b>. Both the lock and
          the unlock are audit-logged.
        </span>
      </p>
      <button type="button" className="btn-outline sg-lg-submit" onClick={onBack}>
        Back to sign in
      </button>
    </div>
  );
}

export default function LoginPage() {
  const {
    login,
    loading,
    error,
    setError,
    needsMfaChallenge,
    pendingAuth,
    submitLoginMfa,
    cancelPendingAuth,
    accountLock,
    clearAccountLock,
  } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (event) => {
    event.preventDefault();
    setError("");
    try {
      await login(username.trim(), password);
    } catch {
      // Errors surface via the context `error` banner; a 423 lock switches the
      // context `accountLock` state and renders the lock scene below.
    }
  };

  const handleMfaSubmit = async (otp) => {
    const value = (typeof otp === "string" ? otp : code).trim();
    if (value.length < 6 || submitting) {
      return;
    }
    setError("");
    setSubmitting(true);
    try {
      await submitLoginMfa(value);
    } catch (err) {
      if (err.status !== 423) {
        setError(err.message || "Verification failed");
        setCode("");
      }
      // A 423 lock is handled by the context: pendingAuth is dropped and the
      // lock scene renders on the next pass.
    } finally {
      setSubmitting(false);
    }
  };

  const backToLogin = () => {
    setError("");
    setCode("");
    setPassword("");
    cancelPendingAuth();
  };

  if (accountLock) {
    return (
      <AuthShell>
        {accountLock.kind === "admin" ? (
          <AdminLockedScene onBack={clearAccountLock} />
        ) : (
          <HoldScene lock={accountLock} onDone={clearAccountLock} />
        )}
      </AuthShell>
    );
  }

  if (needsMfaChallenge) {
    const who = pendingAuth?.username || "";
    return (
      <AuthShell>
        <form
          className="sg-lg-scene"
          onSubmit={(e) => {
            e.preventDefault();
            handleMfaSubmit();
          }}
        >
          <div className="sg-lg-chip">
            <span className="sg-lg-chip-avatar" aria-hidden="true">{initialsOf(who)}</span>
            <span className="sg-lg-chip-name">{who}</span>
            <button type="button" onClick={backToLogin}>Not you?</button>
          </div>
          <h2 className="sg-lg-title">Enter your verification code</h2>
          <p className="sg-lg-sub">The 6-digit code from your authenticator app.</p>
          <AuthError>{error}</AuthError>
          <OtpInput
            value={code}
            onChange={setCode}
            onComplete={handleMfaSubmit}
            disabled={submitting}
            autoFocus
            label="6-digit verification code"
          />
          <button type="submit" className="primary sg-lg-submit" disabled={submitting || code.length < 6}>
            {submitting ? "Verifying..." : "Verify & Sign In"}
          </button>
          <button type="button" className="btn-outline sg-lg-submit" onClick={backToLogin}>
            Back to sign in
          </button>
          <div className="sg-lg-dots" aria-hidden="true">
            <span className="sg-lg-dot is-done" />
            <span className="sg-lg-dot is-on" />
          </div>
        </form>
      </AuthShell>
    );
  }

  return (
    <AuthShell>
      <form className="sg-lg-scene" onSubmit={handleSubmit}>
        <div className="sg-lg-brand">
          <BrandMark />
          <h1>KubeSight</h1>
        </div>
        <p className="sg-lg-sub">Sign in to the control plane</p>
        <AuthError>{error}</AuthError>
        <label className="sg-lg-field">
          Username or email
          <span className="sg-lg-inputwrap">
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              spellCheck="false"
              required
            />
          </span>
        </label>
        <PasswordField
          label="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
        />
        <button type="submit" className="primary sg-lg-submit" disabled={loading}>
          {loading ? "Signing in..." : "Sign In"}
        </button>
      </form>
    </AuthShell>
  );
}
