import { parseApiTime } from "../../lib/apiTime.js";

/**
 * Shared vocabulary for the CI Service Catalog: icons, status mapping, and the
 * small formatters every tab needs. Kept in one place so a build status looks
 * identical on a card, in a table, and in the build drawer.
 */

// The type selects the generated fallback and customization kit, so it is
// split by build tool wherever commands and artifact paths differ. `legacy`
// entries still resolve (older services carry them) but are never offered for
// a new service.
export const APPLICATION_TYPES = [
  { value: "container", label: "Container application" },
  { value: "java_maven", label: "Java / Maven" },
  { value: "java_gradle", label: "Java / Gradle" },
  { value: "java", label: "Java / JAR", legacy: true },
  { value: "node", label: "Node.js" },
  { value: "python", label: "Python" },
  { value: "android", label: "Android" },
  { value: "ios", label: "iOS" },
  { value: "flutter", label: "Flutter" },
  { value: "generic", label: "Generic / custom" },
];

export const CRITICALITIES = ["low", "medium", "high", "critical"];

export const STAGE_TYPES = [
  { value: "checkout", label: "Checkout" },
  { value: "command", label: "Command" },
  { value: "container_image", label: "Build container image" },
  { value: "publish_artifact", label: "Publish artifact" },
  { value: "scan", label: "Security scan" },
];

/** Stage kinds that only run under specific conditions — the editor says so
 * instead of pretending. container_image executes on the Kubernetes runner
 * once BuildKit is configured; the others have no executor yet. */
export const CONDITIONAL_STAGE_TYPES = {
  container_image:
    "Runs on the Kubernetes runner once BuildKit is configured " +
    "(k8s/ci-buildkitd.yaml + CI_BUILDKIT_ADDR). Until then a build records it " +
    "as skipped — it never reports success for work that did not happen.",
  publish_artifact:
    "No executor yet. A build records it as skipped and says so in the stage log.",
  scan: "No executor yet. A build records it as skipped and says so in the stage log.",
};

export const UNIMPLEMENTED_STAGE_TYPES = new Set(Object.keys(CONDITIONAL_STAGE_TYPES));

/** Image scanning — the gate between building an image and pushing it.
 *
 * Only on container_image stages, because it is the same stage: KubeSight stops
 * BuildKit pushing, scans what it built, and pushes only on a pass. There is no
 * arrangement of the pipeline that pushes an unscanned image, which is the
 * reason it is a field here and not a stage of its own. */
export const IMAGE_SCAN_THRESHOLDS = [
  { value: "critical", label: "Critical only" },
  { value: "high", label: "High and above" },
  { value: "medium", label: "Medium and above" },
  { value: "low", label: "Any finding" },
];

export const IMAGE_SCAN_ON_FAIL = [
  { value: "block", label: "Block the push" },
  { value: "warn", label: "Warn and push anyway" },
];

/** What a stage gets when scanning is switched on for the first time. Mirrors
 * templates.default_image_scan() on the backend — keep the two in step. */
export const DEFAULT_IMAGE_SCAN = {
  enabled: true,
  scanner: "trivy",
  threshold: "critical",
  onFail: "block",
  ignoreUnfixed: false,
};

export const RUNNER_TYPES = [
  { value: "", label: "Any compatible runner" },
  { value: "kubernetes", label: "Kubernetes Job" },
  { value: "agent_linux", label: "Linux agent" },
  { value: "agent_macos", label: "macOS agent" },
  { value: "ssh_linux", label: "Linux over SSH" },
  { value: "mock", label: "Simulated (mock)" },
];

/** Build + stage status → the shared status-pill tone. */
export const STATUS_TONE = {
  success: "ok",
  running: "info",
  queued: "unknown",
  pending: "unknown",
  failed: "danger",
  timeout: "danger",
  cancelled: "warn",
  skipped: "unknown",
  active: "ok",
  paused: "warn",
  archived: "unknown",
  online: "ok",
  offline: "unknown",
  draining: "warn",
  disabled: "unknown",
  // Merge checks. `passed`/`failed` are the check's own states; `allowed` and
  // `blocked` are the verdict it reports; `delivered` is whether Bitbucket has
  // been told. Three vocabularies on purpose — a verdict that was reached and
  // never delivered has to be distinguishable from one that passed.
  passed: "ok",
  allowed: "ok",
  blocked: "danger",
  error: "danger",
  delivered: "ok",
  unknown: "unknown",
  not_applicable: "unknown",
};

export const TERMINAL_BUILD_STATUSES = new Set([
  "success",
  "failed",
  "cancelled",
  "timeout",
]);

export const isBuildActive = (status) => !TERMINAL_BUILD_STATUSES.has(status);

// --- Polling that survives a blip -------------------------------------------
// Every CI view polls while a build is in flight, so every one of them is
// exposed to a momentary network failure -- the backend rolling, a reset
// connection, an ingress that drops a SYN. A poll that fails is a blip, not an
// outage: back off and keep the chain alive rather than treating the failure as
// "nothing is running", which freezes the view and leaves a banner with nothing
// left running to clear it.
const RETRY_MS = 8000;
const MAX_RETRY_MS = 60000;
export const FAILURES_BEFORE_BANNER = 3;

/** Backoff for the Nth consecutive failure: 8s, 16s, 32s, then 60s. */
export const retryDelay = (failures) =>
  Math.min(RETRY_MS * 2 ** Math.max(0, failures - 1), MAX_RETRY_MS);

// `fetch` rejects with a bare "Failed to fetch" when the request never reached
// the backend at all. That is the browser's wording, not ours, and it tells the
// reader nothing -- anything carrying a status came from the API and says
// something useful, so only the native strings get replaced.
const NATIVE_FETCH_FAILURES = new Set([
  "Failed to fetch",
  "Load failed",
  "NetworkError when attempting to fetch resource.",
]);

export const describeError = (err, fallback) =>
  !err?.status && NATIVE_FETCH_FAILURES.has(err?.message)
    ? "Lost contact with the backend. Retrying..."
    : err?.message || fallback;

export function StatusPill({ status, children }) {
  if (!status) return null;
  return (
    <span className={`status-pill ${STATUS_TONE[status] || "unknown"}`}>
      {children || status}
    </span>
  );
}

/** "3m 42s" — the format the build list reads in. */
export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  if (minutes < 60) return `${minutes}m ${String(rest).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
}

export function formatRelative(iso) {
  if (!iso) return "—";
  // parseApiTime, not Date.parse: the API serializes UTC without a suffix, and
  // a browser reads that as local time — "3h ago" for a build queued seconds
  // ago, everywhere the offset is not zero.
  const then = parseApiTime(iso);
  if (Number.isNaN(then)) return "—";
  const diff = Math.round((Date.now() - then) / 1000);
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}

export function formatBytes(bytes) {
  if (!bytes && bytes !== 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = Number(bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export const shortSha = (sha) => (sha ? String(sha).slice(0, 8) : "—");

/* ── Inline stroke icons (tokens only, via currentColor) ────────────── */

function IconBase({ children, ...props }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  );
}

export const PlusIcon = () => (
  <IconBase>
    <path d="M12 5v14" />
    <path d="M5 12h14" />
  </IconBase>
);

export const SearchIcon = () => (
  <IconBase>
    <circle cx="11" cy="11" r="7" />
    <path d="m21 21-4.35-4.35" />
  </IconBase>
);

export const PlayIcon = () => (
  <IconBase>
    <path d="m6 3 14 9-14 9V3z" />
  </IconBase>
);

/** Stacked machines — the fleet that executes builds. */
export const RunnerIcon = () => (
  <IconBase>
    <rect x="3" y="4" width="18" height="7" rx="2" />
    <rect x="3" y="13" width="18" height="7" rx="2" />
    <path d="M7 7.5h.01M7 16.5h.01" />
  </IconBase>
);

export const BranchIcon = () => (
  <IconBase>
    <circle cx="6" cy="5" r="2.5" />
    <circle cx="6" cy="19" r="2.5" />
    <circle cx="18" cy="8" r="2.5" />
    <path d="M6 7.5v9M18 10.5c0 4-4 3.5-6 5.5" />
  </IconBase>
);

export const TagIcon = () => (
  <IconBase>
    <path d="M20.6 13.4 11 3.8A2 2 0 0 0 9.6 3H5a2 2 0 0 0-2 2v4.6c0 .5.2 1 .6 1.4l9.6 9.6a2 2 0 0 0 2.8 0l4.6-4.6a2 2 0 0 0 0-2.8z" />
    <circle cx="7.5" cy="7.5" r="1" fill="currentColor" />
  </IconBase>
);

export const RepoIcon = () => (
  <IconBase>
    <path d="M4 4.5A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H6.5A2.5 2.5 0 0 0 4 19.5z" />
    <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20v5H6.5A2.5 2.5 0 0 1 4 19.5z" />
  </IconBase>
);

export const PackageIcon = () => (
  <IconBase>
    <path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z" />
    <path d="m3.3 7 8.7 5 8.7-5" />
    <path d="M12 22V12" />
  </IconBase>
);

export const CheckIcon = () => (
  <IconBase>
    <path d="m20 6-11 11-5-5" />
  </IconBase>
);

export const XIcon = () => (
  <IconBase>
    <path d="M18 6 6 18M6 6l12 12" />
  </IconBase>
);

export const ClockIcon = () => (
  <IconBase>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </IconBase>
);

export const DotIcon = () => (
  <IconBase>
    <circle cx="12" cy="12" r="4" />
  </IconBase>
);

export const TrashIcon = () => (
  <IconBase>
    <path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V6" />
  </IconBase>
);

export const UpIcon = () => (
  <IconBase>
    <path d="m18 15-6-6-6 6" />
  </IconBase>
);

export const DownIcon = () => (
  <IconBase>
    <path d="m6 9 6 6 6-6" />
  </IconBase>
);

/** Per-stage-status glyph for the pipeline strip and stage rows. */
export function StageStatusIcon({ status }) {
  if (status === "success") return <CheckIcon />;
  if (status === "failed" || status === "timeout") return <XIcon />;
  if (status === "running") return <DotIcon />;
  if (status === "cancelled" || status === "skipped") return <XIcon />;
  return <ClockIcon />;
}

export const applicationTypeLabel = (value) =>
  APPLICATION_TYPES.find((type) => type.value === value)?.label || value || "Generic";

/**
 * Last-N-builds trend, oldest → newest left to right.
 *
 * Trend is the point — three reds in the last four builds is a different story
 * from one flake five builds ago, and a row of bars tells it faster than any
 * number. Status is encoded in color AND height so it survives monochrome.
 */
export function Sparkline({ statuses = [], max = 10 }) {
  if (!statuses.length) return null;
  const shown = statuses.slice(0, max).reverse(); // API is newest-first.
  return (
    <span className="sg-ci-spark" aria-label={`Last ${shown.length} builds`} role="img">
      {shown.map((status, index) => (
        <i
          key={index}
          className={`sg-ci-spark-bar sg-ci-spark-bar--${status}`}
          title={status}
        />
      ))}
    </span>
  );
}
