function inferVariant(message) {
  const text = (message || "").toLowerCase();
  if (text.includes("cluster")) return "clusters";
  if (text.includes("policy") || text.includes("policies")) return "policies";
  if (text.includes("alert") || text.includes("notification") || text.includes("receiver")) return "alerts";
  if (text.includes("application") || text.includes("deploy") || text.includes("workload")) return "applications";
  if (text.includes("namespace")) return "namespaces";
  if (text.includes("log") || text.includes("pod")) return "logs";
  if (text.includes("resource")) return "resources";
  return "default";
}

function EmptyStateIcon({ variant }) {
  const common = {
    width: 40,
    height: 40,
    viewBox: "0 0 40 40",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.75,
    strokeLinecap: "round",
    strokeLinejoin: "round",
    "aria-hidden": true,
  };

  switch (variant) {
    case "clusters":
      return (
        <svg {...common}>
          <circle cx="20" cy="20" r="6" />
          <circle cx="8" cy="12" r="3" />
          <circle cx="32" cy="12" r="3" />
          <circle cx="8" cy="28" r="3" />
          <circle cx="32" cy="28" r="3" />
          <path d="M14 14l4 4M26 14l-4 4M14 26l4-4M26 26l-4-4" />
        </svg>
      );
    case "policies":
      return (
        <svg {...common}>
          <path d="M10 8h20v24H10z" />
          <path d="M14 14h12M14 20h12M14 26h8" />
        </svg>
      );
    case "alerts":
      return (
        <svg {...common}>
          <path d="M20 8v4M10 28h20l-2.5-8.5A8 8 0 0 0 12.5 19.5V16a7.5 7.5 0 1 1 15 0v3.5a8 8 0 0 0-5 4.5L10 28z" />
        </svg>
      );
    case "applications":
      return (
        <svg {...common}>
          <rect x="8" y="10" width="24" height="20" rx="2" />
          <path d="M8 16h24M14 22h12" />
        </svg>
      );
    case "namespaces":
      return (
        <svg {...common}>
          <path d="M8 12h24v20H8z" />
          <path d="M8 16h24M14 22h12M14 26h8" />
        </svg>
      );
    case "logs":
      return (
        <svg {...common}>
          <path d="M10 8h20v24H10z" />
          <path d="M14 14h12M14 18h12M14 22h12M14 26h8" />
        </svg>
      );
    case "resources":
      return (
        <svg {...common}>
          <path d="M12 10h16v6H12zM8 24h24v6H8z" />
          <path d="M20 16v8" />
        </svg>
      );
    default:
      return (
        <svg {...common}>
          <rect x="10" y="12" width="20" height="16" rx="2" />
          <path d="M16 12V10a4 4 0 0 1 8 0v2" />
        </svg>
      );
  }
}

export default function EmptyState({ message, hint, variant }) {
  const resolvedVariant = variant || inferVariant(message);

  return (
    <section className={`card empty-state-card empty-state-card--${resolvedVariant}`}>
      <div className="empty-state-icon">
        <EmptyStateIcon variant={resolvedVariant} />
      </div>
      <h3 className="empty-state-title">{message}</h3>
      {hint ? <p className="empty-state-hint muted">{hint}</p> : null}
    </section>
  );
}
