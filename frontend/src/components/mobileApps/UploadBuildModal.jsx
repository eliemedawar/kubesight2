import { useMemo, useState } from "react";
import { formatBytes, IconClose, PlatformBadge } from "./common.jsx";

// Accept filters + validation lists, keyed by platform.
const ACCEPT = { android: ".apk,.aab", ios: ".ipa" };
const EXTS = { android: ["apk", "aab"], ios: ["ipa"] };
const HINT = {
  android: "APK or AAB → Google Play",
  ios: "IPA → App Store Connect",
};

// Upload a final binary straight into KubeSight as a ready-to-publish build,
// bypassing Jenkins. The store publish flow accepts it exactly like a fetched
// build — this just puts the file (and its platform) into the binary store.
export default function UploadBuildModal({
  open,
  app,
  onClose,
  onUpload,
  uploading = false,
  error = "",
}) {
  const configured = app?.platforms?.length ? app.platforms : ["android", "ios"];
  const [platform, setPlatform] = useState(configured[0] || "android");
  const [file, setFile] = useState(null);
  const [version, setVersion] = useState("");
  const [localError, setLocalError] = useState("");

  const extOk = useMemo(() => {
    if (!file) return true;
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    return EXTS[platform].includes(ext);
  }, [file, platform]);

  if (!open) return null;

  const canSubmit = Boolean(file) && extOk && !uploading;

  const submit = (e) => {
    e.preventDefault();
    setLocalError("");
    if (!file) {
      setLocalError("Choose a file to upload.");
      return;
    }
    if (!extOk) {
      const pretty = EXTS[platform].map((x) => `.${x}`).join(" or ");
      setLocalError(`A ${platform} build must be a ${pretty} file.`);
      return;
    }
    onUpload({ file, platform, version: version.trim() });
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={uploading ? undefined : onClose}>
      <section
        className="card modal-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Upload build"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal-header">
          <h3>Upload a binary</h3>
          <button
            type="button"
            className="icon-button"
            onClick={onClose}
            aria-label="Close"
            disabled={uploading}
          >
            <IconClose />
          </button>
        </header>

        <p className="muted sg-ma-upl-hint">
          Add a final APK, AAB or IPA directly to <b>{app?.name}</b>. It lands as a ready-to-publish
          build — no Jenkins fetch needed. Use this for a re-signed IPA or a release AAB you already
          have in hand.
        </p>

        <form onSubmit={submit}>
          <fieldset className="sg-ma-targets">
            <legend className="sg-ma-pick-label">Platform</legend>
            {["android", "ios"].map((p) => (
              <label
                key={p}
                className={`sg-ma-target ${platform === p ? "sg-ma-target--on" : ""}`}
              >
                <input
                  type="radio"
                  name="upload-platform"
                  value={p}
                  checked={platform === p}
                  onChange={() => {
                    setPlatform(p);
                    setLocalError("");
                  }}
                />
                <span className="sg-ma-target-body">
                  <span className="sg-ma-target-label">
                    <PlatformBadge platform={p} />
                  </span>
                  <span className="sg-ma-target-hint">{HINT[p]}</span>
                </span>
              </label>
            ))}
          </fieldset>

          <label className="sg-ma-confirm-field">
            Binary file
            <input
              type="file"
              accept={ACCEPT[platform]}
              onChange={(e) => {
                setFile(e.target.files?.[0] || null);
                setLocalError("");
              }}
            />
          </label>
          {file ? (
            <p className="sg-ma-upl-file">
              <span className="mono">{file.name}</span>
              <span className="sg-tag">{formatBytes(file.size)}</span>
              {!extOk ? <span className="status-pill danger">wrong type</span> : null}
            </p>
          ) : null}

          <label className="sg-ma-confirm-field">
            Version{" "}
            <span className="muted">
              (optional{platform === "ios" ? " — auto-read from the IPA" : ""})
            </span>
            <input
              value={version}
              onChange={(e) => setVersion(e.target.value)}
              placeholder={platform === "ios" ? "read from Info.plist" : "e.g. 2.4.1 (108)"}
              autoComplete="off"
            />
          </label>

          {localError || error ? (
            <p className="banner-message error">{localError || error}</p>
          ) : null}

          <div className="modal-actions">
            <button type="button" className="secondary" onClick={onClose} disabled={uploading}>
              Cancel
            </button>
            <button type="submit" className="primary" disabled={!canSubmit}>
              {uploading ? "Uploading…" : "Upload build"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
