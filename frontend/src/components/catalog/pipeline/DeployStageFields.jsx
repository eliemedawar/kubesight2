import { useEffect, useMemo, useState } from "react";
import { getCiDeployTarget, listCiDeploymentLinks } from "../../../api/ciApi.js";
import { listClusters, listNamespacesByCluster } from "../../../api/clustersApi.js";
import { getHelmChartTemplate, listHelmChartTemplates } from "../../../api/helmApi.js";
import SearchableSelect from "../../common/SearchableSelect.jsx";
import { formatRelative } from "../ciShared.jsx";
import { CommandEditor, EnvRows, Field, Segmented, Switch } from "./controls.jsx";
import {
  blankDeploy,
  blankHelm,
  generateManifest,
  blankHelmImage,
  guessImageRows,
  blankDeploymentRow,
  guessImageValues,
  isHelmDeploy,
  MAX_DEPLOYMENTS,
  MAX_HELM_IMAGES,
  setsSeveralDeployments,
  setsSeveralImages,
  startDeploymentRows,
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
  const helmMode = isHelmDeploy(deploy);
  // A Helm target is edited as a whole: no manifest to keep in step.
  const setHelm = (patch) => onChange({ deploy: { ...deploy, ...patch } });
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

  const targetKey = `${deploy.clusterId}|${deploy.namespace}|${helmMode ? "helm" : ""}`;
  useEffect(() => {
    if (!deploy.clusterId) return undefined;
    let cancelled = false;
    setTarget((prev) => ({ ...prev, key: targetKey, loading: true, error: "" }));
    getCiDeployTarget(deploy.clusterId, deploy.namespace, { helm: helmMode })
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
  const imageStages = stages
    .slice(0, index)
    .filter((other) => other.stageType === "container_image" && String(other.name || "").trim())
    .map((other) => ({ name: String(other.name).trim(), enabled: other.enabled !== false }));
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
        value={helmMode ? "helm" : linkedMode ? "linked" : "fixed"}
        options={[
          { value: "fixed", label: "A deployment I pick" },
          { value: "linked", label: "The service's linked deployment" },
          { value: "helm", label: "A Helm chart" },
        ]}
        disabled={!editable}
        onChange={(mode) => onChange({ deploy: setDeployTargetMode(deploy, mode) })}
      />
    </Field>
  );

  const clusterField = (onPick) => (
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
        onChange={(event) => event.target.value !== deploy.clusterId && onPick(event.target.value)}
      />
    </Field>
  );

  const namespaceField = (onPick) => (
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
        onChange={(event) => event.target.value !== deploy.namespace && onPick(event.target.value)}
      />
    </Field>
  );

  const registryGuard = (
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
  );

  const approvalGuard = (
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
  );

  if (helmMode) {
    const releases = info?.helmReleases || [];
    return (
      <div className="pl-deploy">
        {modeSwitch}
        <div className="pl-grid pl-deploy-target">
          {clusterField((clusterId) => setHelm({ clusterId, namespace: "", releaseName: "" }))}
          {namespaceField((namespace) => setHelm({ namespace, releaseName: "" }))}
          <Field
            label="Helm release"
            hint="Pick one installed in the namespace, or type a new name — the first build installs it."
          >
            <SearchableSelect
              id={`${ids}-release`}
              aria-label="Helm release"
              value={deploy.releaseName || ""}
              disabled={!editable || !deploy.namespace}
              placeholder={deploy.namespace ? "Pick or name a release…" : "Pick a namespace first"}
              searchPlaceholder="Search, or type a new name…"
              allowCustom
              customOptionLabel={(name) => `Install as “${name.toLowerCase()}”`}
              options={releases.map((item) => ({
                value: item.name,
                label: (
                  <span className="pl-deploy-option">
                    <span>{item.name}</span>
                    <small>{item.chart} · rev {item.revision}</small>
                  </span>
                ),
              }))}
              onChange={(event) => setHelm({ releaseName: String(event.target.value || "").trim().toLowerCase() })}
            />
          </Field>
        </div>

        <HelmChartFields ids={ids} deploy={deploy} editable={editable} onChange={setHelm} imageStages={imageStages} />

        <HelmReleaseStatus deploy={deploy} info={info} target={target} targetKey={targetKey} clusterName={clusterName} />

        {!setsSeveralImages(deploy.helm) && (
          <ImageChoice deploy={deploy} editable={editable} set={setHelm} imageStageBefore={imageStageBefore} fixedImage={fixedImage} />
        )}

        <ul className="pl-deploy-guards" aria-label="What this stage checks">
          {registryGuard}
          {approvalGuard}
          <Guard
            icon="undo"
            title="Watched, and rolled back if it fails"
            text={`Passes only when every Deployment of the release has rolled out. Crash loops, image pull errors or not ready within ${timeoutLabel(stage.timeoutSeconds)} roll the release back to its previous revision — or uninstall it, if this stage installed it.`}
          />
        </ul>

        <Authority deploy={deploy} info={info} editable={editable} helm />
      </div>
    );
  }

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

  const severalDeployments = setsSeveralDeployments(deploy);
  const countSwitch = (
    <Field
      label="Deployments"
      hint={
        severalDeployments
          ? "For a pipeline that builds several images — a frontend and a backend, say. Each deployment takes its own stage's image; all of them change together, as one change."
          : "Pick several when this pipeline builds images for more than one deployment."
      }
    >
      <Segmented
        label="How many deployments"
        value={severalDeployments ? "several" : "one"}
        options={[
          { value: "one", label: "One deployment" },
          { value: "several", label: "Several deployments" },
        ]}
        disabled={!editable}
        onChange={(count) =>
          onChange({
            deploy:
              count === "several"
                ? {
                    ...deploy,
                    deploymentSet: "several",
                    deployments: deploy.deployments?.length
                      ? deploy.deployments
                      : startDeploymentRows(deploy, imageStages.map((item) => item.name)),
                  }
                : { ...deploy, deploymentSet: undefined },
          })
        }
      />
    </Field>
  );

  if (severalDeployments) {
    const setRows = (deployments) => onChange({ deploy: { ...deploy, deployments } });
    return (
      <div className="pl-deploy">
        {modeSwitch}
        {countSwitch}
        <div className="pl-grid pl-deploy-target">
          {clusterField((clusterId) => setHelm({ clusterId, namespace: "", deployments: (deploy.deployments || []).map((row) => ({ ...row, deploymentName: "", containerName: "" })) }))}
          {namespaceField((namespace) => setHelm({ namespace, deployments: (deploy.deployments || []).map((row) => ({ ...row, deploymentName: "", containerName: "" })) }))}
        </div>
        <TargetStatus deploy={{ ...deploy, deploymentName: "" }} info={info} target={target} targetKey={targetKey} existing={null} clusterName={clusterName} />
        <DeploymentRows
          ids={ids}
          rows={deploy.deployments || []}
          info={info}
          namespace={deploy.namespace}
          imageStages={imageStages}
          editable={editable}
          onChange={setRows}
        />
        {!isHome && (
          <label className="pl-check">
            <input
              type="checkbox"
              checked={deploy.linkToService !== false}
              disabled={!editable}
              onChange={(event) => onChange({ deploy: { ...deploy, linkToService: event.target.checked } })}
            />
            <span>
              <strong>Link these deployments to the service</strong>
              <small>
                When the pipeline is saved, and whenever this stage deploys. A deployment another service is linked
                to stays that service's.
              </small>
            </span>
          </label>
        )}
        <ul className="pl-deploy-guards" aria-label="What this stage checks">
          {registryGuard}
          {approvalGuard}
          <Guard
            icon="undo"
            title="All of them, or none"
            text={`Every image must be found and every deployment must exist before anything changes; they are applied together, as one change. The stage passes only when all of them have rolled out. Crash loops, image pull errors or not ready within ${timeoutLabel(stage.timeoutSeconds)} put every previous image back.`}
          />
        </ul>
        <Authority deploy={deploy} info={info} editable={editable} />
      </div>
    );
  }

  return (
    <div className="pl-deploy">
      {modeSwitch}
      {countSwitch}
      {/* ── Where ─────────────────────────────────────────────────────── */}
      <div className="pl-grid pl-deploy-target">
        {clusterField((clusterId) => set({ clusterId, namespace: "", deploymentName: "", containerName: "" }))}
        {namespaceField((namespace) => set({ namespace, deploymentName: "", containerName: "" }))}
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
        {registryGuard}
        {approvalGuard}
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

/** The chart a Helm target deploys, its version, the values file for the
 * first install, and the chart value(s) the build's image goes into. */
function HelmChartFields({ ids, deploy, editable, onChange, imageStages }) {
  const helm = { ...blankHelm(), ...(deploy.helm || {}) };
  const image = helm.imageValues || blankHelm().imageValues;
  const several = setsSeveralImages(helm);
  const [charts, setCharts] = useState(null);
  const [detail, setDetail] = useState({ key: "", data: null });
  const setChart = (patch) => onChange({ helm: { ...helm, ...patch } });

  useEffect(() => {
    let cancelled = false;
    listHelmChartTemplates()
      .then((result) => !cancelled && setCharts(Array.isArray(result) ? result : []))
      .catch(() => !cancelled && setCharts([]));
    return () => {
      cancelled = true;
    };
  }, []);

  // The chosen version's values files.
  const detailKey = `${helm.chartTemplateId}@${helm.chartVersion}`;
  useEffect(() => {
    if (!helm.chartTemplateId) return undefined;
    let cancelled = false;
    getHelmChartTemplate(helm.chartTemplateId, helm.chartVersion)
      .then((data) => !cancelled && setDetail({ key: detailKey, data }))
      .catch(() => !cancelled && setDetail({ key: detailKey, data: null }));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detailKey]);

  const chart = (charts || []).find((item) => item.id === helm.chartTemplateId) || null;
  const versions = chart?.versions?.length ? chart.versions.map((item) => item.version) : chart ? [chart.version] : [];
  const valuesFiles = detail.key === detailKey ? detail.data?.chart?.valuesFiles || [] : [];

  // Picking a chart guesses where its image goes from its default values, and
  // names the release after it when none is named yet.
  const stageNames = imageStages.map((stage) => stage.name);
  const pickChart = async (id) => {
    const picked = (charts || []).find((item) => item.id === id);
    if (!picked || id === helm.chartTemplateId) return;
    let imageValues = helm.imageValues;
    let images = helm.images;
    try {
      const { defaultValues } = await getHelmChartTemplate(id);
      imageValues = guessImageValues(defaultValues);
      if (several) images = guessImageRows(defaultValues, stageNames);
    } catch {
      // Keep what was there; the fields below can still be edited.
    }
    onChange({
      helm: { ...helm, chartTemplateId: id, chartName: picked.name, chartVersion: "", valuesFile: "", imageValues, images },
      ...(deploy.releaseName
        ? {}
        : { releaseName: String(picked.name || "").toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 53) }),
    });
  };

  // Several images start from what the chart's values hold — one row per
  // image found, paired in order with the image stages above. Rows already
  // written (switching back and forth) are kept.
  const startSeveral = async () => {
    if (helm.images?.length) {
      setChart({ imageSet: "several" });
      return;
    }
    let defaultValues = detail.key === detailKey ? detail.data?.defaultValues : undefined;
    if (defaultValues === undefined && helm.chartTemplateId) {
      try {
        defaultValues = (await getHelmChartTemplate(helm.chartTemplateId, helm.chartVersion)).defaultValues;
      } catch {
        defaultValues = null;
      }
    }
    setChart({ imageSet: "several", images: guessImageRows(defaultValues, stageNames) });
  };

  return (
    <>
      <div className="pl-grid pl-deploy-target">
        <Field label="Inventory chart" hint="From Inventory → Helm charts.">
          <SearchableSelect
            id={`${ids}-chart`}
            aria-label="Inventory chart"
            value={helm.chartTemplateId}
            disabled={!editable || charts === null}
            placeholder={charts === null ? "Loading charts…" : charts.length ? "Pick a chart…" : "No charts in the inventory"}
            searchPlaceholder="Search charts…"
            options={[
              ...(helm.chartTemplateId && !chart ? [{ value: helm.chartTemplateId, label: helm.chartName || helm.chartTemplateId }] : []),
              ...(charts || []).map((item) => ({
                value: item.id,
                label: (
                  <span className="pl-deploy-option">
                    <span>{item.name}</span>
                    <small>v{item.version}</small>
                  </span>
                ),
              })),
            ]}
            onChange={(event) => pickChart(event.target.value)}
          />
        </Field>
        <Field label="Version" hint="The current one follows the chart: a newer version set current in the inventory is used by the next build.">
          <SearchableSelect
            id={`${ids}-chart-version`}
            aria-label="Chart version"
            value={helm.chartVersion || ""}
            disabled={!editable || !helm.chartTemplateId}
            options={[
              { value: "", label: chart ? `The current version (now v${chart.version})` : "The current version" },
              ...versions.map((version) => ({ value: version, label: `v${version}` })),
            ]}
            onChange={(event) => setChart({ chartVersion: event.target.value, valuesFile: "" })}
          />
        </Field>
        <Field
          label="Values for the first install"
          optional
          hint="Used only when the release is installed. Nothing has to be filled in, and upgrades keep the values the release runs with."
        >
          <SearchableSelect
            id={`${ids}-values-file`}
            aria-label="Values for the first install"
            value={helm.valuesFile || ""}
            disabled={!editable || !helm.chartTemplateId}
            options={[
              { value: "", label: "The chart's own values" },
              ...(helm.valuesFile && !valuesFiles.some((item) => item.path === helm.valuesFile)
                ? [{ value: helm.valuesFile, label: helm.valuesFile }]
                : []),
              ...valuesFiles.map((item) => ({ value: item.path, label: item.path })),
            ]}
            onChange={(event) => setChart({ valuesFile: event.target.value })}
          />
        </Field>
      </div>
      <Field
        label="Images"
        hint={
          several
            ? "Each image goes into its own chart values, all set in the same upgrade: one revision, one approval, one rollback."
            : "The build's image goes into the chart. Pick several for a chart that runs more than one image."
        }
      >
        <Segmented
          label="How many images"
          value={several ? "several" : "one"}
          options={[
            { value: "one", label: "One image" },
            { value: "several", label: "Several images" },
          ]}
          disabled={!editable}
          onChange={(count) => (count === "several" ? startSeveral() : setChart({ imageSet: undefined }))}
        />
      </Field>
      {several ? (
        <HelmImageRows ids={ids} rows={helm.images || []} imageStages={imageStages} editable={editable} onChange={(images) => setChart({ images })} />
      ) : (
        <ImageValuesFields
          ids={`${ids}-image`}
          label="The image goes into"
          hint="The chart values each build sets to the image it deploys. Nothing else changes."
          value={image}
          editable={editable}
          onChange={(imageValues) => setChart({ imageValues })}
        />
      )}
    </>
  );
}

/** Where one image goes in the chart: a repository and a tag value, or one
 * value holding the whole reference. */
function ImageValuesFields({ ids, label, hint, value, editable, onChange }) {
  const image = value || {};
  return (
    <>
      <Field label={label} hint={hint}>
        <Segmented
          label={label}
          value={image.mode === "full" ? "full" : "split"}
          options={[
            { value: "split", label: "A repository and a tag value" },
            { value: "full", label: "One value" },
          ]}
          disabled={!editable}
          onChange={(mode) =>
            onChange(
              mode === "full"
                ? { mode: "full", path: image.path || "image" }
                : { mode: "split", repository: image.repository || "image.repository", tag: image.tag || "image.tag" }
            )
          }
        />
      </Field>
      <div className="pl-grid pl-deploy-target">
        {image.mode === "full" ? (
          <TextField
            id={`${ids}-path`}
            label="Image value"
            value={image.path}
            disabled={!editable}
            placeholder="image"
            onChange={(path) => onChange({ ...image, path })}
          />
        ) : (
          <>
            <TextField
              id={`${ids}-repository`}
              label="Repository value"
              value={image.repository}
              disabled={!editable}
              placeholder="image.repository"
              onChange={(repository) => onChange({ ...image, repository })}
            />
            <TextField
              id={`${ids}-tag`}
              label="Tag value"
              value={image.tag}
              disabled={!editable}
              placeholder="image.tag"
              onChange={(tag) => onChange({ ...image, tag })}
            />
          </>
        )}
      </div>
    </>
  );
}

const FIXED_IMAGE = "__fixed__";

/** The images of a chart that runs several: each from an image stage above
 * (or a fixed reference), each into chart values of its own. */
function HelmImageRows({ ids, rows, imageStages, editable, onChange }) {
  const setRow = (index, patch) => onChange(rows.map((row, position) => (position === index ? { ...row, ...patch } : row)));
  const used = new Set(rows.filter((row) => row.from !== "fixed").map((row) => row.stage));
  const nextStage = (imageStages.find((stage) => !used.has(stage.name)) || {}).name || "";
  return (
    <div className="pl-helm-images">
      {!imageStages.length && (
        <div className="pl-note is-warn">
          <PlIcon name="alert" />
          <p>
            <strong>No “Build an image” stage comes before this one.</strong> Add one per image above, or give each
            image a fixed reference.
          </p>
        </div>
      )}
      {rows.map((row, index) => {
        const fixed = row.from === "fixed";
        return (
          <section className="pl-helm-image" key={index} aria-label={`Image ${index + 1}`}>
            <header>
              <strong>Image {index + 1}</strong>
              {editable && (
                <button
                  type="button"
                  className="btn-ghost pl-rows-remove"
                  aria-label={`Remove image ${index + 1}`}
                  title="Remove"
                  disabled={rows.length === 1}
                  onClick={() => onChange(rows.filter((_, position) => position !== index))}
                >
                  <PlIcon name="x" />
                </button>
              )}
            </header>
            <div className="pl-grid pl-deploy-target">
              <ImageSourceField
                id={`${ids}-image-${index}-from`}
                label={`Where image ${index + 1} comes from`}
                row={row}
                imageStages={imageStages}
                editable={editable}
                onChange={(patch) => setRow(index, patch)}
              />
              {fixed && (
                <TextField
                  id={`${ids}-image-${index}-ref`}
                  label="Image"
                  value={row.image}
                  disabled={!editable}
                  placeholder="registry.areeba.com/migrations:${KUBESIGHT_BUILD_NUMBER}"
                  onChange={(image) => setRow(index, { image })}
                />
              )}
            </div>
            <ImageValuesFields
              ids={`${ids}-image-${index}`}
              label="Goes into"
              value={row.imageValues}
              editable={editable}
              onChange={(imageValues) => setRow(index, { imageValues })}
            />
          </section>
        );
      })}
      {editable && rows.length < MAX_HELM_IMAGES && (
        <button type="button" className="btn-ghost pl-rows-add" onClick={() => onChange([...rows, blankHelmImage(nextStage)])}>
          <PlIcon name="plus" /> Add an image
        </button>
      )}
    </div>
  );
}

/** Where a row's image comes from: an image stage above, or a fixed image. */
function ImageSourceField({ id, label, row, imageStages, editable, onChange }) {
  const fixed = row.from === "fixed";
  const known = imageStages.find((stage) => stage.name === row.stage);
  return (
    <Field
      label="Image from"
      hint={
        fixed
          ? "Deployed as written. ${VAR} expands from build inputs."
          : known && !known.enabled
            ? "That stage is turned off, so a build stops here with nothing deployed."
            : "The image that stage pushes in the same build."
      }
    >
      <SearchableSelect
        id={id}
        aria-label={label}
        value={fixed ? FIXED_IMAGE : row.stage || ""}
        disabled={!editable}
        placeholder="Pick an image stage…"
        options={[
          ...(!fixed && row.stage && !known ? [{ value: row.stage, label: `${row.stage} (not an image stage above)` }] : []),
          ...imageStages.map((stage) => ({ value: stage.name, label: stage.enabled ? stage.name : `${stage.name} (off)` })),
          { value: FIXED_IMAGE, label: "A fixed image" },
        ]}
        onChange={(event) =>
          onChange(
            event.target.value === FIXED_IMAGE
              ? { from: "fixed", stage: "" }
              : { from: "stage", stage: event.target.value, image: "" }
          )
        }
      />
    </Field>
  );
}

/** The deployments of a several-deployments target: each with the container
 * whose image changes and the stage its image comes from. */
function DeploymentRows({ ids, rows, info, namespace, imageStages, editable, onChange }) {
  const setRow = (index, patch) => onChange(rows.map((row, position) => (position === index ? { ...row, ...patch } : row)));
  const used = new Set(rows.filter((row) => row.from !== "fixed").map((row) => row.stage));
  const nextStage = (imageStages.find((stage) => !used.has(stage.name)) || {}).name || "";
  const deployments = info?.deployments || [];
  return (
    <div className="pl-helm-images">
      {!imageStages.length && (
        <div className="pl-note is-warn">
          <PlIcon name="alert" />
          <p>
            <strong>No “Build an image” stage comes before this one.</strong> Add one per image above, or give each
            deployment a fixed image.
          </p>
        </div>
      )}
      {rows.map((row, index) => {
        const live = deployments.find((item) => item.name === row.deploymentName) || null;
        const containers = live?.containers || [];
        const container = containers.find((item) => item.name === row.containerName) || (containers.length === 1 ? containers[0] : null);
        return (
          <section className="pl-helm-image" key={index} aria-label={`Deployment ${index + 1}`}>
            <header>
              <strong>Deployment {index + 1}</strong>
              {editable && (
                <button
                  type="button"
                  className="btn-ghost pl-rows-remove"
                  aria-label={`Remove deployment ${index + 1}`}
                  title="Remove"
                  disabled={rows.length === 1}
                  onClick={() => onChange(rows.filter((_, position) => position !== index))}
                >
                  <PlIcon name="x" />
                </button>
              )}
            </header>
            <div className="pl-grid pl-deploy-target">
              <Field label="Deployment">
                <SearchableSelect
                  id={`${ids}-deployment-${index}`}
                  aria-label={`Deployment ${index + 1}`}
                  value={row.deploymentName}
                  disabled={!editable || !namespace}
                  placeholder={namespace ? "Pick a deployment…" : "Pick a namespace first"}
                  searchPlaceholder="Search deployments…"
                  allowCustom
                  customOptionLabel={(name) => `Use “${name.toLowerCase()}”`}
                  options={deployments.map((item) => ({
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
                    if (name !== row.deploymentName) setRow(index, { deploymentName: name, containerName: "" });
                  }}
                />
              </Field>
              <ImageSourceField
                id={`${ids}-deployment-${index}-from`}
                label={`Where deployment ${index + 1}'s image comes from`}
                row={row}
                imageStages={imageStages}
                editable={editable}
                onChange={(patch) => setRow(index, patch)}
              />
              {containers.length > 1 && (
                <Field label="Container" hint="The one whose image this build replaces.">
                  <SearchableSelect
                    id={`${ids}-deployment-${index}-container`}
                    aria-label={`Container of deployment ${index + 1}`}
                    value={row.containerName}
                    disabled={!editable}
                    placeholder="Pick a container…"
                    options={containers.map((item) => ({
                      value: item.name,
                      label: (
                        <span className="pl-deploy-option">
                          <span>{item.name}</span>
                          <small>{item.image}</small>
                        </span>
                      ),
                    }))}
                    onChange={(event) => setRow(index, { containerName: event.target.value })}
                  />
                </Field>
              )}
              {row.from === "fixed" && (
                <TextField
                  id={`${ids}-deployment-${index}-image`}
                  label="Image"
                  value={row.image}
                  disabled={!editable}
                  placeholder="registry.areeba.com/frontend:${IMAGE_TAG}"
                  onChange={(image) => setRow(index, { image })}
                />
              )}
            </div>
            {row.deploymentName && info && (
              live ? (
                <p className="pl-helm-image-status">
                  <PlIcon name="check" /> {live.ready}/{live.desired} ready
                  {container ? (
                    <>
                      {" "}
                      · <code>{container.name}</code> runs <code>{container.image}</code>
                    </>
                  ) : containers.length > 1 ? (
                    " · pick the container"
                  ) : null}
                </p>
              ) : (
                <p className="pl-helm-image-status is-warn">
                  <PlIcon name="alert" /> Not in {namespace} — builds stop here until it exists. This stage never
                  creates a deployment.
                </p>
              )
            )}
          </section>
        );
      })}
      {editable && rows.length < MAX_DEPLOYMENTS && (
        <button type="button" className="btn-ghost pl-rows-add" onClick={() => onChange([...rows, blankDeploymentRow(nextStage)])}>
          <PlIcon name="plus" /> Add a deployment
        </button>
      )}
    </div>
  );
}

/** Whether the release is there: installed (each build upgrades it) or not
 * yet (the first build installs it). */
function HelmReleaseStatus({ deploy, info, target, targetKey, clusterName }) {
  if (!deploy.clusterId || !deploy.namespace) return null;
  if (target.key !== targetKey || target.loading) {
    return (
      <p className="pl-deploy-status is-loading">
        <span className="sg-ci-pulse" aria-hidden="true" /> Reading {clusterName}…
      </p>
    );
  }
  if (target.error || info?.helmError) {
    return (
      <div className="pl-note is-warn">
        <PlIcon name="alert" />
        <p>
          <strong>Could not read the namespace&apos;s Helm releases.</strong> {target.error || info.helmError} The
          stage can still be saved; a build checks again when it runs.
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
  if (!deploy.releaseName) return null;
  const release = (info?.helmReleases || []).find((item) => item.name === deploy.releaseName);
  const chart = deploy.helm?.chartName || "the chart";
  if (release) {
    return (
      <div className="pl-deploy-status is-exists">
        <PlIcon name="check" />
        <p>
          <strong>Installed</strong> · revision {release.revision} · <code>{release.chart}</code> · {release.status}
          <small>
            Each build upgrades it to {chart}, keeping the values it runs with — only the image values change.
          </small>
        </p>
      </div>
    );
  }
  return (
    <div className="pl-deploy-status is-new">
      <PlIcon name="plus" />
      <p>
        <strong>Not installed in {deploy.namespace} yet</strong> — the first build installs {chart} with{" "}
        {deploy.helm?.valuesFile ? <code>{deploy.helm.valuesFile}</code> : "its own values"}. Nothing has to be
        filled in.
      </p>
    </div>
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

function Authority({ deploy, info, editable, helm = false }) {
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
              whoever saved its target. That needs permission to deploy to this namespace
              {helm ? " and to install and upgrade Helm releases" : ""}.
            </small>
          </>
        )}
        {cannot && editable && (
          <small className="is-warn">
            {helm
              ? "You cannot deploy Helm releases to this namespace, so a change to this target will be refused on save."
              : "You cannot deploy to this namespace, so a change to this target will be refused on save."}
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
