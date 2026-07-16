// Shared presentation helpers for the Mobile Applications feature: platform
// badges, build/publish status pills, publish-step chips, store/target labels,
// small formatters, and the inline stroke icons the screens reuse.

// ── Inline stroke icons (Lucide outlines, explicit viewBox + size via base) ──
const base = {
  viewBox: "0 0 24 24",
  width: 16,
  height: 16,
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
};

export const IconSmartphone = (props) => (
  <svg {...base} {...props}>
    <rect x="5" y="2" width="14" height="20" rx="2" />
    <path d="M12 18h.01" />
  </svg>
);

export const IconDownload = (props) => (
  <svg {...base} {...props}>
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <path d="M7 10l5 5 5-5" />
    <path d="M12 15V3" />
  </svg>
);

export const IconTrash = (props) => (
  <svg {...base} {...props}>
    <path d="M3 6h18" />
    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
    <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
  </svg>
);

export const IconClose = (props) => (
  <svg {...base} {...props}>
    <path d="M18 6 6 18" />
    <path d="m6 6 12 12" />
  </svg>
);

export const IconExternal = (props) => (
  <svg {...base} {...props}>
    <path d="M15 3h6v6" />
    <path d="M10 14 21 3" />
    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
  </svg>
);

export const IconRocket = (props) => (
  <svg {...base} {...props}>
    <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z" />
    <path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z" />
    <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0" />
    <path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" />
  </svg>
);

// ── Formatters ───────────────────────────────────────────────────────
export function formatBytes(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rounded = value >= 100 || unit === 0 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${rounded} ${units[unit]}`;
}

// "4 min ago" style relative timestamps.
export function timeAgo(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  return `${days} d ago`;
}

// ── Platform ─────────────────────────────────────────────────────────
export const PLATFORM_LABEL = { android: "Android", ios: "iOS" };

export function PlatformBadge({ platform }) {
  const label = PLATFORM_LABEL[platform] || platform || "—";
  return <span className={`sg-ma-plat sg-ma-plat--${platform || "unknown"}`}>{label}</span>;
}

// ── Build status ─────────────────────────────────────────────────────
// pending/downloading → info "Fetching"; available → ok; failed → danger.
const BUILD_STATUS_PILL = {
  pending: ["info", "Fetching"],
  downloading: ["info", "Fetching"],
  available: ["ok", "Available"],
  failed: ["danger", "Failed"],
};

export const ACTIVE_BUILD_STATUSES = new Set(["pending", "downloading"]);

export function BuildStatusPill({ status }) {
  const [tone, label] = BUILD_STATUS_PILL[status] || ["muted", status || "—"];
  return <span className={`status-pill ${tone}`}>{label}</span>;
}

// ── Publish status ───────────────────────────────────────────────────
const PUBLISH_STATUS_PILL = {
  queued: ["info", "Queued"],
  uploading: ["info", "Uploading"],
  processing: ["info", "Processing"],
  published: ["ok", "Published"],
  failed: ["danger", "Failed"],
};

export const ACTIVE_PUBLISH_STATUSES = new Set(["queued", "uploading", "processing"]);

export function PublishStatusPill({ status }) {
  const [tone, label] = PUBLISH_STATUS_PILL[status] || ["muted", status || "—"];
  return <span className={`status-pill ${tone}`}>{label}</span>;
}

// ── Publish step chips (keys: credentials, upload, release, confirm) ──
const PUBLISH_STEPS = [
  { key: "credentials", label: "Credentials" },
  { key: "upload", label: "Upload" },
  { key: "release", label: "Release" },
  { key: "confirm", label: "Confirm" },
];

const STEP_CHIP_CLASS = {
  done: "sg-ma-step--done",
  run: "sg-ma-step--run",
  fail: "sg-ma-step--fail",
  wait: "sg-ma-step--wait",
  skip: "sg-ma-step--skip",
};

export function PublishSteps({ steps }) {
  const byKey = new Map((steps || []).map((s) => [s.key, s]));
  return (
    <div className="sg-ma-steps">
      {PUBLISH_STEPS.map((step) => {
        const state = byKey.get(step.key) || { status: "wait" };
        return (
          <span
            key={step.key}
            className={`sg-ma-step ${STEP_CHIP_CLASS[state.status] || "sg-ma-step--wait"}`}
            title={state.detail || step.label}
          >
            {step.label}
            {state.status === "skip" ? " (skipped)" : ""}
          </span>
        );
      })}
    </div>
  );
}

// ── Stores + publish targets ─────────────────────────────────────────
export const STORE_BY_PLATFORM = {
  android: { store: "google_play", label: "Google Play" },
  ios: { store: "app_store", label: "App Store" },
};

// Radio targets per store, each with a one-line description. `danger` targets
// (production / public review) get an extra caution treatment in the dialog.
export const PUBLISH_TARGETS = {
  google_play: [
    { value: "internal", label: "Internal testing", hint: "Up to 100 trusted testers — the fastest track to validate a build." },
    { value: "alpha", label: "Closed alpha", hint: "A closed testing track for a named group of testers." },
    { value: "beta", label: "Open/closed beta", hint: "Wider pre-release testing before a production rollout." },
    { value: "production", label: "Production", hint: "Live on the Google Play Store for all users.", danger: true },
  ],
  app_store: [
    { value: "testflight", label: "TestFlight", hint: "Distribute to TestFlight beta testers." },
    { value: "review", label: "App Store review", hint: "Submit for App Store review and public release.", danger: true },
  ],
};

const TARGET_LABEL = {
  internal: "internal track",
  alpha: "alpha track",
  beta: "beta track",
  production: "production",
  testflight: "TestFlight",
  review: "App Store review",
};

// "Google Play · beta track" / "App Store · TestFlight"
export function storeTargetLabel(store, target) {
  const storeLabel =
    store === "google_play" ? "Google Play" : store === "app_store" ? "App Store" : store || "—";
  const targetLabel = TARGET_LABEL[target] || target || "";
  return targetLabel ? `${storeLabel} · ${targetLabel}` : storeLabel;
}

// Generic ok/error/never pill for the last-test status string.
export function TestStatusPill({ status }) {
  if (!status) return <span className="status-pill muted">Never tested</span>;
  const ok = status === "ok";
  return <span className={`status-pill ${ok ? "ok" : "danger"}`}>{ok ? "Passed" : "Failed"}</span>;
}
