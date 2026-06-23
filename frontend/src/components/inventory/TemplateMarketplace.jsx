import { useCallback, useEffect, useMemo, useState } from "react";

import {
  createWizardTemplate,
  deleteWizardTemplate,
  listWizardTemplates,
} from "../../api/inventoryApi.js";
import { groupTemplatesByCategory, resolveCategoryOrder } from "../../utils/applicationTemplates.js";
import CreateTemplateModal from "./CreateTemplateModal.jsx";

export default function TemplateMarketplace({
  canDeploy = false,
  canManageTemplates = false,
  clusterOptions = [],
  defaultClusterId = "",
  onStartFromScratch,
  onSelectTemplate,
  busy = false,
}) {
  const [templates, setTemplates] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [actionError, setActionError] = useState("");
  const [deletingId, setDeletingId] = useState("");

  const loadTemplates = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    listWizardTemplates()
      .then((items) => {
        if (!cancelled) setTemplates(items);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || "Failed to load templates");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => loadTemplates(), [loadTemplates]);

  const grouped = useMemo(() => groupTemplatesByCategory(templates), [templates]);
  const categories = useMemo(() => resolveCategoryOrder(templates), [templates]);

  const handleTemplateClick = (templateId) => {
    if (!canDeploy || busy) return;
    onSelectTemplate?.(templateId);
  };

  const handleScratchClick = () => {
    if (!canDeploy || busy) return;
    onStartFromScratch?.();
  };

  const handleCreate = async (payload) => {
    await createWizardTemplate(payload);
    setCreateOpen(false);
    loadTemplates();
  };

  const handleDelete = async (template) => {
    if (deletingId) return;
    if (!window.confirm(`Delete the template "${template.name}"? This cannot be undone.`)) return;
    setActionError("");
    setDeletingId(template.id);
    try {
      await deleteWizardTemplate(template.id);
      loadTemplates();
    } catch (err) {
      setActionError(err.message || "Failed to delete template");
    } finally {
      setDeletingId("");
    }
  };

  return (
    <div className="template-marketplace">
      <section className="template-marketplace__featured" aria-labelledby="template-scratch-title">
        <button
          type="button"
          className="template-card template-card--scratch card"
          onClick={handleScratchClick}
          disabled={!canDeploy || busy}
        >
          <span className="template-card__icon" aria-hidden="true">
            +
          </span>
          <div className="template-card__body">
            <h3 id="template-scratch-title">Start From Scratch</h3>
            <p className="muted">
              Launch the Application Builder with a blank slate. Define your own workload, containers, and networking.
            </p>
          </div>
          <span className="template-card__cta">{canDeploy ? "Open builder" : "Deploy permission required"}</span>
        </button>

        {canManageTemplates ? (
          <button
            type="button"
            className="template-card template-card--scratch card"
            onClick={() => setCreateOpen(true)}
            disabled={busy}
          >
            <span className="template-card__icon" aria-hidden="true">
              ★
            </span>
            <div className="template-card__body">
              <h3>Create Template</h3>
              <p className="muted">
                Save a reusable workload as a template and place it in a new or existing category.
              </p>
            </div>
            <span className="template-card__cta">New template</span>
          </button>
        ) : null}
      </section>

      {error ? <p className="banner-message error">{error}</p> : null}
      {actionError ? <p className="banner-message error">{actionError}</p> : null}
      {loading ? <p className="muted">Loading application templates…</p> : null}

      {!loading
        ? categories.map((category) => {
            const items = grouped[category] || [];
            return (
              <section key={category} className="template-marketplace__category" aria-labelledby={`category-${category}`}>
                <h2 id={`category-${category}`} className="template-marketplace__category-title">
                  {category}
                </h2>
                {items.length ? (
                  <div className="template-marketplace__grid">
                    {items.map((template) => (
                      <div key={template.id} className="template-card-slot">
                        <button
                          type="button"
                          className="template-card card"
                          onClick={() => handleTemplateClick(template.id)}
                          disabled={!canDeploy || busy}
                        >
                          <div className="template-card__header">
                            <strong>{template.name}</strong>
                            <span className="template-card__badge">{template.workloadType}</span>
                          </div>
                          <p className="template-card__description muted">{template.description}</p>
                        </button>
                        {canManageTemplates && template.custom ? (
                          <button
                            type="button"
                            className="template-card__delete"
                            title="Delete template"
                            aria-label={`Delete ${template.name}`}
                            onClick={() => handleDelete(template)}
                            disabled={deletingId === template.id}
                          >
                            {deletingId === template.id ? "…" : "×"}
                          </button>
                        ) : null}
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="template-marketplace__empty muted">
                    {category === "Custom"
                      ? "Use Start From Scratch above to build a custom workload tailored to your needs."
                      : "No templates in this category yet."}
                  </p>
                )}
              </section>
            );
          })
        : null}

      {canManageTemplates ? (
        <CreateTemplateModal
          open={createOpen}
          existingCategories={categories}
          clusterOptions={clusterOptions}
          defaultClusterId={defaultClusterId}
          onClose={() => setCreateOpen(false)}
          onSubmit={handleCreate}
        />
      ) : null}
    </div>
  );
}
