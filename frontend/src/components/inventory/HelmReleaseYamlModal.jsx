import { useEffect, useMemo, useState } from "react";

import { getHelmRelease } from "../../api/helmApi.js";
import useEscapeKey from "../../hooks/useEscapeKey.js";
import LoadingState from "../common/LoadingState.jsx";
import YamlPreviewPanel from "./wizard/YamlPreviewPanel.jsx";

/** `metadata.name` — read at metadata's own indentation, so a `name:` under
 * labels or annotations is not mistaken for it. */
function metadataName(text) {
  const lines = text.split("\n");
  const start = lines.findIndex((line) => /^metadata:\s*$/.test(line));
  if (start < 0) return "";
  let indent = null;
  for (const line of lines.slice(start + 1)) {
    if (!line.trim() || /^\s*#/.test(line)) continue;
    const own = line.match(/^([ \t]*)/)[1];
    if (!own) break; // back at the top level: metadata ended
    if (indent === null) indent = own;
    if (own === indent) {
      const match = line.slice(own.length).match(/^name:\s*["']?([^"'\s#]+)/);
      if (match) return match[1];
    }
  }
  return "";
}

/**
 * Split a release manifest into its resources, so one can be read on its own.
 * Helm separates documents with `---` and leads each with a `# Source:` line;
 * a manifest that went through the server's Secret redaction has neither
 * comment, so kind and name are read from the YAML itself.
 */
export function manifestResources(manifest) {
  return String(manifest || "")
    .split(/^---[ \t]*$/m)
    .map((text) => text.replace(/^\s*\n/, "").replace(/\s+$/, ""))
    .filter((text) => text.replace(/^#.*$/gm, "").trim())
    .map((text, index) => {
      const kind = text.match(/^kind:\s*["']?([^"'\s]+)/m)?.[1] || "";
      const name = metadataName(text);
      const source = text.match(/^# Source:\s*(.+)$/m)?.[1] || "";
      const label = kind || name ? `${kind || "Resource"} / ${name || "unnamed"}` : `Document ${index + 1}`;
      return { key: `${index}`, label, source, text: `${text}\n` };
    });
}

const TABS = [
  { key: "manifest", label: "Manifest" },
  { key: "values", label: "Values" },
];

/**
 * The YAML behind an installed release: every resource it rendered, and the
 * values it runs with. Redaction is the server's — Secret data needs
 * secrets:reveal, values need helm:values:view.
 * ``release``: { clusterId, clusterLabel?, namespace, releaseName, chart?, revision? }.
 */
export default function HelmReleaseYamlModal({ release, onClose }) {
  const [tab, setTab] = useState("manifest");
  const [allValues, setAllValues] = useState(false);
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [resourceKey, setResourceKey] = useState("all");

  useEscapeKey(Boolean(release), onClose);

  // A new release starts on the manifest, all resources, own values only.
  useEffect(() => {
    setTab("manifest");
    setAllValues(false);
    setResourceKey("all");
    setDetail(null);
  }, [release]);

  useEffect(() => {
    if (!release) return undefined;
    let cancelled = false;
    setLoading(true);
    setError("");
    getHelmRelease({
      clusterId: release.clusterId,
      namespace: release.namespace,
      releaseName: release.releaseName,
      allValues,
    })
      .then((result) => {
        if (!cancelled) setDetail(result || null);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || "The release could not be loaded.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [release, allValues]);

  const resources = useMemo(() => manifestResources(detail?.manifest), [detail?.manifest]);
  const selected = resources.find((item) => item.key === resourceKey);
  const manifestText = selected ? selected.text : detail?.manifest || "";

  if (!release) return null;
  const name = release.releaseName;
  const titleId = "helm-release-yaml-title";

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel helm-release-yaml-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <h3 id={titleId}>{name}</h3>
            <p className="muted helm-release-yaml-sub">
              {release.namespace}
              {release.clusterLabel ? ` · ${release.clusterLabel}` : ""}
              {release.chart ? ` · ${release.chart}` : ""}
              {release.revision ? ` · revision ${release.revision}` : ""}
            </p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>

        <div className="helm-import-source-tabs" role="tablist" aria-label="What to show">
          {TABS.map((item) => (
            <button
              key={item.key}
              type="button"
              role="tab"
              aria-selected={tab === item.key}
              className={tab === item.key ? "active" : ""}
              onClick={() => setTab(item.key)}
            >
              {item.label}
            </button>
          ))}
        </div>

        {error ? <p className="banner-message error">{error}</p> : null}

        {!detail && loading ? (
          <LoadingState label={`Loading ${name}...`} />
        ) : !detail ? null : tab === "manifest" ? (
          <div className="helm-release-yaml-body">
            {resources.length ? (
              <>
                <label className="helm-release-yaml-pick">
                  <span>Resource</span>
                  <select value={selected ? resourceKey : "all"} onChange={(event) => setResourceKey(event.target.value)}>
                    <option value="all">All resources ({resources.length})</option>
                    {resources.map((item) => (
                      <option key={item.key} value={item.key}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                </label>
                {selected?.source ? <p className="muted helm-release-yaml-note">From {selected.source}</p> : null}
                <YamlPreviewPanel
                  yaml={manifestText}
                  title={selected ? selected.label : "Rendered manifest"}
                  fileName={`${name}-manifest.yaml`}
                  rows={22}
                />
              </>
            ) : (
              <p className="muted">Helm returned no manifest for this release.</p>
            )}
            {detail.secretValuesHidden ? (
              <p className="muted helm-release-yaml-note">
                Secret data is left out. Seeing it needs the secrets:reveal permission.
              </p>
            ) : null}
          </div>
        ) : (
          <div className="helm-release-yaml-body">
            {detail.valuesHidden ? (
              <p className="muted">Viewing a release&apos;s values needs the helm:values:view permission.</p>
            ) : (
              <>
                <label className="helm-release-yaml-check">
                  <input
                    type="checkbox"
                    checked={allValues}
                    disabled={loading}
                    onChange={(event) => setAllValues(event.target.checked)}
                  />
                  <span>Include the chart&apos;s defaults (helm get values --all)</span>
                </label>
                {loading ? (
                  <LoadingState label="Loading values..." />
                ) : detail.valuesYaml ? (
                  <YamlPreviewPanel
                    yaml={detail.valuesYaml}
                    title={allValues ? "All values" : "Values set at install"}
                    fileName={`${name}-values${allValues ? "-all" : ""}.yaml`}
                    rows={22}
                  />
                ) : (
                  <p className="muted">
                    {allValues
                      ? "This release has no values."
                      : "This release was installed with the chart's defaults only. Tick the box above to see them."}
                  </p>
                )}
                {detail.secretValuesHidden ? (
                  <p className="muted helm-release-yaml-note">
                    Values that look like credentials are masked. Seeing them needs the secrets:reveal permission.
                  </p>
                ) : null}
              </>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
