import { setStoredSession } from "../authStorage";
import { request } from "./client";

/**
 * Submit credentials. Returns the raw staged response:
 *   { stage: "authenticated", token, user }
 *   { stage: "password_change" | "mfa_setup" | "mfa", onboardingToken, ... }
 *   { stage: "mfa_challenge", mfaToken }
 * The caller (AuthContext) decides what to persist; a session is only stored
 * once the flow reaches "authenticated".
 */
export const login = async (username, password) => {
  return request("/api/auth/login", {
    method: "POST",
    body: { username, password },
    auth: false,
  });
};

// --- First-login onboarding (authorized by the interim onboarding token) ----

export const changeTemporaryPassword = (onboardingToken, newPassword) =>
  request("/api/auth/first-login/change-password", {
    method: "POST",
    body: { newPassword },
    authToken: onboardingToken,
  });

export const setupTotp = (onboardingToken) =>
  request("/api/auth/first-login/totp/setup", {
    method: "POST",
    authToken: onboardingToken,
  });

export const verifyFirstLoginTotp = (onboardingToken, code) =>
  request("/api/auth/first-login/totp/verify", {
    method: "POST",
    body: { code },
    authToken: onboardingToken,
  });

// --- Normal-login MFA challenge (authorized by the interim MFA token) -------

export const verifyLoginMfa = (mfaToken, code) =>
  request("/api/auth/mfa/verify", {
    method: "POST",
    body: { code },
    authToken: mfaToken,
  });

export const logout = () => request("/api/auth/logout", { method: "POST" }).catch(() => null);

export const fetchCurrentUser = () => request("/api/auth/me");

// Re-exported so AuthContext can persist a session once authenticated.
export { setStoredSession };
