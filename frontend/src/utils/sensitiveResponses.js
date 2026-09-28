// Helpers for backend responses that deliberately withhold a secret: Secret
// YAML without the secrets:reveal permission, and a temporary password that
// could not be emailed in production.

/** Note to show above a Secret's YAML when the backend hid its values, else "". */
export function secretValuesHiddenNote(payload) {
  if (!payload || !payload.valuesHidden) {
    return "";
  }
  const keys = Array.isArray(payload.hiddenKeys) ? payload.hiddenKeys : [];
  const permission = payload.revealPermission || "secrets:reveal";
  const keyText = keys.length ? ` (${keys.join(", ")})` : "";
  return `Secret values are hidden${keyText}. Key names are shown; reading the values requires the ${permission} permission.`;
}

/**
 * Describe the outcome of a create / resend / force-reset action.
 * Returns { tone, title, password?, detail? } or null.
 */
export function temporaryPasswordNotice(result, verb) {
  if (!result) {
    return null;
  }
  const who = result.username ? ` for ${result.username}` : "";
  if (result.temporaryPassword) {
    return {
      tone: "warn",
      title: `User ${verb}. The temporary password could not be emailed, so share it securely${who}:`,
      password: result.temporaryPassword,
    };
  }
  if (result.temporaryPasswordDeliveryFailed) {
    return {
      tone: "error",
      title: `User ${verb}, but the temporary password email${who} failed to send.`,
      detail:
        result.temporaryPasswordHint ||
        "Fix the SMTP settings, then use \"Resend temporary password\" to send a new one.",
      error: result.temporaryPasswordDeliveryError || "",
    };
  }
  if (result.temporaryPasswordEmailed) {
    return { tone: "ok", title: `User ${verb}. A temporary password has been emailed${who}.` };
  }
  return { tone: "ok", title: `User ${verb}.` };
}
