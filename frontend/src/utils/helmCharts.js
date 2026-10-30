/**
 * The stored Inventory chart an installed release came from, by name.
 *
 * Helm labels a release's chart "<name>-<version>" ("payments-1.2.0",
 * "ingress-nginx-4.10.1"), and a chart name may itself contain dashes, so the
 * longest stored name that the label starts with (followed by a version) wins.
 * Returns null when no stored chart matches.
 */
export function matchStoredChart(chartLabel, charts = []) {
  const label = String(chartLabel || "").trim().toLowerCase();
  if (!label) return null;
  let best = null;
  for (const chart of charts) {
    const name = String(chart?.name || "").trim().toLowerCase();
    if (!name) continue;
    const fits = label === name || (label.startsWith(`${name}-`) && /^\d/.test(label.slice(name.length + 1)));
    if (fits && (!best || name.length > best.name.length)) best = { chart, name };
  }
  return best ? best.chart : null;
}

/** "payments-1.2.0" → "1.2.0"; "" when the label carries no version. */
export function chartLabelVersion(chartLabel) {
  const match = /-(\d[^-]*(?:-[^-]+)*)$/.exec(String(chartLabel || ""));
  return match ? match[1] : "";
}
