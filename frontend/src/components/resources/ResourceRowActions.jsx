import { useEffect, useRef, useState } from "react";
import { useMediaQuery } from "../../hooks/useMediaQuery.js";
import {
  IconCode,
  IconCube,
  IconDocument,
  IconEdit,
  IconEye,
  IconHistory,
  IconInfo,
  IconRefresh,
  IconTerminal,
  InventoryIconButton,
} from "../inventory/InventoryActionIcons.jsx";

const ACTION_CONFIG = {
  logs: { label: "View logs", Icon: IconDocument },
  "view-logs": { label: "View logs", Icon: IconDocument },
  describe: { label: "Describe resource", Icon: IconInfo },
  pods: { label: "View pods", Icon: IconCube },
  rollout: { label: "Rollout history", Icon: IconHistory },
  restart: { label: "Restart", Icon: IconRefresh },
  exec: { label: "Exec into pod", Icon: IconTerminal },
  yaml: { label: "View YAML", Icon: IconCode },
  details: { label: "Details", Icon: IconEye },
  edit: { label: "Edit & apply", Icon: IconEdit },
};

function normalizeActionId(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "-");
}

function parseActions(raw) {
  if (Array.isArray(raw)) {
    return raw.map(normalizeActionId).filter(Boolean);
  }
  if (typeof raw === "string") {
    const text = raw.trim();
    if (!text || text === "-") {
      return [];
    }
    if (text.includes("|")) {
      return text.split("|").map((part) => normalizeActionId(part)).filter(Boolean);
    }
    return [normalizeActionId(text)];
  }
  return [];
}

function stopRowClick(event) {
  event.stopPropagation();
}

// Only the keys that would activate a clickable row. Stopping every keydown here
// also swallowed Escape, so a dialog opened from these buttons (focus still on
// the button) could not be closed with the keyboard.
function stopRowKey(event) {
  if (event.key === "Enter" || event.key === " ") {
    event.stopPropagation();
  }
}

function resolveActionConfig(id) {
  return ACTION_CONFIG[id];
}

export default function ResourceRowActions({ actions, fallback = null, onAction }) {
  const isCompact = useMediaQuery("(max-width: 768px)");
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef(null);

  // Callers append write actions (restart, edit) to what the API sent, so the
  // same id can arrive twice; ids nothing handles (e.g. "scale") would render
  // as buttons that do nothing, so only known actions are shown.
  let ids = [...new Set(parseActions(actions))].filter((id) => ACTION_CONFIG[id]);

  if (!ids.length && fallback) {
    ids = [normalizeActionId(fallback)].filter((id) => ACTION_CONFIG[id]);
  }

  useEffect(() => {
    if (!menuOpen) {
      return undefined;
    }
    const handlePointerDown = (event) => {
      if (menuRef.current && !menuRef.current.contains(event.target)) {
        setMenuOpen(false);
      }
    };
    // Capture phase, and the key is claimed, so the app-wide dialog stack
    // leaves whatever is open underneath alone.
    const handleKeyDown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setMenuOpen(false);
        menuRef.current?.querySelector(".resource-actions-toggle")?.focus();
      }
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown, true);
    };
  }, [menuOpen]);

  if (!ids.length) {
    return <span className="muted">—</span>;
  }

  const handleAction = (id, event) => {
    stopRowClick(event);
    setMenuOpen(false);
    onAction?.(id, event);
  };

  if (isCompact) {
    return (
      <div
        className="resource-actions-compact"
        ref={menuRef}
        onClick={stopRowClick}
        onKeyDown={stopRowKey}
        role="presentation"
      >
        <button
          type="button"
          className="btn-sm btn-outline resource-actions-toggle"
          aria-expanded={menuOpen}
          aria-haspopup="menu"
          onClick={(event) => {
            stopRowClick(event);
            setMenuOpen((open) => !open);
          }}
        >
          Actions
        </button>
        {menuOpen ? (
          <div className="resource-actions-menu" role="menu">
            {ids.map((id) => {
              const { label } = resolveActionConfig(id);
              return (
                <button
                  key={id}
                  type="button"
                  role="menuitem"
                  className="resource-actions-menu-item"
                  onClick={(event) => handleAction(id, event)}
                >
                  {label}
                </button>
              );
            })}
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <div
      className="inventory-actions-cell inventory-actions-cell--icons resource-actions-cell"
      onClick={stopRowClick}
      onKeyDown={stopRowKey}
      role="presentation"
    >
      {ids.map((id) => {
        const { label, Icon } = resolveActionConfig(id);
        return (
          <InventoryIconButton
            key={id}
            label={label}
            onClick={(event) => handleAction(id, event)}
          >
            <Icon />
          </InventoryIconButton>
        );
      })}
    </div>
  );
}
