import { useState } from "react";
import { IconClose, TestStatusPill } from "./common.jsx";

// Extra Jenkins build parameters are edited as KEY=value lines — the natural
// shape for job parameters, and it keeps the form from needing a repeater.
function paramsToText(params) {
  return Object.entries(params || {})
    .map(([key, value]) => `${key}=${value}`)
    .join("\n");
}

function textToParams(text) {
  const out = {};
  String(text || "")
    .split("\n")
    .forEach((line) => {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) return;
      const at = trimmed.indexOf("=");
      if (at <= 0) return;
      const key = trimmed.slice(0, at).trim();
      const value = trimmed.slice(at + 1).trim();
      if (key && value) out[key] = value;
    });
  return out;
}

// Seed the local form from an existing app (edit) or blank defaults (create).
function formFromApp(app) {
  const artifact = app?.artifactConfig || {};
  const android = artifact.android || null;
  const ios = artifact.ios || null;
  // Signing runs on Jenkins for both platforms — that is where the keys are
  // (the Android keystore on a Linux agent, the keychain on the Mac agent).
  const resignCfg = app?.resignConfig || {};
  const resignAndroid = resignCfg.android || null;
  const resignIos = resignCfg.ios || null;
  return {
    name: app?.name || "",
    description: app?.description || "",
    enabled: app ? Boolean(app.enabled) : true,
    zohoEnvironment: app?.zohoEnvironment || "",
    jenkinsJobPath: app?.jenkinsJobPath || "",
    // Per-platform artifact config
    androidEnabled: Boolean(android),
    androidSource: android?.source || "archive",
    androidPattern: android?.pattern || "",
    androidPath: android?.path || "",
    iosEnabled: Boolean(ios),
    iosSource: ios?.source || "archive",
    iosPattern: ios?.pattern || "",
    iosPath: ios?.path || "",
    // Signing jobs (per platform)
    androidResignEnabled: Boolean(resignAndroid),
    androidResignJobPath: resignAndroid?.jobPath || "",
    androidResignPattern: resignAndroid?.resultPattern || "",
    androidResignFileParam: resignAndroid?.fileParam || "",
    androidResignExtra: paramsToText(resignAndroid?.extraParams),
    iosResignEnabled: Boolean(resignIos),
    iosResignJobPath: resignIos?.jobPath || "",
    iosResignPattern: resignIos?.resultPattern || "",
    iosResignFileParam: resignIos?.fileParam || "",
    iosResignExtra: paramsToText(resignIos?.extraParams),
    // Google Play
    androidPackageName: app?.androidPackageName || "",
    playServiceAccountJson: "",
    clearPlayServiceAccount: false,
    // App Store Connect
    iosBundleId: app?.iosBundleId || "",
    ascIssuerId: app?.ascIssuerId || "",
    ascKeyId: app?.ascKeyId || "",
    ascAppId: app?.ascAppId || "",
    ascPrivateKey: "",
    clearAscPrivateKey: false,
  };
}

function ArtifactFields({ prefix, form, set }) {
  const enabled = form[`${prefix}Enabled`];
  const source = form[`${prefix}Source`];
  return (
    <div className="sg-ma-artifact">
      <label className="checkbox-label">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => set(`${prefix}Enabled`, e.target.checked)}
        />
        {prefix === "android" ? "Android (APK/AAB)" : "iOS (IPA)"}
      </label>
      {enabled ? (
        <div className="sg-ma-artifact-body">
          <label>
            Artifact source
            <select value={source} onChange={(e) => set(`${prefix}Source`, e.target.value)}>
              <option value="archive">Archived artifact</option>
              <option value="workspace">Workspace file</option>
            </select>
          </label>
          {source === "archive" ? (
            <label>
              Artifact pattern
              <input
                value={form[`${prefix}Pattern`]}
                onChange={(e) => set(`${prefix}Pattern`, e.target.value)}
                placeholder={prefix === "android" ? "*.apk" : "*.ipa"}
                className="mono"
              />
            </label>
          ) : (
            <label>
              Workspace path
              <input
                value={form[`${prefix}Path`]}
                onChange={(e) => set(`${prefix}Path`, e.target.value)}
                placeholder="execution/node/71/ws/pos.apk"
                className="mono"
              />
            </label>
          )}
          <span className="field-hint sg-ma-span">
            Archived artifacts are more reliable — they survive workspace cleanup between builds.
          </span>
        </div>
      ) : null}
    </div>
  );
}

// One platform's signing job. Both platforms sign on Jenkins — Android with the
// upload keystore, iOS with the macOS keychain — so the fields are the same.
function ResignFields({ prefix, form, set }) {
  const enabled = form[`${prefix}ResignEnabled`];
  const isAndroid = prefix === "android";
  return (
    <div className="sg-ma-artifact">
      <label className="checkbox-label">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => set(`${prefix}ResignEnabled`, e.target.checked)}
        />
        {isAndroid ? "Android (APK/AAB)" : "iOS (IPA)"}
      </label>
      {enabled ? (
        <div className="sg-ma-artifact-body">
          <label>
            Jenkins job path
            <input
              value={form[`${prefix}ResignJobPath`]}
              onChange={(e) => set(`${prefix}ResignJobPath`, e.target.value)}
              placeholder={isAndroid ? "mobile/android-resign" : "mobile/ios-resign"}
              className="mono"
              required
            />
          </label>
          <label>
            Signed artifact pattern
            <input
              value={form[`${prefix}ResignPattern`]}
              onChange={(e) => set(`${prefix}ResignPattern`, e.target.value)}
              placeholder={isAndroid ? "*.aab" : "signed/*.ipa"}
              className="mono"
            />
            <span className="field-hint">
              Matched against the build&apos;s archived artifacts.
            </span>
          </label>
          <label>
            Binary file parameter
            <input
              value={form[`${prefix}ResignFileParam`]}
              onChange={(e) => set(`${prefix}ResignFileParam`, e.target.value)}
              placeholder="apkfile"
              className="mono"
            />
            <span className="field-hint">
              The job&apos;s file parameter the binary is uploaded as.
            </span>
          </label>
          <label className="sg-ma-span">
            Extra build parameters
            <textarea
              rows={3}
              value={form[`${prefix}ResignExtra`]}
              onChange={(e) => set(`${prefix}ResignExtra`, e.target.value)}
              placeholder={
                isAndroid ? "KEY_ALIAS=upload" : "PROFILE=comareeba_AppStore.mobileprovision"
              }
              className="mono sg-ma-textarea"
            />
            <span className="field-hint">
              One <code>KEY=value</code> per line, passed to the job as-is.
            </span>
          </label>
        </div>
      ) : null}
    </div>
  );
}

export default function AppFormModal({
  open,
  mode = "create",
  app = null,
  environments = null,
  onClose,
  onSave,
  saving = false,
  error = "",
  onTestPlay,
  onTestAppStore,
  testingPlay = false,
  testingAppStore = false,
}) {
  const [form, setForm] = useState(() => formFromApp(app));
  if (!open) return null;

  const isEdit = mode === "edit";
  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  // Dropdown options for the Zoho environment binding. null (list unavailable —
  // fetch failed) falls back to the legacy free-text input; a stored value that
  // has since been removed from the Zoho config stays selectable so opening the
  // edit form doesn't silently unlink the app.
  const envList = Array.isArray(environments)
    ? form.zohoEnvironment && !environments.includes(form.zohoEnvironment)
      ? [form.zohoEnvironment, ...environments]
      : environments
    : null;

  const buildPayload = () => {
    const artifactConfig = {};
    if (form.androidEnabled) {
      artifactConfig.android =
        form.androidSource === "archive"
          ? { source: "archive", pattern: form.androidPattern.trim() }
          : { source: "workspace", path: form.androidPath.trim() };
    }
    if (form.iosEnabled) {
      artifactConfig.ios =
        form.iosSource === "archive"
          ? { source: "archive", pattern: form.iosPattern.trim() }
          : { source: "workspace", path: form.iosPath.trim() };
    }
    // Only the fields the operator actually filled in are sent — the backend
    // supplies its own defaults for the omitted ones, so sending empty strings
    // would overwrite good defaults with nothing.
    const resignEntry = (prefix) => {
      const entry = { executor: "jenkins" };
      const optional = {
        jobPath: form[`${prefix}ResignJobPath`],
        resultPattern: form[`${prefix}ResignPattern`],
        fileParam: form[`${prefix}ResignFileParam`],
      };
      Object.entries(optional).forEach(([key, value]) => {
        const trimmed = (value || "").trim();
        if (trimmed) entry[key] = trimmed;
      });
      const extra = textToParams(form[`${prefix}ResignExtra`]);
      if (Object.keys(extra).length) entry.extraParams = extra;
      return entry;
    };
    const resignConfig = {};
    if (form.androidResignEnabled) resignConfig.android = resignEntry("android");
    if (form.iosResignEnabled) resignConfig.ios = resignEntry("ios");
    const payload = {
      name: form.name.trim(),
      description: form.description.trim(),
      enabled: form.enabled,
      zohoEnvironment: form.zohoEnvironment.trim(),
      jenkinsJobPath: form.jenkinsJobPath.trim(),
      artifactConfig,
      resignConfig,
      androidPackageName: form.androidPackageName.trim(),
      iosBundleId: form.iosBundleId.trim(),
      ascIssuerId: form.ascIssuerId.trim(),
      ascKeyId: form.ascKeyId.trim(),
      ascAppId: form.ascAppId.trim(),
    };
    // Secrets are write-only: only send when the operator typed one.
    if (form.playServiceAccountJson.trim()) {
      payload.playServiceAccountJson = form.playServiceAccountJson.trim();
    } else if (form.clearPlayServiceAccount) {
      payload.clearPlayServiceAccount = true;
    }
    if (form.ascPrivateKey.trim()) {
      payload.ascPrivateKey = form.ascPrivateKey.trim();
    } else if (form.clearAscPrivateKey) {
      payload.clearAscPrivateKey = true;
    }
    return payload;
  };

  const submit = (e) => {
    e.preventDefault();
    onSave(buildPayload());
  };

  const lastTest = app?.lastTestStatus ? (
    <p className={`sg-ma-testline ${app.lastTestStatus === "ok" ? "" : "sg-ma-testline--err"}`}>
      <TestStatusPill status={app.lastTestStatus} />
      <span>{app.lastTestMessage || (app.lastTestStatus === "ok" ? "Last test passed." : "Last test failed.")}</span>
    </p>
  ) : null;

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel sg-ma-form-panel"
        role="dialog"
        aria-modal="true"
        aria-label={isEdit ? "Edit mobile application" : "Register mobile application"}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal-header">
          <h3>{isEdit ? "Edit application" : "Register application"}</h3>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close">
            <IconClose />
          </button>
        </header>

        <form onSubmit={submit} className="sg-ma-form">
          {/* ── General ─────────────────────────────────────────────── */}
          <h4 className="sg-ma-formsect">General</h4>
          <div className="settings-form sg-ma-grid2">
            <label className="sg-ma-span">
              Name
              <input
                value={form.name}
                onChange={(e) => set("name", e.target.value)}
                placeholder="POS Terminal"
                required
              />
            </label>
            <label className="sg-ma-span">
              Description
              <input
                value={form.description}
                onChange={(e) => set("description", e.target.value)}
                placeholder="Android + iOS point-of-sale client"
              />
            </label>
            <label className="checkbox-label sg-ma-span">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) => set("enabled", e.target.checked)}
              />
              Enabled (allows build fetches and publishing)
            </label>
          </div>

          {/* ── Zoho & Jenkins ─────────────────────────────────────── */}
          <h4 className="sg-ma-formsect">Zoho &amp; Jenkins</h4>
          <div className="settings-form sg-ma-grid2">
            <label>
              Zoho environment
              {envList ? (
                <select
                  value={form.zohoEnvironment}
                  onChange={(e) => set("zohoEnvironment", e.target.value)}
                >
                  <option value="">Not linked</option>
                  {envList.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  value={form.zohoEnvironment}
                  onChange={(e) => set("zohoEnvironment", e.target.value)}
                  placeholder="POS-PROD"
                />
              )}
              <span className="field-hint">
                {envList && environments.length === 0
                  ? "No custom environments configured yet — add one under Zoho Desk settings."
                  : "The custom Environment whose ticket builds feed this app."}
              </span>
            </label>
            <label>
              Jenkins job path
              <input
                value={form.jenkinsJobPath}
                onChange={(e) => set("jenkinsJobPath", e.target.value)}
                placeholder="POS-APK"
                className="mono"
              />
              <span className="field-hint">Folder-style, e.g. POS-APK or mobile/pos-apk.</span>
            </label>
            <div className="sg-ma-span">
              <span className="sg-ma-pick-label">Artifact resolution</span>
              <p className="field-hint sg-ma-artifact-intro">
                Enable each platform this app ships and tell KubeSight where the binary lands after
                a build.
              </p>
              <div className="sg-ma-artifacts">
                <ArtifactFields prefix="android" form={form} set={set} />
                <ArtifactFields prefix="ios" form={form} set={set} />
              </div>
            </div>
          </div>

          {/* ── Google Play ────────────────────────────────────────── */}
          <h4 className="sg-ma-formsect">Google Play</h4>
          <div className="settings-form sg-ma-grid2">
            <label className="sg-ma-span">
              Package name
              <input
                value={form.androidPackageName}
                onChange={(e) => set("androidPackageName", e.target.value)}
                placeholder="com.areeba.pos"
                className="mono"
              />
            </label>
            <label className="sg-ma-span">
              Service account JSON
              <textarea
                rows={4}
                value={form.playServiceAccountJson}
                onChange={(e) => set("playServiceAccountJson", e.target.value)}
                placeholder={
                  app?.playServiceAccountConfigured
                    ? "•••• (leave blank to keep the stored key)"
                    : "Paste the Play publishing service-account JSON"
                }
                className="mono sg-ma-textarea"
                autoComplete="off"
              />
            </label>
            {app?.playServiceAccountConfigured ? (
              <label className="checkbox-label sg-ma-span">
                <input
                  type="checkbox"
                  checked={form.clearPlayServiceAccount}
                  onChange={(e) => set("clearPlayServiceAccount", e.target.checked)}
                  disabled={Boolean(form.playServiceAccountJson.trim())}
                />
                Remove the stored service-account key
              </label>
            ) : null}
            {isEdit ? (
              <div className="sg-ma-span sg-ma-testrow">
                <button
                  type="button"
                  className="secondary"
                  onClick={onTestPlay}
                  disabled={testingPlay || !app?.playServiceAccountConfigured}
                  title={app?.playServiceAccountConfigured ? "" : "Save a service-account key first"}
                >
                  {testingPlay ? "Testing…" : "Test Google Play"}
                </button>
              </div>
            ) : null}
          </div>

          {/* ── Signing ────────────────────────────────────────────── */}
          <h4 className="sg-ma-formsect">Signing</h4>
          <div className="settings-form sg-ma-grid2">
            <p className="field-hint sg-ma-span sg-ma-artifact-intro">
              Shielding strips the code signature, and no store accepts an unsigned binary. Pressing
              &ldquo;Re-sign&rdquo; on a build triggers the Jenkins job below, uploading the binary
              with it. The job signs it and archives the result, and KubeSight pulls that back as a
              new, publishable build. Signing keys stay on Jenkins.
            </p>
            <div className="sg-ma-span">
              <span className="sg-ma-pick-label">Signing jobs</span>
              <div className="sg-ma-artifacts">
                <ResignFields prefix="android" form={form} set={set} />
                <ResignFields prefix="ios" form={form} set={set} />
              </div>
            </div>
            <span className="field-hint sg-ma-span">
              The binary is uploaded as a Jenkins file parameter, so the job never needs to reach
              KubeSight. See <code>k8s/jenkins/resign.Jenkinsfile</code> in the KubeSight repo for a
              ready-made pipeline.
            </span>
          </div>
          {/* ── App Store Connect ──────────────────────────────────── */}
          <h4 className="sg-ma-formsect">App Store Connect</h4>
          <div className="settings-form sg-ma-grid2">
            <label>
              Bundle ID
              <input
                value={form.iosBundleId}
                onChange={(e) => set("iosBundleId", e.target.value)}
                placeholder="com.areeba.pos"
                className="mono"
              />
            </label>
            <label>
              App Store app ID (ASC)
              <input
                value={form.ascAppId}
                onChange={(e) => set("ascAppId", e.target.value)}
                placeholder="6444556677"
                className="mono"
              />
            </label>
            <label>
              Issuer ID
              <input
                value={form.ascIssuerId}
                onChange={(e) => set("ascIssuerId", e.target.value)}
                placeholder="57246542-96fe-1a63-e053-0824d011072a"
                className="mono"
              />
            </label>
            <label>
              Key ID
              <input
                value={form.ascKeyId}
                onChange={(e) => set("ascKeyId", e.target.value)}
                placeholder="2X9R4HXF34"
                className="mono"
              />
            </label>
            <label className="sg-ma-span">
              API private key (.p8)
              <textarea
                rows={4}
                value={form.ascPrivateKey}
                onChange={(e) => set("ascPrivateKey", e.target.value)}
                placeholder={
                  app?.ascPrivateKeyConfigured
                    ? "•••• (leave blank to keep the stored key)"
                    : "Paste the -----BEGIN PRIVATE KEY----- contents"
                }
                className="mono sg-ma-textarea"
                autoComplete="off"
              />
            </label>
            {app?.ascPrivateKeyConfigured ? (
              <label className="checkbox-label sg-ma-span">
                <input
                  type="checkbox"
                  checked={form.clearAscPrivateKey}
                  onChange={(e) => set("clearAscPrivateKey", e.target.checked)}
                  disabled={Boolean(form.ascPrivateKey.trim())}
                />
                Remove the stored private key
              </label>
            ) : null}
            {isEdit ? (
              <div className="sg-ma-span sg-ma-testrow">
                <button
                  type="button"
                  className="secondary"
                  onClick={onTestAppStore}
                  disabled={testingAppStore || !app?.ascPrivateKeyConfigured}
                  title={app?.ascPrivateKeyConfigured ? "" : "Save an API private key first"}
                >
                  {testingAppStore ? "Testing…" : "Test App Store"}
                </button>
              </div>
            ) : null}
          </div>

          {lastTest}
          {error ? <p className="banner-message error">{error}</p> : null}

          <div className="modal-actions">
            <button type="button" className="secondary" onClick={onClose} disabled={saving}>
              Cancel
            </button>
            <button type="submit" className="primary" disabled={saving || !form.name.trim()}>
              {saving ? "Saving…" : isEdit ? "Save changes" : "Register application"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
