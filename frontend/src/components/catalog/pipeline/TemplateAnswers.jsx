import { useState } from "react";
import { PlIcon } from "./icons.jsx";

const SOURCE_LABELS = {
  value: "A value",
  existingSecret: "An existing Secret",
  existingConfigMap: "An existing ConfigMap",
  createConfigMap: "A new ConfigMap",
};

/** Whether this template field still needs something from the person. */
export const needsAnswer = (field, answer) => {
  if (!field.required) return false;
  if (!answer?.source) return !field.default;
  if (answer.source === "value") return !String(answer.value || field.default || "").trim();
  if (answer.source === "existingSecret") return !String(answer.secretName || "").trim();
  return !String(answer.configMapName || "").trim();
};

/**
 * Answers for what an inventory template asks — its variables and mounted
 * files — kept on the stage or link so a build can create the deployment
 * unattended. Credentials and files come from a Secret or ConfigMap that
 * already exists: the answer is its name, never the value.
 */
export default function TemplateAnswers({ template, answers, disabled, onChange }) {
  const [showOptional, setShowOptional] = useState(false);
  const env = template?.env || [];
  const volumes = template?.volumes || [];
  if (!env.length && !volumes.length) return null;

  const envAnswers = answers?.env || {};
  const volumeAnswers = answers?.volumes || {};
  const setEnv = (key, patch) => {
    const next = { ...(envAnswers[key] || {}), ...patch };
    const nextEnv = { ...envAnswers };
    if (!next.source) delete nextEnv[key];
    else nextEnv[key] = next;
    onChange({ env: nextEnv, volumes: volumeAnswers });
  };
  const setVolume = (path, patch) =>
    onChange({ env: envAnswers, volumes: { ...volumeAnswers, [path]: { ...(volumeAnswers[path] || {}), ...patch } } });

  const asked = env.filter((field) => field.required || envAnswers[field.key]?.source);
  const optional = env.filter((field) => !asked.includes(field));
  const shown = showOptional ? [...asked, ...optional] : asked;

  return (
    <div className="pl-tpl-answers">
      <strong className="pl-tpl-answers-title">What the template asks</strong>
      {shown.map((field) => {
        const answer = envAnswers[field.key] || {};
        const source = answer.source || "";
        const missing = needsAnswer(field, answer);
        return (
          <div key={field.key} className={`pl-tpl-row${missing ? " is-missing" : ""}`}>
            <div className="pl-tpl-row-name">
              <code>{field.key}</code>
              {field.required && <span className="pl-tag">Required</span>}
              {field.sensitive && (
                <span className="pl-tag">
                  <PlIcon name="lock" /> Sensitive
                </span>
              )}
              {field.default && !source && <small>default {field.default}</small>}
            </div>
            <div className="pl-tpl-row-inputs">
              <select
                aria-label={`Where ${field.key} comes from`}
                value={source}
                disabled={disabled}
                onChange={(event) => setEnv(field.key, { source: event.target.value })}
              >
                <option value="">{field.default ? "Template default" : "Choose…"}</option>
                {field.sources.map((item) => (
                  <option key={item} value={item}>
                    {SOURCE_LABELS[item] || item}
                  </option>
                ))}
              </select>
              {source === "value" && (
                <input
                  aria-label={`${field.key} value`}
                  value={answer.value || ""}
                  placeholder={field.default || "value"}
                  disabled={disabled}
                  onChange={(event) => setEnv(field.key, { value: event.target.value })}
                />
              )}
              {source === "existingSecret" && (
                <>
                  <input
                    aria-label={`Secret for ${field.key}`}
                    className="is-mono"
                    value={answer.secretName || ""}
                    placeholder="secret-name"
                    disabled={disabled}
                    spellCheck={false}
                    onChange={(event) => setEnv(field.key, { secretName: event.target.value.trim() })}
                  />
                  <input
                    aria-label={`Key in the Secret for ${field.key}`}
                    className="is-mono"
                    value={answer.key || ""}
                    placeholder={`key (${field.key})`}
                    disabled={disabled}
                    spellCheck={false}
                    onChange={(event) => setEnv(field.key, { key: event.target.value.trim() })}
                  />
                </>
              )}
              {(source === "existingConfigMap" || source === "createConfigMap") && (
                <>
                  <input
                    aria-label={`ConfigMap for ${field.key}`}
                    className="is-mono"
                    value={answer.configMapName || ""}
                    placeholder="configmap-name"
                    disabled={disabled}
                    spellCheck={false}
                    onChange={(event) => setEnv(field.key, { configMapName: event.target.value.trim() })}
                  />
                  {source === "createConfigMap" ? (
                    <input
                      aria-label={`${field.key} value`}
                      value={answer.value || ""}
                      placeholder="value"
                      disabled={disabled}
                      onChange={(event) => setEnv(field.key, { value: event.target.value })}
                    />
                  ) : (
                    <input
                      aria-label={`Key in the ConfigMap for ${field.key}`}
                      className="is-mono"
                      value={answer.key || ""}
                      placeholder={`key (${field.key})`}
                      disabled={disabled}
                      spellCheck={false}
                      onChange={(event) => setEnv(field.key, { key: event.target.value.trim() })}
                    />
                  )}
                </>
              )}
            </div>
          </div>
        );
      })}
      {optional.length > 0 && (
        <button type="button" className="btn-ghost pl-link" onClick={() => setShowOptional((open) => !open)}>
          {showOptional
            ? "Hide the variables that use their defaults"
            : `${optional.length} more variable${optional.length === 1 ? "" : "s"} use the template's defaults — change one`}
        </button>
      )}
      {volumes.map((volume) => {
        const answer = volumeAnswers[volume.mountPath] || {};
        const field = volume.kind === "secret" ? "secretName" : "configMapName";
        const source = volume.kind === "secret" ? "existingSecret" : "existingConfigMap";
        const missing = !String(answer[field] || "").trim();
        return (
          <div key={volume.mountPath} className={`pl-tpl-row${missing ? " is-missing" : ""}`}>
            <div className="pl-tpl-row-name">
              <code>{volume.mountPath}</code>
              <span className="pl-tag">File · {volume.kind === "secret" ? "Secret" : "ConfigMap"}</span>
            </div>
            <div className="pl-tpl-row-inputs">
              <input
                aria-label={`${volume.kind === "secret" ? "Secret" : "ConfigMap"} mounted at ${volume.mountPath}`}
                className="is-mono"
                value={answer[field] || ""}
                placeholder={volume.kind === "secret" ? "existing secret name" : "existing configmap name"}
                disabled={disabled || !volume.sources.length}
                spellCheck={false}
                onChange={(event) => setVolume(volume.mountPath, { source, [field]: event.target.value.trim() })}
              />
            </div>
          </div>
        );
      })}
      <small className="pl-tpl-answers-note">
        Credentials and files come from a Secret or ConfigMap that is already in the namespace. KubeSight stores
        only its name, and a build never writes one.
      </small>
    </div>
  );
}
