import { lazy, Suspense, useState } from "react";

import AccessScopeView from "../components/common/AccessScopeView.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import TemplateMarketplace from "../components/inventory/TemplateMarketplace.jsx";
import { getWizardTemplate } from "../api/inventoryApi.js";
import { applyImportToWizard } from "../api/deploymentFormsApi.js";
import { EMPTY_MESSAGES } from "../utils/authz.js";
import { normalizeClusterOptions } from "../utils/clusterOptions.js";
import { applyTemplate, createEmptyWizardState } from "../components/inventory/wizard/wizardDefaults.js";

const AddAppModal = lazy(() => import("../components/inventory/AddAppModal.jsx"));
const GenerateDeploymentFormModal = lazy(() =>
  import("../components/inventory/GenerateDeploymentFormModal.jsx")
);
const ImportDeploymentFormModal = lazy(() =>
  import("../components/inventory/ImportDeploymentFormModal.jsx")
);
const ApplicationBuilderWizard = lazy(() =>
  import("../components/inventory/wizard/ApplicationBuilderWizard.jsx")
);
const SchemaDeployWizard = lazy(() =>
  import("../components/inventory/wizard/SchemaDeployWizard.jsx")
);

/** A template drives the streamlined schema wizard once it declares any schema. */
function hasDeploymentSchema(template) {
  const schema = template?.schema;
  if (!schema) return false;
  return Boolean(
    schema.env?.length ||
      schema.dependencies?.length ||
      schema.ingress?.supported ||
      (schema.overrides && Object.keys(schema.overrides).length),
  );
}

export default function InventoryPage({
  coreLoading = false,
  accessError = "",
  hasClusters,
  clusterOptions = [],
  defaultClusterId = "",
  canRegister = false,
  canDeploy = false,
  canHelmInstall = false,
  canManageTemplates = false,
  onRefresh,
}) {
  const [addModalOpen, setAddModalOpen] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [wizardInitialState, setWizardInitialState] = useState(null);
  const [schemaTemplate, setSchemaTemplate] = useState(null);
  const [schemaInitialAnswers, setSchemaInitialAnswers] = useState(null);
  const [generateFormTemplate, setGenerateFormTemplate] = useState(null);
  const [importFormOpen, setImportFormOpen] = useState(false);
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
    setSchemaTemplate(null);
    setSchemaInitialAnswers(null);
    setWizardInitialState(createEmptyWizardState(resolvedDefaultClusterId));
    setWizardOpen(true);
  };

  const closeSchemaWizard = () => {
    setSchemaTemplate(null);
    setSchemaInitialAnswers(null);
  };

  // "Open in Deploy Wizard" from an imported form: resolve the current template +
  // parsed answers, then open the schema wizard prefilled with them.
  const openWizardFromImport = async (importRecord) => {
    setTemplateBusy(true);
    setTemplateError("");
    try {
      const state = await applyImportToWizard(importRecord.id);
      setImportFormOpen(false);
      setSchemaInitialAnswers(state.answers || null);
      setSchemaTemplate(state.template);
    } catch (err) {
      setTemplateError(err.message || "Failed to open the wizard from the imported form.");
    } finally {
      setTemplateBusy(false);
    }
  };

  const openWizardFromTemplate = async (templateId) => {
    setTemplateBusy(true);
    setTemplateError("");
    setSchemaInitialAnswers(null);  // a plain template open must not reuse imported answers
    try {
      const template = await getWizardTemplate(templateId);
      if (hasDeploymentSchema(template)) {
        // Schema-bearing templates use the streamlined wizard that only asks for
        // what the template leaves open.
        setSchemaTemplate(template);
      } else {
        setSchemaTemplate(null);
        setWizardInitialState(applyTemplate(createEmptyWizardState(resolvedDefaultClusterId), template));
        setWizardOpen(true);
      }
    } catch (err) {
      setTemplateError(err.message || "Failed to load template");
    } finally {
      setTemplateBusy(false);
    }
  };

  const marketplaceHeader = (
    <PageTitle
      title="Inventory Templates"
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
        title="Inventory Templates"
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
        canManageTemplates={canManageTemplates}
        clusterOptions={clusterSelectOptions}
        defaultClusterId={resolvedDefaultClusterId}
        busy={templateBusy}
        onStartFromScratch={openWizardFromScratch}
        onSelectTemplate={openWizardFromTemplate}
        onGenerateForm={canDeploy ? (template) => setGenerateFormTemplate(template) : undefined}
        onImportForm={canDeploy ? () => setImportFormOpen(true) : undefined}
      />

      {addModalOpen ? (
        <Suspense fallback={null}>
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
        </Suspense>
      ) : null}

      {wizardOpen ? (
        <Suspense fallback={null}>
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
        </Suspense>
      ) : null}

      {schemaTemplate ? (
        <Suspense fallback={null}>
          <SchemaDeployWizard
            open={Boolean(schemaTemplate)}
            template={schemaTemplate}
            onClose={closeSchemaWizard}
            onSuccess={() => onRefresh?.()}
            clusterOptions={clusterSelectOptions}
            defaultClusterId={resolvedDefaultClusterId}
            initialAnswers={schemaInitialAnswers}
          />
        </Suspense>
      ) : null}

      {generateFormTemplate ? (
        <Suspense fallback={null}>
          <GenerateDeploymentFormModal
            open={Boolean(generateFormTemplate)}
            template={generateFormTemplate}
            clusterOptions={clusterSelectOptions}
            defaultClusterId={resolvedDefaultClusterId}
            onClose={() => setGenerateFormTemplate(null)}
          />
        </Suspense>
      ) : null}

      {importFormOpen ? (
        <Suspense fallback={null}>
          <ImportDeploymentFormModal
            open={importFormOpen}
            onClose={() => setImportFormOpen(false)}
            onContinueInWizard={openWizardFromImport}
          />
        </Suspense>
      ) : null}
    </div>
  );
}
