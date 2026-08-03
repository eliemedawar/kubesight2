import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import BrandMark from "../BrandMark.jsx";
import { buildNavTree, groupIdForPageKey } from "../../routes/navigation.js";

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
 * **One group open at a time, and by default it is the active one.** An earlier
 * version let groups accumulate, on the reasoning that closing something the
 * user opened is its own kind of interference. That was wrong for a specific
 * reason: with three groups open, "open" stops meaning "this is where you are"
 * and degrades to "this is where you have been". Expansion is the only signal
 * the sidebar has for current section, so it has to be spent on exactly one.
 *
 * Opening another group therefore closes the current one, and the group you are
 * actually in is never left collapsed.
 *
 * **Entries are links, not buttons.** They have real hrefs, so middle-click,
 * ctrl-click, "open in new tab" and "copy link address" all work. On an app
 * whose whole point is now addressable URLs, a nav that could only be operated
 * by left-click was the wrong shape.
 *
 * **No per-item icons.** There was a partial set: 21 glyphs for ~28
 * destinations, mixing filled and stroked styles, with the rest falling back to
 * a hollow circle — which reads as an unselected radio button, "choose one"
 * rather than "go here". (The upgrade page's glyph was keyed `upgradeSafeMode`
 * against a `upgrade` page key, so it had silently never rendered at all.) A
 * complete, consistent family would earn its place; a partial one costs more
 * than it gives, and the labels here are short and unambiguous.
 */
export default function Sidebar({ pages, activePage, onNavigated, open = false }) {
  const groups = useMemo(() => buildNavTree(pages), [pages]);
  const activeGroupId = groupIdForPageKey(activePage);

  const [openGroup, setOpenGroup] = useState(activeGroupId);

  // Navigating moves the open group to wherever you landed.
  useEffect(() => {
    if (activeGroupId) {
      setOpenGroup(activeGroupId);
    }
  }, [activeGroupId]);

  const toggleGroup = (groupId) => {
    setOpenGroup((current) => {
      if (current !== groupId) {
        return groupId;
      }
      // Collapsing the group you are browsing would leave nothing open and no
      // indication of where you are, so it falls back to the active group
      // rather than to nothing.
      return groupId === activeGroupId ? groupId : activeGroupId;
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
          const isOpen = group.id === openGroup;
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
                  {/*
                    Plain Link, not NavLink. NavLink brings its own matching --
                    by path prefix, and it appends its own `active` class even
                    when className is a string -- so `/applications` counted
                    itself active on `/applications/clients` and Inventory
                    stayed lit while you were on Clients.

                    `activePage` is already resolved through the route table's
                    parent chain, which is the one place that knows a drill-down
                    belongs to its parent and a sibling does not. Two sources of
                    truth for "current" is what caused the bug; suppressing the
                    second would have left it there to be re-enabled.
                  */}
                  {group.items.map((item) => (
                    <Link
                      key={item.pageKey}
                      to={item.href}
                      className={`nav-link${activePage === item.pageKey ? " active" : ""}`}
                      // The link itself navigates; this only reports that it
                      // happened, so the mobile drawer can close behind it.
                      onClick={() => onNavigated?.(item.pageKey)}
                    >
                      <span className="nav-link-label">{item.label}</span>
                    </Link>
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
