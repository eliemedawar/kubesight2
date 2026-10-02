import { useEffect, useState } from "react";
import { getCiStoreUploadTargets } from "../../../api/ciApi.js";
import SearchableSelect from "../../common/SearchableSelect.jsx";
import { formatRelative } from "../ciShared.jsx";
import { Field, Segmented } from "./controls.jsx";
import { PlIcon } from "./icons.jsx";
import {
  APP_STORE_TARGETS,
  ARTIFACT_TYPES,
  PLAY_TRACKS,
  STORES,
  blankStoreUpload,
  defaultAppId,
  patchStoreUpload,
  targetLabel,
} from "./storeUploadModel.js";
import { timeoutLabel } from "./stageModel.js";

/**
 * An App store upload stage: which app, which store, which track, which file —
 * and the rules on the way, which are the Mobile Apps rules because the stage
 * publishes through Mobile Apps (same signature gate, same release record).
 */
export default function StoreUploadFields({ ids, service, stage, stages, index, editable, onChange, error }) {
  const upload = stage.storeUpload || blankStoreUpload();
  const set = (patch) => onChange({ storeUpload: patchStoreUpload(upload, patch) });
  const [targets, setTargets] = useState({ data: null, loading: true, error: "" });

  useEffect(() => {
    let cancelled = false;
    getCiStoreUploadTargets(service?.id)
      .then((data) => !cancelled && setTargets({ data, loading: false, error: "" }))
      .catch((err) => !cancelled && setTargets({ data: null, loading: false, error: err.message || "Could not list the mobile apps." }));
    return () => {
      cancelled = true;
    };
  }, [service?.id]);

  const apps = targets.data?.apps || [];
  // A new stage arrives with no app; the one linked to this service is the
  // obvious answer and the backend would pick it too.
  useEffect(() => {
    if (!editable || upload.appId || !apps.length) return;
    const fallback = defaultAppId(apps, service?.id);
    if (fallback) set({ appId: fallback });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apps.length, upload.appId, editable]);

  const app = apps.find((item) => item.id === upload.appId) || null;
  const store = upload.store || "google_play";
  const ready = app ? (store === "google_play" ? app.playReady : app.appStoreReady) : null;
  const targetOptions = store === "app_store" ? APP_STORE_TARGETS : PLAY_TRACKS;
  const kept = stages
    .slice(0, index)
    .some(
      (other) =>
        other.enabled !== false &&
        (other.artifacts || []).some((spec) => String(spec.type || "").toLowerCase() === upload.artifactType)
    );

  return (
    <div className="pl-store">
      <div className="pl-grid">
        <Field
          label="Mobile application"
          error={targets.error}
          hint={
            !targets.loading && !apps.length
              ? "No app is registered yet — register it under Mobile Apps, linked to this CI service."
              : "Registered under Mobile Apps. The one linked to this service is picked for you."
          }
        >
          <SearchableSelect
            id={`${ids}-app`}
            aria-label="Mobile application"
            value={upload.appId ? String(upload.appId) : ""}
            disabled={!editable}
            placeholder={targets.loading ? "Loading apps…" : "Pick an app…"}
            searchPlaceholder="Search apps…"
            options={[
              ...(upload.appId && !app ? [{ value: String(upload.appId), label: `App #${upload.appId}` }] : []),
              ...apps.map((item) => ({
                value: String(item.id),
                label: (
                  <span className="pl-deploy-option">
                    <span>{item.name}</span>
                    <small>
                      {item.linked ? "linked to this service · " : ""}
                      {item.androidPackageName || item.iosBundleId || "no package id"}
                    </small>
                  </span>
                ),
              })),
            ]}
            onChange={(event) => set({ appId: Number(event.target.value) || null })}
          />
        </Field>
        <Field label="Store">
          <Segmented
            label="Store"
            value={store}
            options={STORES}
            disabled={!editable}
            onChange={(next) => set({ store: next })}
          />
        </Field>
        <Field
          label={store === "google_play" ? "Track" : "Target"}
          hint={store === "google_play" ? "Internal testing is the safe default." : "TestFlight makes the build available to testers."}
        >
          <SearchableSelect
            id={`${ids}-target`}
            aria-label={store === "google_play" ? "Track" : "Target"}
            value={upload.target}
            disabled={!editable}
            options={targetOptions}
            onChange={(event) => set({ target: event.target.value })}
          />
        </Field>
      </div>

      {app && ready === false && (
        <div className="pl-note is-warn">
          <PlIcon name="alert" />
          <p>
            <strong>{app.name} is not set up for {store === "google_play" ? "Google Play" : "App Store Connect"}.</strong>{" "}
            {store === "google_play"
              ? "Add its Android package name and the Play service-account key under Mobile Apps."
              : "Add its iOS bundle id and the App Store Connect API key under Mobile Apps."}{" "}
            Builds fail here until it is.
          </p>
        </div>
      )}
      {upload.target === "production" && (
        <div className="pl-note is-warn">
          <PlIcon name="alert" />
          <p>
            <strong>Every build that reaches this stage goes to production.</strong> Put an Approval
            stage in front of it, or publish to a testing track.
          </p>
        </div>
      )}

      {/* ── Which file ────────────────────────────────────────────────── */}
      <div className="pl-grid">
        <Field label="File">
          <Segmented
            label="File type"
            value={upload.artifactType}
            options={ARTIFACT_TYPES[store] || []}
            disabled={!editable}
            onChange={(artifactType) => set({ artifactType })}
          />
        </Field>
        <Field
          label="File name pattern"
          htmlFor={`${ids}-pattern`}
          optional
          hint="Empty takes the build's first file of that type. For example *-prod-release.aab."
        >
          <input
            id={`${ids}-pattern`}
            className="is-mono"
            value={upload.artifactPattern || ""}
            placeholder={`*.${upload.artifactType || "aab"}`}
            disabled={!editable}
            spellCheck={false}
            onChange={(event) => set({ artifactPattern: event.target.value.trim() })}
          />
        </Field>
      </div>
      {!kept && (
        <div className="pl-note is-warn">
          <PlIcon name="alert" />
          <p>
            <strong>No stage before this one keeps a {String(upload.artifactType || "").toUpperCase()} file.</strong>{" "}
            Add its path (for example <code>**/*.{upload.artifactType}</code>) with type{" "}
            <code>{upload.artifactType}</code> to “Files to keep” on the stage that builds it — a build
            with nothing to publish fails here.
          </p>
        </div>
      )}

      {error && (
        <p className="pl-field-error" role="alert">
          <PlIcon name="alert" /> {error}
        </p>
      )}

      <ul className="pl-deploy-guards" aria-label="What this stage checks">
        <li className="pl-deploy-guard">
          <span className="pl-deploy-guard-icon" aria-hidden="true">
            <PlIcon name="shield" />
          </span>
          <span>
            <strong>Unsigned binaries are refused</strong>
            <small>
              Shielding strips signatures, and no store accepts an unsigned file. The stage fails
              with the Mobile Apps explanation instead of uploading it — re-sign it there.
            </small>
          </span>
        </li>
        <li className="pl-deploy-guard">
          <span className="pl-deploy-guard-icon" aria-hidden="true">
            <PlIcon name="store" />
          </span>
          <span>
            <strong>Published through Mobile Apps, once</strong>
            <small>
              The file becomes a release of {app?.name || "the app"} with its publish steps. A restart
              follows the publish that exists rather than uploading again. The build waits up to{" "}
              {timeoutLabel(stage.timeoutSeconds)} for {targetLabel(upload) || "the store"}.
            </small>
          </span>
        </li>
      </ul>

      <Authority upload={upload} canPublish={targets.data?.canPublish} editable={editable} />
    </div>
  );
}

function Authority({ upload, canPublish, editable }) {
  const stamp = upload.authorizedBy;
  const cannot = canPublish === false;
  return (
    <div className={`pl-deploy-authority${cannot && editable ? " is-warn" : ""}`}>
      <PlIcon name="key" />
      <p>
        {stamp ? (
          <>
            <strong>Publishes as {stamp.username}</strong>
            {stamp.at ? <> · authorized {formatRelative(stamp.at)}</> : null}
            <small>
              Every build publishes with their rights, whoever or whatever started it. Changing the
              app, store, track or file makes whoever saves it the one it publishes as.
            </small>
          </>
        ) : (
          <>
            <strong>Saving makes you the person this stage publishes as</strong>
            <small>
              Publishing to a store is admin-only in KubeSight, so only an administrator can set
              this target.
            </small>
          </>
        )}
        {cannot && editable && (
          <small className="is-warn">
            You are not an administrator, so a change to this target will be refused on save.
          </small>
        )}
      </p>
    </div>
  );
}
