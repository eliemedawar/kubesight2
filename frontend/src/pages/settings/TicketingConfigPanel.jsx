import { useCallback, useEffect, useMemo, useState } from "react";
import ErrorBanner from "../../components/common/ErrorBanner.jsx";
import LoadingState from "../../components/common/LoadingState.jsx";
import JenkinsSection from "../../components/ticketing/JenkinsSection.jsx";
import TicketingSettingsTab from "../../components/ticketing/TicketingSettingsTab.jsx";
import { TicketingProvider, useTicketing } from "../../components/ticketing/TicketingContext.jsx";
import {
  configPayload,
  emptyForm,
  formFromConfig,
} from "../../components/ticketing/settingsSchema.js";
import { listTicketingProviders } from "../../api/ticketingApi.js";

/**
 * Jira, Zoho, and Jenkins configuration, mounted from the integrations hub.
 *
 * The forms themselves — `TicketingSettingsTab` and `JenkinsSection` — are the
 * ones the Ticketing page uses. Both are controlled components that expect a
 * parent to hold the draft and own save, and both read the bound API client
 * from `TicketingContext`. This file is that parent: it fetches the real
 * provider descriptor (a hand-rolled `{key}` would leave `capabilities` empty
 * and silently hide capability-gated fields), supplies the context, and holds
 * the same form state `ProviderWorkspace` holds.
 *
 * Jenkins is one connection shared by both ticketing providers, so its card
 * mounts `JenkinsSection` alone and the Jira/Zoho cards hide it.
 */

function ConfigForms({ showJenkinsOnly, canManage, onChanged }) {
  const { key: providerKey, name: providerName, api } = useTicketing();

  const blankForm = useMemo(() => emptyForm(providerKey), [providerKey]);

  const [config, setConfig] = useState(null);
  const [form, setForm] = useState(blankForm);
  const [jenkins, setJenkins] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [savingConfig, setSavingConfig] = useState(false);
  const [savingJenkins, setSavingJenkins] = useState(false);
  const [testingJenkins, setTestingJenkins] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      // A ticketing card needs both; the Jenkins card needs only its own
      // connection, and asking for a provider config it will not render would
      // fail for anyone without ticketing config access.
      const [configResult, jenkinsResult] = await Promise.all([
        showJenkinsOnly ? Promise.resolve(null) : api.getConfig(),
        api.getJenkinsConfig().catch(() => null),
      ]);
      if (configResult) {
        setConfig(configResult);
        setForm(formFromConfig(providerKey, configResult));
      }
      setJenkins(jenkinsResult);
    } catch (err) {
      setError(err.message || "Failed to load the configuration.");
    } finally {
      setLoading(false);
    }
  }, [api, providerKey, showJenkinsOnly]);

  useEffect(() => {
    load();
  }, [load]);

  const setField = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  const dirty = useMemo(() => {
    if (!config) return false;
    return JSON.stringify(form) !== JSON.stringify(formFromConfig(providerKey, config));
  }, [config, form, providerKey]);

  const saveConfig = async () => {
    setSavingConfig(true);
    setError("");
    setNotice("");
    try {
      const updated = await api.updateConfig(configPayload(providerKey, form));
      setConfig(updated);
      setForm(formFromConfig(providerKey, updated));
      setNotice("Configuration saved.");
      onChanged?.();
    } catch (err) {
      setError(err.message || "Failed to save the configuration.");
    } finally {
      setSavingConfig(false);
    }
  };

  // JenkinsSection clears its secret fields only when save resolves true.
  const saveJenkins = async (payload) => {
    setSavingJenkins(true);
    setError("");
    setNotice("");
    try {
      setJenkins(await api.updateJenkinsConfig(payload));
      setNotice("Jenkins connection saved.");
      onChanged?.();
      return true;
    } catch (err) {
      setError(err.message || "Failed to save the Jenkins connection.");
      return false;
    } finally {
      setSavingJenkins(false);
    }
  };

  const testJenkins = async () => {
    setTestingJenkins(true);
    setError("");
    setNotice("");
    try {
      const result = await api.testJenkinsConnection();
      setJenkins((prev) => ({ ...(prev || {}), ...result }));
      if (result.status === "ok") {
        setNotice(result.message || "Jenkins connection OK.");
      } else {
        setError(result.message || "Jenkins connection test failed.");
      }
      onChanged?.();
    } catch (err) {
      setError(err.message || "Jenkins connection test failed.");
    } finally {
      setTestingJenkins(false);
    }
  };

  if (loading) {
    return <LoadingState label="Loading configuration..." />;
  }

  // `.zoho-page` is not decoration: eighteen rules in ticketing.css are scoped
  // to it, including the min-width:0 guards that stop inputs from overflowing
  // their grid cell and overlapping the next column. The forms have always
  // rendered inside it, so this wrapper comes with them.
  return (
    <div className="zoho-page">
      {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}
      {notice ? <p className="settings-panel-notice">{notice}</p> : null}

      {showJenkinsOnly ? (
        <JenkinsSection
          canManage={canManage}
          jenkins={jenkins}
          onSave={saveJenkins}
          saving={savingJenkins}
          onTest={testJenkins}
          testing={testingJenkins}
        />
      ) : (
        <TicketingSettingsTab
          canManage={canManage}
          config={config}
          form={form}
          setField={setField}
          onSaveConfig={saveConfig}
          savingConfig={savingConfig}
          dirty={dirty}
          onDiscard={() => setForm(formFromConfig(providerKey, config))}
          webhookUrl={`${window.location.origin}/api/ticketing/${providerKey}/inbound`}
          jenkins={jenkins}
          onSaveJenkins={saveJenkins}
          savingJenkins={savingJenkins}
          onTestJenkins={testJenkins}
          testingJenkins={testingJenkins}
          showJenkins={false}
        />
      )}
      {showJenkinsOnly ? null : (
        <p className="muted sg-int-crosslink">
          Working with {providerName} tickets — field sync, intake, deploy runs — happens on the
          Ticketing page. This tab is the connection only.
        </p>
      )}
    </div>
  );
}

export default function TicketingConfigPanel({
  providerKey,
  showJenkinsOnly = false,
  canManage = false,
  onChanged,
}) {
  const [descriptor, setDescriptor] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    listTicketingProviders()
      .then((response) => {
        if (cancelled) return;
        const row = (response?.items || []).find((item) => item.key === providerKey);
        if (row) {
          setDescriptor(row);
        } else {
          setError("This provider is not available.");
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || "Failed to load the provider.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [providerKey]);

  if (loading) {
    return <LoadingState label="Loading configuration..." />;
  }
  if (error || !descriptor) {
    return <ErrorBanner message={error || "This provider is not available."} />;
  }

  return (
    <TicketingProvider descriptor={descriptor}>
      <ConfigForms
        showJenkinsOnly={showJenkinsOnly}
        canManage={canManage}
        onChanged={onChanged}
      />
    </TicketingProvider>
  );
}
