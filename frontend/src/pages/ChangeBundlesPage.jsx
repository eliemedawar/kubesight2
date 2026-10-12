import { useCallback, useEffect, useMemo, useState } from "react";
import {
  approveChangeBundle,
  deleteChangeBundle,
  diffBundleItem,
  getChangeBundle,
  listChangeBundles,
  listMyBundles,
  rejectChangeBundle,
} from "../api/changeBundlesApi";
import { ACTION_LABEL } from "../components/changes/actionLabels.js";
import DiffBlock from "../components/changes/DiffBlock.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import { useAuth } from "../context/AuthContext";
import { usePermission } from "../hooks/usePermission.js";
import { useRouteParam, useRouter } from "../routes/RouterContext.jsx";
import { useChangeBundle } from "../context/ChangeBundleContext";
import { setVisibleInterval } from "../lib/visibleInterval";

const STATUS_STYLE = {
  draft: { bg: "var(--bg-interactive)", fg: "var(--text-muted)", label: "Draft" },
  pending_approval: { bg: "var(--warn-soft)", fg: "var(--warn)", label: "Pending approval" },
  approved: { bg: "var(--info-soft)", fg: "var(--info)", label: "Approved" },
  rejected: { bg: "var(--danger-soft)", fg: "var(--danger)", label: "Rejected" },
  scheduled: { bg: "var(--info-soft)", fg: "var(--info)", label: "Scheduled" },
  deploying: { bg: "var(--ai-soft)", fg: "var(--ai)", label: "Deploying" },
  completed: { bg: "var(--ok-soft)", fg: "var(--ok)", label: "Completed" },
  failed: { bg: "var(--danger-soft)", fg: "var(--danger)", label: "Failed" },
  partially_failed: { bg: "var(--warn-soft)", fg: "var(--warn)", label: "Partially failed" },
  expired: { bg: "var(--bg-interactive)", fg: "var(--text-muted)", label: "Expired" },
};

// One change inside a bundle has its own lifecycle (pending → applying →
// succeeded | failed | skipped); it is not a draft once the bundle is submitted.
const ITEM_STATUS_STYLE = {
  pending: { bg: "var(--bg-interactive)", fg: "var(--text-muted)", label: "Waiting" },
  applying: { bg: "var(--ai-soft)", fg: "var(--ai)", label: "Applying" },
  succeeded: { bg: "var(--ok-soft)", fg: "var(--ok)", label: "Applied" },
  failed: { bg: "var(--danger-soft)", fg: "var(--danger)", label: "Failed" },
  skipped: { bg: "var(--bg-interactive)", fg: "var(--text-muted)", label: "Skipped" },
};

function StatusBadge({ status, styles = STATUS_STYLE }) {
  const s = styles[status] || { ...STATUS_STYLE.draft, label: String(status || "Unknown").replace(/_/g, " ") };
  return (
    <span
      style={{
        fontSize: "0.72rem",
        fontWeight: 600,
        color: s.fg,
        background: s.bg,
        borderRadius: 6,
        padding: "2px 10px",
      }}
    >
      {s.label}
    </span>
  );
}

function ItemRow({ bundleId, item }) {
  const [view, setView] = useState(null); // null | "preview" | "diff"
  const [diff, setDiff] = useState(null);

  const toggleDiff = async () => {
    if (view === "diff") {
      setView(null);
      return;
    }
    setView("diff");
    if (diff === null) {
      try {
        const res = await diffBundleItem(bundleId, item.id);
        setDiff(res.diff || "No differences.");
      } catch (err) {
        setDiff(`Diff failed: ${err.message}`);
      }
    }
  };

  return (
    <li className="card" style={{ padding: "var(--space-2) var(--space-3)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center" }}>
        <div style={{ fontSize: "0.85rem" }}>
          <span style={{ color: "var(--accent-strong)", fontWeight: 600 }}>{ACTION_LABEL[item.actionType] || item.actionType}</span>{" "}
          — {item.clusterName || item.clusterId}
          {item.namespace ? ` / ${item.namespace}` : ""}
          {item.resourceName ? ` / ${item.resourceKind} ${item.resourceName}` : ""}
        </div>
        <StatusBadge status={item.status} styles={ITEM_STATUS_STYLE} />
      </div>
      {item.executionResult?.error ? (
        <p className="muted" style={{ margin: "4px 0 0", fontSize: "0.75rem", color: "var(--danger)" }}>
          {item.executionResult.error}
        </p>
      ) : null}
      <div style={{ display: "flex", gap: 12, marginTop: 4 }}>
        <button type="button" className="link-button" style={{ fontSize: "0.78rem" }} onClick={toggleDiff}>
          {view === "diff" ? "Hide diff" : "View diff"}
        </button>
        {item.yamlPreview ? (
          <button
            type="button"
            className="link-button"
            style={{ fontSize: "0.78rem" }}
            onClick={() => setView((v) => (v === "preview" ? null : "preview"))}
          >
            {view === "preview" ? "Hide preview" : "View preview"}
          </button>
        ) : null}
      </div>
      {view === "diff" ? <DiffBlock text={diff ?? "Loading diff…"} /> : null}
      {view === "preview" ? (
        <pre
          style={{
            marginTop: 8,
            maxHeight: 240,
            overflow: "auto",
            background: "var(--bg-inset)",
            color: "var(--text-main)",
            border: "1px solid var(--border-strong)",
            borderRadius: 8,
            padding: 10,
            fontSize: "0.72rem",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {item.yamlPreview}
        </pre>
      ) : null}
    </li>
  );
}

function BundleCard({ bundle, canManage, currentUserId, onApprove, onReject, onDelete, busy }) {
  const [detail, setDetail] = useState(bundle.items ? bundle : null);
  const [open, setOpen] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [detailError, setDetailError] = useState("");

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (next && !detail?.items) {
      setLoadingDetail(true);
      setDetailError("");
      try {
        setDetail(await getChangeBundle(bundle.id));
      } catch (err) {
        setDetailError(err.message || "Could not load the changes in this bundle.");
      } finally {
        setLoadingDetail(false);
      }
    }
  };

  const items = detail?.items || [];
  const canReviewNow = canManage && bundle.status === "pending_approval";
  // Nobody approves their own bundle (the server refuses it); they may still decline it.
  const isOwn = currentUserId != null && bundle.requesterId === currentUserId;

  return (
    <div className="card" style={{ padding: "var(--space-3)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <strong>Bundle #{bundle.id}</strong>
            <StatusBadge status={bundle.status} />
          </div>
          <p className="muted" style={{ margin: "4px 0 0", fontSize: "0.82rem" }}>
            {bundle.requesterName} · {bundle.itemCount} change{bundle.itemCount === 1 ? "" : "s"}
            {(bundle.clusterNames || bundle.clusters)?.length
              ? ` · ${(bundle.clusterNames || bundle.clusters).join(", ")}`
              : ""}
          </p>
          {bundle.requestedWindowLabel ? (
            <p className="muted" style={{ margin: "2px 0 0", fontSize: "0.8rem" }}>
              Window: {bundle.requestedWindowLabel}
            </p>
          ) : null}
          {bundle.requiredApprovals > 1 ? (
            <p className="muted" style={{ margin: "2px 0 0", fontSize: "0.78rem" }}>
              {bundle.approvals} of {bundle.requiredApprovals} approval(s)
            </p>
          ) : null}
          {bundle.rejectionReason ? (
            <p className="muted" style={{ margin: "2px 0 0", fontSize: "0.78rem", color: "var(--danger)" }}>
              Reason: {bundle.rejectionReason}
            </p>
          ) : null}
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "flex-start", flexWrap: "wrap" }}>
          <button type="button" className="btn-outline btn-compact" onClick={toggle}>
            {open ? "Hide changes" : "View changes"}
          </button>
          {canReviewNow ? (
            <>
              <button
                type="button"
                className="primary btn-compact"
                disabled={busy || isOwn}
                title={isOwn ? "Your bundle — another approver decides it." : undefined}
                onClick={() => onApprove(bundle)}
              >
                Approve
              </button>
              <button
                type="button"
                className="btn-outline btn-compact"
                disabled={busy}
                onClick={() => onReject(bundle)}
              >
                Reject
              </button>
            </>
          ) : null}
          {onDelete &&
          ["draft", "rejected", "expired", "failed", "completed", "partially_failed"].includes(
            bundle.status
          ) ? (
            <button
              type="button"
              className="btn-danger-outline btn-compact"
              disabled={busy}
              onClick={() => onDelete(bundle)}
            >
              Delete
            </button>
          ) : null}
        </div>
      </div>

      {bundle.note ? (
        <p style={{ margin: "10px 0 0", fontSize: "0.85rem" }}>{bundle.note}</p>
      ) : null}

      {open ? (
        loadingDetail ? (
          <p className="muted" style={{ marginTop: 10 }}>
            Loading changes…
          </p>
        ) : detailError ? (
          <p className="banner-message error" style={{ marginTop: 10 }}>
            {detailError}
          </p>
        ) : (
          <ul style={{ listStyle: "none", margin: "12px 0 0", padding: 0, display: "grid", gap: 8 }}>
            {items.map((item) => (
              <ItemRow key={item.id} bundleId={bundle.id} item={item} />
            ))}
          </ul>
        )
      ) : null}
    </div>
  );
}

export default function ChangeBundlesPage() {
  const { hasPermission } = usePermission();
  const canManage = hasPermission("change_bundles:manage");
  const canCreate = hasPermission("change_bundles:create");
  const { user } = useAuth();
  const { openDrawer, refresh: refreshDraft } = useChangeBundle();

  const tabs = useMemo(() => {
    const list = [];
    if (canCreate) list.push({ key: "mine", label: "My Bundles" });
    // Bundles waiting for approval live in the Approvals inbox now, with
    // every other kind of approval (old #/change-bundles/pending links go there).
    if (canManage) list.push({ key: "all", label: "All Bundles" });
    return list.length ? list : [{ key: "mine", label: "My Bundles" }];
  }, [canCreate, canManage]);

  const [activeTab, setActiveTab] = useRouteParam("tab", tabs[0]?.key || "mine");

  // The visible set is RBAC-filtered, so an address can name a tab this user
  // cannot open. Correct it in place rather than leaving it in history.
  const [, replaceTab] = useRouteParam("tab", "mine", { replace: true });
  const { navigate } = useRouter();
  useEffect(() => {
    if (activeTab === "pending") {
      navigate({ key: "deploymentRequests", params: { tab: canManage ? "waiting" : "pending" } }, { replace: true });
      return;
    }
    if (tabs.length && !tabs.some((entry) => entry.key === activeTab)) {
      replaceTab(tabs[0].key);
    }
  }, [tabs, activeTab, replaceTab, navigate, canManage]);
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [busyId, setBusyId] = useState(null);

  const load = useCallback(
    async ({ silent = false } = {}) => {
      if (!silent) setLoading(true);
      try {
        let data;
        if (activeTab === "mine") data = await listMyBundles({ limit: 200 });
        else if (activeTab === "pending") data = await listChangeBundles({ status: "pending_approval", limit: 200 });
        else data = await listChangeBundles({ limit: 200 });
        setItems(data.items || []);
        setError("");
      } catch (err) {
        if (!silent) setError(err.message);
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [activeTab]
  );

  useEffect(() => {
    load();
  }, [load]);

  // Statuses change out-of-band (other approvers, scheduled execution). Poll quietly.
  useEffect(() => {
    const stopPolling = setVisibleInterval(() => load({ silent: true }), 15000);
    return stopPolling;
  }, [load]);

  const approve = async (bundle) => {
    setBusyId(bundle.id);
    setActionError("");
    try {
      await approveChangeBundle(bundle.id);
      await load({ silent: true });
    } catch (err) {
      setActionError(err.message || "Failed to approve bundle.");
    } finally {
      setBusyId(null);
    }
  };

  const reject = async (bundle) => {
    const reason = window.prompt("Reason for rejecting this bundle (optional):", "");
    // Cancel on the prompt means "don't reject", not "reject without a reason".
    if (reason === null) return;
    setBusyId(bundle.id);
    setActionError("");
    try {
      await rejectChangeBundle(bundle.id, reason);
      await load({ silent: true });
    } catch (err) {
      setActionError(err.message || "Failed to reject bundle.");
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (bundle) => {
    if (!window.confirm(`Delete bundle #${bundle.id}? This cannot be undone.`)) return;
    setBusyId(bundle.id);
    setActionError("");
    try {
      await deleteChangeBundle(bundle.id);
      await load({ silent: true });
      refreshDraft();
    } catch (err) {
      setActionError(err.message || "Failed to delete bundle.");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="ops-page">
      <section className="card ops-section">
        <div className="card-header-row" style={{ marginBottom: "var(--space-2)" }}>
          <div>
            <h2>Change Bundles</h2>
            <p className="muted">
              Stage Kubernetes changes, submit them for approval with a deployment window, and let
              KubeSight apply them automatically when the window opens.
            </p>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            {canCreate ? (
              <button type="button" className="primary btn-compact" onClick={openDrawer}>
                Open Change Bundle
              </button>
            ) : null}
            <button type="button" className="btn-outline btn-compact" onClick={() => load()} disabled={loading}>
              Refresh
            </button>
          </div>
        </div>

        {tabs.length > 1 ? (
          <nav className="tab-bar" aria-label="Change bundle views">
            {tabs.map((tab) => (
              <button
                key={tab.key}
                type="button"
                className={activeTab === tab.key ? "active" : ""}
                onClick={() => setActiveTab(tab.key)}
              >
                {tab.label}
              </button>
            ))}
          </nav>
        ) : null}

        {actionError ? <ErrorBanner message={actionError} suppressAccessDenied={false} /> : null}

        {loading ? (
          <p className="muted">Loading change bundles…</p>
        ) : error ? (
          <ErrorBanner message={error} suppressAccessDenied={false} />
        ) : items.length === 0 ? (
          <p className="muted">No change bundles yet.</p>
        ) : (
          <div style={{ display: "grid", gap: "var(--space-2)" }}>
            {items.map((bundle) => (
              <BundleCard
                key={bundle.id}
                bundle={bundle}
                canManage={canManage}
                currentUserId={user?.id}
                busy={busyId === bundle.id}
                onApprove={approve}
                onReject={reject}
                onDelete={activeTab === "mine" ? remove : undefined}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
