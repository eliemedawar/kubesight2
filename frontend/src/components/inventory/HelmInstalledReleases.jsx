import { useCallback, useEffect, useMemo, useState } from "react";

import { listHelmReleases, uninstallHelmRelease } from "../../api/helmApi.js";
import { promptDialog } from "../../lib/dialogs.js";
import { clusterOptionLabel, normalizeClusterOptions } from "../../utils/clusterOptions.js";
import { chartLabelVersion } from "../../utils/helmCharts.js";
import { isPendingApproval, pendingApprovalMessage } from "../../utils/pendingApproval.js";
import DataTable from "../common/DataTable.jsx";
import EmptyState from "../common/EmptyState.jsx";
import LoadingState from "../common/LoadingState.jsx";
import SearchableSelect from "../common/SearchableSelect.jsx";
import { formatDeployedAt } from "./HelmReleasePanel.jsx";
import HelmReleaseUpgradeModal from "./HelmReleaseUpgradeModal.jsx";
import NamespaceSelect from "./NamespaceSelect.jsx";

const STATUS_TONE = {
  deployed: "ok",
  failed: "danger",
  "pending-install": "warn",
  "pending-upgrade": "warn",
  "pending-rollback": "warn",
  uninstalling: "warn",
  superseded: "info",
};

const COLUMNS = [
  { key: "release", label: "Release" },
  { key: "namespace", label: "Namespace" },
  { key: "chart", label: "Chart" },
  { key: "appVersion", label: "App version" },
  { key: "revision", label: "Revision" },
  { key: "helmStatus", label: "Status" },
  { key: "updated", label: "Updated" },
  { key: "actions", label: "" },
];

/** Installed Helm releases on one cluster, with Upgrade and Delete per release. */
export default function HelmInstalledReleases({
  clusterOptions = [],
  defaultClusterId = "",
  canUpgrade = false,
  canUninstall = false,
  canManageCharts = false,
  onChanged,
}) {
  const options = normalizeClusterOptions(clusterOptions);
  const [clusterId, setClusterId] = useState(
    defaultClusterId && options.some((item) => item.id === defaultClusterId)
      ? defaultClusterId
      : options[0]?.id || "",
  );
  const [namespace, setNamespace] = useState("");
  const [search, setSearch] = useState("");
  const [releases, setReleases] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [upgradeTarget, setUpgradeTarget] = useState(null);
  // Release key of a delete in flight, so its button can't fire twice.
  const [deleting, setDeleting] = useState("");

  const clusterLabel = clusterOptionLabel(options.find((item) => item.id === clusterId) || { id: clusterId });

  const load = useCallback(async () => {
    if (!clusterId) return;
    setLoading(true);
    setError("");
    try {
      const result = await listHelmReleases({ clusterId, namespace });
      setReleases(Array.isArray(result) ? result : []);
    } catch (err) {
      setReleases([]);
      setError(err.message || "Helm releases could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [clusterId, namespace]);

  useEffect(() => {
    load();
  }, [load]);

  const remove = async (release) => {
    const name = release.name;
    const typed = await promptDialog({
      title: "Delete Helm release",
      message:
        `Uninstall ${name} from ${release.namespace} on ${clusterLabel}? Every Kubernetes resource `
        + "the release created is removed. The namespace and stored charts stay.",
      label: `Type ${name} to delete it`,
      placeholder: name,
      required: true,
      validate: (value) => (value.trim() === name ? "" : `Type ${name} exactly.`),
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (typed == null) return;
    const key = `${release.namespace}/${name}`;
    setDeleting(key);
    setError("");
    setNotice("");
    try {
      const result = await uninstallHelmRelease({ clusterId, namespace: release.namespace, releaseName: name });
      setNotice(
        isPendingApproval(result)
          ? pendingApprovalMessage(result)
          : `${name} was uninstalled from ${release.namespace}.`,
      );
      await load();
      onChanged?.();
    } catch (err) {
      setError(err.message || `${name} could not be uninstalled.`);
    } finally {
      setDeleting("");
    }
  };

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return releases
      .filter((release) =>
        !needle
        || [release.name, release.namespace, release.chart].some((text) =>
          String(text || "").toLowerCase().includes(needle),
        ),
      )
      .map((release) => {
        const key = `${release.namespace}/${release.name}`;
        const status = String(release.status || "unknown");
        const chartLabel = String(release.chart || "-");
        const chartVersion = chartLabelVersion(chartLabel);
        return {
          id: key,
          release: <strong>{release.name}</strong>,
          namespace: release.namespace,
          chart: (
            <span title={chartLabel}>
              {chartVersion ? chartLabel.slice(0, -(chartVersion.length + 1)) : chartLabel}
              {chartVersion ? <span className="muted"> {chartVersion}</span> : null}
            </span>
          ),
          appVersion: release.app_version || "-",
          revision: release.revision || "-",
          helmStatus: <span className={`status-pill ${STATUS_TONE[status] || "info"}`}>{status}</span>,
          updated: formatDeployedAt(release.updated) || "-",
          actions: (
            <div className="helm-release-actions">
              {canUpgrade ? (
                <button
                  type="button"
                  className="btn-outline btn-sm"
                  onClick={() =>
                    setUpgradeTarget({
                      clusterId,
                      clusterLabel,
                      namespace: release.namespace,
                      releaseName: release.name,
                      chart: release.chart,
                      revision: release.revision,
                    })
                  }
                >
                  Upgrade
                </button>
              ) : null}
              {canUninstall ? (
                <button
                  type="button"
                  className="btn-outline btn-sm btn-danger-outline"
                  disabled={deleting === key}
                  onClick={() => remove(release)}
                >
                  {deleting === key ? "Deleting…" : "Delete"}
                </button>
              ) : null}
            </div>
          ),
        };
      });
  }, [releases, search, canUpgrade, canUninstall, deleting, clusterId, clusterLabel]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <section className="helm-installed">
      <div className="helm-installed-filters">
        <label>
          <span>Cluster</span>
          <SearchableSelect
            value={clusterId}
            onChange={(event) => {
              setClusterId(event.target.value);
              setNamespace("");
              setNotice("");
            }}
          >
            {options.map((cluster) => (
              <option key={cluster.id} value={cluster.id}>
                {clusterOptionLabel(cluster)}
              </option>
            ))}
          </SearchableSelect>
        </label>
        <label>
          <span>Namespace</span>
          <NamespaceSelect
            clusterId={clusterId}
            value={namespace}
            required={false}
            allowCreate={false}
            emptyLabel="All namespaces"
            onChange={(event) => setNamespace(event.target.value)}
          />
        </label>
        <label>
          <span>Search</span>
          <input
            type="search"
            value={search}
            placeholder="Release, namespace or chart"
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <button type="button" className="btn-text" onClick={load} disabled={loading || !clusterId}>
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {notice ? <p className="banner-message" role="status">{notice}</p> : null}
      {error ? <p className="banner-message error">{error}</p> : null}

      {!clusterId ? (
        <EmptyState message="No cluster to look at." hint="Add or activate a cluster first." />
      ) : loading && !releases.length ? (
        <LoadingState label="Loading Helm releases..." />
      ) : rows.length ? (
        <DataTable tableClassName="helm-installed-table" columns={COLUMNS} rows={rows} />
      ) : !error ? (
        <EmptyState
          message={search ? "No release matches that search." : "No Helm releases here."}
          hint={search ? undefined : "Releases installed with Helm on this cluster show up here."}
        />
      ) : null}

      <HelmReleaseUpgradeModal
        release={upgradeTarget}
        canManage={canManageCharts}
        onClose={() => setUpgradeTarget(null)}
        onUpgraded={() => {
          setNotice(`${upgradeTarget?.releaseName} was upgraded.`);
          load();
          onChanged?.();
        }}
      />
    </section>
  );
}
