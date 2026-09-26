/** SSH host-key helpers for the Sources tab host-key panel.
 *
 *  The backend stores fingerprints as hex SHA-256 and also returns the OpenSSH
 *  `SHA256:…` form — the one `ssh-keygen -lf` prints — which is what the UI
 *  shows, because that is what an operator compares against on the host.
 */

const HEX64 = /^[0-9a-f]{64}$/i;
const OPENSSH = /^SHA256:[A-Za-z0-9+/]{43}=?$/;

/** "" when the text looks like a SHA-256 fingerprint the server will accept. */
export function fingerprintError(value) {
  const text = String(value || "").trim();
  if (!text) return "Paste the fingerprint.";
  if (OPENSSH.test(text)) return "";
  if (HEX64.test(text.replace(/:/g, ""))) return "";
  if (/^(MD5:|[0-9a-f]{2}(:[0-9a-f]{2}){15}$)/i.test(text)) {
    return "That is an MD5 fingerprint — use ssh-keygen -lf -E sha256.";
  }
  return "Use the SHA256:… form ssh-keygen -lf prints, or 64 hex characters.";
}

/** What a scan result means, as [label, pill class, sentence]. */
export function scanVerdict(scan) {
  if (!scan) return null;
  if (scan.status === "changed") {
    return [
      "changed",
      "is-bad",
      "A DIFFERENT key is recorded for this host. Unless the machine was rebuilt, "
        + "this is exactly what host-key checking exists to catch.",
    ];
  }
  if (scan.status === "match") {
    return scan.recorded?.source === "preapproved"
      ? ["pinned", "is-ok", "Already pinned — this matches the approved fingerprint."]
      : ["recorded (TOFU)", "is-warn", "Recorded on first use but not approved. Pin it to satisfy the pinned policy."];
  }
  return [
    "not recorded",
    "is-muted",
    "Nothing recorded for this host yet. Compare the fingerprint on the host before pinning it.",
  ];
}

export function sourceLabel(source) {
  return source === "preapproved" ? "pinned" : "trust on first use";
}

export function sourceTone(source) {
  return source === "preapproved" ? "is-ok" : "is-warn";
}

/** The command an operator runs ON the host to read its fingerprint. */
export function fingerprintCommand(keyType) {
  const file = {
    "ssh-ed25519": "ssh_host_ed25519_key.pub",
    "ssh-rsa": "ssh_host_rsa_key.pub",
    "rsa-sha2-256": "ssh_host_rsa_key.pub",
    "rsa-sha2-512": "ssh_host_rsa_key.pub",
  }[keyType] || (String(keyType || "").startsWith("ecdsa") ? "ssh_host_ecdsa_key.pub" : "ssh_host_*_key.pub");
  return `ssh-keygen -lf /etc/ssh/${file}`;
}
