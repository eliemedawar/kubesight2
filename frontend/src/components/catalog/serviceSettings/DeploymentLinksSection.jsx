import { useEffect, useMemo, useState } from "react";
import "../../../styles/signal/sharedPipelines.css";
import {
  createCiDeploymentLink,
  deleteCiDeploymentLink,
  getCiDeployTarget,
  listCiDeploymentLinks,
  updateCiDeploymentLink,
} from "../../../api/ciApi.js";
import { listClusters, listNamespacesByCluster } from "../../../api/clustersApi.js";
import { buildRoute } from "../../../routes/routeUrl.js";
import SearchableSelect from "../../common/SearchableSelect.jsx";
import { formatRelative } from "../ciShared.jsx";
import { Field } from "../pipeline/controls.jsx";
import { PlIcon } from "../pipeline/icons.jsx";
import TemplatePicker from "../pipeline/TemplatePicker.jsx";

const SOURCE_LABELS = {
  manual: "Linked by hand",
  inventory: "Linked from the inventory",
  deploy_stage: "From the Deploy stage",
};

const blank = {
  clusterId: "",
  namespace: "",
  workloadName: "",
  containerName: "",
  environment: "",
  templateId: "",
  templateAnswers: { env: {}, volumes: {} },
};

/**
 * Which deployments in the inventory this service builds.
 *
 * The explicit answer the rest of KubeSight reads: the inventory says "built
 * by" on each linked row, ticket-driven deploys build the linked service, and
 * a Deploy stage set to "the service's linked deployment" deploys here — which
 * is how one shared pipeline ships each service to its own workload.
 *
 * Links save at once, like secrets. A Deploy stage with a fixed target adds
 * its own when the pipeline is saved or deploys.
 */
export default function DeploymentLinksSection({ service, canEdit, canDeploy, onError, onNotice, onCount }) {
  const [links, setLinks] = useState(null);
  const [adding, setAdding] = useState(null);
  const [busy, setBusy] = useState(false);
  const [labelling, setLabelling] = useState(null);

  const load = () =>
    listCiDeploymentLinks(service.id)
      .then((data) => {
        setLinks(data.items || []);
        onCount?.(data.items || []);
      })
      .catch((err) => {
        setLinks([]);
        onError(err.message || "Could not load the linked deployments.");
      });

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [service.id]);

  const add = async () => {
    setBusy(true);
    try {
      await createCiDeploymentLink(service.id, adding);
      onNotice(`Linked ${adding.namespace}/${adding.workloadName} to ${service.name}.`);
      setAdding(null);
      load();
    } catch (err) {
      onError(err.message || "Could not link the deployment.");
    } finally {
      setBusy(false);
    }
  };

  const update = async (link, patch, message) => {
    setBusy(true);
    try {
      await updateCiDeploymentLink(service.id, link.id, patch);
      onNotice(message);
      setLabelling(null);
      load();
    } catch (err) {
      onError(err.message || "Could not update the link.");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (link) => {
    if (
      !window.confirm(
        `Unlink ${link.namespace}/${link.workloadName}? Nothing changes on the cluster. The inventory stops naming this service as its builder, and a Deploy stage set to the service's linked deployment stops deploying there.`
      )
    ) {
      return;
    }
    try {
      await deleteCiDeploymentLink(service.id, link.id);
      onNotice(`Unlinked ${link.namespace}/${link.workloadName}.`);
      load();
    } catch (err) {
      onError(err.message || "Could not unlink the deployment.");
    }
  };

  if (links === null) return <p className="pl-field-hint">Loading linked deployments…</p>;

  return (
    <div className="st-secrets st-links">
      {adding && (
        <LinkForm
          value={adding}
          serviceId={service.id}
          busy={busy}
          onChange={setAdding}
          onCancel={() => setAdding(null)}
          onSave={add}
        />
      )}

      {links.length === 0 && !adding ? (
        <div className="pl-empty">
          <span className="pl-empty-glyph" aria-hidden="true">
            <PlIcon name="link" />
          </span>
          <strong>Not linked to a deployment yet</strong>
          <p>
            Link the deployments this service builds — one per environment if it runs in several. You can
            also leave this to a Deploy stage: saving a pipeline that deploys to a deployment links it.
          </p>
          {canEdit && (
            <button type="button" className="primary btn-compact" onClick={() => setAdding({ ...blank })}>
              <PlIcon name="plus" /> Link a deployment
            </button>
          )}
        </div>
      ) : (
        <>
          {links.length > 0 && (
            <ul className="st-secret-list">
              {links.map((link) => (
                <LinkRow
                  key={link.id}
                  link={link}
                  canEdit={canEdit}
                  canDeploy={canDeploy}
                  busy={busy}
                  labelling={labelling === link.id}
                  onLabel={() => setLabelling(link.id)}
                  onCancelLabel={() => setLabelling(null)}
                  onSaveLabel={(patch) => update(link, patch, "Link updated.")}
                  onReauthorize={() =>
                    update(link, { reauthorize: true }, "Builds now deploy here with your rights.")
                  }
                  onRemove={() => remove(link)}
                />
              ))}
            </ul>
          )}
          {canEdit && !adding && (
            <button type="button" className="btn-ghost pl-rows-add" onClick={() => setAdding({ ...blank })}>
              <PlIcon name="plus" /> Link a deployment
            </button>
          )}
        </>
      )}
    </div>
  );
}

function LinkRow({ link, canEdit, canDeploy, busy, labelling, onLabel, onCancelLabel, onSaveLabel, onReauthorize, onRemove }) {
  const [environment, setEnvironment] = useState(link.environment || "");
  const [container, setContainer] = useState(link.containerName || "");
  const [templateId, setTemplateId] = useState(link.templateId || "");
  const [templateAnswers, setTemplateAnswers] = useState(link.templateAnswers || { env: {}, volumes: {} });
  const live = link.live || null;
  const href = buildRoute({ key: "applicationDetails", params: { appId: link.inventoryId } });
  return (
    <li className="st-secret st-link">
      <span className={`st-secret-icon st-link-icon is-${live?.state || "unknown"}`} aria-hidden="true">
        <PlIcon name="rocket" />
      </span>
      <div className="st-secret-copy">
        <strong>
          <code>
            {link.namespace}/{link.workloadName}
          </code>
          {link.environment && <span className="pl-tag is-info">{link.environment}</span>}
          {link.templateName && (
            <span className="pl-tag" title="Created from this inventory template if it is not there">
              Template · {link.templateName}
            </span>
          )}
          <span className="pl-tag">{SOURCE_LABELS[link.source] || link.source}</span>
        </strong>
        <small>
          {link.clusterId}
          {link.containerName ? ` · container ${link.containerName}` : ""}
          {link.createdAt ? ` · linked ${formatRelative(link.createdAt)}` : ""}
          {link.createdBy ? ` by ${link.createdBy}` : ""}
        </small>
        <LiveLine live={live} templateName={link.templateName} />
        {!link.canDeployThrough && (
          <small className="st-link-warn">
            <PlIcon name="alert" /> A Deploy stage set to “the service's linked deployment” cannot deploy here:
            whoever linked it could not deploy to {link.clusterId}/{link.namespace}.
            {canEdit && canDeploy && (
              <>
                {" "}
                <button type="button" className="btn-ghost pl-link" disabled={busy} onClick={onReauthorize}>
                  Deploy as me
                </button>
              </>
            )}
          </small>
        )}
        {labelling && (
          <div className="st-replace st-link-edit">
            <div className="pl-grid">
              <Field
                label="Environment"
                htmlFor={`st-link-env-${link.id}`}
                optional
                hint="A Deploy stage picks this link by its label when the service has several."
              >
                <input
                  id={`st-link-env-${link.id}`}
                  value={environment}
                  placeholder="PROD"
                  onChange={(event) => setEnvironment(event.target.value)}
                  autoFocus
                />
              </Field>
              <Field
                label="Template if it is not there"
                optional
                wide
                hint="The inventory template a Deploy stage creates it from when the deployment is missing."
              >
                <TemplatePicker
                  id={`st-link-tpl-${link.id}`}
                  value={templateId}
                  namespace={link.namespace}
                  deploymentName={link.workloadName}
                  containerName={container}
                  answers={templateAnswers}
                  onAnswersChange={setTemplateAnswers}
                  onChange={(template) => {
                    if ((template?.id || "") !== templateId) setTemplateAnswers({ env: {}, volumes: {} });
                    setTemplateId(template?.id || "");
                  }}
                />
                {templateId && (
                  <button type="button" className="btn-ghost pl-link" onClick={() => setTemplateId("")}>
                    No template
                  </button>
                )}
              </Field>
              <Field label="Container" htmlFor={`st-link-ct-${link.id}`} optional hint="The one whose image a build replaces.">
                <input
                  id={`st-link-ct-${link.id}`}
                  className="is-mono"
                  value={container}
                  placeholder={link.workloadName}
                  spellCheck={false}
                  onChange={(event) => setContainer(event.target.value.trim().toLowerCase())}
                />
              </Field>
            </div>
            <div className="st-replace-actions">
              <button type="button" className="btn-outline btn-compact" onClick={onCancelLabel}>
                Cancel
              </button>
              <button
                type="button"
                className="primary btn-compact"
                disabled={busy}
                onClick={() =>
                  onSaveLabel({ environment: environment.trim(), containerName: container, templateId, templateAnswers })
                }
              >
                <PlIcon name="check" /> Save
              </button>
            </div>
          </div>
        )}
      </div>
      <div className="st-secret-actions">
        {link.inventoryId && (
          <a className="btn-ghost st-action" href={href}>
            <PlIcon name="forward" /> Inventory
          </a>
        )}
        {canEdit && !labelling && (
          <>
            <button type="button" className="btn-ghost st-action" onClick={onLabel}>
              <PlIcon name="variable" /> Label
            </button>
            <button
              type="button"
              className="btn-ghost pl-tool is-danger"
              aria-label={`Unlink ${link.workloadName}`}
              title="Unlink"
              onClick={onRemove}
            >
              <PlIcon name="trash" />
            </button>
          </>
        )}
      </div>
    </li>
  );
}

function LiveLine({ live, templateName }) {
  if (!live) return null;
  if (live.state === "missing") {
    return (
      <small className="st-link-live is-warn">
        Not on the cluster yet
        {templateName
          ? ` — the first deploy creates it from the template ${templateName}.`
          : " — removed, renamed, or not created yet. Give it a template to have a deploy create it."}
      </small>
    );
  }
  if (live.state === "unknown") {
    return <small className="st-link-live">The cluster could not be read, so its current state is unknown.</small>;
  }
  const ready = live.readyReplicas ?? "?";
  const desired = live.replicas ?? "?";
  return (
    <small className={`st-link-live${live.status && live.status !== "Healthy" ? " is-warn" : ""}`}>
      <span className="st-link-status">{live.status || "Unknown"}</span> · {ready}/{desired} ready
      {live.versionTag ? (
        <>
          {" "}
          · runs <code>{live.versionTag}</code>
        </>
      ) : null}
    </small>
  );
}

/** Cluster → namespace → deployment, from what the cluster has right now. */
function LinkForm({ value, serviceId, busy, onChange, onCancel, onSave }) {
  const [clusters, setClusters] = useState([]);
  const [namespaces, setNamespaces] = useState({ clusterId: "", items: [], error: "" });
  const [target, setTarget] = useState({ key: "", data: null, loading: false, error: "" });
  const set = (patch) => onChange({ ...value, ...patch });

  useEffect(() => {
    let cancelled = false;
    listClusters()
      .then((result) => !cancelled && setClusters(result?.items || []))
      .catch(() => !cancelled && setClusters([]));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!value.clusterId) return undefined;
    let cancelled = false;
    listNamespacesByCluster(value.clusterId, { lite: true })
      .then((result) => {
        if (cancelled) return;
        const items = (result?.items || []).map((item) => item.name || item).filter(Boolean).sort();
        setNamespaces({ clusterId: value.clusterId, items, error: "" });
      })
      .catch((err) => {
        if (!cancelled) setNamespaces({ clusterId: value.clusterId, items: [], error: err.message || "Could not list namespaces." });
      });
    return () => {
      cancelled = true;
    };
  }, [value.clusterId]);

  const key = `${value.clusterId}|${value.namespace}`;
  useEffect(() => {
    if (!value.clusterId || !value.namespace) return undefined;
    let cancelled = false;
    setTarget((prev) => ({ ...prev, key, loading: true, error: "" }));
    getCiDeployTarget(value.clusterId, value.namespace)
      .then((data) => !cancelled && setTarget({ key, data, loading: false, error: data?.error || "" }))
      .catch((err) => !cancelled && setTarget({ key, data: null, loading: false, error: err.message || "Could not read the cluster." }));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const deployments = target.key === key ? target.data?.deployments || [] : [];
  const picked = deployments.find((item) => item.name === value.workloadName) || null;
  const takenBy = picked?.linkedService && String(picked.linkedService.id) !== String(serviceId) ? picked.linkedService : null;
  const already = picked?.linkedService && String(picked.linkedService.id) === String(serviceId);
  const containers = picked?.containers || [];
  const namespaceItems = namespaces.clusterId === value.clusterId ? namespaces.items : [];
  const options = useMemo(
    () =>
      deployments.map((item) => ({
        value: item.name,
        label: (
          <span className="pl-deploy-option">
            <span>{item.name}</span>
            <small>
              {item.linkedService
                ? String(item.linkedService.id) === String(serviceId)
                  ? "linked here"
                  : `linked to ${item.linkedService.name}`
                : `${item.ready}/${item.desired} ready`}
            </small>
          </span>
        ),
      })),
    [deployments, serviceId]
  );

  return (
    <div className="st-add" role="group" aria-label="Link a deployment">
      <div className="pl-grid">
        <Field label="Cluster">
          <SearchableSelect
            id="st-link-cluster"
            aria-label="Cluster"
            value={value.clusterId}
            placeholder="Pick a cluster…"
            searchPlaceholder="Search clusters…"
            options={clusters.map((item) => ({ value: item.id, label: item.name || item.id }))}
            onChange={(event) =>
              event.target.value !== value.clusterId &&
              set({ clusterId: event.target.value, namespace: "", workloadName: "", containerName: "" })
            }
          />
        </Field>
        <Field label="Namespace" error={namespaces.clusterId === value.clusterId ? namespaces.error : ""}>
          <SearchableSelect
            id="st-link-namespace"
            aria-label="Namespace"
            value={value.namespace}
            disabled={!value.clusterId}
            placeholder={value.clusterId ? "Pick a namespace…" : "Pick a cluster first"}
            searchPlaceholder="Search namespaces…"
            options={namespaceItems.map((name) => ({ value: name, label: name }))}
            onChange={(event) =>
              event.target.value !== value.namespace &&
              set({ namespace: event.target.value, workloadName: "", containerName: "" })
            }
          />
        </Field>
        <Field
          label="Deployment"
          error={target.key === key ? target.error : ""}
          hint={target.key === key && target.loading ? "Reading the namespace…" : undefined}
        >
          <SearchableSelect
            id="st-link-deployment"
            aria-label="Deployment"
            value={value.workloadName}
            disabled={!value.namespace}
            placeholder={value.namespace ? "Pick a deployment, or name a new one…" : "Pick a namespace first"}
            searchPlaceholder="Search, or type a new name…"
            allowCustom
            customOptionLabel={(name) => `Not there yet: “${name.toLowerCase()}”`}
            options={options}
            onChange={(event) =>
              set({ workloadName: String(event.target.value || "").trim().toLowerCase(), containerName: "" })
            }
          />
        </Field>
        <Field
          label={picked ? "Template if it is ever removed" : "Create it from an inventory template"}
          optional={Boolean(picked) || !value.workloadName}
          wide
          hint={
            picked
              ? "It exists, so builds only change its image. A template is used only if it disappears."
              : "Not on the cluster yet: the first build that deploys to this link creates it from this template. Picking one with no name above names it after the template."
          }
        >
          <TemplatePicker
            id="st-link-template"
            value={value.templateId}
            namespace={value.namespace}
            deploymentName={value.workloadName}
            containerName={value.containerName}
            answers={value.templateAnswers}
            onAnswersChange={(templateAnswers) => set({ templateAnswers })}
            disabled={!value.namespace}
            onChange={(template) =>
              set({
                templateId: template?.id || "",
                templateAnswers: { env: {}, volumes: {} },
                ...(template && !value.workloadName ? { workloadName: template.deploymentName } : {}),
              })
            }
          />
        </Field>
        {containers.length > 1 ? (
          <Field label="Container" hint="The one whose image a build replaces.">
            <SearchableSelect
              id="st-link-container"
              aria-label="Container"
              value={value.containerName}
              placeholder="Pick a container…"
              options={containers.map((item) => ({ value: item.name, label: item.name }))}
              onChange={(event) => set({ containerName: event.target.value })}
            />
          </Field>
        ) : (
          <Field
            label="Environment"
            htmlFor="st-link-env"
            optional
            hint="A label like PROD or UAT. A Deploy stage picks the link by it when there are several."
          >
            <input
              id="st-link-env"
              value={value.environment}
              placeholder="PROD"
              onChange={(event) => set({ environment: event.target.value })}
            />
          </Field>
        )}
        {containers.length > 1 && (
          <Field label="Environment" htmlFor="st-link-env" optional hint="A label like PROD or UAT.">
            <input
              id="st-link-env"
              value={value.environment}
              placeholder="PROD"
              onChange={(event) => set({ environment: event.target.value })}
            />
          </Field>
        )}
      </div>
      {takenBy && (
        <p className="pl-note is-warn">
          <PlIcon name="alert" />
          <span>
            <code>{value.workloadName}</code> is already linked to <strong>{takenBy.name}</strong>. A deployment
            belongs to one CI service — unlink it there first.
          </span>
        </p>
      )}
      {already && (
        <p className="pl-note">
          <PlIcon name="check" />
          <span>Already linked to this service.</span>
        </p>
      )}
      <div className="st-add-actions">
        <button type="button" className="btn-outline btn-compact" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="primary btn-compact"
          disabled={busy || !value.workloadName || Boolean(takenBy) || already}
          onClick={onSave}
        >
          <PlIcon name="link" /> {busy ? "Linking…" : "Link deployment"}
        </button>
      </div>
    </div>
  );
}
