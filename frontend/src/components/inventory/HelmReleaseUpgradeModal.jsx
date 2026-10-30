import { useEffect, useState } from "react";

import { listHelmChartTemplates } from "../../api/helmApi.js";
import useEscapeKey from "../../hooks/useEscapeKey.js";
import { matchStoredChart } from "../../utils/helmCharts.js";
import HelmChartDeployModal from "./HelmChartDeployModal.jsx";

/**
 * Upgrade an installed release onto a stored Inventory chart. Loads the stored
 * charts and preselects the one the release's chart label names; the person
 * can pick another. ``release``: { clusterId, clusterLabel?, namespace,
 * releaseName, chart, revision }.
 */
export default function HelmReleaseUpgradeModal({ release, canManage = false, onClose, onUpgraded }) {
  const [charts, setCharts] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!release) return undefined;
    let cancelled = false;
    setCharts(null);
    setError("");
    listHelmChartTemplates()
      .then((result) => {
        if (!cancelled) setCharts(Array.isArray(result) ? result : []);
      })
      .catch((err) => {
        if (!cancelled) {
          setCharts([]);
          setError(err.message || "Stored Helm charts could not be loaded.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [release]);

  // The deploy modal handles its own Escape; this one only the "no charts" note.
  const nothingToPick = Boolean(release) && charts !== null && (Boolean(error) || !charts.length);
  useEscapeKey(nothingToPick, onClose);

  if (!release || charts === null) return null;
  if (nothingToPick) {
    return (
      <div className="modal-overlay" role="presentation" onClick={onClose}>
        <section
          className="card modal-panel"
          role="dialog"
          aria-modal="true"
          aria-labelledby="helm-upgrade-empty-title"
          onClick={(event) => event.stopPropagation()}
        >
          <header className="modal-header">
            <h3 id="helm-upgrade-empty-title">Upgrade {release.releaseName}</h3>
            <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
              ×
            </button>
          </header>
          <p className={error ? "banner-message error" : "muted"}>
            {error ||
              "There is no stored chart to upgrade to. Import the chart's new version under Inventory → Helm Charts first."}
          </p>
          <div className="modal-actions">
            <button type="button" className="btn-primary" onClick={onClose}>
              Close
            </button>
          </div>
        </section>
      </div>
    );
  }

  return (
    <HelmChartDeployModal
      open
      release={release}
      template={matchStoredChart(release.chart, charts)}
      chartOptions={charts}
      canManage={canManage}
      onClose={onClose}
      onSuccess={onUpgraded}
    />
  );
}
