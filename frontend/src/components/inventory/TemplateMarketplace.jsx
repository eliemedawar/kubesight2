import { useEffect, useMemo, useState } from "react";

import { listWizardTemplates } from "../../api/inventoryApi.js";
import { groupTemplatesByCategory, TEMPLATE_CATEGORIES } from "../../utils/applicationTemplates.js";

export default function TemplateMarketplace({
  canDeploy = false,
  onStartFromScratch,
  onSelectTemplate,
  busy = false,
}) {
  const [templates, setTemplates] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
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

  const grouped = useMemo(() => groupTemplatesByCategory(templates), [templates]);

  const handleTemplateClick = (templateId) => {
    if (!canDeploy || busy) return;
    onSelectTemplate?.(templateId);
  };

  const handleScratchClick = () => {
    if (!canDeploy || busy) return;
    onStartFromScratch?.();
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
      </section>

      {error ? <p className="banner-message error">{error}</p> : null}
      {loading ? <p className="muted">Loading application templates…</p> : null}

      {!loading
        ? TEMPLATE_CATEGORIES.map((category) => {
            const items = grouped[category] || [];
            return (
              <section key={category} className="template-marketplace__category" aria-labelledby={`category-${category}`}>
                <h2 id={`category-${category}`} className="template-marketplace__category-title">
                  {category}
                </h2>
                {items.length ? (
                  <div className="template-marketplace__grid">
                    {items.map((template) => (
                      <button
                        key={template.id}
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
    </div>
  );
}
