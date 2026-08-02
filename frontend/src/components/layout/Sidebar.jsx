import { useEffect, useMemo, useState } from "react";
import { NavLink } from "react-router-dom";
import BrandMark from "../BrandMark.jsx";
import { buildNavTree, groupIdForPageKey } from "../../routes/navigation.js";
import { FALLBACK_ICON, NAV_ICONS } from "./navIcons.jsx";

/**
 * Primary navigation.
 *
 * Two things changed here, and the first is the reason for the rework.
 *
 * **Click controls expansion, not hover.** The previous version opened and
 * closed groups on pointer enter and leave, mediated by three timers — a 160ms
 * open delay, a 140ms close delay, and a 420ms "switch lock" that suppressed
 * opening a second group too soon after the first. Those numbers are what a
 * hover menu needs to stop flickering when the pointer crosses a group on its
 * way somewhere else, and they are also why the menu felt like it had opinions:
 * it opened things you were only passing over, closed things you were reading,
 * and did nothing at all on a trackpad where the pointer stops moving. Groups
 * now open only when you ask them to, and stay open until you say otherwise.
 *
 * The group containing the current page is always expanded — you can never end
 * up looking at a page whose place in the menu is hidden — and navigating into
 * a group expands it. Everything else is the user's to open and close, and the
 * choice survives navigation.
 *
 * **Entries are links, not buttons.** They have real hrefs, so middle-click,
 * ctrl-click, "open in new tab" and "copy link address" all work. On an app
 * whose whole point is now addressable URLs, a nav that could only be operated
 * by left-click was the wrong shape.
 */
export default function Sidebar({ pages, activePage, onNavigated, open = false }) {
  const groups = useMemo(() => buildNavTree(pages), [pages]);
  const activeGroupId = groupIdForPageKey(activePage);

  // Expanded groups. The active one is forced open by the effect below, so this
  // only records what the user has chosen to open beyond that.
  const [expanded, setExpanded] = useState(() =>
    activeGroupId ? new Set([activeGroupId]) : new Set()
  );

  // Navigating into a group opens it. Deliberately additive: it never closes a
  // group the user opened, because a menu that tidies itself up while you are
  // using it is the same complaint as the hover behaviour in a different form.
  useEffect(() => {
    if (!activeGroupId) {
      return;
    }
    setExpanded((current) => {
      if (current.has(activeGroupId)) {
        return current;
      }
      const next = new Set(current);
      next.add(activeGroupId);
      return next;
    });
  }, [activeGroupId]);

  const toggleGroup = (groupId) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(groupId)) {
        next.delete(groupId);
      } else {
        next.add(groupId);
      }
      return next;
    });
  };

  return (
    <aside
      className={`sidebar${open ? " sidebar--open" : ""}`}
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

      <nav aria-label="Main navigation" data-tour="sidebar-nav">
        {groups.map((group) => {
          const isActive = group.id === activeGroupId;
          // The active group is always expanded, whatever the user last chose.
          const isOpen = isActive || expanded.has(group.id);
          const panelId = `sidebar-${group.id}-pages`;

          return (
            <div
              key={group.id}
              className={`sidebar-section${isOpen ? " is-open" : ""}${
                isActive ? " is-active" : ""
              }`}
            >
              <button
                type="button"
                className="sidebar-section-trigger"
                aria-expanded={isOpen}
                aria-controls={panelId}
                onClick={() => toggleGroup(group.id)}
              >
                <span className="sidebar-section-title">{group.label}</span>
                <svg
                  className="sidebar-section-chevron"
                  viewBox="0 0 20 20"
                  fill="currentColor"
                  aria-hidden="true"
                >
                  <path
                    fillRule="evenodd"
                    d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z"
                    clipRule="evenodd"
                  />
                </svg>
              </button>

              {/*
                Collapsed panels use `inert` rather than `hidden`: the height
                transition needs the element to stay in the box tree, and inert
                is what keeps its links out of the tab order while it is closed.
              */}
              <div
                id={panelId}
                className="sidebar-group-pages"
                role="group"
                aria-label={`${group.label} pages`}
                aria-hidden={!isOpen}
                inert={!isOpen}
              >
                <div className="sidebar-group-links">
                  {group.items.map((item) => (
                    <NavLink
                      key={item.pageKey}
                      to={item.href}
                      end={item.href === "/"}
                      className={({ isActive: linkActive }) =>
                        `nav-link${linkActive || activePage === item.pageKey ? " active" : ""}`
                      }
                      // The link itself navigates; this only reports that it
                      // happened, so the mobile drawer can close behind it.
                      onClick={() => onNavigated?.(item.pageKey)}
                    >
                      <span className="nav-link-icon">
                        {NAV_ICONS[item.pageKey] || FALLBACK_ICON}
                      </span>
                      <span className="nav-link-label">{item.label}</span>
                    </NavLink>
                  ))}
                </div>
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
