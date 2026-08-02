import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import ErrorBanner from "../../components/common/ErrorBanner.jsx";
import LoadingState from "../../components/common/LoadingState.jsx";
import { getSmtpSettings, listReceivers } from "../../api/alertRoutingApi.js";
import { SmtpTab, ReceiversTab } from "../AlertRoutingPage.jsx";
import ICONS from "./settingsIcons.jsx";

const ImageRegistriesPage = lazy(() => import("../ImageRegistriesPage.jsx"));
const TicketingConfigPanel = lazy(() => import("./TicketingConfigPanel.jsx"));

/**
 * The Configuration tab, per integration.
 *
 * Every panel here mounts the form that already exists for that provider rather
 * than a second copy of it. A duplicated credential form is a form that drifts:
 * the two versions validate differently, one learns about a new field and the
 * other does not, and eventually they disagree about what "saved" means. So the
 * hub is a new address for these forms, not a new implementation of them.
 *
 * Where no reusable form exists — Bitbucket's CRUD is welded into the
 * Application Intelligence analyze flow, and Hermes is configured by
 * environment variable — the panel says where the settings actually live
 * instead of pretending to own them.
 */

function ReadOnlyNotice({ children }) {
  return (
    <p className="settings-card-footnote sg-int-readonly">
      {ICONS.lock}
      <span>{children}</span>
    </p>
  );
}

/** A panel that cannot edit here, and says where it can. */
function HandoffPanel({ title, body, actionLabel, onAction }) {
  return (
    <section className="card sg-int-handoff">
      <h4>{title}</h4>
      <p>{body}</p>
      {actionLabel && onAction ? (
        <button type="button" className="btn-outline" onClick={onAction}>
          {actionLabel}
        </button>
      ) : null}
    </section>
  );
}

/* ─── SMTP ─── */
function SmtpConfig({ onChanged }) {
  const [smtp, setSmtp] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setSmtp(await getSmtpSettings());
    } catch (err) {
      setError(err.message || "Failed to load SMTP settings.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading) return <LoadingState label="Loading SMTP settings..." />;
  return (
    <>
      {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}
      <SmtpTab
        smtp={smtp}
        onSaved={() => {
          load();
          onChanged?.();
        }}
      />
    </>
  );
}

/* ─── Slack / Webhooks ───
   Both are rows in the same receivers table, discriminated by type, so one
   component serves both narrowed to the type its card stands for. */
function ReceiversConfig({ typeFilter, title, description, emptyMessage, onChanged }) {
  const [receivers, setReceivers] = useState([]);
  const [users, setUsers] = useState([]);
  const [roles, setRoles] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const receiverList = await listReceivers();
      setReceivers(receiverList?.items || receiverList || []);
      // The shared modal takes users and roles for user/role receivers, which
      // this screen never shows — empty lists rather than two calls whose
      // results cannot affect anything rendered here.
      setUsers([]);
      setRoles([]);
    } catch (err) {
      setError(err.message || "Failed to load receivers.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading) return <LoadingState label="Loading destinations..." />;
  return (
    <>
      {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}
      <ReceiversTab
        receivers={receivers}
        groups={[]}
        users={users}
        roles={roles}
        typeFilter={typeFilter}
        title={title}
        description={description}
        emptyMessage={emptyMessage}
        onChanged={() => {
          load();
          onChanged?.();
        }}
      />
    </>
  );
}

/* ─── Hermes ───
   Configured by environment variable on the backend, so there is nothing to
   edit here and nothing to pretend about. */
function HermesConfig({ integration }) {
  return (
    <>
      <HandoffPanel
        title="Configured by environment"
        body="Hermes is wired up through HERMES_API_URL and HERMES_API_TOKEN on the backend, not through this screen. Change them where the backend is deployed, then use Test connection on the Overview tab to confirm the new endpoint answers."
      />
      <ReadOnlyNotice>{integration.message}</ReadOnlyNotice>
    </>
  );
}

export default function ConfigurationPanel({ integration, hasPermission, onChanged }) {
  const key = integration.key;

  if (key === "smtp") {
    return <SmtpConfig onChanged={onChanged} />;
  }

  if (key === "slack") {
    return (
      <ReceiversConfig
        typeFilter="slack"
        title="Slack destinations"
        description="Slack incoming-webhook URLs that alerts are posted to."
        emptyMessage="No Slack destination yet. Add one to start posting alerts to a channel."
        onChanged={onChanged}
      />
    );
  }

  if (key === "webhooks") {
    return (
      <ReceiversConfig
        typeFilter="webhook"
        title="Webhook destinations"
        description="HTTP endpoints alerts are POSTed to, with optional headers and a signing secret."
        emptyMessage="No webhook endpoint yet. Add one to forward alerts to your own service."
        onChanged={onChanged}
      />
    );
  }

  if (key === "registries") {
    return (
      <Suspense fallback={<LoadingState label="Loading registries..." />}>
        <ImageRegistriesPage embedded canManage={hasPermission("registries:manage")} />
      </Suspense>
    );
  }

  if (key === "jira" || key === "zoho" || key === "jenkins") {
    return (
      <Suspense fallback={<LoadingState label="Loading configuration..." />}>
        <TicketingConfigPanel
          providerKey={key === "jenkins" ? "jira" : key}
          showJenkinsOnly={key === "jenkins"}
          canManage={hasPermission("ticketing:manage")}
          onChanged={onChanged}
        />
      </Suspense>
    );
  }

  if (key === "hermes") {
    return <HermesConfig integration={integration} />;
  }

  if (key === "bitbucket") {
    return (
      <HandoffPanel
        title="Managed with the repositories that use them"
        body="Bitbucket credential profiles are created and picked while starting a repository analysis, so they live in Application Intelligence rather than here. This card exists so you can see, in one place, whether any usable credentials are saved."
      />
    );
  }

  return (
    <HandoffPanel
      title="Nothing to configure"
      body={`${integration.name} does not expose any settings through KubeSight.`}
    />
  );
}
