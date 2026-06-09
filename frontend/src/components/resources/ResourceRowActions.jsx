import {
  IconCode,
  IconCube,
  IconDocument,
  IconEye,
  IconHistory,
  IconInfo,
  InventoryIconButton,
} from "../inventory/InventoryActionIcons.jsx";

const ACTION_CONFIG = {
  logs: { label: "View logs", Icon: IconDocument },
  "view-logs": { label: "View logs", Icon: IconDocument },
  describe: { label: "Describe resource", Icon: IconInfo },
  pods: { label: "View pods", Icon: IconCube },
  rollout: { label: "Rollout history", Icon: IconHistory },
  yaml: { label: "View YAML", Icon: IconCode },
  details: { label: "Details", Icon: IconEye },
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

export default function ResourceRowActions({ actions, fallback = null, onAction }) {
  let ids = parseActions(actions);

  if (!ids.length && fallback) {
    ids = [normalizeActionId(fallback)];
  }

  if (!ids.length) {
    return <span className="muted">—</span>;
  }

  return (
    <div
      className="inventory-actions-cell inventory-actions-cell--icons resource-actions-cell"
      onClick={stopRowClick}
      onKeyDown={stopRowClick}
      role="presentation"
    >
      {ids.map((id) => {
        const config = ACTION_CONFIG[id] || {
          label: id.replace(/-/g, " "),
          Icon: IconDocument,
        };
        const { label, Icon } = config;
        return (
          <InventoryIconButton
            key={id}
            label={label}
            onClick={(event) => {
              stopRowClick(event);
              onAction?.(id, event);
            }}
          >
            <Icon />
          </InventoryIconButton>
        );
      })}
    </div>
  );
}
