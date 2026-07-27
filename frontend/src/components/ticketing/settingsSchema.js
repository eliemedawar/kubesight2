// What the Settings room renders, per provider.
//
// The settings form used to be ~500 lines of hand-written JSX for Zoho's ~20
// fields. Adding Jira that way would have meant a second copy of the same
// scaffolding — rail, cards, completeness pills, dirty tracking, save bar —
// differing only in which inputs it contains. So the *inputs* are data and the
// scaffolding is one component (`TicketingSettingsTab`).
//
// The schema is also what drives the form's initial state and its save payload
// (see `emptyForm` / `formFromConfig` / `configPayload` below), so a provider
// cannot gain a field in the UI and silently fail to send it.
//
// Section shape:
//   key      stable id, used for the rail + scroll anchors
//   label    rail label
//   done     (config, jenkins) => boolean — drives the rail's completeness tick
//   intro    paragraph above the fields
//   pill     "enabled" | "writeback" | "secret" | a literal string | undefined
//   render   "webhook" for the section that shows the copyable URL strip
//   fields   see below
//
// Field shape:
//   key          config/form key (camelCase, as the API speaks it)
//   label        input label
//   type         "text" (default) | "password" | "number" | "checkbox" | "select"
//   placeholder, hint, title, options (for select), min (for number)
//   secretOf     config key that reports whether a secret is already stored;
//                its presence turns the placeholder into "•••• (leave blank to keep)"
//   default      value used by `emptyForm` and as the fallback in `formFromConfig`
//   row          fields sharing a row id are laid out side by side

const SYNC_FIELDS = (opts) => [
  {
    key: "syncApplication",
    type: "checkbox",
    label: "Publish deployments → Application field",
    default: true,
  },
  {
    key: "syncEnvironment",
    type: "checkbox",
    label: "Publish namespaces → Environment field",
    default: true,
  },
  {
    key: "syncVariables",
    type: "checkbox",
    label:
      "Publish deployment env-var names → Variable field (needs the Variable field ID; tickets carrying a Variable + Value become variable-change runs)",
    default: false,
  },
  {
    key: "cascadeEnabled",
    type: "checkbox",
    label: opts.cascadeLabel,
    default: true,
  },
  {
    key: "syncIntervalMinutes",
    type: "number",
    min: 1,
    label: "Auto-sync interval (minutes)",
    default: 30,
  },
];

const ZOHO_SECTIONS = [
  {
    key: "connection",
    label: "Connection",
    pill: "enabled",
    done: (config) => Boolean(config?.orgId && config?.layoutId),
    fields: [
      {
        key: "enabled",
        type: "checkbox",
        label: "Enabled (allows scheduled + manual sync)",
        default: false,
      },
      { key: "orgId", label: "Org ID", placeholder: "854214247", default: "" },
      {
        key: "layoutId",
        label: "Layout ID (DevOps Request)",
        placeholder: "999149000010342586",
        default: "",
      },
      { key: "departmentId", label: "Department ID (optional)", default: "" },
      {
        key: "apiBase",
        label: "Desk API base",
        default: "https://desk.zoho.com/api/v1",
      },
      {
        key: "tokenEndpoint",
        label: "Token endpoint",
        default: "https://accounts.zoho.com/oauth/v2/token",
      },
      // Not shown, but part of the config the form round-trips.
      { key: "accountsBase", hidden: true, default: "https://accounts.zoho.com" },
      { key: "statusFilter", hidden: true, default: "active,degraded" },
    ],
  },
  {
    key: "oauth",
    label: "OAuth client",
    pill: "server-to-server, acts as zagent",
    intro:
      "Secrets are stored encrypted and never shown again — leave a field blank to keep the current value.",
    done: (config) =>
      Boolean(
        config?.clientId && config?.clientSecretConfigured && config?.refreshTokenConfigured
      ),
    fields: [
      { key: "clientId", label: "Client ID", default: "" },
      {
        key: "clientSecret",
        type: "password",
        label: "Client Secret",
        secretOf: "clientSecretConfigured",
        default: "",
      },
      {
        key: "refreshToken",
        type: "password",
        label: "Refresh Token",
        secretOf: "refreshTokenConfigured",
        default: "",
      },
    ],
  },
  {
    key: "fields",
    label: "Field mapping",
    intro:
      "Field IDs come from DEVOPS-REQUEST-FIELD-SYNC-CONFIG.md — they bind the sync to the exact dropdowns on the DevOps Request layout.",
    done: (config) => Boolean(config?.appFieldId),
    wide: true,
    fields: [
      {
        key: "appFieldId",
        label: "Application field ID (deployments dropdown)",
        placeholder: "999149000010343250",
        default: "",
      },
      {
        key: "appFieldApiName",
        label: "Application field API name",
        placeholder: "cf_application",
        default: "cf_application",
      },
      {
        key: "environmentFieldId",
        label: "Environment field ID (namespaces dropdown — optional)",
        placeholder: "999149000010343580",
        hint: "Leave blank to not publish the namespace list.",
        default: "",
      },
      {
        key: "environmentFieldApiName",
        label: "Environment field API name",
        placeholder: "cf_environment",
        default: "cf_environment",
      },
      {
        key: "tagFieldApiName",
        label: "Tag field API name (inbound version)",
        placeholder: "cf_tag",
        default: "cf_tag",
      },
      {
        key: "variableFieldId",
        label: "Variable field ID (env-var picklist — optional)",
        hint: "Must be a Picklist field on the layout. Leave blank to not manage it.",
        default: "",
      },
      {
        key: "variableFieldApiName",
        label: "Variable field API name",
        placeholder: "cf_variable",
        default: "cf_variable",
      },
      {
        key: "valueFieldApiName",
        label: "Value field API name (inbound variable value)",
        placeholder: "cf_value",
        default: "cf_value",
      },
    ],
  },
  {
    key: "sync",
    label: "Sync behaviour",
    intro:
      "Choose which fields KubeSight publishes. A field left off here is yours to edit manually in the layout editor — the sync won't touch it.",
    done: () => true,
    fields: SYNC_FIELDS({
      cascadeLabel:
        "Cascade: filter Application by the selected Environment — and, when variables are published, filter Variable by the selected Application (needs the fields published; requires Desk.settings.CREATE on the Zoho token)",
    }),
  },
  {
    key: "webhook",
    label: "Inbound webhook",
    render: "webhook",
    pill: "secret",
    intro:
      "Configure a Zoho Desk workflow rule to POST new DevOps Request tickets to this URL, sending the shared secret in the X-Ticketing-Secret header (X-Zoho-Secret is also accepted).",
    done: (config) => Boolean(config?.inboundSecretConfigured),
    fields: [
      {
        key: "inboundSecret",
        type: "password",
        label: "Shared secret (X-Ticketing-Secret header)",
        secretOf: "inboundSecretConfigured",
        default: "",
      },
    ],
  },
  {
    key: "writeback",
    label: "Ticket write-back",
    pill: "writeback",
    intro:
      "When a deploy-automation run finishes, update its Desk ticket: set the status, post a comment + resolution describing the result, and reassign the ticket to the service account. Needs the token minted with Desk.tickets.ALL. Status labels must match your Desk config exactly (they're matched literally).",
    done: (config) => Boolean(config?.ticketWritebackEnabled),
    fields: [
      {
        key: "ticketWritebackEnabled",
        type: "checkbox",
        label: "Update the ticket when a run finishes",
        default: false,
      },
      {
        key: "ticketOwnerEmail",
        label: "Owner email",
        title: "The agent every updated ticket is reassigned to",
        placeholder: "zagent@areeba.com",
        default: "zagent@areeba.com",
      },
      { key: "ticketStatusStarted", row: "status", label: "Status — started", placeholder: "Open", default: "Open" },
      { key: "ticketStatusDeployed", row: "status", label: "Status — deployed", placeholder: "Closed", default: "Closed" },
      { key: "ticketStatusFailed", row: "status", label: "Status — failed", placeholder: "Failed", default: "Failed" },
      { key: "ticketStatusCancelled", row: "status", label: "Status — canceled", placeholder: "Canceled", default: "Canceled" },
    ],
  },
];

const JIRA_SECTIONS = [
  {
    key: "connection",
    label: "Connection",
    pill: "enabled",
    intro:
      "The site root only — KubeSight appends the REST path itself, so a pasted /rest/api/3 suffix is stripped on save.",
    done: (config) => Boolean(config?.baseUrl && config?.projectKey),
    fields: [
      {
        key: "enabled",
        type: "checkbox",
        label: "Enabled (allows scheduled + manual sync)",
        default: false,
      },
      {
        key: "baseUrl",
        label: "Site URL",
        placeholder: "https://areeba.atlassian.net",
        default: "",
      },
      {
        key: "deploymentType",
        type: "select",
        label: "Deployment",
        options: [
          { value: "cloud", label: "Jira Cloud (email + API token)" },
          { value: "server", label: "Jira Server / Data Center (personal access token)" },
        ],
        hint: "Cloud uses REST v3 and HTTP Basic; Server/DC uses v2 and a Bearer token.",
        default: "cloud",
      },
      { key: "projectKey", label: "Project key", placeholder: "KUB", default: "" },
      {
        key: "issueTypeId",
        label: "Issue type ID (optional)",
        hint: "The DevOps Request issue type. Informational — the webhook decides what arrives.",
        default: "",
      },
      {
        key: "screenId",
        label: "Screen ID",
        placeholder: "10120",
        hint: "Jira places fields on screens; this is the screen KubeSight is allowed to edit.",
        default: "",
      },
    ],
  },
  {
    key: "credential",
    label: "Credential",
    pill: "API token / PAT",
    intro:
      "Stored encrypted and never shown again — leave blank to keep the current value. The account needs Jira administrator rights to edit custom-field options and screens.",
    done: (config) => Boolean(config?.apiTokenConfigured),
    fields: [
      {
        key: "email",
        label: "Account email (Cloud only)",
        placeholder: "zagent@areeba.com",
        hint: "The Atlassian account the API token belongs to. Not used for Server/DC tokens.",
        default: "",
      },
      {
        key: "apiToken",
        type: "password",
        label: "API token / personal access token",
        secretOf: "apiTokenConfigured",
        default: "",
      },
    ],
  },
  {
    key: "fields",
    label: "Field mapping",
    intro:
      "Jira custom field ids look like customfield_10050 — the id is also the key the webhook sends, so the API name is filled in for you if you leave it blank.",
    done: (config) => Boolean(config?.appFieldId),
    wide: true,
    fields: [
      {
        key: "appFieldId",
        label: "Application field ID (deployments dropdown)",
        placeholder: "customfield_10050",
        default: "",
      },
      {
        key: "appFieldApiName",
        label: "Application field key",
        placeholder: "customfield_10050",
        hint: "Defaults to the field ID.",
        default: "",
      },
      {
        key: "environmentFieldId",
        label: "Environment field ID (namespaces dropdown — optional)",
        placeholder: "customfield_10051",
        hint: "Leave blank to not publish the namespace list.",
        default: "",
      },
      {
        key: "environmentFieldApiName",
        label: "Environment field key",
        placeholder: "customfield_10051",
        default: "",
      },
      {
        key: "tagFieldApiName",
        label: "Tag field key (inbound version)",
        placeholder: "customfield_10052",
        default: "",
      },
      {
        key: "variableFieldId",
        label: "Variable field ID (env-var dropdown — optional)",
        hint: "Must be a select field on the screen. Leave blank to not manage it.",
        default: "",
      },
      {
        key: "variableFieldApiName",
        label: "Variable field key",
        default: "",
      },
      {
        key: "valueFieldApiName",
        label: "Value field key (inbound variable value)",
        default: "",
      },
      {
        key: "cascadeFieldId",
        label: "Cascade field ID (cascading select — optional)",
        hint: "Jira cannot filter one field by another. Point this at a cascading-select field and the sync publishes the whole Environment → Application tree into it.",
        default: "",
      },
      {
        key: "cascadeFieldApiName",
        label: "Cascade field key",
        hint: "Defaults to the field ID.",
        default: "",
      },
    ],
  },
  {
    key: "sync",
    label: "Sync behaviour",
    intro:
      "Choose which fields KubeSight publishes. A field left off here is yours to edit manually in the screen editor — the sync won't touch it.",
    done: () => true,
    fields: SYNC_FIELDS({
      cascadeLabel:
        "Cascade: publish the Environment → Application tree into the cascading-select field above (skipped with a note when no cascade field is set)",
    }),
  },
  {
    key: "webhook",
    label: "Inbound webhook",
    render: "webhook",
    pill: "secret",
    intro:
      "Add a Jira webhook (or an Automation rule) that POSTs created/updated issues to this URL, sending the shared secret in the X-Ticketing-Secret header. The webhook must include the custom fields above in its payload.",
    done: (config) => Boolean(config?.inboundSecretConfigured),
    fields: [
      {
        key: "inboundSecret",
        type: "password",
        label: "Shared secret (X-Ticketing-Secret header)",
        secretOf: "inboundSecretConfigured",
        default: "",
      },
    ],
  },
  {
    key: "writeback",
    label: "Issue write-back",
    pill: "writeback",
    intro:
      "When a deploy-automation run finishes, move its issue through a workflow transition, reassign it and post a comment. Jira has no 'set status' — these are TRANSITION names, matched against whatever is available on the issue at that moment, so a transition the workflow does not offer is skipped and noted rather than failing the run.",
    done: (config) => Boolean(config?.ticketWritebackEnabled),
    fields: [
      {
        key: "ticketWritebackEnabled",
        type: "checkbox",
        label: "Update the issue when a run finishes",
        default: false,
      },
      {
        key: "ticketOwnerEmail",
        label: "Assignee email",
        title: "The account every updated issue is assigned to",
        placeholder: "zagent@areeba.com",
        default: "",
      },
      { key: "transitionStarted", row: "status", label: "Transition — started", placeholder: "In Progress", default: "In Progress" },
      { key: "transitionDeployed", row: "status", label: "Transition — deployed", placeholder: "Done", default: "Done" },
      { key: "transitionFailed", row: "status", label: "Transition — failed", placeholder: "Done", default: "Done" },
      { key: "transitionCancelled", row: "status", label: "Transition — cancelled", placeholder: "Done", default: "Done" },
    ],
  },
];

const SCHEMAS = { zoho: ZOHO_SECTIONS, jira: JIRA_SECTIONS };

// The Jenkins router is one shared connection edited from either provider's tab,
// so it is appended to every schema's rail rather than living in one of them.
export const JENKINS_SECTION = { key: "jenkins", label: "Jenkins router" };

export function sectionsFor(providerKey) {
  return SCHEMAS[providerKey] || [];
}

export function railFor(providerKey) {
  return [...sectionsFor(providerKey).map((s) => ({ key: s.key, label: s.label })), JENKINS_SECTION];
}

export function sectionDone(providerKey, sectionKey, config, jenkins) {
  if (sectionKey === JENKINS_SECTION.key) {
    return Boolean(jenkins?.baseUrl && jenkins?.apiTokenConfigured);
  }
  const section = sectionsFor(providerKey).find((s) => s.key === sectionKey);
  return section ? Boolean(section.done?.(config, jenkins)) : false;
}

/** Every field across a provider's sections, flattened. */
export function allFields(providerKey) {
  return sectionsFor(providerKey).flatMap((s) => s.fields || []);
}

/** Blank form state: every field at its declared default. */
export function emptyForm(providerKey) {
  const out = {};
  for (const field of allFields(providerKey)) out[field.key] = field.default ?? "";
  return out;
}

/**
 * Form state from a saved config.
 *
 * Secrets are write-only by contract: the API returns only whether one is set,
 * so a `secretOf` field always starts blank and a blank one on save means
 * "keep the current value".
 */
export function formFromConfig(providerKey, config = {}) {
  const out = {};
  for (const field of allFields(providerKey)) {
    if (field.secretOf) {
      out[field.key] = "";
      continue;
    }
    const value = config[field.key];
    if (field.type === "checkbox") {
      out[field.key] = value === undefined ? Boolean(field.default) : Boolean(value);
    } else if (Array.isArray(value)) {
      // statusFilter comes back as a list and is edited as a comma string.
      out[field.key] = value.join(", ");
    } else {
      out[field.key] = value === undefined || value === null || value === "" ? field.default ?? "" : value;
    }
  }
  return out;
}

/**
 * The PUT body for a form.
 *
 * Secrets are omitted when blank so an untouched form never clears a stored one,
 * and numbers are coerced here rather than in the input handler so the form can
 * hold the half-typed string a number input produces.
 */
export function configPayload(providerKey, form) {
  const payload = {};
  for (const field of allFields(providerKey)) {
    const value = form[field.key];
    if (field.secretOf) {
      if (String(value ?? "").trim()) payload[field.key] = String(value).trim();
      continue;
    }
    if (field.type === "checkbox") payload[field.key] = Boolean(value);
    else if (field.type === "number") payload[field.key] = Number(value) || field.default || 0;
    else payload[field.key] = value ?? "";
  }
  return payload;
}
