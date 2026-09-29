import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import "../../styles/signal/pipelineWorkspace.css";
import "../../styles/signal/mergeChecks.css";
import {
  getMergeCheckEnforcement,
  getMergeCheckWebhook,
  getServiceMergeChecks,
  listMergeChecks,
  redeliverMergeCheck,
  revealMergeCheckSecret,
  rotateMergeCheckSecret,
  saveServiceMergeChecks,
  setupMergeChecksInSource,
} from "../../api/mergeChecksApi.js";
import LoadingState from "../common/LoadingState.jsx";
import { useRouter } from "../../routes/RouterContext.jsx";
import { Switch } from "./pipeline/controls.jsx";
import { PlIcon } from "./pipeline/icons.jsx";
import ChecksView from "./mergeChecks/ChecksView.jsx";
import ConnectionView from "./mergeChecks/ConnectionView.jsx";
import GateStatus from "./mergeChecks/GateStatus.jsx";
import GateView from "./mergeChecks/GateView.jsx";
import HistoryView from "./mergeChecks/HistoryView.jsx";
import {
  activeTools,
  dirtyKeys,
  gateNeverBlocks,
  readiness,
  rebaseForm,
  toForm,
  watchedBranches,
} from "./mergeChecks/mergeCheckModel.js";

const SECTIONS = ["checks", "gate", "connection", "history"];

/**
 * Merge Checks tab: the gate between a pull request and a merge.
 *
 * It opens on the one question that matters — is a failing pull request
 * actually stopped? — answered in four steps, three of which live in
 * Bitbucket and are asked of it rather than assumed. Below that, four views:
 * which checks run, how many problems pass, how Bitbucket is wired, and what
 * was decided.
 *
 * KubeSight reports a verdict. Bitbucket is what enforces it — nothing here can
 * stand between a developer and the Merge button, and the page never says
 * otherwise.
 *
 * Saving: the On/Off switch and each script save on their own, the moment
 * they are used; everything else collects in the save bar. A partial save
 * never throws away an edit still in progress (rebaseForm).
 */
export default function MergeChecksPanel({ service, canEdit, canView = true, onDirtyChange, onGoToTab, onOpenBuild }) {
  const { route, getRoute, navigate } = useRouter();
  const [config, setConfig] = useState(null);
  const [form, setForm] = useState(null);
  const [saved, setSaved] = useState(null);
  const [runs, setRuns] = useState([]);
  const [runsLoaded, setRunsLoaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [secret, setSecret] = useState("");
  const [busyCheckId, setBusyCheckId] = useState(null);
  // Unsaved script text, per tool. A script saves on its own button — it is
  // long enough that tying it to the rest of the form would lose work.
  const [drafts, setDrafts] = useState({});
  const [enforcement, setEnforcement] = useState(null);
  const [webhook, setWebhook] = useState(null);
  const [settingUp, setSettingUp] = useState(false);
  // What the setup button also does in Bitbucket. Protecting the branch is the
  // point of the feature, so it is on; blocking direct pushes changes how a
  // whole team works, so it is a deliberate tick.
  const [protectBranches, setProtectBranches] = useState(true);
  const [blockDirectPush, setBlockDirectPush] = useState(false);

  const section = SECTIONS.includes(route.query?.section) ? route.query.section : "checks";
  const setSection = (next) => {
    const active = getRoute();
    const query = { ...active.query };
    if (next === "checks") delete query.section;
    else query.section = next;
    navigate({ key: active.key, params: active.params, query }, { replace: true });
  };

  const load = useCallback(async () => {
    try {
      const data = await getServiceMergeChecks(service.id);
      const next = toForm(data);
      setConfig(data);
      setForm(next);
      setSaved(next);
      setDrafts({});
      setError("");
    } catch (err) {
      setError(err.message || "Could not load the merge check configuration.");
    } finally {
      setLoading(false);
    }
  }, [service.id]);

  const loadRuns = useCallback(async () => {
    try {
      const data = await listMergeChecks(service.id, { limit: 25 });
      setRuns(data.items || []);
    } catch {
      /* The list is secondary; the configuration still works without it. */
    } finally {
      setRunsLoaded(true);
    }
  }, [service.id]);

  const loadEnforcement = useCallback(async () => {
    try {
      setEnforcement(await getMergeCheckEnforcement(service.id));
    } catch {
      // "We could not ask Bitbucket" is itself an answer the status renders.
      setEnforcement({ known: false, enforced: false, reason: "" });
    }
  }, [service.id]);

  const loadWebhook = useCallback(async () => {
    try {
      setWebhook(await getMergeCheckWebhook(service.id));
    } catch {
      setWebhook({ known: false, exists: false, reason: "" });
    }
  }, [service.id]);

  useEffect(() => {
    load();
    loadRuns();
    loadEnforcement();
    loadWebhook();
  }, [load, loadRuns, loadEnforcement, loadWebhook]);

  // A running check settles within seconds of its build ending, so the list
  // refreshes itself while anything is in flight and stops when nothing is.
  const hasRunning = runs.some((run) => run.state === "queued" || run.state === "running");
  useEffect(() => {
    if (!hasRunning) return undefined;
    const timer = setInterval(loadRuns, 5000);
    return () => clearInterval(timer);
  }, [hasRunning, loadRuns]);

  const changed = useMemo(() => dirtyKeys(form, saved), [form, saved]);
  const unsavedScripts = Object.keys(drafts).filter((tool) => {
    const script = (config?.checkScripts || []).find((item) => item.tool === tool);
    return script && drafts[tool] !== (script.commands || []).join("\n");
  });
  const dirty = changed.length > 0 || unsavedScripts.length > 0;

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => {
    if (!dirty) return undefined;
    const warn = (event) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  // Leaving the tab takes its own query key with it.
  useEffect(
    () => () => {
      const active = getRoute();
      if (active.key === "serviceDetail" && active.query?.section) {
        const { section: _section, ...rest } = active.query;
        navigate({ key: active.key, params: active.params, query: rest }, { replace: true });
      }
    },
    [getRoute, navigate]
  );

  useEffect(() => {
    if (!notice) return undefined;
    const timer = window.setTimeout(() => setNotice(""), 7000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));
  const toggleIn = (key, value) =>
    setForm((prev) => {
      const current = new Set(prev[key] || []);
      if (current.has(value)) current.delete(value);
      else current.add(value);
      return { ...prev, [key]: [...current] };
    });

  /**
   * Send a form to the server. `partial` saves (the switch, a script) are sent
   * from the SAVED copy plus their own change, so an edit still in the save
   * bar is neither sent early nor lost afterwards.
   */
  const send = async (base, overrides = {}, { partial = false, message = "Saved." } = {}) => {
    setSaving(true);
    setError("");
    try {
      const data = await saveServiceMergeChecks(service.id, { ...base, ...overrides });
      const next = toForm(data);
      setConfig(data);
      setForm((current) => (partial ? rebaseForm(current, saved, next) : next));
      setSaved(next);
      if (message) setNotice(message);
      // A changed event list shows up as "out of sync" in the status card.
      loadWebhook();
      return true;
    } catch (err) {
      setError(err.message || "Could not save the merge check configuration.");
      return false;
    } finally {
      setSaving(false);
    }
  };

  const save = () => {
    if (!changed.length || saving) return;
    send(form, {}, { message: "Merge checks saved. The next pull request uses them." });
  };

  const saveRef = useRef(save);
  saveRef.current = save;
  useEffect(() => {
    const onKey = (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        saveRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const discard = () => {
    setForm(saved);
    setDrafts({});
    setError("");
    setNotice("Changes discarded.");
  };

  const setEnabled = (enabled) =>
    send(saved, { enabled }, {
      partial: true,
      message: enabled
        ? "Merge checks are on. The next pull request into a watched branch is checked."
        : "Merge checks are off. Nothing is checked or reported.",
    });

  const saveScript = async (tool, text) => {
    const ok = await send(saved, { customCommands: { [tool]: text } }, {
      partial: true,
      message: "Script saved. It replaces the generated one from the next pull request.",
    });
    if (ok) setDrafts((prev) => {
      const next = { ...prev };
      delete next[tool];
      return next;
    });
  };

  // An empty override is how the backend is told "use the generated script";
  // it never stores an empty script, because a stage with no commands fails
  // validation rather than falling back.
  const resetScript = async (tool) => {
    const ok = await send(saved, { customCommands: { [tool]: [] } }, {
      partial: true,
      message: "The generated script is back.",
    });
    if (ok) setDrafts((prev) => {
      const next = { ...prev };
      delete next[tool];
      return next;
    });
  };

  // Webhook + secret + pipeline (+ branch protection), done by KubeSight with
  // the service's own credential. Idempotent, so it doubles as "resync".
  const setupInSource = async ({ quiet = false } = {}) => {
    setSettingUp(true);
    setError("");
    if (!quiet) setNotice("");
    try {
      const data = await setupMergeChecksInSource(service.id, { protectBranches, blockDirectPush });
      const next = toForm(data.config);
      setConfig(data.config);
      setForm((current) => rebaseForm(current, saved, next));
      setSaved(next);
      const parts = [
        data.webhook.action === "created"
          ? `Webhook created in ${data.webhook.repository}`
          : `Webhook in ${data.webhook.repository} updated`,
        "with the secret",
      ];
      if (data.pipeline.action === "created") parts.push("and the pipeline generated");
      const protection = data.protection || {};
      const protectedList = (protection.branches || []).map((row) => row.branch);
      let message = `${parts.join(" ")}.`;
      if (protection.ok) {
        message += ` A pull request into ${protectedList.join(", ")} can no longer merge until KubeSight passes it`;
        message += blockDirectPush ? ", and direct pushes are blocked." : ".";
        if (!protection.hardBlock) {
          message +=
            " This Bitbucket plan does not enforce merge checks, so a blocked pull request shows as failing but can still be merged.";
        }
      }
      if (!quiet) setNotice(`${message}${next.enabled ? "" : " Switch merge checks on when you are ready."}`);
      if (protection.ok === false) {
        setError(`The webhook is set up, but the branch protection was not: ${protection.error}`);
      }
    } catch (err) {
      setError(err.message || "KubeSight could not set this up in Bitbucket.");
    } finally {
      setSettingUp(false);
      loadWebhook();
      loadEnforcement();
    }
  };

  const reveal = async () => {
    try {
      const data = await revealMergeCheckSecret(service.id);
      setSecret(data.secret);
    } catch (err) {
      setError(err.message || "Could not read the webhook secret.");
    }
  };

  const rotate = async () => {
    if (
      !window.confirm(
        "Generate a new secret? The current one stops working immediately, and Bitbucket " +
          "is refused until the webhook has the new one."
      )
    ) {
      return;
    }
    try {
      const data = await rotateMergeCheckSecret(service.id);
      setSecret(data.secret);
      setNotice(data.message);
      // A webhook that already exists would start failing now; hand it the new
      // secret straight away rather than leaving that to be remembered.
      if (webhook?.exists) setupInSource({ quiet: true });
    } catch (err) {
      setError(err.message || "Could not rotate the webhook secret.");
    }
  };

  const copy = (text, what) => {
    navigator.clipboard?.writeText(text).then(
      () => setNotice(`${what} copied.`),
      () => setNotice(`Select and copy the ${what.toLowerCase()} by hand.`)
    );
  };

  const redeliver = async (checkId) => {
    setBusyCheckId(checkId);
    setError("");
    try {
      await redeliverMergeCheck(checkId);
      setNotice("The verdict was sent again.");
      loadRuns();
    } catch (err) {
      setError(err.message || "The verdict could not be delivered.");
    } finally {
      setBusyCheckId(null);
    }
  };

  if (!canView) return null;
  if (loading || !form) return <LoadingState label="Loading merge checks…" />;

  const active = activeTools(config, form);
  const branches = watchedBranches(config, service);
  const status = readiness({ config, webhook, enforcement, enabled: saved.enabled });
  const gateOpen = gateNeverBlocks(config.effectiveGate, active);
  const setupDisabled = !config.sourceReady || !config.canReportVerdict?.ok;
  const blocked = runs.filter((run) => run.verdict === "blocked").length;
  const environments = config.unconfiguredEnvironments || [];

  const tabs = [
    { key: "checks", icon: "shield", label: "Checks", count: active.length },
    { key: "gate", icon: "gauge", label: "Quality gate" },
    { key: "connection", icon: "link", label: "Connection" },
    { key: "history", icon: "history", label: "History", count: runs.length, alert: blocked > 0 },
  ];
  const tabDirty = {
    checks: changed.some((key) => key === "tools" || key === "toolsMode") || unsavedScripts.length > 0,
    gate: changed.some((key) => key === "gateMode" || key.startsWith("max") || key.endsWith("Severity") || key === "eslintCountWarnings" || key === "blockOnToolError"),
    connection: changed.some((key) => ["events", "targetBranches", "statusKey", "postComment"].includes(key)),
  };

  return (
    <div className={`pl-root mc-root${dirty ? " is-dirty" : ""}`}>
      {/* ── Identity + the master switch ──────────────────────────────── */}
      <header className="pl-top">
        <div className="pl-top-id">
          <span className="pl-top-glyph" aria-hidden="true">
            <PlIcon name="pullRequest" />
          </span>
          <div>
            <h3>
              Merge checks
              {!canEdit && (
                <span className="pl-tag">
                  <PlIcon name="lock" /> View only
                </span>
              )}
            </h3>
            <p className="pl-top-sentence">
              {saved.enabled ? (
                <>
                  Every pull request into <b>{branches.join(", ")}</b> runs <b>{active.length}</b>{" "}
                  {active.length === 1 ? "check" : "checks"} on its latest commit, and the verdict goes
                  back to Bitbucket as the <code>{saved.statusKey}</code> build status.
                </>
              ) : (
                <>
                  Off — pull requests are not checked. When on, each pull request into{" "}
                  <b>{branches.join(", ")}</b> runs <b>{active.length}</b>{" "}
                  {active.length === 1 ? "check" : "checks"} and Bitbucket gets the verdict.
                </>
              )}
              {config.lastEventAt && (
                <span className="pl-top-meta">Last pull request {new Date(config.lastEventAt).toLocaleString()}</span>
              )}
            </p>
          </div>
        </div>
        <div className="mc-master">
          <Switch
            checked={saved.enabled}
            disabled={!canEdit || saving}
            label={saved.enabled ? "Checking pull requests" : "Off"}
            onChange={setEnabled}
          />
        </div>
      </header>

      {error && (
        <div className="pl-banner is-error" role="alert">
          <PlIcon name="alert" />
          <p>{error}</p>
          <button type="button" className="btn-ghost pl-banner-close" aria-label="Dismiss" onClick={() => setError("")}>
            <PlIcon name="x" />
          </button>
        </div>
      )}

      {environments.map((item) => (
        <div key={item.environment} className="pl-banner" role="status">
          <PlIcon name="alert" />
          <p>
            No build image is configured for {item.label}. Set the <code>{item.environment}</code> image
            before relying on this check.
          </p>
        </div>
      ))}

      <GateStatus
        status={status}
        branches={branches}
        gateOpen={gateOpen}
        canEdit={canEdit}
        setupBusy={settingUp}
        setupDisabled={setupDisabled}
        protectBranches={protectBranches}
        onProtectBranches={setProtectBranches}
        blockDirectPush={blockDirectPush}
        onBlockDirectPush={setBlockDirectPush}
        onSetup={() => setupInSource()}
        onEnable={() => setEnabled(true)}
        onGoToTab={onGoToTab}
      />

      <div className="pl-panel mc-panel">
        <div className="mc-tabs" role="tablist" aria-label="Merge check sections">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              type="button"
              role="tab"
              aria-selected={section === tab.key}
              className={`btn-ghost mc-tab${section === tab.key ? " is-on" : ""}`}
              onClick={() => setSection(tab.key)}
            >
              <PlIcon name={tab.icon} />
              {tab.label}
              {tab.count !== undefined && <span className="pl-count">{tab.count}</span>}
              {tab.alert && <span className="pl-dot is-error" aria-label="has blocked pull requests" />}
              {tabDirty[tab.key] && <span className="pl-dot" aria-label="unsaved changes" />}
            </button>
          ))}
        </div>

        <div className="mc-panel-body" role="tabpanel">
          {section === "checks" && (
            <ChecksView
              config={config}
              form={form}
              active={active}
              gate={config.effectiveGate}
              canEdit={canEdit}
              saving={saving}
              drafts={drafts}
              onDraft={(tool, text) => setDrafts((prev) => ({ ...prev, [tool]: text }))}
              onSaveScript={saveScript}
              onResetScript={resetScript}
              onMode={(mode) =>
                setForm((prev) => ({
                  ...prev,
                  toolsMode: mode,
                  // Switching to "choose myself" starts from what automatic
                  // picked, rather than from an empty list.
                  tools: mode === "custom" && prev.toolsMode !== "custom" ? [...(config.recommendedTools || [])] : prev.tools,
                }))
              }
              onToggleTool={(tool) => toggleIn("tools", tool)}
            />
          )}
          {section === "gate" && (
            <GateView
              config={config}
              form={form}
              active={active}
              canEdit={canEdit}
              onMode={(mode) => set("gateMode", mode)}
              onField={(key, value) => set(key, value)}
            />
          )}
          {section === "connection" && (
            <ConnectionView
              config={config}
              form={form}
              webhook={webhook}
              service={service}
              canEdit={canEdit}
              secret={secret}
              onReveal={reveal}
              onRotate={rotate}
              onCopy={copy}
              onField={set}
              onToggleEvent={(value) => toggleIn("events", value)}
            />
          )}
          {section === "history" && (
            <HistoryView
              runs={runs}
              loading={!runsLoaded}
              canEdit={canEdit}
              busyCheckId={busyCheckId}
              onRedeliver={redeliver}
              onOpenBuild={(id) => onOpenBuild?.(String(id))}
            />
          )}
        </div>
      </div>

      {/* ── Dock: toast + save bar ────────────────────────────────────── */}
      <div className="pl-dock">
        {notice && (
          <div className="pl-toast" role="status">
            <PlIcon name="check" />
            <span>{notice}</span>
            <button type="button" className="btn-ghost" aria-label="Dismiss" onClick={() => setNotice("")}>
              <PlIcon name="x" />
            </button>
          </div>
        )}
        {canEdit && changed.length > 0 && (
          <div className="pl-savebar" role="region" aria-label="Unsaved merge check changes">
            <span className="pl-savebar-dot" aria-hidden="true" />
            <div className="pl-savebar-text">
              <strong>{saving ? "Saving…" : "Unsaved changes"}</strong>
              <span>
                {[tabDirty.checks && "checks", tabDirty.gate && "quality gate", tabDirty.connection && "connection"]
                  .filter(Boolean)
                  .join(", ") || "Ready to save"}
                {unsavedScripts.length > 0 && " · scripts save on their own button"}
              </span>
            </div>
            <button type="button" className="btn-outline btn-compact" onClick={discard} disabled={saving}>
              Discard
            </button>
            <button type="button" className="primary btn-compact" onClick={save} disabled={saving}>
              <PlIcon name="check" />
              {saving ? "Saving…" : "Save"}
              <kbd>Ctrl S</kbd>
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
