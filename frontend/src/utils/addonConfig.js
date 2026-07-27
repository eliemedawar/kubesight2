/** Client-side mirror of the backend's add-on `config` rules.
 *
 *  The API is the authority — it re-validates and canonicalizes everything on
 *  save. These helpers exist so the wizard can refuse to advance with a MetalLB
 *  pool that would be rejected, instead of failing at build time.
 */

/** One entry per line or comma, kept as typed so half-finished input is not
    reformatted under the cursor. */
export function splitEntries(text) {
  return String(text || "")
    .split(/[\n,]/)
    .map((part) => part.trim())
    .filter(Boolean);
}

function isIpv4(value) {
  const match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(String(value).trim());
  return Boolean(match) && match.slice(1).every((octet) => Number(octet) <= 255);
}

/** "" when every entry is a valid CIDR, bare address, or start-end range. */
export function ipRangeListError(text) {
  const entries = splitEntries(text);
  if (!entries.length) return "At least one range or CIDR is required.";
  for (const entry of entries) {
    if (entry.includes("-")) {
      const [start, end] = entry.split("-");
      if (!isIpv4(start || "") || !isIpv4(end || "")) {
        return `"${entry}" is not a valid start-end range.`;
      }
      continue;
    }
    const [address, prefix, ...rest] = entry.split("/");
    if (rest.length || !isIpv4(address || "")) {
      return `"${entry}" is not a valid address or CIDR.`;
    }
    if (prefix !== undefined && !(/^\d{1,2}$/.test(prefix) && Number(prefix) <= 32)) {
      return `"${entry}" has an invalid prefix length.`;
    }
  }
  return "";
}

/** "" when this add-on's declared config fields are all filled in and valid. */
export function addonConfigError(addon, catalogEntry) {
  for (const field of catalogEntry?.configFields || []) {
    const raw = addon?.config?.[field.key];
    const text = Array.isArray(raw) ? raw.join("\n") : raw;
    if (!text) {
      if (field.required) return `${field.label} is required.`;
      continue;
    }
    if (field.type === "ipRangeList") {
      const error = ipRangeListError(text);
      if (error) return error;
    }
  }
  return "";
}

/** "" when every selected add-on is configured; the first problem otherwise. */
export function addonSelectionError(addons = [], catalog = []) {
  for (const addon of addons) {
    const entry = catalog.find((item) => item.id === addon.id);
    const error = addonConfigError(addon, entry);
    if (error) return `${entry?.displayName || addon.id}: ${error}`;
  }
  return "";
}
