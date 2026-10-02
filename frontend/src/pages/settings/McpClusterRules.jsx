import { useState } from "react";

/**
 * Settings → MCP tools → Clusters: what agents may do on each cluster.
 *
 * A mode per cluster rather than a grid of switches, because the decisions
 * people come here to make are whole-cluster ones — "Hermes may look at prod
 * but not touch it", "keep agents out of the DR cluster". Custom is there for
 * the rest, and only lists the tools that actually work on a cluster.
 *
 * Every mode only narrows the Tools tab: a tool switched off there is off on
 * every cluster, and is shown here as such rather than as a switch that would
 * look like it could bring it back.
 */

export const CLUSTER_MODES = [
  { value: "full", label: "Full", hint: "Follows the Tools tab." },
  {
    value: "read",
    label: "Read only",
    hint: "Agents can look, but nothing that changes something runs here.",
  },
  {
    value: "off",
    label: "Off",
    hint: "Agents cannot see this cluster or name it.",
  },
  {
    value: "custom",
    label: "Custom",
    hint: "Pick the tools agents may use on this cluster.",
  },
];

const DOMAIN_LABEL = {
  ci: "CI",
  clusters: "Clusters",
  workloads: "Workloads",
  deploys: "Deploys",
  observability: "Observability",
  apps: "Applications",
  platform: "Platform",
};

const shortName = (name) => name.replace(/^kubesight_/, "");

/** The rules as the API stores them: only clusters that are not "full". */
export function rulesPayload(rules) {
  const out = {};
  Object.entries(rules).forEach(([id, rule]) => {
    if (!rule || rule.mode === "full") return;
    out[id] = {
      mode: rule.mode,
      disabledTools:
        rule.mode === "custom" ? [...rule.disabledTools].sort() : [],
    };
  });
  return out;
}

/** A stable string for comparing two rule sets (draft vs saved). */
export const rulesKey = (rules) =>
  JSON.stringify(
    Object.entries(rulesPayload(rules)).sort(([a], [b]) =>
      a < b ? -1 : a > b ? 1 : 0,
    ),
  );

export function rulesFromPayload(clusters = []) {
  const out = {};
  clusters.forEach((cluster) => {
    out[cluster.id] = {
      mode: cluster.mode || "full",
      disabledTools: cluster.disabledTools || [],
    };
  });
  return out;
}

function Segmented({ value, onChange, disabled, label }) {
  return (
    <div className="mcp-filter mcp-seg" role="radiogroup" aria-label={label}>
      {CLUSTER_MODES.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          className={`btn-ghost${value === option.value ? " is-active" : ""} mode-${option.value}`}
          disabled={disabled}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

function ClusterRow({
  cluster,
  rule,
  savedRule,
  scopedTools,
  globalOff,
  canManage,
  ticketAgentEnabled,
  onChange,
  Switch,
}) {
  const [open, setOpen] = useState(false);
  const mode = rule?.mode || "full";
  const disabled = new Set(rule?.disabledTools || []);
  const changed =
    mode !== (savedRule?.mode || "full") ||
    (mode === "custom" &&
      rulesKey({ x: rule }) !==
        rulesKey({ x: savedRule || { mode: "full", disabledTools: [] } }));

  const usable = scopedTools.filter((tool) => !globalOff.has(tool.name));
  const customOn = usable.filter((tool) => !disabled.has(tool.name)).length;
  const hint = CLUSTER_MODES.find((option) => option.value === mode)?.hint;

  const setMode = (next) => {
    onChange({
      mode: next,
      disabledTools: next === "custom" ? [...disabled] : [],
    });
    if (next === "custom") setOpen(true);
  };

  const setTool = (name, on) => {
    const next = new Set(disabled);
    if (on) next.delete(name);
    else next.add(name);
    onChange({ mode: "custom", disabledTools: [...next] });
  };

  const setAll = (names, on) => {
    const next = new Set(disabled);
    names.forEach((name) => (on ? next.delete(name) : next.add(name)));
    onChange({ mode: "custom", disabledTools: [...next] });
  };

  // Hermes deploys a ticket through kubesight_ticket_execute, onto a target's
  // cluster. Say so where it stops working, not in a footnote.
  const blocksTickets =
    ticketAgentEnabled &&
    (mode === "off" ||
      mode === "read" ||
      (mode === "custom" && disabled.has("kubesight_ticket_execute")));

  const groups = Object.keys(DOMAIN_LABEL)
    .map((domain) => ({
      domain,
      items: scopedTools.filter((tool) => tool.domain === domain),
    }))
    .filter((group) => group.items.length > 0);

  return (
    <li
      className={`mcp-cluster${changed ? " is-changed" : ""} is-mode-${mode}`}
    >
      <div className="mcp-cluster-head">
        <div className="mcp-cluster-id">
          <span className="mcp-cluster-name">{cluster.name}</span>
          <code className="mcp-perm">{cluster.id}</code>
          {cluster.missing && <span className="mcp-tag">Not connected</span>}
          {changed && <span className="mcp-tag is-pending">Unsaved</span>}
        </div>
        <Segmented
          value={mode}
          disabled={!canManage}
          label={`Agent access to ${cluster.name}`}
          onChange={setMode}
        />
      </div>
      <p className="mcp-cluster-hint">
        {mode === "custom" ? (
          <>
            <strong>{customOn}</strong> of {usable.length} cluster tools on
            here.{" "}
            <button
              type="button"
              className="btn-ghost mcp-link"
              onClick={() => setOpen((v) => !v)}
            >
              {open ? "Hide tools" : "Choose tools"}
            </button>
          </>
        ) : (
          hint
        )}
        {blocksTickets && (
          <span className="mcp-cluster-warn">
            {" "}
            Hermes cannot deploy tickets to this cluster while it is set like
            this.
          </span>
        )}
      </p>

      {mode === "custom" && open && (
        <div className="mcp-cluster-tools">
          {groups.map(({ domain, items }) => {
            const switchable = items.filter(
              (tool) => !globalOff.has(tool.name),
            );
            const allOn = switchable.every((tool) => !disabled.has(tool.name));
            return (
              <div className="mcp-cluster-group" key={domain}>
                <div className="mcp-cluster-group-head">
                  <span>{DOMAIN_LABEL[domain]}</span>
                  {canManage && switchable.length > 1 && (
                    <button
                      type="button"
                      className="btn-ghost mcp-link"
                      onClick={() =>
                        setAll(
                          switchable.map((tool) => tool.name),
                          !allOn,
                        )
                      }
                    >
                      {allOn ? "All off" : "All on"}
                    </button>
                  )}
                </div>
                <ul>
                  {items.map((tool) => {
                    const everywhereOff = globalOff.has(tool.name);
                    const on = !everywhereOff && !disabled.has(tool.name);
                    return (
                      <li key={tool.name} className={on ? "" : "is-off"}>
                        <code className="mcp-name">{shortName(tool.name)}</code>
                        {tool.write ? (
                          <span
                            className={`mcp-tag${tool.destructive ? " is-danger" : " is-write"}`}
                          >
                            {tool.destructive ? "Destructive" : "Writes"}
                          </span>
                        ) : null}
                        {everywhereOff ? (
                          <span className="mcp-tag mcp-cluster-everywhere">
                            Off everywhere
                          </span>
                        ) : (
                          <Switch
                            checked={on}
                            disabled={!canManage}
                            label={`${tool.name} on ${cluster.name}`}
                            onChange={(value) => setTool(tool.name, value)}
                          />
                        )}
                      </li>
                    );
                  })}
                </ul>
              </div>
            );
          })}
        </div>
      )}
    </li>
  );
}

export default function McpClusterRules({
  clusters,
  tools,
  globalOff,
  rules,
  savedRules,
  onChange,
  canManage,
  ticketAgentEnabled,
  Switch,
}) {
  const scopedTools = tools.filter((tool) => tool.clusterScoped);

  if (!clusters.length) {
    return (
      <section className="settings-card">
        <p className="muted">
          KubeSight has no clusters yet, so there is nothing to set per cluster.
        </p>
      </section>
    );
  }

  const counts = CLUSTER_MODES.map((option) => ({
    ...option,
    count: clusters.filter(
      (cluster) => (rules[cluster.id]?.mode || "full") === option.value,
    ).length,
  })).filter((option) => option.count > 0);

  return (
    <section className="settings-card mcp-clusters">
      <div className="mcp-clusters-head">
        <p className="settings-card-sub">
          Narrow what agents can do on one cluster. This only ever takes away: a
          tool that is off on the Tools tab stays off on every cluster.{" "}
          {scopedTools.length} tools work on a cluster; the others (CI, users,
          settings…) are not affected by these rules.
        </p>
        <div className="mcp-clusters-counts">
          {counts.map((option) => (
            <span
              key={option.value}
              className={`mcp-tag mode-tag-${option.value}`}
            >
              {option.count} {option.label}
            </span>
          ))}
        </div>
      </div>
      <ul className="mcp-cluster-list">
        {clusters.map((cluster) => (
          <ClusterRow
            key={cluster.id}
            cluster={cluster}
            rule={rules[cluster.id]}
            savedRule={savedRules[cluster.id]}
            scopedTools={scopedTools}
            globalOff={globalOff}
            canManage={canManage}
            ticketAgentEnabled={ticketAgentEnabled}
            onChange={(rule) => onChange({ ...rules, [cluster.id]: rule })}
            Switch={Switch}
          />
        ))}
      </ul>
    </section>
  );
}
