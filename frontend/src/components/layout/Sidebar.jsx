export default function Sidebar({ pages, activePage, onNavigate }) {
  const sections = [];
  const sectionIndex = new Map();

  pages.forEach((page) => {
    const sectionLabel = page.section || "";
    if (!sectionIndex.has(sectionLabel)) {
      sectionIndex.set(sectionLabel, sections.length);
      sections.push({ label: sectionLabel, pages: [] });
    }
    sections[sectionIndex.get(sectionLabel)].pages.push(page);
  });

  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <h1>KubeSight</h1>
        <p className="brand-subtitle">Control Plane</p>
      </div>
      <nav>
        {sections.map((section) => (
          <div key={section.label || "main"} className="sidebar-section">
            {section.label ? <p className="sidebar-section-label">{section.label}</p> : null}
            {section.pages.map((page) => (
              <button
                key={page.key}
                type="button"
                className={`nav-link ${activePage === page.key ? "active" : ""}`}
                onClick={() => onNavigate(page.key)}
              >
                {page.label}
              </button>
            ))}
          </div>
        ))}
      </nav>
      <p className="sidebar-footer">Kubernetes Dashboard v1.0.0</p>
    </aside>
  );
}
