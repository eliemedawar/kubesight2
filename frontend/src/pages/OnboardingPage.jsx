import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "../context/AuthContext";
import AuthShell, { AuthError } from "../components/auth/AuthShell.jsx";
import OtpInput from "../components/auth/OtpInput.jsx";
import PasswordField from "../components/auth/PasswordField.jsx";

// Mirrors validate_password_policy() in backend/api/services/auth_service.py.
const PASSWORD_RULES = [
  { key: "length", label: "At least 12 characters", test: (v) => v.length >= 12 },
  { key: "upper", label: "One uppercase letter", test: (v) => /[A-Z]/.test(v) },
  { key: "lower", label: "One lowercase letter", test: (v) => /[a-z]/.test(v) },
  { key: "digit", label: "One number", test: (v) => /[0-9]/.test(v) },
  { key: "special", label: "One special character", test: (v) => /[^A-Za-z0-9]/.test(v) },
];

const STEP_INDEX = { password_change: 1, mfa_setup: 2, mfa: 2 };

const TickIcon = () => (
  <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="m4.5 12.5 5 5 10-11" />
  </svg>
);

function StepTrack({ current }) {
  const steps = [
    { n: 1, label: "Password" },
    { n: 2, label: "Authenticator" },
  ];
  return (
    <div className="sg-lg-track" aria-label={`Setup step ${current} of ${steps.length}`}>
      {steps.map((step, i) => {
        const state = current > step.n ? "is-done" : current === step.n ? "is-on" : "";
        return (
          <span key={step.n} style={{ display: "contents" }}>
            {i > 0 ? <span className="sg-lg-track-join" /> : null}
            <span className={`sg-lg-step ${state}`}>
              <span className="sg-lg-step-n">{current > step.n ? <TickIcon /> : step.n}</span>
              {step.label}
            </span>
          </span>
        );
      })}
    </div>
  );
}

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
  const [copied, setCopied] = useState(false);

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

  const handleTotpSubmit = async (otp) => {
    const value = (typeof otp === "string" ? otp : code).trim();
    if (value.length < 6 || busy) {
      return;
    }
    setError("");
    setBusy(true);
    try {
      await submitFirstLoginTotp(value);
      // On success AuthContext finalizes the session; this screen unmounts.
    } catch (err) {
      if (err.status !== 423) {
        setError(err.message || "Invalid code. Try again.");
        setCode("");
      }
      // A 423 lock drops pendingAuth in the context; the lock scene takes over.
    } finally {
      setBusy(false);
    }
  };

  const copySecret = () => {
    if (!enrollment?.secret) {
      return;
    }
    navigator.clipboard?.writeText(enrollment.secret);
    setCopied(true);
    setTimeout(() => setCopied(false), 1400);
  };

  const currentStep = STEP_INDEX[stage] || 1;
  const secretDisplay = enrollment?.secret
    ? enrollment.secret.replace(/(.{4})/g, "$1 ").trim()
    : "";

  return (
    <AuthShell wide>
      <div className="sg-lg-scene">
        <StepTrack current={currentStep} />
        <h1 className="sg-lg-title">Welcome to KubeSight</h1>
        <p className="sg-lg-sub">
          {stage === "password_change"
            ? "Your temporary password works once. Choose your own to continue."
            : stage === "mfa_setup"
              ? "Scan with any TOTP app — Google Authenticator, Microsoft Authenticator, Authy — then enter the first code it shows."
              : "Enter the 6-digit code from your existing authenticator app to finish signing in."}
        </p>

        <AuthError>{error}</AuthError>

        {stage === "password_change" ? (
          <form onSubmit={handlePasswordSubmit} className="sg-lg-scene">
            <PasswordField
              label="New password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
              autoFocus
            />
            <PasswordField
              label="Confirm new password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="new-password"
            />
            <ul className="sg-lg-rules">
              {passwordChecks.map((c) => (
                <li key={c.key} className={c.ok ? "is-ok" : ""}>
                  <span className="sg-lg-tick"><TickIcon /></span>
                  {c.label}
                </li>
              ))}
              <li className={passwordsMatch ? "is-ok" : ""}>
                <span className="sg-lg-tick"><TickIcon /></span>
                Passwords match
              </li>
            </ul>
            <button
              type="submit"
              className="primary sg-lg-submit"
              disabled={busy || !passwordValid || !passwordsMatch}
            >
              {busy ? "Saving..." : "Set password and continue"}
            </button>
          </form>
        ) : null}

        {stage === "mfa_setup" ? (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              handleTotpSubmit();
            }}
            className="sg-lg-scene"
          >
            {enrolling ? <p className="sg-lg-sub">Generating your MFA secret…</p> : null}
            {!enrolling && !enrollment ? (
              <button type="button" className="btn-outline sg-lg-submit" onClick={loadEnrollment}>
                Retry generating QR code
              </button>
            ) : null}
            {enrollment ? (
              <div className="sg-lg-enroll">
                {enrollment.qrDataUri ? (
                  <div className="sg-lg-qr">
                    <img src={enrollment.qrDataUri} alt="MFA QR code" width={148} height={148} />
                  </div>
                ) : null}
                <div className="sg-lg-keyside">
                  <span className="sg-lg-keylabel">
                    {enrollment.qrDataUri ? "Can’t scan? Enter this key" : "Add this key to your app"}
                  </span>
                  <span className="sg-lg-key">{secretDisplay}</span>
                  <button type="button" className="sg-lg-copy" onClick={copySecret}>
                    {copied ? "Copied" : "Copy key"}
                  </button>
                </div>
              </div>
            ) : null}
            <OtpInput
              value={code}
              onChange={setCode}
              onComplete={enrollment ? handleTotpSubmit : undefined}
              disabled={busy || !enrollment}
              label="First 6-digit code from the app"
            />
            <button
              type="submit"
              className="primary sg-lg-submit"
              disabled={busy || code.length < 6 || !enrollment}
            >
              {busy ? "Verifying..." : "Verify & finish"}
            </button>
          </form>
        ) : null}

        {stage === "mfa" ? (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              handleTotpSubmit();
            }}
            className="sg-lg-scene"
          >
            <OtpInput
              value={code}
              onChange={setCode}
              onComplete={handleTotpSubmit}
              disabled={busy}
              autoFocus
              label="6-digit verification code"
            />
            <button type="submit" className="primary sg-lg-submit" disabled={busy || code.length < 6}>
              {busy ? "Verifying..." : "Verify & finish"}
            </button>
          </form>
        ) : null}

        <button type="button" className="btn-link sg-lg-cancel" onClick={cancelPendingAuth}>
          Cancel and return to sign in
        </button>
      </div>
    </AuthShell>
  );
}
