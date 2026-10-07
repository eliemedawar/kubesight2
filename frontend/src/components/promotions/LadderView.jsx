import { Fragment, useEffect, useMemo, useState } from "react";

import { listClusters } from "../../api/clustersApi.js";
import {
  addEnvironmentBinding,
  createEnvironment,
  deleteEnvironment,
  getNamespaceMap,
  previewBindingRule,
  removeEnvironmentBinding,
  reorderEnvironments,
  updateEnvironment,
  updatePromotionPolicy,
} from "../../api/promotionsApi.js";
import { ModeIcon, PrIcon } from "./icons.jsx";
import ScheduleEditor from "./ScheduleEditor.jsx";
import { MODES, SOAK_PRESETS, formatMinutes } from "./promotionModel.js";

const EXEMPT_SUGGESTIONS = ["redis", "postgres", "nginx", "busybox", "docker.io/*", "*/bitnami/*"];

/** A stable colour per rung, so an environment reads the same everywhere here. */
const envTone = (index) => `var(--chart-${(index % 6) + 2})`;

/**
 * The ladder itself: the environments in order and how strictly each holds
 * the line; where each one lives (rules: exact namespaces, patterns such as
 * `*-sit`, whole clusters — with a live preview of what a rule would take);
 * the namespace map that shows every namespace and the gaps; and the
 * ladder-wide rules. Every change saves on its own.
 */
export default function LadderView({ setup, overview, canManage, onChanged }) {
  const [clusters, setClusters] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [mapVersion, setMapVersion] = useState(0);

  useEffect(() => {
    let cancelled = false;
    listClusters()
      .then((data) => !cancelled && setClusters((data?.items || data || []).map((c) => ({ id: c.id, name: c.name || c.id }))))
      .catch(() => !cancelled && setClusters([]));
    return () => {
      cancelled = true;
    };
  }, []);

  const run = async (action) => {
    setBusy(true);
    setError("");
    try {
      await action();
      await onChanged();
      setMapVersion((v) => v + 1);
      return true;
    } catch (err) {
      setError(err.message || "That did not save.");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const envs = setup.environments;
  const stats = useMemo(() => new Map((overview?.environments || []).map((e) => [e.id, e])), [overview]);

  return (
    <div className="pr-ladder-view">
      {!canManage && (
        <div className="pr-callout pr-callout--info" role="status">
          <PrIcon.Lock />
          <p>You can see the ladder. Changing it needs the “Set up the environment ladder” permission.</p>
        </div>
      )}
      {error && (
        <p className="pr-callout pr-callout--danger" role="alert">
          <PrIcon.Stop />
          <span>{error}</span>
        </p>
      )}

      <section className="pr-block">
        <header className="pr-block-head">
          <h3>Environments</h3>
          <p>In the order every image climbs. New environments start in Warn — switch to Enforce once Releases shows nothing you would not have stopped.</p>
        </header>
        <div className="pr-envs">
          {envs.map((env, index) => (
            <Fragment key={env.id}>
              <EnvironmentCard
                env={env}
                index={index}
                count={envs.length}
                stats={stats.get(env.id)}
                canManage={canManage}
                busy={busy}
                run={run}
                onMove={(delta) => {
                  const ids = envs.map((e) => e.id);
                  const [moved] = ids.splice(index, 1);
                  ids.splice(index + delta, 0, moved);
                  run(() => reorderEnvironments(ids));
                }}
              />
              {index < envs.length - 1 && (
                <span className="pr-envs-arrow" aria-hidden="true">
                  <PrIcon.Arrow />
                </span>
              )}
            </Fragment>
          ))}
          {canManage && <AddEnvironment busy={busy} run={run} />}
        </div>
      </section>

      <section className="pr-block">
        <header className="pr-block-head">
          <h3>Release schedule</h3>
          <p>
            When each environment takes the next release. A scheduled hop closes a release at every cut-off and deploys it at the departure time;
            a hop without a schedule is promoted on demand from the Timetable.
          </p>
        </header>
        <div className="tt-sched-list">
          {envs.slice(1).map((env, i) => (
            <ScheduleEditor key={env.id} env={env} prev={envs[i]} canManage={canManage} onSaved={onChanged} />
          ))}
        </div>
      </section>

      <section className="pr-block">
        <header className="pr-block-head">
          <h3>Where each environment lives</h3>
          <p>
            A rule puts namespaces into an environment: a name, a pattern such as <code>*-sit</code>, or a whole cluster.
            A namespace&apos;s own name wins over a pattern, the most specific pattern over a looser one, and both over a
            whole cluster.
          </p>
        </header>
        <div className="pr-where">
          <RulesCard envs={envs} clusters={clusters} canManage={canManage} busy={busy} run={run} />
          <NamespaceMap envs={envs} clusters={clusters} canManage={canManage} busy={busy} run={run} version={mapVersion} />
        </div>
      </section>

      <section className="pr-block">
        <header className="pr-block-head">
          <h3>Ladder-wide rules</h3>
        </header>
        <div className="pr-where">
          <PolicyCard policy={setup.policy} canManage={canManage} busy={busy} run={run} />
          <section className="pr-card pr-howto">
            <h4>How the rule works</h4>
            <ul>
              <li>
                <PrIcon.Up />
                <span>
                  An image may enter an environment once it ran <b>healthy</b> in the one below — for at least that
                  environment&apos;s soak time. Images are built once: the exact tag moves up.
                </span>
              </li>
              <li>
                <PrIcon.Equal />
                <span>A rollback or redeploy of an image that already ran in the environment is always allowed.</span>
              </li>
              <li>
                <PrIcon.Hand />
                <span>
                  Skipping needs an <b>exception</b>: a written reason, approved by someone else.
                </span>
              </li>
              <li>
                <PrIcon.Ladder />
                <span>It holds for every way in: Deploy, CI Deploy stages, ticket automation, Helm and change bundles.</span>
              </li>
            </ul>
          </section>
        </div>
      </section>
    </div>
  );
}

function EnvironmentCard({ env, index, count, stats, canManage, busy, run, onMove }) {
  const [name, setName] = useState(env.name);
  const [confirming, setConfirming] = useState(false);
  useEffect(() => setName(env.name), [env.name]);
  const first = index === 0;
  const last = index === count - 1;
  const soakIsPreset = SOAK_PRESETS.some((p) => p.minutes === env.minSoakMinutes);

  const saveName = () => {
    const next = name.trim();
    if (!next || next === env.name) return setName(env.name);
    run(() => updateEnvironment(env.id, { name: next }));
  };

  return (
    <article className={`pr-env pr-env--${first ? "entry" : env.mode}`} style={{ "--pr-env-tone": envTone(index) }}>
      <header className="pr-env-head">
        <span className="pr-env-index">{index + 1}</span>
        <input
          className="pr-env-name"
          value={name}
          disabled={!canManage || busy}
          onChange={(event) => setName(event.target.value)}
          onBlur={saveName}
          onKeyDown={(event) => event.key === "Enter" && event.currentTarget.blur()}
          aria-label={`Name of environment ${index + 1}`}
        />
        {canManage && (
          <span className="pr-env-tools">
            <button type="button" className="icon-button pr-icon-btn" disabled={busy || first} onClick={() => onMove(-1)} aria-label={`Move ${env.name} earlier`}>
              <PrIcon.ChevronLeft />
            </button>
            <button type="button" className="icon-button pr-icon-btn" disabled={busy || last} onClick={() => onMove(1)} aria-label={`Move ${env.name} later`}>
              <PrIcon.ChevronRight />
            </button>
            <button type="button" className="icon-button pr-icon-btn pr-icon-btn--danger" disabled={busy} onClick={() => setConfirming(true)} aria-label={`Delete ${env.name}`}>
              <PrIcon.Trash />
            </button>
          </span>
        )}
      </header>
      <code className="pr-env-key">{env.key}</code>

      {first ? (
        <p className="pr-env-entry">
          <ModeIcon mode="entry" /> Entry — every build lands here first, so it takes any image.
        </p>
      ) : (
        <div className="pr-env-mode">
          <div className="pr-seg pr-seg--full" role="radiogroup" aria-label={`How strictly ${env.name} holds the line`}>
            {MODES.map((mode) => (
              <button
                key={mode.key}
                type="button"
                role="radio"
                aria-checked={env.mode === mode.key}
                className={`btn-ghost pr-seg-btn pr-seg-btn--${mode.key}${env.mode === mode.key ? " is-on" : ""}`}
                disabled={!canManage || busy}
                onClick={() => env.mode !== mode.key && run(() => updateEnvironment(env.id, { mode: mode.key }))}
                title={mode.hint}
              >
                <ModeIcon mode={mode.key} />
                {mode.label}
              </button>
            ))}
          </div>
        </div>
      )}

      <label className="pr-env-soak">
        <span>Soak before the next</span>
        <select
          value={soakIsPreset ? env.minSoakMinutes : "custom"}
          disabled={!canManage || busy || last}
          onChange={(event) => run(() => updateEnvironment(env.id, { minSoakMinutes: Number(event.target.value) }))}
        >
          {SOAK_PRESETS.map((preset) => (
            <option key={preset.minutes} value={preset.minutes}>
              {preset.label}
            </option>
          ))}
          {!soakIsPreset && <option value="custom">{formatMinutes(env.minSoakMinutes)}</option>}
        </select>
      </label>

      <footer className="pr-env-foot">
        <span>
          <b>{env.bindings.length}</b> {env.bindings.length === 1 ? "rule" : "rules"}
        </span>
        <span>
          <b>{stats?.appCount ?? 0}</b> apps
        </span>
        <span>
          <b>{stats?.workloadCount ?? 0}</b> workloads
        </span>
      </footer>

      {confirming && (
        <div className="pr-env-confirm" role="alertdialog" aria-label={`Delete ${env.name}`}>
          <p>
            Delete <b>{env.name}</b>? Its namespaces leave the ladder and what it remembers about passed images is
            forgotten.
          </p>
          <div>
            <button type="button" className="btn-outline" onClick={() => setConfirming(false)}>
              Keep
            </button>
            <button type="button" className="btn-outline danger" disabled={busy} onClick={() => run(() => deleteEnvironment(env.id))}>
              Delete
            </button>
          </div>
        </div>
      )}
    </article>
  );
}

function AddEnvironment({ busy, run }) {
  const [name, setName] = useState("");
  return (
    <form
      className="pr-env pr-env--add"
      onSubmit={async (event) => {
        event.preventDefault();
        if (!name.trim()) return;
        if (await run(() => createEnvironment({ name: name.trim() }))) setName("");
      }}
    >
      <PrIcon.Plus />
      <p>Add an environment at the top of the ladder.</p>
      <input value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Staging" maxLength={80} aria-label="New environment" />
      <button type="submit" className="btn-outline" disabled={busy || !name.trim()}>
        Add
      </button>
    </form>
  );
}

// ── Rules ───────────────────────────────────────────────────────────────

function RulesCard({ envs, clusters, canManage, busy, run }) {
  const [envId, setEnvId] = useState(envs[1]?.id || envs[0]?.id || "");
  const [clusterId, setClusterId] = useState("");
  const [kind, setKind] = useState("pattern");
  const [pattern, setPattern] = useState("");
  const [picked, setPicked] = useState([]);
  const [nsMap, setNsMap] = useState({ loading: false, items: [] });
  const [preview, setPreview] = useState(null);

  useEffect(() => {
    if (!clusterId && clusters[0]) setClusterId(clusters[0].id);
  }, [clusters, clusterId]);
  useEffect(() => {
    if (!envs.some((e) => e.id === Number(envId))) setEnvId(envs[1]?.id || envs[0]?.id || "");
  }, [envs, envId]);

  const env = envs.find((e) => e.id === Number(envId));

  useEffect(() => {
    if (!clusterId || kind !== "namespaces") return undefined;
    let cancelled = false;
    setNsMap({ loading: true, items: [] });
    getNamespaceMap(clusterId)
      .then((data) => !cancelled && setNsMap({ loading: false, items: data?.items || [] }))
      .catch(() => !cancelled && setNsMap({ loading: false, items: [] }));
    return () => {
      cancelled = true;
    };
  }, [clusterId, kind]);

  // Live preview of what a pattern / whole-cluster rule would take.
  useEffect(() => {
    if (!canManage || !clusterId || kind === "namespaces" || (kind === "pattern" && !pattern.trim())) {
      setPreview(null);
      return undefined;
    }
    let cancelled = false;
    const handle = setTimeout(() => {
      previewBindingRule({ clusterId, environmentId: Number(envId) || null, pattern: pattern.trim(), wholeCluster: kind === "whole" })
        .then((data) => !cancelled && setPreview({ ...data, error: "" }))
        .catch((err) => !cancelled && setPreview({ error: err.message, matches: [], count: 0 }));
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [canManage, clusterId, envId, kind, pattern]);

  const suggestions = env ? [`*-${env.key}`, `${env.key}-*`, `*-${env.key}-*`] : [];
  const canAdd =
    canManage && !busy && env && clusterId && (kind === "whole" || (kind === "pattern" ? pattern.trim() && !preview?.error : picked.length));

  const add = async () => {
    const payload =
      kind === "whole"
        ? { clusterId, wholeCluster: true }
        : kind === "pattern"
          ? { clusterId, pattern: pattern.trim() }
          : { clusterId, namespaces: picked };
    if (await run(() => addEnvironmentBinding(env.id, payload))) {
      setPattern("");
      setPicked([]);
      setPreview(null);
    }
  };

  const takes = (preview?.matches || []).filter((m) => m.takes);
  const kept = (preview?.matches || []).filter((m) => !m.takes);

  return (
    <section className="pr-card pr-rules">
      <h4>Rules</h4>
      {canManage && (
        <div className="pr-composer">
          <div className="pr-composer-row">
            <label className="pr-field">
              <span>Environment</span>
              <select value={envId} onChange={(event) => setEnvId(event.target.value)}>
                {envs.map((e) => (
                  <option key={e.id} value={e.id}>
                    {e.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="pr-field">
              <span>Cluster</span>
              <select value={clusterId} onChange={(event) => setClusterId(event.target.value)}>
                {clusters.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="pr-seg pr-seg--full" role="radiogroup" aria-label="Kind of rule">
            {[
              ["pattern", "Pattern"],
              ["namespaces", "Namespaces"],
              ["whole", "Whole cluster"],
            ].map(([key, label]) => (
              <button
                key={key}
                type="button"
                role="radio"
                aria-checked={kind === key}
                className={`btn-ghost pr-seg-btn${kind === key ? " is-on" : ""}`}
                onClick={() => setKind(key)}
              >
                {label}
              </button>
            ))}
          </div>

          {kind === "pattern" && (
            <>
              <label className="pr-field">
                <span>
                  Pattern <em>* matches anything, ? one character</em>
                </span>
                <input
                  className="pr-mono-input"
                  value={pattern}
                  onChange={(event) => setPattern(event.target.value.toLowerCase())}
                  placeholder={suggestions[0] || "*-sit"}
                  aria-label="Namespace pattern"
                />
              </label>
              <div className="pr-suggest">
                {suggestions.map((s) => (
                  <button key={s} type="button" className="btn-ghost pr-suggest-btn" onClick={() => setPattern(s)}>
                    {s}
                  </button>
                ))}
              </div>
            </>
          )}

          {kind === "namespaces" && (
            <div className="pr-ns-pick">
              {nsMap.loading ? (
                <p className="pr-muted">Loading namespaces…</p>
              ) : nsMap.items.length === 0 ? (
                <p className="pr-muted">No namespaces found on this cluster.</p>
              ) : (
                nsMap.items.map((row) => {
                  const own = row.ruleKind === "exact";
                  return (
                    <label key={row.namespace} className={`pr-ns${picked.includes(row.namespace) ? " is-on" : ""}${own ? " is-taken" : ""}`}>
                      <input
                        type="checkbox"
                        disabled={own}
                        checked={picked.includes(row.namespace)}
                        onChange={() =>
                          setPicked(picked.includes(row.namespace) ? picked.filter((n) => n !== row.namespace) : [...picked, row.namespace])
                        }
                      />
                      <span>{row.namespace}</span>
                      {row.environmentName && <em>{own ? row.environmentName : `${row.environmentName} via ${row.rule}`}</em>}
                    </label>
                  );
                })
              )}
            </div>
          )}

          {kind !== "namespaces" && preview && (
            <div className={`pr-preview${preview.error ? " is-error" : ""}`} aria-live="polite">
              {preview.error ? (
                <p>{preview.error}</p>
              ) : (
                <>
                  <p>
                    <b>
                      Takes {preview.count} namespace{preview.count === 1 ? "" : "s"}
                    </b>
                    {preview.moved > 0 && ` · moves ${preview.moved} from another environment`}
                    {kept.length > 0 && ` · ${kept.length} stay where a more specific rule puts them`}
                  </p>
                  {takes.length > 0 && (
                    <div className="pr-preview-chips">
                      {takes.slice(0, 18).map((m) => (
                        <span key={m.namespace} className={`pr-ns-chip${m.from && m.from !== env?.name ? " is-moved" : ""}`} title={m.from ? `now in ${m.from}` : "not in the ladder yet"}>
                          {m.namespace}
                        </span>
                      ))}
                      {takes.length > 18 && <span className="pr-ns-chip">+{takes.length - 18}</span>}
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          <div className="pr-composer-foot">
            <button type="button" className="primary" disabled={!canAdd} onClick={add}>
              <PrIcon.Plus />
              Add rule to {env?.name || "…"}
            </button>
          </div>
        </div>
      )}

      <div className="pr-rule-groups">
        {envs.map((e, index) => (
          <div key={e.id} className="pr-rule-group" style={{ "--pr-env-tone": envTone(index) }}>
            <p className="pr-rule-env">
              <i aria-hidden="true" />
              {e.name}
              <em>
                {e.bindings.length} {e.bindings.length === 1 ? "rule" : "rules"}
              </em>
            </p>
            {e.bindings.length === 0 ? (
              <p className="pr-muted pr-rule-none">No rule yet — nothing can pass {e.name}.</p>
            ) : (
              <ul className="pr-rule-list">
                {e.bindings.map((b) => (
                  <li key={b.id} className={`pr-rule-chip pr-rule-chip--${b.wholeCluster ? "whole" : b.pattern ? "pattern" : "exact"}`}>
                    {b.wholeCluster ? <PrIcon.Cluster /> : b.pattern ? <PrIcon.Asterisk /> : <PrIcon.Box />}
                    <span className="pr-rule-cluster">{b.clusterName}</span>
                    <code>{b.wholeCluster ? "whole cluster" : b.namespace}</code>
                    {canManage && (
                      <button
                        type="button"
                        className="icon-button pr-x pr-x--sm"
                        disabled={busy}
                        onClick={() => run(() => removeEnvironmentBinding(b.id))}
                        aria-label={`Remove ${b.namespace} from ${e.name}`}
                      >
                        <PrIcon.X />
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

// ── Namespace map ───────────────────────────────────────────────────────

function NamespaceMap({ envs, clusters, canManage, busy, run, version }) {
  const [clusterId, setClusterId] = useState("");
  const [data, setData] = useState({ loading: false, items: [], assigned: 0, unassigned: 0 });
  const [show, setShow] = useState("all");
  const [term, setTerm] = useState("");

  useEffect(() => {
    if (!clusterId && clusters[0]) setClusterId(clusters[0].id);
  }, [clusters, clusterId]);

  useEffect(() => {
    if (!clusterId) return undefined;
    let cancelled = false;
    setData((d) => ({ ...d, loading: true }));
    getNamespaceMap(clusterId)
      .then((res) => !cancelled && setData({ loading: false, ...res }))
      .catch(() => !cancelled && setData({ loading: false, items: [], assigned: 0, unassigned: 0 }));
    return () => {
      cancelled = true;
    };
  }, [clusterId, version]);

  const toneOf = new Map(envs.map((e, i) => [e.id, envTone(i)]));
  const rows = data.items
    .filter((r) => (show === "all" ? true : show === "none" ? !r.environmentId : String(r.environmentId) === show))
    .filter((r) => !term.trim() || r.namespace.includes(term.trim().toLowerCase()));

  return (
    <section className="pr-card pr-nsmap">
      <h4>Namespace map</h4>
      <div className="pr-composer-row">
        <label className="pr-field">
          <span>Cluster</span>
          <select value={clusterId} onChange={(event) => setClusterId(event.target.value)}>
            {clusters.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <label className="pr-field">
          <span>Find</span>
          <input value={term} onChange={(event) => setTerm(event.target.value)} placeholder="namespace" />
        </label>
      </div>
      <div className="pr-nsmap-filters" role="group" aria-label="Show">
        <button type="button" className={`btn-ghost pr-fchip${show === "all" ? " is-on" : ""}`} onClick={() => setShow("all")}>
          All <b>{data.items.length}</b>
        </button>
        <button type="button" className={`btn-ghost pr-fchip pr-fchip--warn${show === "none" ? " is-on" : ""}`} onClick={() => setShow("none")}>
          Not in the ladder <b>{data.unassigned || 0}</b>
        </button>
        {envs.map((e) => (
          <button
            key={e.id}
            type="button"
            className={`btn-ghost pr-fchip${show === String(e.id) ? " is-on" : ""}`}
            onClick={() => setShow(String(e.id))}
          >
            <i className="pr-env-dot" style={{ background: toneOf.get(e.id) }} aria-hidden="true" />
            {e.name} <b>{data.items.filter((r) => r.environmentId === e.id).length}</b>
          </button>
        ))}
      </div>
      <div className="pr-nsmap-table" role="table" aria-label="Namespaces">
        {data.loading && !data.items.length ? (
          <p className="pr-muted">Loading…</p>
        ) : rows.length === 0 ? (
          <p className="pr-muted">No namespace here.</p>
        ) : (
          rows.map((r) => (
            <div key={r.namespace} className="pr-nsmap-row" role="row">
              <code role="cell">{r.namespace}</code>
              <span role="cell">
                {r.environmentId ? (
                  <span className="pr-env-chip" style={{ "--pr-env-tone": toneOf.get(r.environmentId) }}>
                    <i aria-hidden="true" />
                    {r.environmentName}
                  </span>
                ) : canManage ? (
                  <select
                    className="pr-assign"
                    value=""
                    disabled={busy}
                    onChange={(event) =>
                      event.target.value &&
                      run(() => addEnvironmentBinding(Number(event.target.value), { clusterId, namespace: r.namespace }))
                    }
                    aria-label={`Assign ${r.namespace} to an environment`}
                  >
                    <option value="">Assign to…</option>
                    {envs.map((e) => (
                      <option key={e.id} value={e.id}>
                        {e.name}
                      </option>
                    ))}
                  </select>
                ) : (
                  <span className="pr-muted">not in the ladder</span>
                )}
              </span>
              <span role="cell" className="pr-nsmap-rule">
                {r.rule ? (
                  <>
                    {r.ruleKind === "pattern" ? <PrIcon.Asterisk /> : r.ruleKind === "whole" ? <PrIcon.Cluster /> : <PrIcon.Box />}
                    {r.ruleKind === "exact" ? "by name" : r.rule}
                  </>
                ) : (
                  ""
                )}
              </span>
            </div>
          ))
        )}
      </div>
    </section>
  );
}

// ── Policy ──────────────────────────────────────────────────────────────

function PolicyCard({ policy, canManage, busy, run }) {
  const [draft, setDraft] = useState("");
  const exempt = policy.exemptImages || [];
  const add = (value) => {
    const text = (value || draft).trim();
    if (!text || exempt.includes(text)) return;
    run(async () => {
      await updatePromotionPolicy({ exemptImages: [...exempt, text] });
      setDraft("");
    });
  };
  return (
    <section className="pr-card pr-policy">
      <h4>Images and tags</h4>
      <label className="pr-switch-row">
        <input
          type="checkbox"
          role="switch"
          checked={policy.requireVersionedTags}
          disabled={!canManage || busy}
          onChange={(event) => run(() => updatePromotionPolicy({ requireVersionedTags: event.target.checked }))}
        />
        <span>
          <b>Refuse mutable tags</b>
          <small>
            <code>:latest</code> or no tag can name a different build each time, so “it passed SIT” proves nothing.
          </small>
        </span>
      </label>
      <div className="pr-exempt">
        <b>Images the ladder ignores</b>
        <small>
          Third-party images you pull rather than build. Globs work: <code>docker.io/*</code>.
        </small>
        <div className="pr-exempt-chips">
          {exempt.length === 0 && <span className="pr-muted">None.</span>}
          {exempt.map((pattern) => (
            <span key={pattern} className="pr-exempt-chip">
              <code>{pattern}</code>
              {canManage && (
                <button
                  type="button"
                  className="icon-button pr-x pr-x--sm"
                  disabled={busy}
                  onClick={() => run(() => updatePromotionPolicy({ exemptImages: exempt.filter((p) => p !== pattern) }))}
                  aria-label={`Stop ignoring ${pattern}`}
                >
                  <PrIcon.X />
                </button>
              )}
            </span>
          ))}
        </div>
        {canManage && (
          <>
            <form
              className="pr-exempt-add"
              onSubmit={(event) => {
                event.preventDefault();
                add();
              }}
            >
              <input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="redis, docker.io/*" aria-label="Image pattern to ignore" />
              <button type="submit" className="btn-outline" disabled={busy || !draft.trim()}>
                Add
              </button>
            </form>
            <div className="pr-suggest">
              {EXEMPT_SUGGESTIONS.filter((s) => !exempt.includes(s)).map((s) => (
                <button key={s} type="button" className="btn-ghost pr-suggest-btn" disabled={busy} onClick={() => add(s)}>
                  + {s}
                </button>
              ))}
            </div>
          </>
        )}
      </div>
    </section>
  );
}
