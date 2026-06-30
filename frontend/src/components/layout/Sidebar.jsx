const NAV_ICONS = {
  dashboard: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path d="M2 4.5A2.5 2.5 0 014.5 2h2A2.5 2.5 0 019 4.5v2A2.5 2.5 0 016.5 9h-2A2.5 2.5 0 012 6.5v-2zM2 13.5A2.5 2.5 0 014.5 11h2A2.5 2.5 0 019 13.5v2A2.5 2.5 0 016.5 18h-2A2.5 2.5 0 012 15.5v-2zM11 4.5A2.5 2.5 0 0113.5 2h2A2.5 2.5 0 0118 4.5v2A2.5 2.5 0 0115.5 9h-2A2.5 2.5 0 0111 6.5v-2zM11 13.5A2.5 2.5 0 0113.5 11h2A2.5 2.5 0 0118 13.5v2A2.5 2.5 0 0115.5 18h-2A2.5 2.5 0 0111 15.5v-2z" />
    </svg>
  ),
  clusters: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M2 5a2 2 0 012-2h12a2 2 0 012 2v2a2 2 0 01-2 2H4a2 2 0 01-2-2V5zm14 1a1 1 0 11-2 0 1 1 0 012 0zM2 13a2 2 0 012-2h12a2 2 0 012 2v2a2 2 0 01-2 2H4a2 2 0 01-2-2v-2zm14 1a1 1 0 11-2 0 1 1 0 012 0z" clipRule="evenodd" />
    </svg>
  ),
  clusterOverview: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path d="M2 11a1 1 0 011-1h2a1 1 0 011 1v5a1 1 0 01-1 1H3a1 1 0 01-1-1v-5zM8 7a1 1 0 011-1h2a1 1 0 011 1v9a1 1 0 01-1 1H9a1 1 0 01-1-1V7zM14 4a1 1 0 011-1h2a1 1 0 011 1v12a1 1 0 01-1 1h-2a1 1 0 01-1-1V4z" />
    </svg>
  ),
  clusterManagement: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M11.49 3.17c-.38-1.56-2.6-1.56-2.98 0a1.532 1.532 0 01-2.286.948c-1.372-.836-2.942.734-2.106 2.106.54.886.061 2.042-.947 2.287-1.561.379-1.561 2.6 0 2.978a1.532 1.532 0 01.947 2.287c-.836 1.372.734 2.942 2.106 2.106a1.532 1.532 0 012.287.947c.379 1.561 2.6 1.561 2.978 0a1.533 1.533 0 012.287-.947c1.372.836 2.942-.734 2.106-2.106a1.533 1.533 0 01.947-2.287c1.561-.379 1.561-2.6 0-2.978a1.532 1.532 0 01-.947-2.287c.836-1.372-.734-2.942-2.106-2.106a1.532 1.532 0 01-2.287-.947zM10 13a3 3 0 100-6 3 3 0 000 6z" clipRule="evenodd" />
    </svg>
  ),
  namespaces: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" />
    </svg>
  ),
  inventory: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M5 3a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2V5a2 2 0 00-2-2H5zm0 2h10v7h-2l-1 2H8l-1-2H5V5z" clipRule="evenodd" />
    </svg>
  ),
  resources: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path d="M7 3a1 1 0 000 2h6a1 1 0 100-2H7zM4 7a1 1 0 011-1h10a1 1 0 110 2H5a1 1 0 01-1-1zM2 11a2 2 0 012-2h12a2 2 0 012 2v4a2 2 0 01-2 2H4a2 2 0 01-2-2v-4z" />
    </svg>
  ),
  logs: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4zm2 6a1 1 0 011-1h6a1 1 0 110 2H7a1 1 0 01-1-1zm1 3a1 1 0 100 2h6a1 1 0 100-2H7z" clipRule="evenodd" />
    </svg>
  ),
  alerts: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7 4a1 1 0 11-2 0 1 1 0 012 0zm-1-9a1 1 0 00-1 1v4a1 1 0 102 0V6a1 1 0 00-1-1z" clipRule="evenodd" />
    </svg>
  ),
  alertPolicies: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M2.166 4.999A11.954 11.954 0 0010 1.944 11.954 11.954 0 0017.834 5c.11.65.166 1.32.166 2.001 0 5.225-3.34 9.67-8 11.317C5.34 16.67 2 12.225 2 7c0-.682.057-1.35.166-2.001zm11.541 3.708a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" />
    </svg>
  ),
  alertRouting: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M12.586 4.586a2 2 0 112.828 2.828l-3 3a2 2 0 01-2.828 0 1 1 0 00-1.414 1.414 4 4 0 005.656 0l3-3a4 4 0 00-5.656-5.656l-1.5 1.5a1 1 0 101.414 1.414l1.5-1.5zm-5 5a2 2 0 012.828 0 1 1 0 101.414-1.414 4 4 0 00-5.656 0l-3 3a4 4 0 105.656 5.656l1.5-1.5a1 1 0 10-1.414-1.414l-1.5 1.5a2 2 0 11-2.828-2.828l3-3z" clipRule="evenodd" />
    </svg>
  ),
  userManagement: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path d="M9 6a3 3 0 11-6 0 3 3 0 016 0zM17 6a3 3 0 11-6 0 3 3 0 016 0zM12.93 17c.046-.327.07-.66.07-1a6.97 6.97 0 00-1.5-4.33A5 5 0 0119 16v1h-6.07zM6 11a5 5 0 015 5v1H1v-1a5 5 0 015-5z" />
    </svg>
  ),
  auditLogs: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-12a1 1 0 10-2 0v4a1 1 0 00.293.707l2.828 2.829a1 1 0 101.415-1.415L11 9.586V6z" clipRule="evenodd" />
    </svg>
  ),
  settings: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M11.49 3.17c-.38-1.56-2.6-1.56-2.98 0a1.532 1.532 0 01-2.286.948c-1.372-.836-2.942.734-2.106 2.106.54.886.061 2.042-.947 2.287-1.561.379-1.561 2.6 0 2.978a1.532 1.532 0 01.947 2.287c-.836 1.372.734 2.942 2.106 2.106a1.532 1.532 0 012.287.947c.379 1.561 2.6 1.561 2.978 0a1.533 1.533 0 012.287-.947c1.372.836 2.942-.734 2.106-2.106a1.533 1.533 0 01.947-2.287c1.561-.379 1.561-2.6 0-2.978a1.532 1.532 0 01-.947-2.287c.836-1.372-.734-2.942-2.106-2.106a1.532 1.532 0 01-2.287-.947zM10 13a3 3 0 100-6 3 3 0 000 6z" clipRule="evenodd" />
    </svg>
  ),
  upgradeSafeMode: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-8.707l-3-3a1 1 0 00-1.414 0l-3 3a1 1 0 001.414 1.414L9 9.414V13a1 1 0 102 0V9.414l1.293 1.293a1 1 0 001.414-1.414z" clipRule="evenodd" />
    </svg>
  ),
  serviceCatalog: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path d="M3 3a2 2 0 012-2h4a1 1 0 011 1v16a1 1 0 01-1.447.894L5 17.118l-1.553.776A1 1 0 012 17V3zm10-2a2 2 0 00-2 2v14a1 1 0 001.447.894L15 16.882l1.553.776A1 1 0 0018 16.764V3a2 2 0 00-2-2h-3z" />
    </svg>
  ),
  applicationServices: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M2 5a2 2 0 012-2h12a2 2 0 012 2v10a2 2 0 01-2 2H4a2 2 0 01-2-2V5zm3.293 1.293a1 1 0 011.414 0l3 3a1 1 0 010 1.414l-3 3a1 1 0 01-1.414-1.414L7.586 10 5.293 7.707a1 1 0 010-1.414zM11 12a1 1 0 100 2h3a1 1 0 100-2h-3z" clipRule="evenodd" />
    </svg>
  ),
  clients: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-6-3a2 2 0 11-4 0 2 2 0 014 0zm-2 4a5 5 0 00-4.546 2.916A5.986 5.986 0 0010 16a5.986 5.986 0 004.546-2.084A5 5 0 0010 11z" clipRule="evenodd" />
    </svg>
  ),
};

const K8S_LOGO = (
  <svg viewBox="0 0 32 32" fill="none" aria-hidden="true" className="sidebar-brand-logo">
    <circle cx="16" cy="16" r="16" fill="url(#k8s-grad)" />
    <path d="M16 5.5c-.4 0-.78.16-1.06.44L9.06 12H7a1.5 1.5 0 00-1.5 1.5v2A1.5 1.5 0 007 17h.15l-.12.47A6.5 6.5 0 0016 24.5a6.5 6.5 0 008.97-7.03L24.85 17H25a1.5 1.5 0 001.5-1.5v-2A1.5 1.5 0 0025 12h-2.06l-5.88-6.06A1.5 1.5 0 0016 5.5z" fill="rgba(255,255,255,0.15)" />
    <path d="M16 8l-1.2 4.8H11l3.6 2.8-1.2 4.4 2.6-2 2.6 2-1.2-4.4L21 12.8h-3.8L16 8z" fill="white" />
    <defs>
      <linearGradient id="k8s-grad" x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
        <stop stopColor="#3b82f6" />
        <stop offset="1" stopColor="#6366f1" />
      </linearGradient>
    </defs>
  </svg>
);

export default function Sidebar({ pages, activePage, onNavigate, open = false }) {
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
    <aside className={`sidebar${open ? " sidebar--open" : ""}`} aria-label="Primary navigation">
      <div className="sidebar-brand">
        <div className="sidebar-brand-inner">
          {K8S_LOGO}
          <div>
            <h1>KubeSight</h1>
            <p className="brand-subtitle">Control Plane</p>
          </div>
        </div>
      </div>
      <nav aria-label="Main navigation">
        {sections.map((section) => (
          <div key={section.label || "main"} className="sidebar-section">
            {section.label ? (
              <p className="sidebar-section-label" aria-hidden="true">{section.label}</p>
            ) : null}
            {section.pages.map((page) => (
              <button
                key={page.key}
                type="button"
                className={`nav-link ${activePage === page.key ? "active" : ""}`}
                onClick={() => onNavigate(page.key)}
                aria-current={activePage === page.key ? "page" : undefined}
              >
                <span className="nav-link-icon">
                  {NAV_ICONS[page.key] || (
                    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                      <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm0-2a6 6 0 100-12 6 6 0 000 12z" clipRule="evenodd" />
                    </svg>
                  )}
                </span>
                <span className="nav-link-label">{page.label}</span>
              </button>
            ))}
          </div>
        ))}
      </nav>
      <div className="sidebar-footer">
        <span className="sidebar-footer-version">v1.0.0</span>
      </div>
    </aside>
  );
}
