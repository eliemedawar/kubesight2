import { useEffect, useState } from "react";
import { listCiDeployTemplates, previewCiDeployTemplate } from "../../../api/ciApi.js";
import SearchableSelect from "../../common/SearchableSelect.jsx";
import { PlIcon } from "./icons.jsx";
import TemplateAnswers from "./TemplateAnswers.jsx";

let cache = null;

/** The inventory's deployment templates, fetched once per page load. */
export function useDeployTemplates() {
  const [items, setItems] = useState(cache);
  useEffect(() => {
    if (cache) return undefined;
    let cancelled = false;
    listCiDeployTemplates()
      .then((data) => {
        cache = data.items || [];
        if (!cancelled) setItems(cache);
      })
      .catch(() => !cancelled && setItems([]));
    return () => {
      cancelled = true;
    };
  }, []);
  return items;
}

/**
 * Pick an inventory template (Inventory → Templates) to create a missing
 * deployment from, and see — before saving — what a build would create from
 * it in this namespace, or why it cannot.
 */
export default function TemplatePicker({
  id,
  value,
  namespace,
  deploymentName,
  containerName = "",
  answers,
  onAnswersChange,
  disabled,
  onChange,
}) {
  const templates = useDeployTemplates();
  const [preview, setPreview] = useState({ key: "", data: null, loading: false });
  const key = `${value}|${namespace}|${deploymentName}|${containerName}|${JSON.stringify(answers || {})}`;

  useEffect(() => {
    if (!value || !namespace || !deploymentName) return undefined;
    let cancelled = false;
    setPreview({ key, data: null, loading: true });
    const timer = window.setTimeout(() => {
      previewCiDeployTemplate(value, { namespace, deploymentName, containerName, answers })
        .then((data) => !cancelled && setPreview({ key, data, loading: false }))
        .catch((err) => !cancelled && setPreview({ key, data: { ok: false, error: err.message }, loading: false }));
    }, 400);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const items = templates || [];
  const picked = items.find((item) => item.id === value) || null;
  const current = preview.key === key ? preview : { data: null, loading: false };

  return (
    <div className="pl-template-pick">
      <SearchableSelect
        id={id}
        aria-label="Inventory template"
        value={value || ""}
        disabled={disabled}
        placeholder={templates === null ? "Loading templates…" : items.length ? "Pick a template…" : "No templates in the inventory"}
        searchPlaceholder="Search templates…"
        options={[
          ...(value && !picked ? [{ value, label: value }] : []),
          ...items.map((item) => ({
            value: item.id,
            disabled: !item.usable,
            label: (
              <span className="pl-deploy-option">
                <span>{item.name}</span>
                <small>
                  {item.category}
                  {item.usable ? (item.image ? ` · ${item.image}` : "") : ` · ${item.workloadType}, not usable`}
                </small>
              </span>
            ),
          })),
        ]}
        onChange={(event) => {
          const next = items.find((item) => item.id === event.target.value) || null;
          onChange(next);
        }}
      />
      {picked && onAnswersChange && (
        <TemplateAnswers template={picked} answers={answers} disabled={disabled} onChange={onAnswersChange} />
      )}
      {value && namespace && deploymentName ? (
        current.loading || !current.data ? (
          <p className="pl-deploy-status is-loading">
            <span className="sg-ci-pulse" aria-hidden="true" /> Checking the template…
          </p>
        ) : current.data.ok ? (
          <div className="pl-deploy-status is-new">
            <PlIcon name="check" />
            <p>
              <strong>Creates</strong>{" "}
              {current.data.creates.map((item) => `${item.kind}/${item.name}`).join(", ")}
              <small>
                With the image this build pushed in place of the template's. The template is read when the
                build deploys, so later edits to it are used too.
              </small>
            </p>
          </div>
        ) : (
          <div className="pl-note is-warn">
            <PlIcon name="alert" />
            <p>
              <strong>A build cannot use this template.</strong> {current.data.error}
            </p>
          </div>
        )
      ) : null}
    </div>
  );
}
