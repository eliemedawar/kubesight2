import { useCallback, useEffect, useMemo, useState } from "react";

import {
  createWizardTemplate,
  deleteWizardTemplate,
  deleteWizardTemplateCategory,
  getWizardTemplate,
  listWizardTemplates,
  updateWizardTemplate,
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
  const [editTemplate, setEditTemplate] = useState(null);
  const [actionError, setActionError] = useState("");
  const [deletingId, setDeletingId] = useState("");
  const [editingId, setEditingId] = useState("");
  const [deletingCategory, setDeletingCategory] = useState("");
  const [query, setQuery] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");

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

  // Only categories that actually contain templates — the built-in placeholders
  // (Web/Database/…) are no longer shipped, so they shouldn't clutter the pickers.
  const presentCategories = useMemo(() => {
    const present = new Set(templates.map((t) => (t.category || "Custom").trim() || "Custom"));
    return resolveCategoryOrder(templates).filter((category) => present.has(category));
  }, [templates]);

  const filteredTemplates = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return templates.filter((t) => {
      if (categoryFilter && (t.category || "Custom") !== categoryFilter) return false;
      if (!needle) return true;
      return (
        (t.name || "").toLowerCase().includes(needle) ||
        (t.description || "").toLowerCase().includes(needle)
      );
    });
  }, [templates, query, categoryFilter]);

  const grouped = useMemo(() => groupTemplatesByCategory(filteredTemplates), [filteredTemplates]);
  const categories = useMemo(() => resolveCategoryOrder(filteredTemplates), [filteredTemplates]);
  const hasFilter = Boolean(query.trim() || categoryFilter);

  const handleTemplateClick = (templateId) => {
    if (!canDeploy || busy) return;
    onSelectTemplate?.(templateId);
  };

  const handleScratchClick = () => {
    if (!canDeploy || busy) return;
    onStartFromScratch?.();
  };

  // Strip a trailing " vN" suffix; a name with no suffix is treated as version 1.
  const splitVersion = (name) => {
    const match = /^(.*?)\s+v(\d+)$/i.exec((name || "").trim());
    return match ? { base: match[1].trim(), version: Number.parseInt(match[2], 10) } : { base: (name || "").trim(), version: 1 };
  };

  // The next "vN" name for a template family, scanning existing names so we never
  // collide with a version that already exists.
  const nextVersionName = (name) => {
    const { base } = splitVersion(name);
    let max = 1;
    for (const t of templates) {
      const parsed = splitVersion(t.name);
      if (parsed.base.toLowerCase() === base.toLowerCase() && parsed.version > max) max = parsed.version;
    }
    return `${base} v${max + 1}`;
  };

  const handleSubmitTemplate = async (payload, { asNewVersion = false } = {}) => {
    if (editTemplate && asNewVersion) {
      // Fork into a fresh template with a bumped version name; the backend assigns
      // a new id, so the original template is left untouched.
      await createWizardTemplate({ ...payload, name: nextVersionName(payload.name) });
    } else if (editTemplate) {
      await updateWizardTemplate(editTemplate.id, payload);
    } else {
      await createWizardTemplate(payload);
    }
    setCreateOpen(false);
    setEditTemplate(null);
    loadTemplates();
  };

  const handleEdit = async (template) => {
    if (editingId || busy) return;
    setActionError("");
    setEditingId(template.id);
    try {
      const detail = await getWizardTemplate(template.id);
      setEditTemplate(detail);
      setCreateOpen(true);
    } catch (err) {
      setActionError(err.message || "Failed to load template for editing");
    } finally {
      setEditingId("");
    }
  };

  const handleDeleteCategory = async (category, count) => {
    if (deletingCategory) return;
    if (
      !window.confirm(
        `Delete the "${category}" category and its ${count} template${count === 1 ? "" : "s"}? This cannot be undone.`,
      )
    )
      return;
    setActionError("");
    setDeletingCategory(category);
    try {
      await deleteWizardTemplateCategory(category);
      if (categoryFilter === category) setCategoryFilter("");
      loadTemplates();
    } catch (err) {
      setActionError(err.message || "Failed to delete category");
    } finally {
      setDeletingCategory("");
    }
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
            onClick={() => {
              setEditTemplate(null);
              setCreateOpen(true);
            }}
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

      {!loading ? (
        <div className="template-marketplace__filters">
          <input
            type="search"
            className="template-marketplace__search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search templates by name…"
            aria-label="Search templates by name"
          />
          <select
            value={categoryFilter}
            onChange={(e) => setCategoryFilter(e.target.value)}
            aria-label="Filter by category"
          >
            <option value="">All categories</option>
            {presentCategories.map((category) => (
              <option key={category} value={category}>{category}</option>
            ))}
          </select>
          {hasFilter ? (
            <button
              type="button"
              className="btn-outline"
              onClick={() => {
                setQuery("");
                setCategoryFilter("");
              }}
            >
              Clear
            </button>
          ) : null}
        </div>
      ) : null}

      {error ? <p className="banner-message error">{error}</p> : null}
      {actionError ? <p className="banner-message error">{actionError}</p> : null}
      {loading ? <p className="muted">Loading application templates…</p> : null}

      {!loading && hasFilter && filteredTemplates.length === 0 ? (
        <p className="template-marketplace__empty muted">No templates match your filters.</p>
      ) : null}

      {!loading && !hasFilter && templates.length === 0 ? (
        <p className="template-marketplace__empty muted">
          No templates yet. Use <strong>Create Template</strong> or <strong>Start From Scratch</strong> above to add one.
        </p>
      ) : null}

      {!loading
        ? categories
            .filter((category) => (grouped[category] || []).length)
            .map((category) => {
              const items = grouped[category] || [];
              const customCount = items.filter((t) => t.custom).length;
              return (
                <section key={category} className="template-marketplace__category" aria-labelledby={`category-${category}`}>
                  <div className="template-marketplace__category-head">
                    <h2 id={`category-${category}`} className="template-marketplace__category-title">
                      {category}
                    </h2>
                    {canManageTemplates && customCount > 0 ? (
                      <button
                        type="button"
                        className="template-marketplace__category-delete"
                        title={`Delete the "${category}" category and its templates`}
                        onClick={() => handleDeleteCategory(category, customCount)}
                        disabled={deletingCategory === category}
                      >
                        {deletingCategory === category ? "Deleting…" : "Delete category"}
                      </button>
                    ) : null}
                  </div>
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
                          <>
                            <button
                              type="button"
                              className="template-card__edit"
                              title="Edit template"
                              aria-label={`Edit ${template.name}`}
                              onClick={() => handleEdit(template)}
                              disabled={editingId === template.id || deletingId === template.id}
                            >
                              {editingId === template.id ? "…" : "✎"}
                            </button>
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
                          </>
                        ) : null}
                      </div>
                    ))}
                  </div>
                </section>
              );
            })
        : null}

      {canManageTemplates ? (
        <CreateTemplateModal
          open={createOpen}
          template={editTemplate}
          existingCategories={presentCategories}
          clusterOptions={clusterOptions}
          defaultClusterId={defaultClusterId}
          onClose={() => {
            setCreateOpen(false);
            setEditTemplate(null);
          }}
          onSubmit={handleSubmitTemplate}
        />
      ) : null}
    </div>
  );
}
