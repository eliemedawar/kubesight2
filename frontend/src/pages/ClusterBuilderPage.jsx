/** Cluster Builder.
 *
 *  The page owns data and permissions; the three rooms render it — the Floor
 *  (what is happening now, and everything that has happened), the wizard, and
 *  Sources (everything a build consumes).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import PageTitle from "../components/common/PageTitle.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import BuildDetail from "../components/clusterBuilder/BuildDetail.jsx";
import BuildsFloor from "../components/clusterBuilder/BuildsFloor.jsx";
import SourcesTab from "../components/clusterBuilder/SourcesTab.jsx";
import Wizard from "../components/clusterBuilder/Wizard.jsx";
import { deriveReadiness } from "../utils/clusterBuilder.js";
import {
  getBuilderOptions,
  listBuildProfiles,
  listClusterBuilds,
  listSshCredentials,
  listSshProfiles,
  listVSphereConnections,
} from "../api/clusterBuildsApi.js";

const POLL_INTERVAL_MS = 5000;
const EMPTY_INFRA = { vsphere: [], credentials: [], profiles: [], buildProfiles: [] };

export default function ClusterBuilderPage({
  canCreate = false,
  canExecute = false,
  canManageInfra = false,
}) {
  const [tab, setTab] = useState("floor");
  const [builds, setBuilds] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [options, setOptions] = useState(null);
  const [selectedBuildId, setSelectedBuildId] = useState(null);
  const [infra, setInfra] = useState(EMPTY_INFRA);
  // Ticks so elapsed clocks advance between polls.
  const [now, setNow] = useState(() => Date.now());

  const notify = useCallback((message, isError = false) => {
    if (isError) { setError(message); setNotice(""); }
    else { setNotice(message); setError(""); }
  }, []);

  const reloadBuilds = useCallback(async () => {
    try {
      const data = await listClusterBuilds();
      setBuilds(data.items || []);
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  const reloadInfra = useCallback(async () => {
    const [vsphere, credentials, profiles, buildProfiles] = await Promise.all([
      canManageInfra ? listVSphereConnections().catch(() => ({ items: [] })) : Promise.resolve({ items: [] }),
      canManageInfra ? listSshCredentials().catch(() => ({ items: [] })) : Promise.resolve({ items: [] }),
      canManageInfra ? listSshProfiles().catch(() => ({ items: [] })) : Promise.resolve({ items: [] }),
      listBuildProfiles().catch(() => ({ items: [] })),
    ]);
    setInfra({
      vsphere: vsphere.items || [],
      credentials: credentials.items || [],
      profiles: profiles.items || [],
      buildProfiles: buildProfiles.items || [],
    });
  }, [canManageInfra]);

  useEffect(() => {
    reloadBuilds();
    reloadInfra();
    getBuilderOptions().then(setOptions).catch((err) => setError(err.message));
  }, [reloadBuilds, reloadInfra]);

  const active = builds.some(
    (build) => build.status === "building" || build.status === "preflighting"
  );

  useEffect(() => {
    if (!active) return undefined;
    const id = setInterval(reloadBuilds, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [active, reloadBuilds]);

  // The Floor's live strip shows a ticking clock; nothing else needs a second hand.
  useEffect(() => {
    if (!active || tab !== "floor" || selectedBuildId) return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active, tab, selectedBuildId]);

  const readiness = useMemo(() => deriveReadiness({
    builds,
    infra,
    addonCatalog: options?.addons || [],
    canManageInfra,
  }), [builds, infra, options, canManageInfra]);

  const tabs = [
    { id: "floor", label: "Builds" },
    ...(canCreate ? [{ id: "new", label: "New build" }] : []),
    ...(canManageInfra ? [{ id: "sources", label: "Sources" }] : []),
  ];

  const openBuild = (id) => { setTab("floor"); setSelectedBuildId(id); };

  return (
    <div className="sg-cb-page">
      <PageTitle
        title="Cluster Builder"
        subtitle="Pick machines from vCenter, assign roles, and KubeSight builds the cluster."
      />
      {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}
      {notice ? <div className="sg-cb-notice">{notice}</div> : null}

      <div className="sg-cb-tabs">
        {tabs.map((entry) => (
          <button
            key={entry.id}
            type="button"
            className={`sg-cb-tab ${tab === entry.id ? "is-active" : ""}`}
            onClick={() => { setTab(entry.id); setSelectedBuildId(null); }}
          >
            {entry.label}
          </button>
        ))}
      </div>

      {tab === "floor" && selectedBuildId ? (
        <BuildDetail
          buildId={selectedBuildId}
          canExecute={canExecute}
          notify={notify}
          onBack={() => { setSelectedBuildId(null); reloadBuilds(); }}
          onDeleted={() => { setSelectedBuildId(null); reloadBuilds(); }}
          addonCatalog={options?.addons || []}
          buildProfiles={infra.buildProfiles}
        />
      ) : null}

      {tab === "floor" && !selectedBuildId ? (
        loading ? <p className="muted">Loading…</p> : (
          <BuildsFloor
            builds={builds}
            readiness={readiness}
            catalog={options?.addons || []}
            canCreate={canCreate}
            now={now}
            onOpenBuild={openBuild}
            onNewBuild={() => setTab("new")}
            onOpenSources={() => { if (canManageInfra) setTab("sources"); }}
          />
        )
      ) : null}

      {tab === "new" && canCreate ? (
        <Wizard
          options={options}
          infra={infra}
          notify={notify}
          onCancel={() => setTab("floor")}
          onBuildLaunched={(id) => { reloadBuilds(); openBuild(id); }}
        />
      ) : null}

      {tab === "sources" && canManageInfra ? (
        <SourcesTab
          infra={infra}
          reloadInfra={reloadInfra}
          notify={notify}
          addonCatalog={options?.addons || []}
        />
      ) : null}
    </div>
  );
}
