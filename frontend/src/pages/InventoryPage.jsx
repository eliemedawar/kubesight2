import { useState } from "react";

import AccessScopeView from "../components/common/AccessScopeView.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import AddAppModal from "../components/inventory/AddAppModal.jsx";
import TemplateMarketplace from "../components/inventory/TemplateMarketplace.jsx";
import ApplicationBuilderWizard from "../components/inventory/wizard/ApplicationBuilderWizard.jsx";
import { getWizardTemplate } from "../api/inventoryApi.js";
import { EMPTY_MESSAGES } from "../utils/authz.js";
import { normalizeClusterOptions } from "../utils/clusterOptions.js";
import { applyTemplate, createEmptyWizardState } from "../components/inventory/wizard/wizardDefaults.js";

export default function InventoryPage({
  coreLoading = false,
  accessError = "",
  hasClusters,
  clusterOptions = [],
  defaultClusterId = "",
  canRegister = false,
  canDeploy = false,
  canHelmInstall = false,
  onRefresh,
}) {
  const [addModalOpen, setAddModalOpen] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [wizardInitialState, setWizardInitialState] = useState(null);
  const [templateBusy, setTemplateBusy] = useState(false);
  const [templateError, setTemplateError] = useState("");

  const canAddApp = canRegister || canDeploy || canHelmInstall;
  const clusterSelectOptions = normalizeClusterOptions(clusterOptions);
  const resolvedDefaultClusterId =
    defaultClusterId && clusterSelectOptions.some((c) => c.id === defaultClusterId)
      ? defaultClusterId
      : clusterSelectOptions[0]?.id || "";

  const openWizardFromScratch = () => {
    setTemplateError("");
    setWizardInitialState(createEmptyWizardState(resolvedDefaultClusterId));
    setWizardOpen(true);
  };

  const openWizardFromTemplate = async (templateId) => {
    setTemplateBusy(true);
    setTemplateError("");
    try {
      const template = await getWizardTemplate(templateId);
      setWizardInitialState(applyTemplate(createEmptyWizardState(resolvedDefaultClusterId), template));
      setWizardOpen(true);
    } catch (err) {
      setTemplateError(err.message || "Failed to load template");
    } finally {
      setTemplateBusy(false);
    }
  };

  const marketplaceHeader = (
    <PageTitle
      title="Application Templates"
      subtitle="Deployment hub for launching workloads. View running applications in Resources."
    />
  );

  if (coreLoading || accessError || !hasClusters) {
    return (
      <div className="ops-page inventory-page template-marketplace-page">
        <AccessScopeView
          coreLoading={coreLoading}
          accessError={accessError}
          empty={!hasClusters}
          emptyMessage={EMPTY_MESSAGES.noClusters}
          loadingLabel="Loading clusters..."
          header={marketplaceHeader}
        >
          {null}
        </AccessScopeView>
      </div>
    );
  }

  return (
    <div className="ops-page inventory-page template-marketplace-page">
      <PageTitle
        title="Application Templates"
        subtitle="Choose a template or start from scratch to deploy workloads to your clusters. View deployed applications in Resources."
        actionLabel={canDeploy ? "Start From Scratch" : canAddApp ? "Add Application" : undefined}
        onAction={
          canDeploy
            ? openWizardFromScratch
            : canAddApp
              ? () => setAddModalOpen(true)
              : undefined
        }
      />

      {templateError ? <p className="banner-message error">{templateError}</p> : null}

      <TemplateMarketplace
        canDeploy={canDeploy}
        busy={templateBusy}
        onStartFromScratch={openWizardFromScratch}
        onSelectTemplate={openWizardFromTemplate}
      />

      <AddAppModal
        open={addModalOpen}
        onClose={() => setAddModalOpen(false)}
        onSuccess={() => onRefresh?.()}
        onOpenWizard={openWizardFromScratch}
        clusterOptions={clusterSelectOptions}
        defaultClusterId={resolvedDefaultClusterId}
        canRegister={canRegister}
        canDeploy={canDeploy}
        canHelmInstall={canHelmInstall}
      />

      <ApplicationBuilderWizard
        open={wizardOpen}
        onClose={() => {
          setWizardOpen(false);
          setWizardInitialState(null);
        }}
        onSuccess={() => onRefresh?.()}
        clusterOptions={clusterSelectOptions}
        defaultClusterId={resolvedDefaultClusterId}
        initialState={wizardInitialState}
        showTemplatePicker={false}
      />
    </div>
  );
}
