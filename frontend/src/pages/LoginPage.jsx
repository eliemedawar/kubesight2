import { useState } from "react";
import { useAuth } from "../context/AuthContext";
import BrandMark from "../components/BrandMark.jsx";

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
      // error is set in context
    }
  };

  const handleMfaSubmit = async (event) => {
    event.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      await submitLoginMfa(code.trim());
    } catch (err) {
      setError(err.message || "Verification failed");
      setCode("");
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

  if (needsMfaChallenge) {
    return (
      <div className="login-screen">
        <form className="login-card" onSubmit={handleMfaSubmit}>
          <div className="login-brand">
            <BrandMark className="login-logo" />
            <h1>KubeSight</h1>
          </div>
          <p className="brand-subtitle">Two-factor authentication</p>
          <p className="muted">
            Enter the 6-digit code from your authenticator app
            {pendingAuth?.username ? ` for ${pendingAuth.username}` : ""}.
          </p>
          {error ? <p className="banner-message error">{error}</p> : null}
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
          <button type="submit" className="primary" disabled={submitting || code.length < 6}>
            {submitting ? "Verifying..." : "Verify & Sign In"}
          </button>
          <button type="button" className="btn-outline" onClick={backToLogin}>
            Back to sign in
          </button>
        </form>
      </div>
    );
  }

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={handleSubmit}>
        <div className="login-brand">
          <BrandMark className="login-logo" />
          <h1>KubeSight</h1>
        </div>
        <p className="brand-subtitle">Control Plane Sign In</p>
        {error ? <p className="banner-message error">{error}</p> : null}
        <label>
          Username or email
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        <button type="submit" className="primary" disabled={loading}>
          {loading ? "Signing in..." : "Sign In"}
        </button>
      </form>
    </div>
  );
}
