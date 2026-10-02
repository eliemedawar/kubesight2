import { useEffect, useMemo, useRef, useState } from "react";
import "../../styles/signal/pipelineWorkspace.css";
import "../../styles/signal/serviceSettings.css";
import { deleteCiService, updateCiService } from "../../api/ciApi.js";
import { listRegistries } from "../../api/registriesApi.js";
import { pageHref } from "../../routes/RouterContext.jsx";
import { CRITICALITIES } from "./ciShared.jsx";
import { Field, Segmented } from "./pipeline/controls.jsx";
import { PlIcon } from "./pipeline/icons.jsx";
import SchedulesSection from "./serviceSettings/SchedulesSection.jsx";
import SecretsSection from "./serviceSettings/SecretsSection.jsx";
import { railSummary } from "./serviceSettings/scheduleModel.js";

// What a build stage of this service may use. Each row is three-way: inherit the
// installation default, name a value, or take the limit off entirely. Ephemeral
// storage ships open — no limit, no request — so "Default" and "No limit" read
// the same out of the box, and the choice still says which one was CHOSEN,
// because an installation that later sets a cap should apply to the first and
// not to the second.
const RESOURCE_FIELDS = [
  {
    key: "cpu",
    label: "CPU",
    unit: "cores",
    placeholder: "2",
    presets: ["500m", "1", "2", "4"],
    hint: "Cores per stage container. 500m is half a core.",
  },
  {
    key: "memory",
    label: "Memory",
    unit: "",
    placeholder: "4Gi",
    presets: ["2Gi", "4Gi", "8Gi", "16Gi"],
    hint: "A stage that goes over is OOM-killed and the build fails there.",
  },
  {
    key: "ephemeralStorage",
    label: "Disk (ephemeral storage)",
    unit: "",
    placeholder: "8Gi",
    presets: ["8Gi", "16Gi", "32Gi", "64Gi"],
    hint:
      "The checkout, build output and the shared /workspace. With a limit, kubelet evicts the pod at the ceiling and the build stops without saying why.",
  },
];

const OFF_WORDS = new Set(["off", "none", "no", "0", "false", "unlimited"]);

const resourceMode = (value) => {
  const text = String(value ?? "").trim();
  if (!text) return "default";
  return OFF_WORDS.has(text.toLowerCase()) ? "off" : "custom";
};

// "Default" names the number it resolves to, as the API reports it, so an
// installation that sets CI_STAGE_MEMORY_LIMIT sees its own value here.
const defaultLabel = (value) =>
  !value || OFF_WORDS.has(String(value).toLowerCase()) ? "no limit" : value;

const STATUSES = [
  { value: "active", label: "Active", hint: "Builds run" },
  { value: "paused", label: "Paused", hint: "Builds refused, nothing lost" },
  { value: "archived", label: "Archived", hint: "Retired" },
];

const toForm = (service) => ({
  status: service.status || "active",
  criticality: service.criticality || "medium",
  ownerTeam: service.ownerTeam || "",
  maxConcurrentBuilds: Number(service.maxConcurrentBuilds) || 1,
  registryConnectionId: service.registryConnectionId ? String(service.registryConnectionId) : "",
  buildResources: { ...(service.buildResources || {}) },
});

const modesFor = (resources) =>
  Object.fromEntries(RESOURCE_FIELDS.map((field) => [field.key, resourceMode((resources || {})[field.key])]));

const SECTIONS = [
  { id: "st-general", label: "General", icon: "sparkle" },
  { id: "st-builds", label: "Builds", icon: "server" },
  { id: "st-schedules", label: "Schedules", icon: "clock" },
  { id: "st-registry", label: "Image registry", icon: "image" },
  { id: "st-secrets", label: "Secrets", icon: "key" },
  { id: "st-danger", label: "Danger zone", icon: "trash" },
];

/**
 * Settings tab: how this service's builds behave, what they may use, where
 * their images go, the secrets they read, and deleting it.
 *
 * Two kinds of change, and the page says which is which: the service's own
 * settings collect in the save bar and are sent together; secrets are records
 * of their own and save the moment they are added, replaced or removed.
 *
 * The registry is where container_image stages push. Without one those stages
 * skip with an explanation rather than pretending to have built something, and
 * that explanation points here — so the field has to live here.
 */
export default function ServiceSettingsPanel({
  service,
  expectedSecrets = [],
  onSaved,
  onDeleted,
  onDirtyChange,
  canEdit,
  canDelete,
  canViewSecrets,
  canManageSecrets,
  // Schedules: listed with ci_builds:view, changed with ci_pipelines:edit AND
  // ci_builds:run (the API requires both), run now with ci_builds:run.
  canViewSchedules = false,
  canEditSchedules = false,
  canRunSchedules = false,
  onOpenBuild,
}) {
  const [saved, setSaved] = useState(() => toForm(service));
  const [form, setForm] = useState(() => toForm(service));
  // Which of the three the user picked, kept beside the value because the
  // value alone cannot say it: an empty box under Custom and an untouched
  // field both send nothing, and choosing Custom has to leave the box there to
  // type in rather than snapping back to Default.
  const [modes, setModes] = useState(() => modesFor(service.buildResources));
  const [registries, setRegistries] = useState(null);
  const [secretCount, setSecretCount] = useState(null);
  const [schedules, setSchedules] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmText, setConfirmText] = useState("");
  const [deleting, setDeleting] = useState(false);
  const resourceDefaults = service.buildResourceDefaults || {};

  // Best effort: a user who cannot list registries still sees the choice,
  // with whatever is already linked preserved on save.
  useEffect(() => {
    listRegistries()
      .then((data) => setRegistries(data.items || []))
      .catch(() => setRegistries([]));
  }, []);

  useEffect(() => {
    if (!notice) return undefined;
    const timer = window.setTimeout(() => setNotice(""), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const changed = useMemo(() => {
    const keys = [];
    for (const key of ["status", "criticality", "ownerTeam", "registryConnectionId"]) {
      if (String(form[key] ?? "") !== String(saved[key] ?? "")) keys.push(key);
    }
    if (Number(form.maxConcurrentBuilds) !== Number(saved.maxConcurrentBuilds)) keys.push("maxConcurrentBuilds");
    const normal = (resources) =>
      JSON.stringify(
        Object.fromEntries(
          Object.entries(resources || {})
            .filter(([, value]) => String(value ?? "").trim() !== "")
            .sort()
        )
      );
    if (normal(form.buildResources) !== normal(saved.buildResources)) keys.push("buildResources");
    return keys;
  }, [form, saved]);
  const dirty = changed.length > 0;

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

  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  const setResource = (key, value) =>
    setForm((prev) => {
      const next = { ...prev.buildResources };
      // Absent, not empty: an absent key is what tells the backend to fall back
      // to the installation default for this field only.
      if (value === null) delete next[key];
      else next[key] = value;
      return { ...prev, buildResources: next };
    });

  const setMode = (key, mode) => {
    setModes((prev) => ({ ...prev, [key]: mode }));
    if (mode === "default") setResource(key, null);
    else if (mode === "off") setResource(key, "off");
    else setResource(key, resourceMode(form.buildResources[key]) === "custom" ? form.buildResources[key] : "");
  };

  const concurrencyError =
    !Number.isInteger(Number(form.maxConcurrentBuilds)) ||
    Number(form.maxConcurrentBuilds) < 1 ||
    Number(form.maxConcurrentBuilds) > 20
      ? "Between 1 and 20."
      : "";

  const save = async () => {
    if (!dirty || saving || concurrencyError) return;
    setSaving(true);
    setError("");
    try {
      const updated = await updateCiService(service.id, {
        ...form,
        maxConcurrentBuilds: Number(form.maxConcurrentBuilds),
        registryConnectionId: form.registryConnectionId || null,
      });
      // What came back is the truth, and it can differ from what was typed:
      // Custom with an empty box stores nothing, which IS Default. Re-derive
      // from the response so the page never claims a setting the service
      // does not have.
      const next = toForm(updated);
      setSaved(next);
      setForm(next);
      setModes(modesFor(updated.buildResources));
      setNotice("Settings saved. They apply from the next build, a retry included.");
      onSaved(updated);
    } catch (err) {
      setError(err.message || "Could not save settings.");
    } finally {
      setSaving(false);
    }
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
    setModes(modesFor(saved.buildResources));
    setNotice("Changes discarded.");
  };

  const removeService = async () => {
    setDeleting(true);
    try {
      await deleteCiService(service.id);
      onDeleted();
    } catch (err) {
      setError(err.message || "Could not delete the service.");
      setDeleting(false);
    }
  };

  const registry = (registries || []).find((item) => String(item.id) === String(form.registryConnectionId));
  const registryHost = registry?.baseUrl ? registry.baseUrl.replace(/^https?:\/\//, "").replace(/\/+$/, "") : "";
  const resourceSummary = RESOURCE_FIELDS.slice(0, 2)
    .map((field) => {
      const mode = modes[field.key];
      const value =
        mode === "default"
          ? defaultLabel(resourceDefaults[field.key])
          : mode === "off"
            ? "no limit"
            : form.buildResources[field.key] || "—";
      return field.key === "cpu" && value !== "no limit" ? `${value} CPU` : value;
    })
    .join(" / ");

  const railState = {
    "st-general": STATUSES.find((item) => item.value === form.status)?.label,
    "st-builds": `${form.maxConcurrentBuilds} at a time`,
    "st-registry": form.registryConnectionId ? registry?.name || "Linked" : "None",
    "st-schedules": railSummary(schedules).text,
    "st-secrets": secretCount === null ? "" : `${secretCount.length}`,
    "st-danger": "",
  };
  const railTone = {
    "st-general": form.status === "active" ? "" : "warn",
    "st-registry": form.registryConnectionId ? "" : "warn",
    "st-schedules": railSummary(schedules).tone,
  };
  const sectionDirty = {
    "st-general": changed.some((key) => ["status", "criticality", "ownerTeam"].includes(key)),
    "st-builds": changed.some((key) => ["maxConcurrentBuilds", "buildResources"].includes(key)),
    "st-registry": changed.includes("registryConnectionId"),
  };
  const sections = SECTIONS.filter(
    (item) =>
      (item.id !== "st-secrets" || canViewSecrets) &&
      (item.id !== "st-schedules" || canViewSchedules) &&
      (item.id !== "st-danger" || canDelete)
  );

  return (
    <div className={`pl-root st-root${dirty ? " is-dirty" : ""}`}>
      <header className="pl-top">
        <div className="pl-top-id">
          <span className="pl-top-glyph" aria-hidden="true">
            <PlIcon name="server" />
          </span>
          <div>
            <h3>
              Service settings
              {!canEdit && (
                <span className="pl-tag">
                  <PlIcon name="lock" /> View only
                </span>
              )}
            </h3>
            <p className="pl-top-sentence">
              <b>{STATUSES.find((item) => item.value === saved.status)?.label}</b>, {saved.criticality} criticality
              {saved.ownerTeam ? <>, owned by <b>{saved.ownerTeam}</b></> : ", no owner set"}. Up to{" "}
              <b>{saved.maxConcurrentBuilds}</b> {Number(saved.maxConcurrentBuilds) === 1 ? "build" : "builds"} at once,{" "}
              {resourceSummary} per stage; images{" "}
              {saved.registryConnectionId ? (
                <>
                  push to <b>{registry?.name || "the linked registry"}</b>
                </>
              ) : (
                <b>have nowhere to go</b>
              )}
              .
            </p>
          </div>
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

      <div className="st-layout">
        <nav className="st-rail" aria-label="Settings sections">
          {sections.map((item) => (
            <a
              key={item.id}
              href={`#${item.id}`}
              className={`st-rail-link${item.id === "st-danger" ? " is-danger" : ""}`}
              onClick={(event) => {
                event.preventDefault();
                document.getElementById(item.id)?.scrollIntoView({ behavior: "smooth", block: "start" });
              }}
            >
              <PlIcon name={item.icon} />
              <span>{item.label}</span>
              {sectionDirty[item.id] && <span className="pl-dot" aria-label="unsaved changes" />}
              {railState[item.id] && (
                <small className={railTone[item.id] ? `is-${railTone[item.id]}` : ""}>{railState[item.id]}</small>
              )}
            </a>
          ))}
          <p className="st-rail-note">
            <PlIcon name="lock" />
            Secrets and schedules save on their own, as soon as they are added or changed. Everything else waits for Save.
          </p>
        </nav>

        <div className="st-sections">
          {/* ── General ─────────────────────────────────────────────── */}
          <section className="pl-panel st-card" id="st-general" aria-labelledby="st-general-title">
            <header className="st-card-head">
              <h4 id="st-general-title">General</h4>
              <p>Whether the service takes builds, and who it belongs to.</p>
            </header>
            <div className="st-card-body">
              <Field
                label="Status"
                hint={
                  form.status === "active"
                    ? "Builds, retries and merge checks run as usual."
                    : form.status === "paused"
                      ? "Run build is refused while paused. Pipelines, secrets and history are kept."
                      : "Archived services take no builds and drop out of day-to-day lists. History is kept."
                }
              >
                <Segmented
                  label="Status"
                  value={form.status}
                  disabled={!canEdit}
                  options={STATUSES}
                  onChange={(value) => set("status", value)}
                />
              </Field>
              <div className="pl-grid">
                <Field label="Criticality" hint="How much a broken build of this service matters — shown on the catalog.">
                  <Segmented
                    label="Criticality"
                    value={form.criticality}
                    disabled={!canEdit}
                    options={CRITICALITIES.map((value) => ({
                      value,
                      label: value.charAt(0).toUpperCase() + value.slice(1),
                    }))}
                    onChange={(value) => set("criticality", value)}
                  />
                </Field>
                <Field label="Owner / team" htmlFor="st-owner" optional hint="Who to ask when a build of this service breaks.">
                  <input
                    id="st-owner"
                    value={form.ownerTeam}
                    placeholder="Payments squad"
                    disabled={!canEdit}
                    onChange={(event) => set("ownerTeam", event.target.value)}
                  />
                </Field>
              </div>
            </div>
          </section>

          {/* ── Builds ──────────────────────────────────────────────── */}
          <section className="pl-panel st-card" id="st-builds" aria-labelledby="st-builds-title">
            <header className="st-card-head">
              <h4 id="st-builds-title">Builds</h4>
              <p>
                How many run at once, and what every stage may use on the node. A single stage can still
                set its own on the Pipeline tab.
              </p>
            </header>
            <div className="st-card-body">
              <Field
                label="Builds at the same time"
                error={concurrencyError}
                hint={`Up to ${form.maxConcurrentBuilds || "?"} ${Number(form.maxConcurrentBuilds) === 1 ? "build" : "builds"} of this service run at once; more wait in the queue.`}
              >
                <div className="st-stepper">
                  <button
                    type="button"
                    className="btn-ghost"
                    aria-label="Fewer"
                    disabled={!canEdit || Number(form.maxConcurrentBuilds) <= 1}
                    onClick={() => set("maxConcurrentBuilds", Math.max(1, Number(form.maxConcurrentBuilds) - 1))}
                  >
                    −
                  </button>
                  <input
                    type="number"
                    min={1}
                    max={20}
                    aria-label="Builds at the same time"
                    value={form.maxConcurrentBuilds}
                    disabled={!canEdit}
                    onChange={(event) => set("maxConcurrentBuilds", event.target.value)}
                  />
                  <button
                    type="button"
                    className="btn-ghost"
                    aria-label="More"
                    disabled={!canEdit || Number(form.maxConcurrentBuilds) >= 20}
                    onClick={() => set("maxConcurrentBuilds", Math.min(20, Number(form.maxConcurrentBuilds) + 1))}
                  >
                    +
                  </button>
                </div>
              </Field>

              <div className="st-resources">
                {RESOURCE_FIELDS.map((field) => {
                  const mode = modes[field.key];
                  const value = form.buildResources[field.key] || "";
                  return (
                    <div key={field.key} className={`st-resource is-${mode}`}>
                      <div className="st-resource-label">
                        <strong>{field.label}</strong>
                        <small>{field.hint}</small>
                      </div>
                      <div className="st-resource-control">
                        <Segmented
                          label={field.label}
                          value={mode}
                          disabled={!canEdit}
                          options={[
                            { value: "default", label: `Default · ${defaultLabel(resourceDefaults[field.key])}` },
                            { value: "custom", label: "Custom" },
                            { value: "off", label: "No limit" },
                          ]}
                          onChange={(next) => setMode(field.key, next)}
                        />
                        {mode === "custom" && (
                          <div className="st-resource-custom">
                            <input
                              className="is-mono"
                              value={value}
                              placeholder={field.placeholder}
                              disabled={!canEdit}
                              aria-label={`${field.label} limit`}
                              onChange={(event) => setResource(field.key, event.target.value)}
                              autoFocus={!value}
                            />
                            <div className="pl-presets" role="group" aria-label={`Common ${field.label} limits`}>
                              {field.presets.map((preset) => (
                                <button
                                  key={preset}
                                  type="button"
                                  className={`btn-ghost${value === preset ? " is-on" : ""}`}
                                  disabled={!canEdit}
                                  onClick={() => setResource(field.key, preset)}
                                >
                                  {preset}
                                </button>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
              {modes.ephemeralStorage === "custom" && (
                <p className="pl-note">
                  <PlIcon name="alert" />
                  <span>
                    A disk limit also raises the shared <code>/workspace</code> ceiling to match, so a build is
                    not evicted below what you asked for, and a small request is added with it — without one,
                    Kubernetes would want the full size free on a node before scheduling the build.
                  </span>
                </p>
              )}
            </div>
          </section>

          {/* ── Schedules ───────────────────────────────────────────── */}
          {canViewSchedules && (
            <section className="pl-panel st-card" id="st-schedules" aria-labelledby="st-schedules-title">
              <header className="st-card-head">
                <h4 id="st-schedules-title">Schedules</h4>
                <p>
                  Builds that start on their own — nightly, every weekday morning, once a week — at a cron time
                  in the timezone you choose. Each one is an ordinary build, shown in Builds with its schedule's
                  name.
                </p>
              </header>
              <div className="st-card-body">
                <SchedulesSection
                  service={service}
                  canEdit={canEditSchedules}
                  canRun={canRunSchedules && service.status === "active"}
                  onError={setError}
                  onNotice={setNotice}
                  onSummary={setSchedules}
                  onOpenBuild={onOpenBuild}
                />
              </div>
            </section>
          )}

          {/* ── Image registry ──────────────────────────────────────── */}
          <section className="pl-panel st-card" id="st-registry" aria-labelledby="st-registry-title">
            <header className="st-card-head">
              <h4 id="st-registry-title">Image registry</h4>
              <p>Where the Build an image stages push. Without one they are skipped, and the build says why.</p>
            </header>
            <div className="st-card-body">
              {registries === null ? (
                <p className="pl-field-hint">Loading registries…</p>
              ) : (
                <div className="st-registries" role="radiogroup" aria-label="Image registry">
                  {[{ id: "", name: "No registry", baseUrl: "", none: true }, ...registries].map((item) => {
                    const on = String(item.id) === String(form.registryConnectionId || "");
                    return (
                      <label key={item.id || "none"} className={`st-registry${on ? " is-on" : ""}${item.none ? " is-none" : ""}`}>
                        <input
                          type="radio"
                          name="st-registry"
                          checked={on}
                          disabled={!canEdit}
                          onChange={() => set("registryConnectionId", item.id ? String(item.id) : "")}
                        />
                        <span className="st-registry-icon" aria-hidden="true">
                          <PlIcon name={item.none ? "x" : "image"} />
                        </span>
                        <span className="st-registry-copy">
                          <strong>{item.name}</strong>
                          <small>
                            {item.none
                              ? "Image stages are skipped"
                              : [item.baseUrl, item.enabled === false ? "disabled" : ""].filter(Boolean).join(" · ")}
                          </small>
                        </span>
                      </label>
                    );
                  })}
                </div>
              )}
              {registries && registries.length === 0 && (
                <p className="pl-note">
                  <PlIcon name="image" />
                  <span>
                    No registries are connected to KubeSight yet.{" "}
                    <a className="pl-link" href={pageHref("imageRegistries")}>
                      Connect one on Image Registries
                    </a>{" "}
                    and it appears here.
                  </span>
                </p>
              )}
              <div className="st-imageref">
                <span>An image built by this service is pushed as</span>
                <code>
                  {registryHost || "<no registry>"}/{service.slug}:&lt;branch&gt;-&lt;build no.&gt;
                </code>
                <small>A stage's own image name or tag, set on the Pipeline tab, replaces the last two parts.</small>
              </div>
            </div>
          </section>

          {/* ── Secrets ─────────────────────────────────────────────── */}
          {canViewSecrets && (
            <section className="pl-panel st-card" id="st-secrets" aria-labelledby="st-secrets-title">
              <header className="st-card-head">
                <h4 id="st-secrets-title">Secrets</h4>
                <p>
                  Attached to stages by name on the Pipeline tab and injected as environment variables. One
                  of this service's own wins over a global secret with the same name.
                </p>
              </header>
              <div className="st-card-body">
                <SecretsSection
                  service={service}
                  expectedSecrets={expectedSecrets}
                  canManage={canManageSecrets}
                  onError={setError}
                  onNotice={setNotice}
                  onCount={setSecretCount}
                />
              </div>
            </section>
          )}

          {/* ── Danger zone ─────────────────────────────────────────── */}
          {canDelete && (
            <section className="pl-panel st-card st-danger" id="st-danger" aria-labelledby="st-danger-title">
              <header className="st-card-head">
                <h4 id="st-danger-title">Danger zone</h4>
                <p>
                  Deleting removes the service with its pipelines, builds, logs and artifact records. Stored
                  artifact files are left in place. This cannot be undone.
                </p>
              </header>
              <div className="st-card-body">
                <Field
                  label={
                    <>
                      Type <code>{service.slug}</code> to confirm
                    </>
                  }
                  htmlFor="st-delete-confirm"
                >
                  <div className="mc-copyrow st-delete-row">
                    <input
                      id="st-delete-confirm"
                      className="is-mono"
                      value={confirmText}
                      autoComplete="off"
                      spellCheck={false}
                      onChange={(event) => setConfirmText(event.target.value)}
                    />
                    <button
                      type="button"
                      className="btn-outline danger"
                      disabled={confirmText.trim() !== service.slug || deleting}
                      onClick={removeService}
                    >
                      <PlIcon name="trash" /> {deleting ? "Deleting…" : "Delete this service"}
                    </button>
                  </div>
                </Field>
              </div>
            </section>
          )}
        </div>
      </div>

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
        {canEdit && dirty && (
          <div className="pl-savebar" role="region" aria-label="Unsaved settings">
            <span className={`pl-savebar-dot${concurrencyError ? " is-error" : ""}`} aria-hidden="true" />
            <div className="pl-savebar-text">
              <strong>{saving ? "Saving…" : "Unsaved changes"}</strong>
              <span>
                {concurrencyError
                  ? "Builds at the same time must be between 1 and 20"
                  : SECTIONS.filter((item) => sectionDirty[item.id]).map((item) => item.label.toLowerCase()).join(", ")}
              </span>
            </div>
            <button type="button" className="btn-outline btn-compact" onClick={discard} disabled={saving}>
              Discard
            </button>
            <button type="button" className="primary btn-compact" onClick={save} disabled={saving || Boolean(concurrencyError)}>
              <PlIcon name="check" />
              {saving ? "Saving…" : "Save settings"}
              <kbd>Ctrl S</kbd>
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
