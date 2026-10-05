import { useEffect, useMemo, useState } from "react";
import { getCiDeployTarget, listCiDeploymentLinks } from "../../../api/ciApi.js";
import { listClusters, listNamespacesByCluster } from "../../../api/clustersApi.js";
import SearchableSelect from "../../common/SearchableSelect.jsx";
import { formatRelative } from "../ciShared.jsx";
import { CommandEditor, EnvRows, Field, Segmented, Switch } from "./controls.jsx";
import {
  blankDeploy,
  generateManifest,
  isLinkedDeploy,
  patchDeploy,
  pickLinkedDeployment,
  SERVICE_TYPES,
  setDeployTargetMode,
} from "./deployModel.js";
import { PlIcon } from "./icons.jsx";
import TemplatePicker from "./TemplatePicker.jsx";
import { timeoutLabel } from "./stageModel.js";

/**
 * A Deploy stage: where the build's image goes, and what happens on the way.
 *
 * Read top to bottom it answers the questions in the order someone asks them —
 * where does this deploy, does that exist, which image, what stops a bad
 * deploy, and who is it done as — so the safety rules are on the page, not
 * discovered from a failed build.
 */
export default function DeployStageFields({ ids, service: ciService, stage, stages, index, editable, onChange }) {
  const deploy = stage.deploy || blankDeploy();
  const set = (patch) => onChange({ deploy: patchDeploy(deploy, patch) });
  const setCreate = (patch) => set({ create: patch });
  const linkedMode = isLinkedDeploy(deploy);
  // A pipeline from the Pipelines page has no deployment of its own: each
  // service that builds with it has its own.
  const isHome = ciService?.kind === "pipeline";

  const [clusters, setClusters] = useState([]);
  const [namespaces, setNamespaces] = useState({ clusterId: "", items: [], error: "" });
  const [target, setTarget] = useState({ key: "", data: null, loading: false, error: "" });
  // Folded while the deployment exists: the form only matters if it is removed.
  const [showCreate, setShowCreate] = useState(false);

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
    if (!deploy.clusterId) return undefined;
    let cancelled = false;
    listNamespacesByCluster(deploy.clusterId, { lite: true })
      .then((result) => {
        if (cancelled) return;
        const items = (result?.items || []).map((item) => item.name || item).filter(Boolean).sort();
        setNamespaces({ clusterId: deploy.clusterId, items, error: "" });
      })
      .catch((err) => {
        if (!cancelled) setNamespaces({ clusterId: deploy.clusterId, items: [], error: err.message || "Could not list namespaces." });
      });
    return () => {
      cancelled = true;
    };
  }, [deploy.clusterId]);

  const targetKey = `${deploy.clusterId}|${deploy.namespace}`;
  useEffect(() => {
    if (!deploy.clusterId) return undefined;
    let cancelled = false;
    setTarget((prev) => ({ ...prev, key: targetKey, loading: true, error: "" }));
    getCiDeployTarget(deploy.clusterId, deploy.namespace)
      .then((data) => !cancelled && setTarget({ key: targetKey, data, loading: false, error: data?.error || "" }))
      .catch((err) => !cancelled && setTarget({ key: targetKey, data: null, loading: false, error: err.message || "Could not read the cluster." }));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targetKey]);

  // The service's own links, for "the service's linked deployment".
  const [links, setLinks] = useState(null);
  useEffect(() => {
    if (!linkedMode || isHome || !ciService?.id) return undefined;
    let cancelled = false;
    listCiDeploymentLinks(ciService.id, { live: false })
      .then((data) => !cancelled && setLinks(data.items || []))
      .catch(() => !cancelled && setLinks([]));
    return () => {
      cancelled = true;
    };
  }, [linkedMode, isHome, ciService?.id]);

  const info = target.key === targetKey ? target.data : null;
  const existing = useMemo(
    () => (info?.deployments || []).find((item) => item.name === deploy.deploymentName) || null,
    [info, deploy.deploymentName]
  );
  const containers = existing?.containers || [];
  const imageStageBefore = stages.slice(0, index).some((other) => other.stageType === "container_image" && other.enabled !== false);
  const fixedImage = Boolean(deploy.image) || deploy.imageMode === "fixed";
  const form = deploy.create || {};
  const fromTemplate = (form.source || "form") === "template";
  const service = form.service || {};
  const clusterName = (clusters.find((item) => item.id === deploy.clusterId) || {}).name || deploy.clusterId;
  const namespaceItems = namespaces.clusterId === deploy.clusterId ? namespaces.items : [];

  const modeSwitch = (
    <Field
      label="Deploys to"
      hint={
        linkedMode
          ? isHome
            ? "Each service that builds with this pipeline deploys to its own linked deployment."
            : "Whatever deployment this service is linked to under Settings → Deployments, looked up when the build starts."
          : undefined
      }
    >
      <Segmented
        label="Deploy target"
        value={linkedMode ? "linked" : "fixed"}
        options={[
          { value: "fixed", label: "A deployment I pick" },
          { value: "linked", label: "The service's linked deployment" },
        ]}
        disabled={!editable}
        onChange={(mode) => onChange({ deploy: setDeployTargetMode(deploy, mode) })}
      />
    </Field>
  );

  if (linkedMode) {
    return (
      <div className="pl-deploy">
        {modeSwitch}
        <div className="pl-grid pl-deploy-target">
          <Field
            label="Environment"
            htmlFor={`${ids}-environment`}
            optional
            hint="Needed when a service is linked to several deployments: the link with this label is used."
          >
            <input
              id={`${ids}-environment`}
              value={deploy.environment || ""}
              placeholder="PROD"
              disabled={!editable}
              onChange={(event) => onChange({ deploy: { ...deploy, environment: event.target.value } })}
            />
          </Field>
          <Field
            label="Container"
            htmlFor={`${ids}-container`}
            optional
            hint="Empty uses the link's container, or the deployment's only one."
          >
            <input
              id={`${ids}-container`}
              className="is-mono"
              value={deploy.containerName || ""}
              placeholder="app"
              disabled={!editable}
              spellCheck={false}
              onChange={(event) => onChange({ deploy: { ...deploy, containerName: event.target.value.trim().toLowerCase() } })}
            />
          </Field>
        </div>
        <LinkedPreview isHome={isHome} links={links} environment={deploy.environment} />
        <ImageChoice deploy={deploy} editable={editable} set={set} imageStageBefore={imageStageBefore} fixedImage={fixedImage} />
        <ul className="pl-deploy-guards" aria-label="What this stage checks">
          <Guard
            icon="shield"
            title="The image must be in the cluster's registry"
            text="Checked against the registries linked to the deployment's cluster before anything is applied. Missing or unconfirmable means nothing is deployed."
          />
          <Guard
            icon="lock"
            title="Approval"
            text="If that cluster needs approval, the change is queued as a change bundle and this stage waits for it."
          />
          <Guard
            icon="undo"
            title="Watched, and rolled back if it fails"
            text={`Passes only when every replica runs the new image. Crash loops, image pull errors or not ready within ${timeoutLabel(stage.timeoutSeconds)} put the previous image back.`}
          />
          <Guard
            icon="key"
            title="Deploys with the rights of whoever linked the deployment"
            text="The person who linked it to the service must still be able to deploy there. If they cannot, the stage stops and says who has to link it again. It never creates a deployment."
          />
        </ul>
      </div>
    );
  }

  return (
    <div className="pl-deploy">
      {modeSwitch}
      {/* ── Where ─────────────────────────────────────────────────────── */}
      <div className="pl-grid pl-deploy-target">
        <Field label="Cluster">
          <SearchableSelect
            id={`${ids}-cluster`}
            aria-label="Cluster"
            value={deploy.clusterId}
            disabled={!editable}
            placeholder="Pick a cluster…"
            searchPlaceholder="Search clusters…"
            options={[
              ...(deploy.clusterId && !clusters.some((item) => item.id === deploy.clusterId)
                ? [{ value: deploy.clusterId, label: deploy.clusterId }]
                : []),
              ...clusters.map((item) => ({ value: item.id, label: item.name || item.id })),
            ]}
            onChange={(event) =>
              event.target.value !== deploy.clusterId &&
              set({ clusterId: event.target.value, namespace: "", deploymentName: "", containerName: "" })
            }
          />
        </Field>
        <Field
          label="Namespace"
          error={namespaces.clusterId === deploy.clusterId ? namespaces.error : ""}
          hint={deploy.clusterId ? "Must already exist — a build never creates a namespace." : "Pick a cluster first."}
        >
          <SearchableSelect
            id={`${ids}-namespace`}
            aria-label="Namespace"
            value={deploy.namespace}
            disabled={!editable || !deploy.clusterId}
            placeholder="Pick a namespace…"
            searchPlaceholder="Search namespaces…"
            options={[
              ...(deploy.namespace && !namespaceItems.includes(deploy.namespace)
                ? [{ value: deploy.namespace, label: deploy.namespace }]
                : []),
              ...namespaceItems.map((name) => ({ value: name, label: name })),
            ]}
            onChange={(event) =>
              event.target.value !== deploy.namespace &&
              set({ namespace: event.target.value, deploymentName: "", containerName: "" })
            }
          />
        </Field>
        <Field
          label="Deployment"
          hint="Pick one that exists, or type a new name in the search to create it."
        >
          <SearchableSelect
            id={`${ids}-deployment`}
            aria-label="Deployment"
            value={deploy.deploymentName}
            disabled={!editable || !deploy.namespace}
            placeholder={deploy.namespace ? "Pick or name a deployment…" : "Pick a namespace first"}
            searchPlaceholder="Search, or type a new name…"
            allowCustom
            customOptionLabel={(name) => `Create “${name.toLowerCase()}”`}
            options={(info?.deployments || []).map((item) => ({
              value: item.name,
              label: (
                <span className="pl-deploy-option">
                  <span>{item.name}</span>
                  <small>
                    {item.ready}/{item.desired} ready
                  </small>
                </span>
              ),
            }))}
            onChange={(event) => {
              const name = String(event.target.value || "").trim().toLowerCase();
              if (name !== deploy.deploymentName) set({ deploymentName: name, containerName: "" });
            }}
          />
        </Field>
        {existing && containers.length > 1 ? (
          <Field label="Container" hint="The one whose image each build replaces.">
            <SearchableSelect
              id={`${ids}-container`}
              aria-label="Container"
              value={deploy.containerName}
              disabled={!editable}
              placeholder="Pick a container…"
              searchPlaceholder="Search containers…"
              options={containers.map((item) => ({
                value: item.name,
                label: (
                  <span className="pl-deploy-option">
                    <span>{item.name}</span>
                    <small>{item.image}</small>
                  </span>
                ),
              }))}
              onChange={(event) => set({ containerName: event.target.value })}
            />
          </Field>
        ) : (
          !existing && (
            <Field
              label="Container name"
              htmlFor={`${ids}-container`}
              optional
              hint="In the deployment it creates. Empty uses the deployment's name."
            >
              <input
                id={`${ids}-container`}
                className="is-mono"
                value={deploy.containerName}
                placeholder={deploy.deploymentName || "app"}
                disabled={!editable}
                spellCheck={false}
                onChange={(event) => set({ containerName: event.target.value.trim().toLowerCase() })}
              />
            </Field>
          )
        )}
      </div>

      <TargetStatus deploy={deploy} info={info} target={target} targetKey={targetKey} existing={existing} clusterName={clusterName} />

      {/* ── Is this the service's deployment (the inventory link) ──────── */}
      {!isHome && deploy.deploymentName && (
        <LinkToService
          deploy={deploy}
          existing={existing}
          service={ciService}
          editable={editable}
          onChange={(linkToService) => onChange({ deploy: { ...deploy, linkToService } })}
        />
      )}
      {isHome && (
        <p className="pl-note is-muted">
          <PlIcon name="link" />
          <span>
            Every service that builds with this pipeline would deploy to this one deployment. To send each
            service to its own, choose “The service's linked deployment” above.
          </span>
        </p>
      )}

      {/* ── Which image ───────────────────────────────────────────────── */}
      <ImageChoice deploy={deploy} editable={editable} set={set} imageStageBefore={imageStageBefore} fixedImage={fixedImage} />

      {/* ── What stops a bad deploy ───────────────────────────────────── */}
      <ul className="pl-deploy-guards" aria-label="What this stage checks">
        <Guard
          icon="shield"
          tone={info && !info.linkedRegistries ? "warn" : undefined}
          title="The image must be in the cluster's registry"
          text={
            info && !info.linkedRegistries
              ? `No registry is linked to ${clusterName}, so no image can be confirmed and every deploy will stop here. Link the registry this service pushes to with the cluster under Registries.`
              : `Checked before anything is applied${info ? ` against ${info.linkedRegistries} linked registr${info.linkedRegistries === 1 ? "y" : "ies"}` : ""}. Missing or unconfirmable means nothing is deployed.`
          }
        />
        <Guard
          icon="lock"
          tone={info?.requiredApprovals ? "info" : undefined}
          title={info?.requiredApprovals ? `${clusterName} needs ${info.requiredApprovals} approval${info.requiredApprovals === 1 ? "" : "s"}` : "Approval"}
          text={
            info?.requiredApprovals
              ? `The change is sent as a change bundle and this stage waits for it — up to its ${timeoutLabel(stage.timeoutSeconds)} limit (change it under “If it fails or hangs”). Declined or expired means nothing is deployed.`
              : info
                ? "This cluster applies changes without approval."
                : "If the cluster needs approval, the change is queued and the stage waits for it."
          }
        />
        <Guard
          icon="undo"
          title="Watched, and rolled back if it fails"
          text={`The stage passes only when every replica runs the new image. Crash loops, image pull errors or not ready within ${timeoutLabel(stage.timeoutSeconds)} put the previous image back${deploy.createIfMissing ? " (or remove a deployment this stage just created)" : ""}.`}
        />
      </ul>

      {/* ── Who it deploys as ─────────────────────────────────────────── */}
      <Authority deploy={deploy} info={info} editable={editable} />

      {/* ── If it is not there ────────────────────────────────────────── */}
      <div className={`pl-scan pl-deploy-create${deploy.createIfMissing ? " is-on" : ""}`}>
        <div className="pl-scan-head">
          <span className="pl-scan-icon" aria-hidden="true">
            <PlIcon name="plus" />
          </span>
          <div>
            <strong>Create it if it is missing</strong>
            <p>
              Used only when {deploy.deploymentName ? <code>{deploy.deploymentName}</code> : "the deployment"} is
              not in the namespace{fromTemplate && form.templateName ? <> — created from the inventory template <strong>{form.templateName}</strong></> : ""}. Once it exists, builds change nothing but its image.
              {existing && deploy.createIfMissing && (
                <>
                  {" "}
                  It exists now, so this is only kept for if it is ever removed.{" "}
                  <button type="button" className="btn-ghost pl-link" onClick={() => setShowCreate((open) => !open)}>
                    {showCreate ? "Hide the form" : "Show the form"}
                  </button>
                </>
              )}
            </p>
          </div>
          <Switch
            checked={Boolean(deploy.createIfMissing)}
            disabled={!editable}
            label={deploy.createIfMissing ? "On" : "Off"}
            onChange={(next) => set({ createIfMissing: next })}
          />
        </div>
        {deploy.createIfMissing && (!existing || showCreate) && (
          <div className="pl-scan-body">
            <Field label="Create it from">
              <Segmented
                label="Create it from"
                value={fromTemplate ? "template" : "form"}
                options={[
                  { value: "template", label: "A template from the inventory" },
                  { value: "form", label: "This form" },
                ]}
                disabled={!editable}
                onChange={(source) => setCreate({ source })}
              />
            </Field>
            {fromTemplate ? (
              <Field
                label="Inventory template"
                hint="From Inventory → Templates. The deployment, its Service and storage come from it; the image is the one this build pushed."
              >
                <TemplatePicker
                  id={`${ids}-template`}
                  value={form.templateId}
                  namespace={deploy.namespace}
                  deploymentName={deploy.deploymentName}
                  containerName={deploy.containerName}
                  answers={form.answers}
                  onAnswersChange={(answers) => set({ create: { answers } })}
                  disabled={!editable}
                  onChange={(template) =>
                    set({
                      create: {
                        templateId: template?.id || "",
                        templateName: template?.name || "",
                        // Answers belong to one template's questions.
                        ...(template?.id !== form.templateId ? { answers: { env: {}, volumes: {} } } : {}),
                      },
                      ...(template && !deploy.deploymentName ? { deploymentName: template.deploymentName } : {}),
                    })
                  }
                />
              </Field>
            ) : (
            <>
            {!form.customManifest && (
              <>
                <div className="pl-grid pl-deploy-form">
                  <NumberField id={`${ids}-port`} label="Container port" value={form.port} disabled={!editable} onChange={(port) => setCreate({ port })} placeholder="8080" />
                  <NumberField id={`${ids}-replicas`} label="Replicas" value={form.replicas} disabled={!editable} onChange={(replicas) => setCreate({ replicas })} placeholder="1" />
                  <TextField id={`${ids}-cpu-req`} label="CPU request" value={form.cpuRequest} disabled={!editable} onChange={(cpuRequest) => setCreate({ cpuRequest })} placeholder="100m" />
                  <TextField id={`${ids}-cpu-lim`} label="CPU limit" value={form.cpuLimit} disabled={!editable} onChange={(cpuLimit) => setCreate({ cpuLimit })} placeholder="No limit" />
                  <TextField id={`${ids}-mem-req`} label="Memory request" value={form.memoryRequest} disabled={!editable} onChange={(memoryRequest) => setCreate({ memoryRequest })} placeholder="256Mi" />
                  <TextField id={`${ids}-mem-lim`} label="Memory limit" value={form.memoryLimit} disabled={!editable} onChange={(memoryLimit) => setCreate({ memoryLimit })} placeholder="512Mi" />
                </div>
                <Field label="Environment variables" optional>
                  <EnvRows
                    value={form.env || {}}
                    disabled={!editable}
                    keyPlaceholder="SPRING_PROFILES_ACTIVE"
                    valuePlaceholder="prod"
                    onChange={(env) => setCreate({ env })}
                  />
                </Field>
                <label className="pl-check">
                  <input
                    type="checkbox"
                    checked={Boolean(service.enabled)}
                    disabled={!editable || !form.port}
                    onChange={(event) => setCreate({ service: { ...service, enabled: event.target.checked } })}
                  />
                  <span>
                    <strong>Also create a Service</strong>
                    <small>
                      {form.port
                        ? "So other workloads can reach it by name. Left alone if one with this name already exists."
                        : "Needs a container port."}
                    </small>
                  </span>
                </label>
                {service.enabled && form.port ? (
                  <div className="pl-grid pl-deploy-form">
                    <NumberField id={`${ids}-svc-port`} label="Service port" value={service.port} disabled={!editable} onChange={(port) => setCreate({ service: { ...service, port } })} placeholder="80" />
                    <Field label="Reachable">
                      <SearchableSelect
                        id={`${ids}-svc-type`}
                        aria-label="Reachable"
                        value={service.type || "ClusterIP"}
                        disabled={!editable}
                        options={SERVICE_TYPES.map((item) => ({
                          value: item.value,
                          label: `${item.label} (${item.value})`,
                        }))}
                        onChange={(event) => setCreate({ service: { ...service, type: event.target.value } })}
                      />
                    </Field>
                  </div>
                ) : null}
              </>
            )}
            <Field
              label="Manifest"
              wide
              hint={
                form.customManifest ? (
                  <>
                    Edited by hand, so the form is set aside.{" "}
                    {editable && (
                      <button
                        type="button"
                        className="btn-ghost pl-link"
                        onClick={() => {
                          const next = { ...deploy, create: { ...form, customManifest: false } };
                          onChange({ deploy: { ...next, manifest: generateManifest(next) } });
                        }}
                      >
                        Go back to the form
                      </button>
                    )}
                  </>
                ) : (
                  "Generated from the form. Edit it to take over — only a Deployment and its Service, in this namespace."
                )
              }
            >
              <CommandEditor
                id={`${ids}-manifest`}
                caption="yaml · applied only when the deployment is missing · ${IMAGE} is set by the build"
                lines={String(deploy.manifest || "").split("\n")}
                disabled={!editable}
                onChange={(lines) =>
                  onChange({
                    deploy: { ...deploy, manifest: lines.join("\n"), create: { ...form, customManifest: true } },
                  })
                }
              />
            </Field>
            </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function ImageChoice({ deploy, editable, set, imageStageBefore, fixedImage }) {
  return (
    <>
      <Field label="Image" hint={fixedImage ? "Deployed as written. ${VAR} expands from build inputs." : undefined}>
        <Segmented
          label="Which image"
          value={fixedImage ? "fixed" : "built"}
          options={[
            { value: "built", label: "The image this build pushed" },
            { value: "fixed", label: "A fixed image" },
          ]}
          disabled={!editable}
          onChange={(mode) => set(mode === "built" ? { image: "", imageMode: undefined } : { imageMode: "fixed" })}
        />
      </Field>
      {fixedImage ? (
        <input
          aria-label="Image to deploy"
          className="is-mono pl-deploy-image"
          value={deploy.image}
          placeholder="registry.areeba.com/payments-api:${IMAGE_TAG}"
          disabled={!editable}
          spellCheck={false}
          onChange={(event) => set({ image: event.target.value.trim(), imageMode: "fixed" })}
        />
      ) : (
        !imageStageBefore && (
          <div className="pl-note is-warn">
            <PlIcon name="alert" />
            <p>
              <strong>No “Build an image” stage comes before this one.</strong> A build would have
              nothing to deploy and fail here. Add one above, or deploy a fixed image.
            </p>
          </div>
        )
      )}
    </>
  );
}

/** Which link a build would use right now — the same pick the backend makes. */
function LinkedPreview({ isHome, links, environment }) {
  if (isHome) {
    return (
      <p className="pl-note is-muted">
        <PlIcon name="link" />
        <span>
          Run on its own, this pipeline has no service behind it, so this stage stops with that reason. It
          deploys when a CI service that uses the pipeline builds, to that service's linked deployment.
        </span>
      </p>
    );
  }
  if (links === null) {
    return (
      <p className="pl-deploy-status is-loading">
        <span className="sg-ci-pulse" aria-hidden="true" /> Reading this service's linked deployments…
      </p>
    );
  }
  const { link, problem } = pickLinkedDeployment(links, environment);
  if (!link) {
    return (
      <div className="pl-note is-warn">
        <PlIcon name="alert" />
        <p>
          <strong>{problem}</strong> A build stops at this stage until it is. Link deployments under Settings →
          Deployments.
        </p>
      </div>
    );
  }
  return (
    <div className="pl-deploy-status is-exists">
      <PlIcon name="check" />
      <p>
        <strong>Deploys to {link.clusterId} / {link.namespace} / {link.workloadName}</strong>
        {link.environment ? <> · {link.environment}</> : null}
        {link.templateName ? <> · created from the template {link.templateName} if it is not there</> : null}
        <small>
          {link.canDeployThrough
            ? `With the rights of ${link.authorizedBy || "whoever linked it"}, re-checked when the build runs.`
            : "Whoever linked it could not deploy there, so the stage will stop. Someone who can must choose “Deploy as me” on the link."}
        </small>
      </p>
    </div>
  );
}

/** "Link this deployment to the service": the inventory link a fixed target
 * implies. On by default; never takes a deployment another service has. */
function LinkToService({ deploy, existing, service, editable, onChange }) {
  const owner = existing?.linkedService || null;
  const ours = owner && service && String(owner.id) === String(service.id);
  if (owner && !ours) {
    return (
      <p className="pl-note is-muted">
        <PlIcon name="link" />
        <span>
          <code>{deploy.deploymentName}</code> is linked to <strong>{owner.name}</strong>, so it stays that
          service's in the inventory. This stage still deploys to it.
        </span>
      </p>
    );
  }
  return (
    <label className="pl-check">
      <input
        type="checkbox"
        checked={deploy.linkToService !== false}
        disabled={!editable}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span>
        <strong>{ours ? "Linked to this service" : "Link this deployment to the service"}</strong>
        <small>
          {ours
            ? "The inventory names this service on it, and ticket-driven deploys build this service."
            : "When the pipeline is saved, and whenever this stage deploys. The inventory then names this service on it."}
        </small>
      </span>
    </label>
  );
}

function TargetStatus({ deploy, info, target, targetKey, existing, clusterName }) {
  if (!deploy.clusterId || !deploy.namespace) return null;
  if (target.key !== targetKey || target.loading) {
    return (
      <p className="pl-deploy-status is-loading">
        <span className="sg-ci-pulse" aria-hidden="true" /> Reading {clusterName}…
      </p>
    );
  }
  if (target.error) {
    return (
      <div className="pl-note is-warn">
        <PlIcon name="alert" />
        <p>
          <strong>Could not read the cluster.</strong> {target.error} The stage can still be saved; a
          build checks again when it runs.
        </p>
      </div>
    );
  }
  if (info?.namespaceExists === false) {
    return (
      <div className="pl-note is-warn">
        <PlIcon name="alert" />
        <p>
          <strong>
            <code>{deploy.namespace}</code> does not exist on {clusterName}.
          </strong>{" "}
          Builds stop here until it is created — a build never creates a namespace.
        </p>
      </div>
    );
  }
  if (!deploy.deploymentName) return null;
  if (existing) {
    const container = existing.containers.find((item) => item.name === deploy.containerName) ||
      (existing.containers.length === 1 ? existing.containers[0] : null);
    return (
      <div className="pl-deploy-status is-exists">
        <PlIcon name="check" />
        <p>
          <strong>Exists</strong> · {existing.ready}/{existing.desired} ready
          {container ? (
            <>
              {" "}
              · <code>{container.name}</code> runs <code>{container.image}</code>
            </>
          ) : null}
          <small>Each build replaces only this container's image. Replicas, env and everything else stay as they are.</small>
        </p>
      </div>
    );
  }
  return deploy.createIfMissing ? (
    <div className="pl-deploy-status is-new">
      <PlIcon name="plus" />
      <p>
        <strong>Not in {deploy.namespace} yet</strong> — the first build creates it from the manifest below.
      </p>
    </div>
  ) : (
    <div className="pl-note is-warn">
      <PlIcon name="alert" />
      <p>
        <strong>
          <code>{deploy.deploymentName}</code> is not in {deploy.namespace}.
        </strong>{" "}
        Builds stop here until it exists, or until “Create it if it is missing” is on.
      </p>
    </div>
  );
}

function Authority({ deploy, info, editable }) {
  const stamp = deploy.authorizedBy;
  const cannot = info && !info.canDeploy;
  return (
    <div className={`pl-deploy-authority${cannot && editable ? " is-warn" : ""}`}>
      <PlIcon name="key" />
      <p>
        {stamp ? (
          <>
            <strong>Deploys as {stamp.username}</strong>
            {stamp.at ? <> · authorized {formatRelative(stamp.at)}</> : null}
            <small>
              Every build deploys with their rights, whoever or whatever started it — a webhook, a
              ticket, Hermes. Changing the target, image or manifest makes whoever saves it the one
              it deploys as.
            </small>
          </>
        ) : (
          <>
            <strong>Saving makes you the person this stage deploys as</strong>
            <small>
              Builds are often started by a webhook or a ticket, so the stage uses the rights of
              whoever saved its target. That needs permission to deploy to this namespace.
            </small>
          </>
        )}
        {cannot && editable && (
          <small className="is-warn">
            You cannot deploy to this namespace, so a change to this target will be refused on save.
          </small>
        )}
      </p>
    </div>
  );
}

function Guard({ icon, title, text, tone }) {
  return (
    <li className={`pl-deploy-guard${tone ? ` is-${tone}` : ""}`}>
      <span className="pl-deploy-guard-icon" aria-hidden="true">
        <PlIcon name={icon} />
      </span>
      <span>
        <strong>{title}</strong>
        <small>{text}</small>
      </span>
    </li>
  );
}

function NumberField({ id, label, value, onChange, disabled, placeholder }) {
  return (
    <Field label={label} htmlFor={id}>
      <input
        id={id}
        type="number"
        min={0}
        value={value ?? ""}
        placeholder={placeholder}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value === "" ? null : Number(event.target.value))}
      />
    </Field>
  );
}

function TextField({ id, label, value, onChange, disabled, placeholder }) {
  return (
    <Field label={label} htmlFor={id}>
      <input
        id={id}
        className="is-mono"
        value={value || ""}
        placeholder={placeholder}
        disabled={disabled}
        spellCheck={false}
        onChange={(event) => onChange(event.target.value.trim())}
      />
    </Field>
  );
}
