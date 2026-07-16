import { useState } from "react";
import {
  IconClose,
  PlatformBadge,
  PUBLISH_TARGETS,
  STORE_BY_PLATFORM,
} from "./common.jsx";

// Promote a build to a store track. The store is derived from the build's
// platform; the operator picks a target track and must type the app name
// exactly to arm the (irreversible) confirm.
export default function PublishDialog({
  open,
  app,
  build,
  onClose,
  onConfirm,
  publishing = false,
  error = "",
}) {
  const storeInfo = STORE_BY_PLATFORM[build?.platform] || null;
  const targets = storeInfo ? PUBLISH_TARGETS[storeInfo.store] || [] : [];
  const [target, setTarget] = useState(targets[0]?.value || "");
  const [confirmName, setConfirmName] = useState("");

  if (!open || !build) return null;

  const nameMatches = confirmName.trim() === (app?.name || "").trim() && Boolean(app?.name);
  const canConfirm = Boolean(target) && nameMatches && !publishing;

  const submit = (e) => {
    e.preventDefault();
    if (!canConfirm || !storeInfo) return;
    onConfirm({ store: storeInfo.store, target });
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel sg-ma-publish-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Publish build"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <h3>Publish to {storeInfo?.label || "store"}</h3>
            <p className="sg-ma-publish-sub">
              <PlatformBadge platform={build.platform} />
              <span className="sg-tag">{(build.artifactType || "").toUpperCase()}</span>
              <span className="mono">{build.version || build.fileName}</span>
            </p>
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close">
            <IconClose />
          </button>
        </header>

        <form onSubmit={submit}>
          <fieldset className="sg-ma-targets">
            <legend className="sg-ma-pick-label">Target track</legend>
            {targets.map((t) => (
              <label
                key={t.value}
                className={`sg-ma-target ${target === t.value ? "sg-ma-target--on" : ""} ${
                  t.danger ? "sg-ma-target--danger" : ""
                }`}
              >
                <input
                  type="radio"
                  name="publish-target"
                  value={t.value}
                  checked={target === t.value}
                  onChange={() => setTarget(t.value)}
                />
                <span className="sg-ma-target-body">
                  <span className="sg-ma-target-label">
                    {t.label}
                    {t.danger ? <span className="status-pill danger">Public</span> : null}
                  </span>
                  <span className="sg-ma-target-hint">{t.hint}</span>
                </span>
              </label>
            ))}
          </fieldset>

          <div className="sg-ma-confirm-note">
            Publishing hands this binary to {storeInfo?.label || "the store"} and cannot be undone
            from KubeSight. Type the application name{" "}
            <b>{app?.name}</b> to confirm.
          </div>
          <label className="sg-ma-confirm-field">
            Application name
            <input
              value={confirmName}
              onChange={(e) => setConfirmName(e.target.value)}
              placeholder={app?.name}
              autoComplete="off"
            />
          </label>

          {error ? <p className="banner-message error">{error}</p> : null}

          <div className="modal-actions">
            <button type="button" className="secondary" onClick={onClose} disabled={publishing}>
              Cancel
            </button>
            <button type="submit" className="btn-danger" disabled={!canConfirm}>
              {publishing ? "Publishing…" : `Publish to ${storeInfo?.label || "store"}`}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
