import { useEffect, useMemo, useState } from "react";
import { getMcpAccess, updateMcpAccess } from "../../api/mcpAccessApi.js";
import LoadingState from "../../components/common/LoadingState.jsx";
import McpClusterRules, {
  rulesFromPayload,
  rulesKey,
  rulesPayload,
} from "./McpClusterRules.jsx";

/**
 * Settings → MCP tools: switch each of KubeSight's agent tools on or off.
 *
 * Grouped by the same seven domains the server and the kubesight skill use, so
 * "take Helm away from Hermes" is one row in the deploys group. The page keeps
 * a draft and saves it whole: flipping twenty switches one request at a time
 * would leave an agent half-way between two policies while somebody was still
 * clicking.
 *
 * Two shortcuts sit above the list because they are the decisions people
 * actually come here to make: "everything", and "read only".
 *
 * The Clusters tab narrows the same switches per cluster (McpClusterRules).
 * Both tabs share one draft and one save, so a change that spans them — "turn
 * Helm off, and make prod read only" — lands as one policy, never two.
 */

const DOMAIN_INFO = {
  ci: {
    label: "CI",
    hint: "Services, pipelines, builds, build logs, runners, repository source",
  },
  clusters: {
    label: "Clusters",
    hint: "Clusters, nodes, namespaces, resources, events, topology",
  },
  workloads: {
    label: "Workloads",
    hint: "What is running, and restart / scale / rollback / exec",
  },
  deploys: {
    label: "Deploys",
    hint: "Apply, dry run, diff, deployment requests, change bundles, Helm",
  },
  observability: {
    label: "Observability",
    hint: "Pod logs, alerts, alert policies, audit trail, dashboard",
  },
  apps: {
    label: "Applications",
    hint: "Application intelligence, application services, clients",
  },
  platform: {
    label: "Platform",
    hint: "Registries, ticketing and the ticket agent, mobile releases, users, roles",
  },
};

const FILTERS = [
  { value: "all", label: "All" },
  { value: "read", label: "Reads" },
  { value: "write", label: "Writes" },
  { value: "off", label: "Off" },
];

// The row shows the lead of the description (the full text is the tooltip). A
// lead as short as "Start here." says nothing, so it borrows the next sentence.
const firstSentence = (text = "") => {
  const match = text.match(/^.*?[.!?](?=\s|$)/s);
  if (!match) return text.trim();
  if (match[0].length >= 40) return match[0].trim();
  const two = text.match(/^.*?[.!?](?=\s|$).*?[.!?](?=\s|$)/s);
  return (two ? two[0] : match[0]).trim();
};

const shortName = (name) => name.replace(/^kubesight_/, "");

function Switch({ checked, mixed = false, disabled, onChange, label }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={mixed ? "mixed" : checked}
      aria-label={label}
      className={`btn-ghost settings-switch mcp-switch${mixed ? " is-mixed" : ""}`}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    />
  );
}

function sameSet(a, b) {
  if (a.size !== b.size) return false;
  for (const item of a) if (!b.has(item)) return false;
  return true;
}

export default function McpToolsPanel({ canManage = false }) {
  const [data, setData] = useState(null);
  const [enabled, setEnabled] = useState(true);
  const [off, setOff] = useState(() => new Set());
  const [rules, setRules] = useState({});
  const [tab, setTab] = useState("tools");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [savedNote, setSavedNote] = useState("");

  const adopt = (payload) => {
    setData(payload);
    setEnabled(Boolean(payload.enabled));
    setOff(
      new Set(
        payload.tools.filter((tool) => !tool.enabled).map((tool) => tool.name),
      ),
    );
    setRules(rulesFromPayload(payload.clusters));
  };

  useEffect(() => {
    let live = true;
    getMcpAccess()
      .then((payload) => live && adopt(payload))
      .catch(
        (err) =>
          live && setError(err.message || "Could not load the MCP tools."),
      )
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, []);

  const savedOff = useMemo(
    () =>
      new Set(
        (data?.tools || [])
          .filter((tool) => !tool.enabled)
          .map((tool) => tool.name),
      ),
    [data],
  );

  const tools = data?.tools || [];
  const writeTools = tools.filter((tool) => tool.write);
  const onCount = tools.length - off.size;
  const writesOn = writeTools.filter((tool) => !off.has(tool.name)).length;

  const changedTools = useMemo(() => {
    const changed = [];
    for (const name of off) if (!savedOff.has(name)) changed.push(name);
    for (const name of savedOff) if (!off.has(name)) changed.push(name);
    return changed;
  }, [off, savedOff]);
  const masterChanged = data ? enabled !== Boolean(data.enabled) : false;
  const savedRules = useMemo(() => rulesFromPayload(data?.clusters), [data]);
  const changedClusters = useMemo(() => {
    const ids = new Set([...Object.keys(rules), ...Object.keys(savedRules)]);
    return [...ids].filter(
      (id) =>
        rulesKey({ x: rules[id] || { mode: "full" } }) !==
        rulesKey({ x: savedRules[id] || { mode: "full" } }),
    );
  }, [rules, savedRules]);
  const dirty =
    masterChanged || changedTools.length > 0 || changedClusters.length > 0;
  const restricted = Object.values(rules).filter(
    (rule) => rule && rule.mode !== "full",
  ).length;

  const ticketToolsOff = tools.filter(
    (tool) => tool.usedByTicketAgent && off.has(tool.name),
  );
  const ticketAgentBroken =
    data?.ticketAgentEnabled && (!enabled || ticketToolsOff.length > 0);

  const touch = () => setSavedNote("");

  const setTool = (name, on) => {
    touch();
    setOff((prev) => {
      const next = new Set(prev);
      if (on) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const setMany = (names, on) => {
    touch();
    setOff((prev) => {
      const next = new Set(prev);
      names.forEach((name) => (on ? next.delete(name) : next.add(name)));
      return next;
    });
  };

  const allOn = () => {
    touch();
    setOff(new Set());
  };

  const readOnly = () => {
    touch();
    setOff(new Set(writeTools.map((tool) => tool.name)));
  };

  const discard = () => {
    touch();
    adopt(data);
  };

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const payload = await updateMcpAccess({
        enabled,
        disabledTools: [...off].sort(),
        clusterRules: rulesPayload(rules),
      });
      adopt(payload);
      setSavedNote("Saved. Agents see the change on their next call.");
    } catch (err) {
      setError(err.message || "Could not save the MCP tools.");
    } finally {
      setSaving(false);
    }
  };

  const needle = query.trim().toLowerCase();
  const visible = tools.filter((tool) => {
    if (filter === "read" && tool.write) return false;
    if (filter === "write" && !tool.write) return false;
    if (filter === "off" && !off.has(tool.name)) return false;
    if (!needle) return true;
    return (
      tool.name.toLowerCase().includes(needle) ||
      tool.description.toLowerCase().includes(needle) ||
      tool.permission.toLowerCase().includes(needle)
    );
  });

  const groups = (data?.domains || [])
    .map((domain) => ({
      domain,
      all: tools.filter((tool) => tool.domain === domain),
      shown: visible.filter((tool) => tool.domain === domain),
    }))
    .filter((group) => group.shown.length > 0);

  if (loading) return <LoadingState label="Loading MCP tools..." />;
  if (!data)
    return (
      <p className="banner-message error">
        {error || "Could not load the MCP tools."}
      </p>
    );

  const isReadOnlyPreset = sameSet(
    off,
    new Set(writeTools.map((tool) => tool.name)),
  );
  const endpoint = `${window.location.origin}/api/mcp`;

  return (
    <div className="settings-panel-body mcp-panel">
      {error && <p className="banner-message error">{error}</p>}

      <section className="settings-card mcp-master">
        <div className="mcp-master-row">
          <div className="mcp-master-text">
            <h3>Agents can call KubeSight</h3>
            <p className="settings-card-sub">
              {enabled ? (
                <>
                  <strong>{onCount}</strong> of {tools.length} tools on ·{" "}
                  <strong>{writesOn}</strong> of {writeTools.length} that change
                  something
                </>
              ) : (
                <>
                  Off. Every tool call is refused and the tool list an agent
                  sees is empty.
                </>
              )}
            </p>
          </div>
          <Switch
            checked={enabled}
            disabled={!canManage}
            label="MCP server on"
            onChange={(value) => {
              touch();
              setEnabled(value);
            }}
          />
        </div>
        <div className="mcp-endpoint">
          <span className="muted">Endpoint</span>
          <code>{endpoint}</code>
        </div>
        <p className="field-hint">
          A tool switched on still needs the permission of the token calling it,
          so this can take access away from every agent at once but never gives
          a token more than its owner has.
        </p>
      </section>

      {ticketAgentBroken && (
        <p className="banner-message warning-banner mcp-warn">
          The Hermes ticket agent is on and needs{" "}
          {!enabled
            ? "the MCP server"
            : ticketToolsOff.map((tool) => shortName(tool.name)).join(", ")}
          . With {!enabled || ticketToolsOff.length > 1 ? "these" : "this"} off,
          inbound tickets will not be handled.
        </p>
      )}

      <div className="mcp-tabs" role="tablist" aria-label="MCP access">
        {[
          {
            value: "tools",
            label: "Tools",
            count: `${onCount}/${tools.length}`,
            dirty: changedTools.length > 0,
          },
          {
            value: "clusters",
            label: "Clusters",
            count: restricted
              ? `${restricted} restricted`
              : `${(data.clusters || []).length}`,
            dirty: changedClusters.length > 0,
          },
        ].map((option) => (
          <button
            key={option.value}
            type="button"
            role="tab"
            aria-selected={tab === option.value}
            className={`btn-ghost${tab === option.value ? " is-active" : ""}`}
            onClick={() => setTab(option.value)}
          >
            {option.label}
            <span className="mcp-tab-count">{option.count}</span>
            {option.dirty && (
              <span className="mcp-tab-dot" aria-label="unsaved changes" />
            )}
          </button>
        ))}
      </div>

      {tab === "clusters" ? (
        <div className={enabled ? "" : "is-dimmed"}>
          <McpClusterRules
            clusters={data.clusters || []}
            tools={tools}
            globalOff={off}
            rules={rules}
            savedRules={savedRules}
            onChange={(next) => {
              touch();
              setRules(next);
            }}
            canManage={canManage}
            ticketAgentEnabled={Boolean(data.ticketAgentEnabled)}
            Switch={Switch}
          />
        </div>
      ) : (
        <>
          <div className={`mcp-toolbar${enabled ? "" : " is-dimmed"}`}>
            <input
              type="search"
              className="mcp-search"
              placeholder="Search tools, descriptions or permissions"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              aria-label="Search tools"
            />
            <div className="mcp-filter" role="radiogroup" aria-label="Show">
              {FILTERS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="radio"
                  aria-checked={filter === option.value}
                  className={`btn-ghost${filter === option.value ? " is-active" : ""}`}
                  onClick={() => setFilter(option.value)}
                >
                  {option.label}
                  {option.value === "off" && off.size > 0 ? (
                    <span className="mcp-count">{off.size}</span>
                  ) : null}
                </button>
              ))}
            </div>
            {canManage && (
              <div className="mcp-presets">
                <button
                  type="button"
                  className="btn-outline btn-compact"
                  disabled={off.size === 0}
                  onClick={allOn}
                >
                  All on
                </button>
                <button
                  type="button"
                  className="btn-outline btn-compact"
                  disabled={isReadOnlyPreset}
                  onClick={readOnly}
                  title="Switch off every tool that changes something; keep every read"
                >
                  Read only
                </button>
              </div>
            )}
          </div>

          <div className={`mcp-groups${enabled ? "" : " is-dimmed"}`}>
            {groups.length === 0 && (
              <p className="muted mcp-empty">
                {filter === "off" && !needle
                  ? "Every tool is on."
                  : "No tool matches."}
              </p>
            )}
            {groups.map(({ domain, all, shown }) => {
              const info = DOMAIN_INFO[domain] || { label: domain, hint: "" };
              const domainOn = all.filter((tool) => !off.has(tool.name)).length;
              const allDomainOn = domainOn === all.length;
              const noneOn = domainOn === 0;
              return (
                <section className="settings-card mcp-group" key={domain}>
                  <header className="mcp-group-head">
                    <div>
                      <h3>{info.label}</h3>
                      <p className="settings-card-sub">{info.hint}</p>
                    </div>
                    <span className="mcp-group-count">
                      {domainOn}/{all.length} on
                    </span>
                    <Switch
                      checked={allDomainOn}
                      mixed={!allDomainOn && !noneOn}
                      disabled={!canManage}
                      label={`All ${info.label} tools`}
                      // Off only from all-on; from a mix, the first press restores.
                      onChange={() =>
                        setMany(
                          all.map((tool) => tool.name),
                          !allDomainOn,
                        )
                      }
                    />
                  </header>
                  <ul className="mcp-rows">
                    {shown.map((tool) => {
                      const on = !off.has(tool.name);
                      const changed = changedTools.includes(tool.name);
                      return (
                        <li
                          key={tool.name}
                          className={`mcp-row${on ? "" : " is-off"}${changed ? " is-changed" : ""}`}
                        >
                          <div className="mcp-row-main">
                            <div className="mcp-row-title">
                              <code className="mcp-name">
                                {shortName(tool.name)}
                              </code>
                              {tool.write ? (
                                <span
                                  className={`mcp-tag${tool.destructive ? " is-danger" : " is-write"}`}
                                >
                                  {tool.destructive ? "Destructive" : "Writes"}
                                </span>
                              ) : (
                                <span className="mcp-tag">Read</span>
                              )}
                              {tool.usedByTicketAgent && (
                                <span className="mcp-tag is-info">
                                  Ticket agent
                                </span>
                              )}
                              {changed && (
                                <span className="mcp-tag is-pending">
                                  Unsaved
                                </span>
                              )}
                            </div>
                            <p className="mcp-desc" title={tool.description}>
                              {firstSentence(tool.description)}
                            </p>
                          </div>
                          <code
                            className="mcp-perm"
                            title="Permission the calling token needs"
                          >
                            {tool.permission}
                          </code>
                          <Switch
                            checked={on}
                            disabled={!canManage}
                            label={`${tool.name} on`}
                            onChange={(value) => setTool(tool.name, value)}
                          />
                        </li>
                      );
                    })}
                  </ul>
                </section>
              );
            })}
          </div>
        </>
      )}

      {!canManage && (
        <p className="muted">
          You can see these switches; changing them needs settings:manage.
        </p>
      )}
      {savedNote && !dirty && <p className="muted mcp-saved">{savedNote}</p>}

      {canManage && dirty ? (
        <div className="settings-savebar" role="status">
          <span className="settings-savebar-dot" aria-hidden="true" />
          <span className="settings-savebar-text">
            <strong>Unsaved changes</strong> ·{" "}
            {[
              masterChanged ? `server ${enabled ? "on" : "off"}` : null,
              changedTools.length
                ? `${changedTools.length} ${changedTools.length === 1 ? "tool" : "tools"}`
                : null,
              changedClusters.length
                ? `${changedClusters.length} ${changedClusters.length === 1 ? "cluster" : "clusters"}`
                : null,
            ]
              .filter(Boolean)
              .join(", ")}
          </span>
          <button
            type="button"
            className="btn-ghost"
            onClick={discard}
            disabled={saving}
          >
            Discard
          </button>
          <button
            type="button"
            className="primary"
            onClick={save}
            disabled={saving}
          >
            {saving ? "Saving…" : "Save changes"}
          </button>
        </div>
      ) : null}
    </div>
  );
}
