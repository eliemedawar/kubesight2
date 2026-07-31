// Coach-mark step definitions for every page, keyed by the page keys used in
// utils/authz.js NAV_PAGES (plus drill-down pages and a one-time "welcome"
// tour for the global chrome).
//
// RBAC works on two levels:
//  1. `when(ctx)` predicates mirror the exact permission checks the pages use
//     to render their controls, so users never get a step for a feature their
//     role can't see. ctx = { isAdmin, hasPermission, pageAllowed }.
//  2. The CoachMarks engine skips any step whose `target` never appears in
//     the DOM — covering empty states and controls gated deeper in the tree.
//
// `title` and `body` may be strings or functions of ctx, so the copy itself
// can differ between admins and other roles.

import { pageNeedsClusterContext, pageNeedsNamespaceContext } from "../utils/authz.js";

export const WELCOME_TOUR_KEY = "welcome";

const WELCOME_STEPS = [
  {
    target: '[data-tour="sidebar-nav"]',
    title: "Welcome to KubeSight",
    body: (ctx) =>
      ctx.isAdmin
        ? "The sidebar is your map. As an administrator you see every area — infrastructure, monitoring, services, and administration."
        : "The sidebar is your map. It only lists the areas your role has access to, so what you see here is exactly what you can use.",
  },
  {
    target: '[data-tour="cluster-select"]',
    title: "Active cluster",
    body: "Most pages are scoped to this cluster. Switch it here and dashboards, resources, logs, and alerts follow along.",
    // The topbar only shows the selector on cluster-scoped pages, so gate the
    // step on the page the welcome tour happens to run on.
    when: (ctx) => pageNeedsClusterContext(ctx.pageKey),
  },
  {
    target: '[data-tour="namespace-select"]',
    title: "Active namespace",
    body: "Narrow the view to one namespace. Resource and log pages honour this selection.",
    when: (ctx) => pageNeedsNamespaceContext(ctx.pageKey),
  },
  {
    target: '[data-tour="notifications"]',
    title: "Notifications",
    body: "Alert and deployment-request updates land here. The badge counts what's new since you last looked.",
  },
  {
    target: ".fab",
    title: "Change Bundle",
    body: "Stage Kubernetes changes as you browse, then submit them together for approval with a deployment window.",
  },
  {
    target: '[data-tour="help-button"]',
    title: "Replay tips anytime",
    body: "These tips show once per page. Click this ? button whenever you want to replay them for the page you're on.",
  },
];

const PAGE_TOURS = {
  dashboard: [
    {
      target: ".sg-ph",
      title: (ctx) => (ctx.isAdmin ? "Operations Dashboard" : "Your dashboard"),
      body: "A live health summary of the active cluster. The status pill next to the title reflects overall cluster health right now.",
    },
    {
      target: ".ov-range",
      title: "Time range",
      body: "Choose how far back the CPU, memory, and network charts look.",
    },
    {
      target: ".sg-ph-actions .primary",
      title: "Upgrade Safe Mode",
      body: "Jump to the Upgrade Center to precheck and plan a Kubernetes version upgrade.",
      when: (ctx) => ctx.pageAllowed("upgrade"),
    },
    {
      target: ".sg-kpi-grid",
      title: "Cluster vitals",
      body: "Health, CPU, memory, nodes, running pods, and active alerts at a glance — each tile trends over the selected range.",
    },
    {
      target: ".sg-feed",
      title: "Recent events",
      body: "The latest cluster events. Use “View all” to investigate anything suspicious.",
    },
    {
      target: ".dashboard-my-access-first",
      title: "Your access at a glance",
      body: "This panel summarises the clusters, namespaces, and permissions your account has been granted.",
      when: (ctx) => !ctx.isAdmin,
    },
  ],

  clusters: [
    {
      target: ".sg-ph",
      title: "Clusters",
      body: (ctx) =>
        ctx.isAdmin
          ? "Every cluster connected to KubeSight, with live health."
          : "The clusters your account has been granted access to, with live health.",
    },
    {
      target: ".sg-card-grid",
      title: "Cluster cards",
      body: "Each card shows status, node and pod counts. Click a card to drill into that cluster's overview.",
    },
    {
      target: ".sg-ph-actions .primary",
      title: "Configure recipients",
      body: "Manage who receives email notifications for deployment requests.",
      when: (ctx) => ctx.hasPermission("deployment_requests:manage"),
    },
  ],

  clusterManagement: [
    {
      target: ".cluster-mgmt-header",
      title: "Cluster Management",
      body: "Connect and maintain the clusters KubeSight manages. Kubeconfig is the recommended way to onboard one.",
    },
    {
      target: ".cluster-mgmt-header .primary",
      title: "Add a cluster",
      body: "Upload a kubeconfig (recommended) or enter API settings manually. KubeSight stores a server-side kubeconfig for kubectl operations.",
      when: (ctx) => ctx.hasPermission("clusters:add"),
    },
    {
      // The shared DataTable renders no class on its <table>; .table-shell is
      // its stable wrapper.
      target: ".table-shell",
      title: "Connected clusters",
      body: "Test, edit, or remove connections from each row — the actions you see match your permissions.",
    },
  ],

  clusterOverview: [
    {
      target: ".page-title",
      title: "Cluster Overview",
      body: "A deeper look at the cluster you selected — status, version, and capacity.",
    },
    {
      target: ".stat-grid",
      title: "Key facts",
      body: "Status, Kubernetes version, CPU and memory usage, and the namespaces in your scope.",
    },
  ],

  namespaces: [
    {
      target: ".page-title",
      title: "Namespaces",
      body: (ctx) =>
        ctx.isAdmin
          ? "All namespaces in the active cluster, segmented by tenancy."
          : "The namespaces you can see in the active cluster — your access rules decide this list.",
    },
    {
      target: ".table-shell",
      title: "Usage at a glance",
      body: "Pods, deployments, services, CPU, memory, and alert counts per namespace.",
    },
  ],

  resources: [
    {
      target: ".resources-tab-bar",
      title: "Resource types",
      body: (ctx) =>
        ctx.isAdmin
          ? "Switch between pods, deployments, services, and the other resource types in the selected namespace."
          : "Switch between resource types. Only the types your role can view appear as tabs.",
    },
    {
      target: ".resource-search-input",
      title: "Search",
      body: "Filter the current tab by name — handy in busy namespaces.",
    },
    {
      target: ".resources-issues-filter",
      title: "Issues only",
      body: "Hide healthy workloads and focus on what's crashing, pending, or restarting.",
    },
    {
      target: ".resources-allns-filter",
      title: "All namespaces",
      body: "On the Pods tab you can widen the view beyond the active namespace.",
    },
    {
      target: ".resources-tab-refresh",
      title: "Refresh",
      body: "Re-fetch the current tab. Data is cached briefly to keep navigation fast.",
    },
  ],

  inventory: [
    {
      target: ".page-title",
      title: "Inventory Templates",
      body: "Your deployment hub. Launch workloads from templates here, then watch them run in Resources.",
    },
    {
      target: ".page-title-action",
      title: (ctx) => (ctx.hasPermission("apps:deploy") ? "Start from scratch" : "Add application"),
      body: (ctx) =>
        ctx.hasPermission("apps:deploy")
          ? "Open the guided wizard to build and deploy a workload step by step — no template needed."
          : "Register an application that already runs in the cluster so KubeSight can track it.",
      when: (ctx) => ctx.hasPermission("apps:deploy") || ctx.hasPermission("inventory:register"),
    },
    {
      target: ".template-marketplace__filters",
      title: "Find a template",
      body: "Search and filter the template marketplace by category.",
    },
    {
      target: ".template-marketplace__grid",
      title: "Template cards",
      body: "Each card is a ready-made blueprint. Pick one to pre-fill the deploy wizard.",
    },
  ],

  myRequests: [
    {
      target: ".sg-ph",
      title: "My Requests",
      body: "Every deployment request you've submitted, with its current status. You'll also get a notification when a decision lands.",
    },
    {
      target: ".tab-bar",
      title: "Active vs History",
      body: "Active lists requests still awaiting a decision; History keeps the rest, with search and status filters.",
    },
  ],

  changeBundles: [
    {
      target: ".card-header-row",
      title: "Change Bundles",
      body: "Stage several Kubernetes changes, submit them as one bundle with a deployment window, and let KubeSight apply them when the window opens.",
    },
    {
      target: ".btn-primary.btn-compact",
      title: "Open the bundle drawer",
      body: "Collect changes as you browse other pages — they stage here until you submit.",
      when: (ctx) => ctx.hasPermission("change_bundles:create"),
    },
    {
      target: ".tab-bar",
      title: "Approvals",
      body: "Switch between your own bundles and the ones waiting for your approval.",
      when: (ctx) => ctx.hasPermission("change_bundles:manage"),
    },
  ],

  logs: [
    {
      target: ".log-filters",
      title: "Pick a target",
      body: "Choose cluster, namespace, pod, and container to pull logs from. The lists respect your access scope.",
    },
    {
      target: ".log-filters-toggles",
      title: "Live streaming",
      body: "Toggle live tail, timestamps, and previous-container logs.",
    },
    {
      target: ".log-viewer",
      title: "Read and search",
      body: "Search text, filter by level, jump to the latest lines, or maximize the viewer for a full-screen read.",
    },
  ],

  alerts: [
    {
      target: ".al-ph",
      title: "Alert center",
      body: "Everything firing in your scope, grouped and triaged. The chips show which clusters and namespaces feed this view.",
    },
    {
      target: ".al-tabs",
      title: "Sections",
      body: (ctx) =>
        `Open shows what's firing now, History what has resolved, and Policies defines what gets watched.${
          ctx.isAdmin ? " Routing (admin-only) controls where notifications are delivered." : ""
        }`,
    },
    {
      target: ".al-search",
      title: "Filter alerts",
      body: "Search by resource, policy, or pattern to cut through a noisy feed.",
    },
    {
      target: ".al-seg",
      title: "Alert types",
      body: "Toggle between infrastructure and service alerts.",
    },
  ],

  serviceCatalog: [
    {
      target: ".sg-ph",
      title: "Service Catalog",
      body: "Reusable service blueprints — versioned definitions of the services your team deploys.",
    },
    {
      target: ".sg-cat-new",
      title: "New blueprint",
      body: "Define a new service blueprint: components, defaults, and topology.",
      when: (ctx) => ctx.hasPermission("service_blueprints:create"),
    },
    {
      target: ".sg-cat-tabs",
      title: "Status filter",
      body: "Filter blueprints by lifecycle status.",
    },
    {
      target: ".sg-cat-search",
      title: "Search",
      body: "Find a blueprint by name.",
    },
    {
      target: ".sg-card-grid",
      title: "Blueprint cards",
      body: (ctx) =>
        ctx.hasPermission("service_blueprints:deploy")
          ? "Open a card for details — or hit Deploy to launch the blueprint through the wizard."
          : "Open a card to inspect the blueprint's components and versions.",
    },
  ],

  applicationServices: [
    {
      target: ".page-title",
      title: "Application Services",
      body: "Logical service groupings with topology and deployment mappings — how your components fit together.",
    },
    {
      target: ".page-title-action",
      title: "New service",
      body: "Create a service grouping and draw its topology on the canvas.",
      when: (ctx) => ctx.hasPermission("app_services:create"),
    },
    {
      target: ".user-filters",
      title: "Search & health",
      body: "Filter services by name or health state to spot trouble quickly.",
    },
  ],

  components: [
    {
      target: ".page-title",
      title: "Components",
      body: "The building blocks referenced by services and blueprints.",
    },
    {
      target: ".page-title-action",
      title: "New component",
      body: "Register a component so blueprints and services can reference it.",
      when: (ctx) => ctx.hasPermission("components:create"),
    },
    {
      target: ".user-filters",
      title: "Filter",
      body: "Search and filter the component list.",
    },
  ],

  clients: [
    {
      target: ".page-title",
      title: "Clients",
      body: "Client organisations and their connectivity to your services.",
    },
    {
      target: ".page-title-action",
      title: "New client",
      body: "Add a client, then map which services they reach and over which transports.",
      when: (ctx) => ctx.hasPermission("clients:create"),
    },
    {
      target: ".user-filters",
      title: "Filter",
      body: "Search clients by name. Open one to see its service-access topology.",
    },
  ],

  userManagement: [
    {
      target: ".sg-ph",
      title: "Users & access",
      body: "Manage who can sign in and what they can do.",
    },
    {
      target: ".users-invite",
      title: "Invite a user",
      body: "New users get a temporary password, then set their own and enrol in MFA on first login.",
      when: (ctx) => ctx.hasPermission("users:create") || ctx.hasPermission("users:manage"),
    },
    {
      target: ".user-management-tabs",
      title: "Users and roles",
      body: "Roles bundle permissions and access rules — assign one per user, or build custom roles.",
      when: (ctx) => ctx.hasPermission("roles:view"),
    },
    {
      target: ".users-kpis",
      title: "Account health",
      body: "Active, still-onboarding, and locked accounts. Locked users can be unlocked from their row actions.",
    },
    {
      target: ".user-filters",
      title: "Find a user",
      body: "Search by name or filter by role.",
    },
    {
      target: ".users-table-card",
      title: "User actions",
      body: (ctx) =>
        ctx.isAdmin
          ? "Edit, disable, unlock, or resend a temporary password from each row."
          : "The actions on each row match your permissions — editing and disabling require additional rights.",
    },
  ],

  auditLogs: [
    {
      target: ".card.ops-section",
      title: "Audit Logs",
      body: "A tamper-evident trail of who did what, and when, across KubeSight.",
    },
    {
      target: ".user-filters",
      title: "Filter the trail",
      body: "Narrow by user, action, or search text to investigate a specific event.",
    },
  ],

  deploymentRequests: [
    {
      target: ".sg-ph",
      title: "Deployment Requests",
      body: "Requests submitted by users who need an approval before deploying.",
    },
    {
      target: ".tab-bar",
      title: "Queue views",
      body: "Active holds requests awaiting a decision; History keeps past decisions, with search and status filters.",
    },
    {
      target: ".sg-rq-list",
      title: (ctx) => (ctx.hasPermission("deployment_requests:manage") ? "Approve or deny" : "Request queue"),
      body: (ctx) =>
        ctx.hasPermission("deployment_requests:manage")
          ? "Review each request and approve or deny it — the requester is notified either way."
          : "Track the queue of requests and their outcomes.",
    },
  ],

  imageRegistries: [
    {
      target: ".page-title",
      title: "Image Registries",
      body: "Link a container registry (e.g. Sonatype Nexus) so KubeSight verifies each image exists before deploying.",
    },
    {
      target: ".page-title-action",
      title: "Add a registry",
      body: "Point at your registry host, choose auth, and pick the enforcement mode for pre-deploy checks.",
      when: (ctx) => ctx.hasPermission("registries:manage"),
    },
    {
      target: ".data-table",
      title: "Linked registries",
      body: (ctx) =>
        ctx.hasPermission("registries:manage")
          ? "Test, edit, or remove a registry from its row. The last-test badge shows connection health."
          : "The registries currently linked, with their enforcement mode and connection health.",
    },
    {
      target: ".inline-form",
      title: "Check an image",
      body: "Paste any image reference to test availability — the same check runs automatically before every deploy.",
    },
  ],

  // The Ticketing tab has two shapes: the provider picker, and one provider's
  // workspace. The picker's steps run when no provider is open (the cards are on
  // screen); the workspace steps only match once one is.
  ticketing: [
    {
      target: ".sg-tk-grid",
      title: "Ticketing",
      body: "Pick the platform your deployment tickets come from. Jira and Zoho Desk both feed the same pipeline — tickets in, Jenkins deploys, outcome written back.",
    },
    {
      target: ".sg-tk-note",
      title: "One deploy surface",
      body: "The source cluster, its namespaces and the custom environments belong to this provider — each tab publishes its own selection. The Jenkins router itself is one shared connection.",
    },
    {
      target: ".sg-zh-cmdbar",
      title: "The integration",
      body: "Deployment tickets flow from your ticketing platform into Jenkins deployments. The pill shows whether this integration is on.",
    },
    {
      target: ".sg-zh-copy",
      title: "Refresh",
      body: "Re-read the config, live deployments, the ticket form, and the ticket log.",
    },
    {
      target: ".sg-zh-cmdactions .secondary",
      title: "Test connection",
      body: "Verify KubeSight can reach the platform before relying on the sync.",
      when: (ctx) => ctx.hasPermission("ticketing:manage"),
    },
    {
      target: ".sg-zh-cmdactions .primary",
      title: "Sync now",
      body: "Trigger an immediate field sync instead of waiting for the next scheduled run.",
      when: (ctx) => ctx.hasPermission("ticketing:manage"),
    },
    {
      target: ".sg-zh-subtabs",
      title: "Sections",
      body: "Overview for status, Field sync for mappings, Tickets & runs for live deployments, and Settings for configuration.",
    },
  ],

  mobileApps: [
    {
      target: ".page-title",
      title: "Mobile Applications",
      body: "Jenkins builds land here as APK, AAB, and IPA binaries — pulled from deployment tickets — ready to publish to Google Play and the App Store.",
    },
    {
      target: ".sg-ma-flow",
      title: "The release pipeline",
      body: "Every binary follows this path: a deployment ticket triggers the Jenkins build, the artifact lands in KubeSight's binary store, and from there it ships to the stores. The counts are live.",
    },
    {
      target: ".sg-ma-tiles",
      title: "Tiles are filters",
      body: "Ready, in flight, or failed — summarised from every app's latest build. Click a tile to filter the cards below to just those apps; click again to clear.",
    },
    {
      target: ".sg-ma-grid",
      title: "Your applications",
      body: "Each card is a registered app: platforms, Zoho environment, latest build, and whether its store credentials are ready. Open one to see its releases and setup.",
    },
    {
      target: ".page-title-action",
      title: "Register an application",
      body: "Link a Jenkins job, artifact patterns, and store credentials, then fetch builds from deployment tickets.",
      when: (ctx) => ctx.hasPermission("mobile_apps:manage"),
    },
    {
      target: ".sg-ma-grid",
      title: "Publishing to the stores",
      body: "Publishing a build to Google Play or the App Store is an administrator-only action — you'll confirm it from inside an app's drawer, per release.",
      when: (ctx) => ctx.isAdmin,
    },
  ],

  settings: [
    {
      target: ".settings-rail",
      title: "Settings sections",
      body: (ctx) =>
        ctx.isAdmin
          ? "Jump between sections. Administration shortcuts to related admin pages live at the bottom."
          : "Jump between your profile, appearance, and security sections.",
    },
    {
      target: ".theme-tiles",
      title: "Theme",
      body: "Light, dark, or follow the OS. This is a per-browser preference — changing it never affects teammates.",
    },
    {
      target: "#settings-workspace",
      title: "Workspace defaults",
      body: (ctx) =>
        ctx.hasPermission("settings:manage")
          ? "Defaults that apply to everyone. Edit them and a save bar appears at the bottom of the page."
          : "Defaults that apply to everyone — shown read-only because your role can't change them.",
    },
    {
      target: "#settings-security",
      title: "Security",
      body: "Password and multi-factor authentication for your own account.",
    },
  ],

  upgrade: [
    {
      target: ".page-title",
      title: "Upgrade Center",
      body: "Plan and execute Kubernetes version upgrades safely.",
    },
    {
      target: ".upgrade-middle-grid",
      title: "Precheck & plan",
      body: "Precheck results grade cluster readiness; the upgrade plan lists the steps that will run.",
    },
    {
      target: ".upgrade-actions",
      title: (ctx) => (ctx.hasPermission("upgrades:start") ? "Run it" : "Run a precheck"),
      body: (ctx) =>
        ctx.hasPermission("upgrades:start")
          ? "Run a precheck first, then start the upgrade — KubeSight blocks the start until the cluster is ready."
          : "Run a precheck to evaluate readiness. Starting the upgrade itself requires additional rights.",
      when: (ctx) => ctx.hasPermission("upgrades:precheck") || ctx.hasPermission("upgrades:start"),
    },
  ],

  applicationDetails: [
    {
      target: ".app-details-tabs",
      title: "Application sections",
      body: "Overview, workloads, Helm history, and more for this application.",
    },
    {
      target: ".app-details-panel",
      title: "Details & actions",
      body: "Everything KubeSight knows about this app. Action buttons — deploy, Helm upgrade or rollback, view logs — appear based on your permissions.",
    },
  ],
};

function resolveSteps(defs, ctx) {
  return defs
    .filter((step) => (typeof step.when === "function" ? Boolean(step.when(ctx)) : true))
    .map((step) => ({
      target: step.target,
      title: typeof step.title === "function" ? step.title(ctx) : step.title,
      body: typeof step.body === "function" ? step.body(ctx) : step.body,
    }));
}

export function getWelcomeSteps(ctx) {
  return resolveSteps(WELCOME_STEPS, ctx);
}

export function getTourSteps(pageKey, ctx) {
  const defs = PAGE_TOURS[pageKey];
  if (!defs || !defs.length) {
    return [];
  }
  return resolveSteps(defs, ctx);
}

export function hasTour(pageKey) {
  return Boolean(PAGE_TOURS[pageKey]?.length);
}
