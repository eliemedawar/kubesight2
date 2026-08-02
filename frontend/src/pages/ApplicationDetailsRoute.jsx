import { lazy, Suspense, useCallback, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import ApplicationDetailsPage from "./ApplicationDetailsPage.jsx";
import ConfirmDialog from "../components/common/ConfirmDialog.jsx";
import { useAuth } from "../context/AuthContext.jsx";
import { useApplicationDetail, useInventoryClusterOptions } from "../hooks/useInventory.js";
import { removeFromInventory, updateCatalogEntry } from "../api/inventoryApi.js";

const EditCatalogModal = lazy(() => import("../components/inventory/EditCatalogModal.jsx"));

/**
 * Route container for one application.
 *
 * `ApplicationDetailsPage` stays a presentational component taking twenty-odd
 * props; this owns the data and the two mutations it needs. Splitting rather
 * than merging keeps the largest component on the route untouched, so a
 * regression here is attributable to the move and not to a rewrite of the view.
 *
 * The screen was unreachable before routing — nothing ever set the page key
 * that rendered it (audit finding F3) — so its data path had no exercised code
 * path at all. It is reachable by URL and, now, from the inventory.
 */
export default function ApplicationDetailsRoute({ allowedClusters = [] }) {
  const { applicationId } = useParams();
  const navigate = useNavigate();
  const auth = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();

  const { detail, loading, reload } = useApplicationDetail(applicationId);
  const { options: clusterOptions } = useInventoryClusterOptions({
    allowedClusters,
    enabled: true,
  });

  // The tab is in the URL, so a colleague can be sent the Logs tab of a failing
  // application rather than the application and directions.
  const tab = searchParams.get("tab") || "overview";
  const setTab = useCallback(
    (next) => {
      const params = new URLSearchParams(searchParams);
      if (next && next !== "overview") {
        params.set("tab", next);
      } else {
        params.delete("tab");
      }
      // replace: flicking through tabs should not fill the back stack.
      setSearchParams(params, { replace: true });
    },
    [searchParams, setSearchParams]
  );

  const [editOpen, setEditOpen] = useState(false);
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState("");
  const [removing, setRemoving] = useState(false);
  const [removeTarget, setRemoveTarget] = useState(null);
  const [removeError, setRemoveError] = useState("");

  const entryId = detail?.summary?.catalogEntryId || detail?.catalog?.id;

  const saveCatalog = async (payload) => {
    if (!entryId) {
      return;
    }
    setEditSaving(true);
    setEditError("");
    try {
      await updateCatalogEntry(entryId, payload);
      setEditOpen(false);
      await reload();
    } catch (err) {
      setEditError(err.message || "Update failed");
    } finally {
      setEditSaving(false);
    }
  };

  const confirmRemove = async () => {
    const id = removeTarget?.summary?.catalogEntryId || removeTarget?.catalog?.id;
    if (!id) {
      return;
    }
    setRemoving(true);
    setRemoveError("");
    try {
      await removeFromInventory(id);
      setRemoveTarget(null);
      navigate("/applications");
    } catch (err) {
      setRemoveError(err.message || "Could not remove this application.");
    } finally {
      setRemoving(false);
    }
  };

  return (
    <>
      <ApplicationDetailsPage
        detail={detail}
        selectedApplicationId={applicationId}
        loading={loading}
        user={auth.user}
        activeTab={tab}
        onTabChange={setTab}
        onBack={() => navigate("/applications")}
        onRefreshDetail={reload}
        canViewLogs={auth.hasPermission("logs:view")}
        canUpdateCatalog={auth.hasPermission("inventory:update")}
        canRemoveFromInventory={auth.hasPermission("inventory:remove")}
        canDeploy={auth.hasPermission("apps:deploy")}
        canViewHelm={auth.hasPermission("helm:view")}
        canUpgradeHelm={auth.hasPermission("helm:upgrade")}
        canRollbackHelm={auth.hasPermission("helm:rollback")}
        canUninstallHelm={auth.hasPermission("helm:uninstall")}
        onEditCatalog={() => setEditOpen(true)}
        onRemoveFromInventory={(target) => setRemoveTarget(target)}
        onDeployUpdate={() => {}}
        clusterOptions={clusterOptions}
        onHelmUpgrade={() => navigate("/applications")}
        onHelmActionComplete={reload}
      />

      {editOpen ? (
        <Suspense fallback={null}>
          <EditCatalogModal
            open={editOpen}
            catalog={detail?.catalog || {}}
            onClose={() => setEditOpen(false)}
            onSave={saveCatalog}
            saving={editSaving}
            error={editError}
          />
        </Suspense>
      ) : null}

      {/*
        Replaces a window.confirm whose entire job was reassurance — that this
        removes KubeSight's record and touches nothing in the cluster. That is
        exactly the sentence window.confirm renders worst and the one an
        operator most needs to believe before clicking, so it gets a dialog that
        can actually say it. No typed phrase: nothing in the cluster changes,
        and friction on a safe action is what trains people to click through the
        gates that matter.
      */}
      <ConfirmDialog
        open={Boolean(removeTarget)}
        tone="warn"
        title="Remove from inventory?"
        body="This removes KubeSight's record of the application. Nothing is deleted from the Kubernetes cluster, and the workload keeps running."
        confirmLabel="Remove record"
        busy={removing}
        error={removeError}
        onCancel={() => {
          setRemoveTarget(null);
          setRemoveError("");
        }}
        onConfirm={confirmRemove}
      />
    </>
  );
}
