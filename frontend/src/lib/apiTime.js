/**
 * Parse an API timestamp into epoch milliseconds.
 *
 * The backend serializes UTC datetimes with isoformat(), which can omit the
 * timezone suffix. Browsers interpret those naive values as local time, making
 * elapsed durations drift by the local UTC offset.
 */
export function parseApiTime(value) {
  if (!value) return NaN;
  const raw = String(value).trim();
  if (!raw) return NaN;
  const normalized = /(?:[Zz]|[+-]\d{2}:\d{2})$/.test(raw) ? raw : `${raw}Z`;
  return Date.parse(normalized);
}
