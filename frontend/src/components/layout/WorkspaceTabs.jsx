import { pageHref } from "../../routes/RouterContext.jsx";
import { WORKSPACES } from "../../lib/navigation.js";
import { navIcon } from "./navIcons.jsx";

/**
 * The tab strip on top of a workspace (Build Center, Service Architecture).
 * Each tab is a whole page with its own address, so tabs are links: Back steps
 * between them and middle-click opens one in a new tab. Only the tabs this
 * user may open are passed in.
 */
export default function WorkspaceTabs({ workspaceKey, tabs, activePageKey, onNavigate }) {
  const workspace = WORKSPACES[workspaceKey];
  if (!workspace || !tabs?.length) {
    return null;
  }
  return (
    <div className="ws-bar">
      <div className="ws-bar-head">
        <span className="ws-bar-icon">{navIcon(workspaceKey)}</span>
        <div className="ws-bar-text">
          <span className="ws-bar-title">{workspace.label}</span>
          <span className="ws-bar-desc">{workspace.description}</span>
        </div>
      </div>
      {tabs.length > 1 ? (
        <nav className="ws-tabs" aria-label={`${workspace.label} sections`}>
          {tabs.map((tab) => {
            const isActive = tab.pageKey === activePageKey;
            return (
              <a
                key={tab.pageKey}
                href={pageHref(tab.pageKey)}
                className={`ws-tab${isActive ? " is-active" : ""}`}
                aria-current={isActive ? "page" : undefined}
                onClick={(event) => {
                  if (
                    event.defaultPrevented ||
                    event.button !== 0 ||
                    event.metaKey ||
                    event.ctrlKey ||
                    event.shiftKey ||
                    event.altKey
                  ) {
                    return;
                  }
                  event.preventDefault();
                  onNavigate(tab.pageKey);
                }}
              >
                <span className="ws-tab-icon">{navIcon(tab.pageKey)}</span>
                {tab.label}
              </a>
            );
          })}
        </nav>
      ) : null}
    </div>
  );
}
