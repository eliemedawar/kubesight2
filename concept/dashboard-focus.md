# KubeSight · Focused dashboard concept

Open `dashboard-focus.html` directly in a browser. No build or server is required. All values and history are illustrative. The existing application is unchanged.

## Design direction

A calm operational workspace with a clear reading order: identify the cluster, understand capacity, locate the node that needs attention. White surfaces, subtle borders, restrained resource colors, and amber warnings keep the important information easy to scan. Coral is reserved for KubeSight branding and navigation.

## What stays

- Cluster selector, node readiness, data freshness, and refresh.
- Four summary cards: CPU, memory, local storage, and running/pending pods.
- One resource chart with memory selected initially; CPU and storage are available as tabs.
- An actionable attention panel linked to affected nodes.
- Node capacity as the primary detailed view: readiness, CPU, memory used/total/free, filesystem used/total/free, and pods.

## What moves out of the overview

- Namespace breakdowns and detailed workload inventories → Workloads.
- Network charts → dedicated observability views.
- Routine event feeds → Activity.
- Version checks, upgrades, and cluster metadata → cluster details.
- Persistent volume management → Storage.
- AI summaries, greetings, duplicate health tiles, and decorative trend claims are omitted.

Navigation entries in this prototype explain the intended destination; they do not implement those other screens.

## Interactions and responsive behavior

Search nodes by name (`/` focuses search), filter to nodes needing attention, and sort by resource usage. Select a node or attention item to open its detail drawer; Escape or the close button dismisses it. Metric and time-range controls update the illustrative chart. Switch to staging to review a quiet cluster with missing storage telemetry. Refresh explicitly reloads the sample snapshot.

On mobile, the node table becomes a stacked layout with memory and storage side by side. Resource values remain visible without horizontal scrolling. Secondary navigation collapses, and the node drawer fits the viewport.

## Data semantics for implementation

- CPU and memory compare measured use with allocatable capacity. The live implementation must confirm and label the denominator provided by its API.
- Local storage means measured node filesystem usage, not PVC provisioned capacity or Kubernetes ephemeral-storage allocation.
- Readiness is independent of resource usage: a Ready node may still need capacity attention.
- Missing telemetry remains unavailable, never zero. Aggregates must disclose partial reporting, as demonstrated by staging storage.
- The illustrative warning threshold is 80%. Production should use the configured alert policy and required duration, not a hardcoded universal threshold.
- The current dashboard exposes node memory fields. Integrating this concept requires verifying node CPU and filesystem telemetry availability and extending the API where needed. Do not derive storage usage from requested capacity.
- Production history must use timestamped observations from the metrics service. The prototype's deterministic history is for visual review only.
- Preserve existing access controls, cluster scope, loading, empty, and error states when applying the design.

## Validation

Browser checks cover search, filtering, empty results, sorting, node drawers and Escape, metric/range switching, cluster switching, unavailable telemetry, simulated refresh, and page overflow at 1440, 1024, 768, 390, and 360 pixels. Desktop and mobile PNG previews accompany the HTML.
