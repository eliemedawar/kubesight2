import { useCallback, useEffect, useMemo, useState } from "react";
import BrandMark from "../BrandMark.jsx";
import { pageHref } from "../../routes/RouterContext.jsx";
import { buildNavGroups, sidebarKeyFor } from "../../lib/navigation.js";
import { navIcon } from "./navIcons.jsx";

// Which groups a user folded away. Per browser, like the theme: a convenience,
// never state anything else depends on.
const COLLAPSED_KEY = "kubesight.nav.collapsed.v1";

function readCollapsed() {
  try {
    const raw = window.localStorage.getItem(COLLAPSED_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(parsed) ? parsed : []);
  } catch {
    return new Set();
  }
}

function writeCollapsed(set) {
  try {
    window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...set]));
  } catch {
    // Storage blocked: collapsing still works for this page view.
  }
}

/** Plain left clicks navigate in-app; modified clicks keep browser behaviour. */
function isPlainClick(event) {
  return !(
    event.defaultPrevented ||
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey
  );
}

const Chevron = () => (
  <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
    <path fillRule="evenodd" d="M5.23 7.21a.75.75 0 011.06.02L10 11.17l3.71-3.94a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z" clipRule="evenodd" />
  </svg>
);

const SearchGlyph = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
    <circle cx="8.5" cy="8.5" r="5.5" />
    <path d="m13 13 4 4" />
  </svg>
);

const isMac =
  typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || "");

/**
 * Primary navigation: every destination is visible at once, grouped by what
 * you are doing (build, deliver, run, observe, administer). Groups fold, and
 * the group holding the current page always stays open. A workspace entry
 * (Build Center, Architecture) lists its tabs underneath while you are in it.
 */
export default function Sidebar({
  pages,
  activePage,
  onNavigate,
  onOpenJump,
  open = false,
}) {
  const groups = useMemo(() => buildNavGroups(pages), [pages]);
  const activeItemKey = sidebarKeyFor(activePage);
  const [collapsed, setCollapsed] = useState(readCollapsed);

  const activeGroupId = groups.find((group) =>
    group.items.some((item) => item.key === activeItemKey)
  )?.id;

  // Landing on a page inside a folded group unfolds it, so "where am I" is
  // never hidden. Folding it again afterwards is the user's call.
  useEffect(() => {
    if (!activeGroupId) return;
    setCollapsed((current) => {
      if (!current.has(activeGroupId)) return current;
      const next = new Set(current);
      next.delete(activeGroupId);
      writeCollapsed(next);
      return next;
    });
  }, [activeGroupId]);

  const toggleGroup = useCallback((groupId) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(groupId)) next.delete(groupId);
      else next.add(groupId);
      writeCollapsed(next);
      return next;
    });
  }, []);

  const renderLink = ({ key, label, pageKey, iconKey, isActive, className = "" }) => (
    <a
      key={key}
      href={pageHref(pageKey)}
      className={`nav-link${isActive ? " active" : ""}${className ? ` ${className}` : ""}`}
      aria-current={isActive ? "page" : undefined}
      onClick={(event) => {
        if (!isPlainClick(event)) return;
        event.preventDefault();
        onNavigate(pageKey);
      }}
    >
      {iconKey ? <span className="nav-link-icon">{navIcon(iconKey)}</span> : null}
      <span className="nav-link-label">{label}</span>
    </a>
  );

  return (
    <aside
      className={`sidebar sidebar--v2${open ? " sidebar--open" : ""}`}
      aria-label="Primary navigation"
    >
      <div className="sidebar-brand">
        <div className="sidebar-brand-inner">
          <BrandMark className="sidebar-brand-logo" />
          <div>
            <h1>KubeSight</h1>
            <p className="brand-subtitle">Control Plane</p>
          </div>
        </div>
      </div>

      {onOpenJump ? (
        <button type="button" className="nav-jump" onClick={onOpenJump}>
          <span className="nav-jump-icon">
            <SearchGlyph />
          </span>
          <span className="nav-jump-label">Jump to…</span>
          <kbd className="nav-jump-kbd">{isMac ? "⌘K" : "Ctrl K"}</kbd>
        </button>
      ) : null}

      <nav className="nav-groups" aria-label="Main navigation" data-tour="sidebar-nav">
        {groups.map((group) => {
          const isCollapsed = Boolean(group.label) && collapsed.has(group.id);
          const listId = `nav-group-${group.id}`;
          return (
            <div
              key={group.id}
              className={`nav-group${isCollapsed ? " is-collapsed" : ""}${
                group.id === activeGroupId ? " is-active" : ""
              }`}
            >
              {group.label ? (
                <button
                  type="button"
                  className="nav-group-head"
                  aria-expanded={!isCollapsed}
                  aria-controls={listId}
                  onClick={() => toggleGroup(group.id)}
                >
                  <span className="nav-group-label">{group.label}</span>
                  <span className="nav-group-chevron">
                    <Chevron />
                  </span>
                </button>
              ) : null}
              <div id={listId} className="nav-group-items" hidden={isCollapsed}>
                {group.items.map((item) => {
                  const isActive = item.key === activeItemKey;
                  if (!item.tabs) {
                    return renderLink({
                      key: item.key,
                      label: item.label,
                      pageKey: item.pageKey,
                      iconKey: item.key,
                      isActive,
                    });
                  }
                  // A workspace: the entry itself opens the first tab; while it
                  // is the current area its tabs show as a nested list.
                  const activeTab = item.tabs.find((tab) => tab.pageKey === activePage)?.pageKey;
                  return (
                    <div key={item.key} className={`nav-ws${isActive ? " is-active" : ""}`}>
                      {renderLink({
                        key: item.key,
                        label: item.label,
                        pageKey: activeTab || item.pageKey,
                        iconKey: item.key,
                        isActive,
                        className: "nav-ws-link",
                      })}
                      {isActive && item.tabs.length > 1 ? (
                        <div className="nav-ws-tabs" role="group" aria-label={`${item.label} sections`}>
                          {item.tabs.map((tab) =>
                            renderLink({
                              key: tab.pageKey,
                              label: tab.label,
                              pageKey: tab.pageKey,
                              isActive: tab.pageKey === activeTab,
                              className: "nav-ws-tab",
                            })
                          )}
                        </div>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}
      </nav>
      <div className="sidebar-footer">
        <span className="sidebar-footer-version">v1.0.0</span>
      </div>
    </aside>
  );
}
