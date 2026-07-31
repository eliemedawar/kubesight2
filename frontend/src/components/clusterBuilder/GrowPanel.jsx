/** Day two: adding workers to a cluster that is already running.
 *
 *  Deliberately the same three moves as the wizard — pick machines, preflight
 *  them, acknowledge what it found — because it is the same decision, and the
 *  phase machine it drives is literally the same one. What differs is the
 *  scope: only the new machines are probed and prepared, and only workers are
 *  offered, because changing a live cluster's control-plane or load-balancer
 *  tier re-forms etcd quorum or the VIP.
 */

import { useEffect, useMemo, useState } from "react";
import { StatusPill } from "./common.jsx";
import { groupChecks } from "../../utils/clusterBuilder.js";
import {
  addClusterBuildNodes,
  listVSphereVms,
  preflightClusterGrowth,
  growClusterBuild,
  removeClusterBuildNode,
} from "../../api/clusterBuildsApi.js";

/** Machines on the build that have not joined the cluster yet. */
const PENDING_STATUSES = new Set(["pending", "preflight_passed", "preflight_failed"]);

export default function GrowPanel({ build, canExecute, notify, onChanged, onClose }) {
  const [vms, setVms] = useState([]);
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [picked, setPicked] = useState({});
  const [manual, setManual] = useState({ hostname: "", address: "" });
  const [busy, setBusy] = useState(false);
  const [preflight, setPreflight] = useState(null);
  const [acked, setAcked] = useState(false);
  const [error, setError] = useState("");

  const pending = (build.nodes || []).filter((node) => PENDING_STATUSES.has(node.status));
  const inCluster = useMemo(
    () => new Set(
      (build.nodes || [])
        .filter((node) => !PENDING_STATUSES.has(node.status))
        .map((node) => node.vsphereVmMoid)
        .filter(Boolean)
    ),
    [build.nodes]
  );
  const alreadyAdded = useMemo(
    () => new Set(pending.map((node) => node.vsphereVmMoid).filter(Boolean)),
    [pending]
  );

  useEffect(() => {
    let ignore = false;
    if (!build.vsphereConnectionId) return undefined;
    setLoading(true);
    listVSphereVms(build.vsphereConnectionId)
      .then((data) => { if (!ignore) setVms(data.items || []); })
      .catch((error) => notify(`vCenter inventory failed: ${error.message}`, true))
      .finally(() => { if (!ignore) setLoading(false); });
    return () => { ignore = true; };
  }, [build.vsphereConnectionId, notify]);

  const candidates = useMemo(() => {
    const query = search.trim().toLowerCase();
    return vms
      .filter((vm) => !inCluster.has(vm.moid) && !alreadyAdded.has(vm.moid))
      .filter((vm) => (query
        ? [vm.name, vm.guestHostname, vm.guestIp, vm.esxiHost]
          .filter(Boolean).some((value) => String(value).toLowerCase().includes(query))
        : true));
  }, [vms, search, inCluster, alreadyAdded]);

  const pickedCount = Object.keys(picked).length;

  const run = async (fn, after) => {
    setBusy(true);
    setError("");
    try {
      const result = await fn();
      if (after) after(result);
    } catch (failure) {
      // Shown inline as well as page-level: this panel sits below the fold, and
      // a notice at the top of the page is invisible from here.
      const message = failure.message || String(failure);
      setError(message);
      notify(message, true);
    } finally {
      setBusy(false);
    }
  };

  const addPicked = () => {
    const nodes = Object.entries(picked).map(([moid, entry]) => ({
      role: "worker",
      vsphereVmMoid: moid,
      address: entry.address || undefined,
    }));
    if (manual.address.trim()) {
      nodes.push({
        role: "worker",
        hostname: manual.hostname.trim() || undefined,
        address: manual.address.trim(),
      });
    }
    if (!nodes.length) return;
    run(() => addClusterBuildNodes(build.id, nodes), () => {
      setPicked({});
      setManual({ hostname: "", address: "" });
      setPreflight(null);
      setAcked(false);
      onChanged();
    });
  };

  const grouped = preflight ? groupChecks(preflight) : null;
  const canGrow = Boolean(
    grouped && grouped.verdict !== "fail" && (grouped.verdict === "pass" || acked)
  );

  return (
    <div className="card sg-cb-grow">
      <div className="sg-cb-sect">
        <h2>Add worker machines</h2>
        <span className="sg-cb-sect-right">
          <button className="btn-ghost btn-sm" type="button" onClick={onClose}>Close</button>
        </span>
      </div>
      {error ? <p className="sg-cb-grow-error" role="alert">{error}</p> : null}

      <p className="muted sg-cb-grow-lede">
        The same preparation and join phases this cluster was built with run again,
        scoped to the new machines. Nothing that already joined is touched, and the
        cluster keeps serving throughout. Workers only — changing the control-plane
        or load-balancer tier of a live cluster re-forms etcd quorum or the VIP.
      </p>

      {pending.length ? (
        <div className="sg-cb-grow-queue">
          <div className="sg-cb-grow-queue-head">
            <span>Queued to join</span>
            <span className="muted">{pending.length} machine{pending.length === 1 ? "" : "s"}</span>
          </div>
          {pending.map((node) => (
            <div className="sg-cb-entry" key={node.id}>
              <span className="sg-cb-entry-id">
                <span className="en">{node.hostname || node.vsphereVmName || node.address}</span>
                <span className="ea sg-cb-mono">
                  {node.address}{node.vsphereHost ? ` · ${node.vsphereHost}` : ""}
                </span>
              </span>
              <span className="sg-cb-entry-right">
                <StatusPill status={node.status} />
                <button
                  className="btn-ghost btn-sm"
                  type="button"
                  disabled={busy}
                  onClick={() => run(
                    () => removeClusterBuildNode(build.id, node.id),
                    () => { setPreflight(null); setAcked(false); onChanged(); }
                  )}
                >
                  Remove from queue
                </button>
              </span>
            </div>
          ))}
        </div>
      ) : null}

      {build.vsphereConnectionId ? (
        <>
          <div className="sg-cb-pick-top">
            <input
              className="sg-cb-search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search machines not already in this cluster…"
              aria-label="Search machines"
            />
            <span className="muted sg-cb-pick-count">
              {loading ? "Loading inventory…" : `${candidates.length} available`}
            </span>
          </div>
          <div className="sg-cb-vmlist sg-cb-grow-list">
            {candidates.map((vm) => {
              const selection = picked[vm.moid];
              const toolsOk = vm.toolsRunState === "RUNNING";
              return (
                <div key={vm.moid} className={`sg-cb-vm ${selection ? "is-picked is-worker" : ""}`}>
                  <label className="sg-cb-vm-id sg-cb-grow-pick">
                    <input
                      type="checkbox"
                      checked={Boolean(selection)}
                      onChange={(event) => setPicked((previous) => {
                        const next = { ...previous };
                        if (event.target.checked) next[vm.moid] = { address: "" };
                        else delete next[vm.moid];
                        return next;
                      })}
                    />
                    <span>
                      <span className="vnm sg-cb-mono">{vm.name}</span>
                      <span className="vsub">
                        {vm.guestOs || "Unknown guest"}
                        {vm.powerState !== "POWERED_ON"
                          ? <span className="sg-cb-warn-text"> · powered off</span>
                          : null}
                        {toolsOk ? " · Tools running" : <span className="sg-cb-warn-text"> · no Tools</span>}
                      </span>
                    </span>
                  </label>
                  <span className="sg-cb-vm-addr">
                    {toolsOk && vm.guestIp ? (
                      <span className="sg-cb-mono">{vm.guestIp}</span>
                    ) : selection ? (
                      <input
                        className="sg-cb-input sg-cb-mono sg-cb-ipfix"
                        placeholder="management address"
                        aria-label={`Management address for ${vm.name}`}
                        value={selection.address}
                        onChange={(event) => setPicked((previous) => ({
                          ...previous,
                          [vm.moid]: { address: event.target.value },
                        }))}
                      />
                    ) : <span className="muted">no Tools address</span>}
                  </span>
                  <span className="sg-cb-vm-spec">
                    {vm.cpuCount ?? "—"} vCPU
                    {vm.memoryMiB ? ` · ${Math.round(vm.memoryMiB / 1024)} GiB` : ""}
                  </span>
                  <span className="sg-cb-vm-host">
                    <span className="sg-cb-hostchip">{vm.esxiHost || "—"}</span>
                  </span>
                </div>
              );
            })}
            {!candidates.length && !loading ? (
              <p className="muted sg-cb-vm-empty">
                Every machine in this vCenter is already part of the cluster or queued.
              </p>
            ) : null}
          </div>
        </>
      ) : (
        <p className="muted">
          This build has no vCenter connection, so machines are entered by hand.
        </p>
      )}

      <div className="sg-cb-grow-manual">
        <input
          className="sg-cb-input sg-cb-mono"
          placeholder="hostname (optional)"
          aria-label="Manual hostname"
          value={manual.hostname}
          onChange={(event) => setManual({ ...manual, hostname: event.target.value })}
        />
        <input
          className="sg-cb-input sg-cb-mono"
          placeholder="address of a machine not in vCenter"
          aria-label="Manual address"
          value={manual.address}
          onChange={(event) => setManual({ ...manual, address: event.target.value })}
        />
        <button
          className="btn-outline"
          type="button"
          disabled={busy || (!pickedCount && !manual.address.trim())}
          onClick={addPicked}
        >
          {pickedCount || manual.address.trim()
            ? `Queue ${pickedCount + (manual.address.trim() ? 1 : 0)} machine${
              pickedCount + (manual.address.trim() ? 1 : 0) === 1 ? "" : "s"}`
            : "Queue machines"}
        </button>
      </div>

      {pending.length ? (
        <div className="sg-cb-grow-run">
          <button
            className="btn-outline"
            type="button"
            disabled={busy || !canExecute}
            onClick={() => run(
              () => preflightClusterGrowth(build.id),
              (result) => { setPreflight(result); setAcked(false); onChanged(); }
            )}
          >
            {busy ? "Working…" : "Preflight the new machines"}
          </button>
          {grouped ? (
            <span className={`sg-cb-grow-verdict is-${grouped.verdict}`}>
              {grouped.counts.fail
                ? `${grouped.counts.fail} check${grouped.counts.fail === 1 ? "" : "s"} must be fixed`
                : grouped.counts.warn
                  ? `${grouped.counts.warn} warning${grouped.counts.warn === 1 ? "" : "s"}, nothing blocking`
                  : `All ${grouped.total} checks pass`}
            </span>
          ) : null}
        </div>
      ) : null}

      {grouped?.attention.length ? (
        <div className="sg-cb-grow-checks">
          {grouped.attention.map((group) => (
            <div className={`sg-cb-fix is-${group.status === "fail" ? "fail" : "warn"}`} key={group.key}>
              <b>{group.status === "fail" ? "Blocking" : "Warning"}</b>
              <span>
                {group.label} — {group.machines.map((m) => m.name).join(", ")}
                {group.hint ? ` · ${group.hint}` : ""}
              </span>
            </div>
          ))}
        </div>
      ) : null}

      {grouped && grouped.verdict === "warn" ? (
        <div className="sg-cb-ackbar">
          <label>
            <input
              type="checkbox"
              checked={acked}
              onChange={(event) => setAcked(event.target.checked)}
            />
            I understand the {grouped.counts.warn} warning
            {grouped.counts.warn === 1 ? "" : "s"} and want to proceed.
          </label>
        </div>
      ) : null}

      {grouped ? (
        <div className="sg-cb-actions">
          <button
            className="primary"
            type="button"
            disabled={!canGrow || busy || !canExecute}
            onClick={() => run(
              () => growClusterBuild(
                build.id,
                grouped.verdict === "warn" ? { ackWarnings: ["Acknowledged when growing"] } : {}
              ),
              () => { setPreflight(null); setAcked(false); onChanged(); onClose(); }
            )}
          >
            {busy
              ? "Joining…"
              : `Join ${pending.length} machine${pending.length === 1 ? "" : "s"} to the cluster`}
          </button>
        </div>
      ) : null}
    </div>
  );
}
