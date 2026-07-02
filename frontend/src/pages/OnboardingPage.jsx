import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "../context/AuthContext";

const PASSWORD_RULES = [
  { key: "length", label: "At least 12 characters", test: (v) => v.length >= 12 },
  { key: "upper", label: "One uppercase letter", test: (v) => /[A-Z]/.test(v) },
  { key: "lower", label: "One lowercase letter", test: (v) => /[a-z]/.test(v) },
  { key: "digit", label: "One number", test: (v) => /[0-9]/.test(v) },
  { key: "special", label: "One special character", test: (v) => /[^A-Za-z0-9]/.test(v) },
];

const STEP_INDEX = { password_change: 1, mfa_setup: 2, mfa: 2 };

export default function OnboardingPage() {
  const {
    pendingAuth,
    submitPasswordChange,
    startTotpSetup,
    submitFirstLoginTotp,
    cancelPendingAuth,
  } = useAuth();
  const stage = pendingAuth?.stage;

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [code, setCode] = useState("");
  const [enrollment, setEnrollment] = useState(null);
  const [enrolling, setEnrolling] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const passwordChecks = PASSWORD_RULES.map((rule) => ({ ...rule, ok: rule.test(password) }));
  const passwordValid = passwordChecks.every((c) => c.ok);
  const passwordsMatch = password.length > 0 && password === confirm;

  // Guard so the enrolment request is only auto-fired once per mfa_setup entry
  // (never in a retry loop — on failure we surface the error + a Retry button).
  const requestedRef = useRef(false);

  const loadEnrollment = useCallback(async () => {
    setError("");
    setEnrolling(true);
    try {
      const data = await startTotpSetup();
      setEnrollment(data);
    } catch (err) {
      setError(err.message || "Could not start MFA setup. Please retry.");
      requestedRef.current = false; // allow a manual retry
    } finally {
      setEnrolling(false);
    }
  }, [startTotpSetup]);

  // Fetch the TOTP secret + QR once when the MFA-setup step is reached.
  useEffect(() => {
    if (stage !== "mfa_setup") {
      requestedRef.current = false;
      return;
    }
    if (requestedRef.current || enrollment) {
      return;
    }
    requestedRef.current = true;
    loadEnrollment();
  }, [stage, enrollment, loadEnrollment]);

  const handlePasswordSubmit = async (event) => {
    event.preventDefault();
    setError("");
    if (!passwordValid) {
      setError("Password does not meet the requirements below.");
      return;
    }
    if (!passwordsMatch) {
      setError("Passwords do not match.");
      return;
    }
    setBusy(true);
    try {
      await submitPasswordChange(password);
      setPassword("");
      setConfirm("");
    } catch (err) {
      setError(err.message || "Could not change password.");
    } finally {
      setBusy(false);
    }
  };

  const handleTotpSubmit = async (event) => {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      await submitFirstLoginTotp(code.trim());
      // On success AuthContext finalizes the session; this screen unmounts.
    } catch (err) {
      setError(err.message || "Invalid code. Try again.");
      setCode("");
    } finally {
      setBusy(false);
    }
  };

  const currentStep = STEP_INDEX[stage] || 1;

  return (
    <div className="login-screen">
      <div className="login-card onboarding-card">
        <h1>Welcome to KubeSight</h1>
        <p className="brand-subtitle">Finish setting up your account</p>

        <ol className="onboarding-steps">
          <li className={currentStep >= 1 ? (currentStep > 1 ? "done" : "active") : ""}>
            1. Set password
          </li>
          <li className={currentStep >= 2 ? "active" : ""}>2. Set up MFA</li>
        </ol>

        {error ? <p className="banner-message error">{error}</p> : null}

        {stage === "password_change" ? (
          <form onSubmit={handlePasswordSubmit} className="onboarding-form">
            <p className="muted">
              Choose a new permanent password to replace your temporary one.
            </p>
            <label>
              New password
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="new-password"
                autoFocus
                required
              />
            </label>
            <label>
              Confirm new password
              <input
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                autoComplete="new-password"
                required
              />
            </label>
            <ul className="password-rules">
              {passwordChecks.map((c) => (
                <li key={c.key} className={c.ok ? "ok" : ""}>
                  {c.ok ? "✓" : "○"} {c.label}
                </li>
              ))}
              <li className={passwordsMatch ? "ok" : ""}>
                {passwordsMatch ? "✓" : "○"} Passwords match
              </li>
            </ul>
            <button
              type="submit"
              className="primary"
              disabled={busy || !passwordValid || !passwordsMatch}
            >
              {busy ? "Saving..." : "Change password"}
            </button>
          </form>
        ) : null}

        {stage === "mfa_setup" ? (
          <form onSubmit={handleTotpSubmit} className="onboarding-form">
            <p className="muted">
              Scan this QR code with Google Authenticator, Microsoft Authenticator, Authy,
              or any TOTP app, then enter the 6-digit code it shows.
            </p>
            {enrolling ? <p className="muted">Generating your MFA secret…</p> : null}
            {!enrolling && !enrollment ? (
              <button type="button" className="btn-outline" onClick={loadEnrollment}>
                Retry generating QR code
              </button>
            ) : null}
            {enrollment ? (
              <>
                {enrollment.qrDataUri ? (
                  <div className="mfa-qr">
                    <img src={enrollment.qrDataUri} alt="MFA QR code" width={192} height={192} />
                  </div>
                ) : null}
                <p className="mfa-secret">
                  {enrollment.qrDataUri
                    ? "Can't scan? Enter this key manually:"
                    : "Add this key to your authenticator app:"}
                  <code>{enrollment.secret}</code>
                </p>
              </>
            ) : null}
            <label>
              Authentication code
              <input
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]*"
                maxLength={6}
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                required
              />
            </label>
            <button type="submit" className="primary" disabled={busy || code.length < 6 || !enrollment}>
              {busy ? "Verifying..." : "Verify & finish"}
            </button>
          </form>
        ) : null}

        {stage === "mfa" ? (
          <form onSubmit={handleTotpSubmit} className="onboarding-form">
            <p className="muted">
              Enter the 6-digit code from your existing authenticator app to finish signing in.
            </p>
            <label>
              Authentication code
              <input
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]*"
                maxLength={6}
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                autoFocus
                required
              />
            </label>
            <button type="submit" className="primary" disabled={busy || code.length < 6}>
              {busy ? "Verifying..." : "Verify & finish"}
            </button>
          </form>
        ) : null}

        <button type="button" className="btn-link onboarding-cancel" onClick={cancelPendingAuth}>
          Cancel and return to sign in
        </button>
      </div>
    </div>
  );
}
