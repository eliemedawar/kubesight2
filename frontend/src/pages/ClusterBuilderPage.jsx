import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { parseApiTime } from "../lib/apiTime";
import PageTitle from "../components/common/PageTitle.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import {
  cancelClusterBuild,
  createClusterBuild,
  createBuildProfile,
  createSshCredential,
  createSshProfile,
  createVSphereConnection,
  deleteBuildProfile,
  deleteClusterBuild,
  deleteSshCredential,
  deleteSshProfile,
  deleteVSphereConnection,
  getBuilderOptions,
  getClusterBuild,
  getClusterBuildLogs,
  listBuildProfiles,
  listClusterBuilds,
  listSshCredentials,
  listSshProfiles,
  listVSphereConnections,
  listVSphereVms,
  preflightClusterBuild,
  retryClusterBuild,
  startClusterBuild,
  testSshProfile,
  testVSphereConnection,
  updateClusterBuild,
} from "../api/clusterBuildsApi.js";

const POLL_INTERVAL_MS = 5000;

const PHASE_LABELS = {
  base_prep: "Node preparation",
  loadbalancer: "Load balancer + VIP",
  pull_images: "Image pull",
  init: "kubeadm init",
  cni: "CNI install",
  join_cp: "Join control planes",
  join_workers: "Join workers",
  verify: "Cluster verification",
  onboard: "Register in KubeSight",
  addons: "Install add-ons",
};

const PHASE_NOTES = {
  base_prep: "containerd + kubeadm on every machine, in parallel",
  loadbalancer: "haproxy + keepalived — VIP live before init",
  pull_images: "fails early if the registry is missing anything",
  init: "through the stable endpoint — it goes into the certs",
  cni: "network plugin, pod CIDR applied",
  join_cp: "one at a time, etcd quorum checked between",
  join_workers: "in parallel",
  verify: "nodes Ready · CoreDNS · etcd · smoke pod",
  onboard: "admin.conf → Clusters page, no manual step",
  addons: "selected add-ons, pinned and verified",
};

const PHASE_ORDER = [
  "base_prep", "loadbalancer", "pull_images", "init", "cni",
  "join_cp", "join_workers", "verify", "onboard", "addons",
];

function addonDisplayName(addon, catalog = []) {
  const id = typeof addon === "string" ? addon : addon?.id;
  const match = catalog.find((item) => item.id === id);
  if (match?.displayName) return match.displayName;
  return String(id || "Unknown add-on")
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function addonSummary(addons = [], catalog = [], includeVersions = false) {
  if (!addons.length) return "None";
  return addons.map((addon) => {
    const name = addonDisplayName(addon, catalog);
    const version = typeof addon === "object" ? addon.version : "";
    return includeVersions && version ? `${name} v${version}` : name;
  }).join(", ");
}

function AddonChips({ addons = [], catalog = [], empty = "None" }) {
  if (!addons.length) return <span className="muted">{empty}</span>;
  return (
    <span className="sg-cb-chiprow">
      {addons.map((addon) => {
        const id = typeof addon === "string" ? addon : addon.id;
        const version = typeof addon === "object" ? addon.version : "";
        return (
          <span className="sg-cb-chip" key={`${id}-${version}`}>
            {addonDisplayName(addon, catalog)}
            {version ? <span className="sg-cb-chip-version">v{version}</span> : null}
          </span>
        );
      })}
    </span>
  );
}

const ROLE_LABELS = {
  control_plane: "Control plane",
  worker: "Worker",
  loadbalancer: "Load balancer",
};

function StatusPill({ status }) {
  const map = {
    draft: ["Draft", "sg-cb-pill-muted"],
    preflighting: ["Preflighting…", "sg-cb-pill-active"],
    preflight_passed: ["Preflight passed", "sg-cb-pill-ok"],
    preflight_failed: ["Preflight failed", "sg-cb-pill-bad"],
    building: ["Building…", "sg-cb-pill-active"],
    completed: ["Completed", "sg-cb-pill-ok"],
    failed: ["Failed", "sg-cb-pill-bad"],
    cancelled: ["Cancelled", "sg-cb-pill-muted"],
    pending: ["Pending", "sg-cb-pill-muted"],
    running: ["Running…", "sg-cb-pill-active"],
    skipped: ["Skipped", "sg-cb-pill-muted"],
    preparing: ["Preparing…", "sg-cb-pill-active"],
    ready: ["Prepared", "sg-cb-pill-ok"],
    joined: ["Joined", "sg-cb-pill-ok"],
    pass: ["Pass", "sg-cb-pill-ok"],
    warn: ["Warning", "sg-cb-pill-warn"],
    fail: ["Fail", "sg-cb-pill-bad"],
  };
  const [label, cls] = map[status] || [status || "—", "sg-cb-pill-muted"];
  return <span className={`sg-cb-pill ${cls}`}>{label}</span>;
}

/** The build's fingerprint: circles = LB, red = CP, squares = workers;
    filled when that node has joined (or the build completed). */
function ShapeGlyph({ shape = [], buildStatus }) {
  if (!shape.length) return null;
  const done = new Set(["joined", "ready"]);
  let prevRole = null;
  return (
    <div className="sg-cb-shape" aria-hidden="true">
      {shape.map((node, index) => {
        const sep = prevRole && prevRole !== node.role;
        prevRole = node.role;
        const filled = buildStatus === "completed" || done.has(node.status);
        const failed = node.status === "failed";
        const cls = [
          "sg-cb-shape-i",
          node.role === "loadbalancer" ? "is-lb" : "",
          node.role === "control_plane" ? "is-cp" : "",
          filled ? "is-done" : "",
          failed ? "is-fail" : "",
        ].filter(Boolean).join(" ");
        return (
          <span key={index} className="sg-cb-shape-wrap">
            {sep ? <span className="sg-cb-shape-sep">·</span> : null}
            <i className={cls} />
          </span>
        );
      })}
    </div>
  );
}

function Field({ label, children, hint, error }) {
  return (
    <label className="sg-cb-field">
      <span className="sg-cb-field-label">{label}</span>
      {children}
      {error
        ? <span className="sg-cb-field-error">{error}</span>
        : hint ? <span className="sg-cb-field-hint">{hint}</span> : null}
    </label>
  );
}

function WizardSteps({ current }) {
  const labels = ["Basics", "Nodes", "Preflight", "Build"];
  return (
    <div className="sg-cb-steps" aria-label="Wizard steps">
      {labels.map((label, index) => (
        <span key={label} className="sg-cb-steps-item">
          {index > 0 ? <span className="sg-cb-steps-arrow">→</span> : null}
          <span className={`sg-cb-step ${index === current ? "is-active" : ""} ${index < current ? "is-done" : ""}`}>
            <i>{index < current ? "✓" : index + 1}</i>{label}
          </span>
        </span>
      ))}
    </div>
  );
}

function AddonSelector({ catalog = [], value = [], onChange }) {
  const selectedById = new Map(value.map((addon) => [addon.id, addon]));

  const toggle = (addon, checked) => {
    if (!checked) {
      onChange(value.filter((item) => item.id !== addon.id));
      return;
    }
    const version = addon.defaultVersion || addon.versions?.[0] || "";
    onChange([...value.filter((item) => item.id !== addon.id), { id: addon.id, version }]);
  };

  const setVersion = (id, version) => {
    onChange(value.map((addon) => addon.id === id ? { ...addon, version } : addon));
  };

  return (
    <section className="sg-cb-form-section">
      <div className="sg-cb-form-section-head">
        <div>
          <h4>Plugins &amp; cluster add-ons</h4>
          <p className="muted">Optional, version-pinned components installed after the cluster is registered.</p>
        </div>
        <span className="muted">{value.length ? `${value.length} selected` : "None selected"}</span>
      </div>
      {catalog.length ? (
        <div className="sg-cb-addon-grid">
          {catalog.map((addon) => {
            const selected = selectedById.get(addon.id);
            const versions = addon.versions || [];
            return (
              <div key={addon.id} className={`sg-cb-addon-card ${selected ? "is-selected" : ""}`}>
                <label className="sg-cb-addon-choice">
                  <input
                    type="checkbox"
                    checked={Boolean(selected)}
                    onChange={(e) => toggle(addon, e.target.checked)}
                  />
                  <span>
                    <span className="sg-cb-addon-title">
                      <strong>{addon.displayName || addonDisplayName(addon)}</strong>
                      {addon.supportTier ? <span className="sg-cb-addon-tier">{addon.supportTier}</span> : null}
                    </span>
                    {addon.description ? <span className="muted sg-cb-addon-description">{addon.description}</span> : null}
                  </span>
                </label>
                {selected && versions.length ? (
                  <label className="sg-cb-addon-version">
                    <span>Version</span>
                    <select value={selected.version} onChange={(e) => setVersion(addon.id, e.target.value)}>
                      {versions.map((version) => <option key={version} value={version}>v{version}</option>)}
                    </select>
                  </label>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : (
        <p className="muted sg-cb-form-section-empty">No optional add-ons are available for this builder.</p>
      )}
    </section>
  );
}

function WizardConfigurationSummary({ basics, addonCatalog = [], buildProfile }) {
  const endpoint = basics.endpointMode === "managed_haproxy"
    ? `${basics.vipAddress}:6443`
    : basics.controlPlaneEndpoint;
  return (
    <div className="card sg-cb-config-summary">
      <div className="sg-cb-config-item">
        <span className="sg-cb-config-label">Cluster</span>
        <strong>v{basics.k8sVersion} · {basics.topologyType === "stacked_ha" ? "Highly available" : "Single control plane"}</strong>
        <span className="muted">{basics.cniPlugin} · {endpoint}</span>
      </div>
      <div className="sg-cb-config-item">
        <span className="sg-cb-config-label">Add-ons</span>
        <AddonChips addons={basics.addons} catalog={addonCatalog} />
      </div>
      <div className="sg-cb-config-item">
        <span className="sg-cb-config-label">Sources</span>
        <strong>{buildProfile?.name || "Internet defaults"}</strong>
        <span className="muted">
          Images: {buildProfile?.k8sImageRegistry || "upstream registries"}
        </span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Flow strip (builds home)
// ---------------------------------------------------------------------------

function FlowStrip({ builds, infra, canManageInfra }) {
  const inFlight = builds.filter((b) => b.status === "building" || b.status === "preflighting");
  const born = builds.filter((b) => b.status === "completed" && b.resultClusterId);
  const lastBorn = born[0];
  const vcOk = infra.vsphere.some((c) => c.lastConnectionStatus === "ok");
  const segments = [];
  if (canManageInfra) {
    segments.push(
      <div className="sg-cb-fnode" key="vc">
        <div className="sg-cb-fk">vCenter</div>
        <div className="sg-cb-fv">{infra.vsphere.length}<span className="sg-cb-fu"> connection{infra.vsphere.length === 1 ? "" : "s"}</span></div>
        <div className="sg-cb-fs">
          {infra.vsphere.length === 0 ? <span className="muted">none configured</span>
            : vcOk ? <span className="sg-cb-pill sg-cb-pill-ok">Connected</span>
            : <span className="sg-cb-pill sg-cb-pill-muted">Untested</span>}
        </div>
      </div>,
      <div className="sg-cb-fnode" key="ssh">
        <div className="sg-cb-fk">SSH reach</div>
        <div className="sg-cb-fv">{infra.profiles.length}<span className="sg-cb-fu"> profile{infra.profiles.length === 1 ? "" : "s"}</span></div>
        <div className="sg-cb-fs">{infra.credentials.length} credential{infra.credentials.length === 1 ? "" : "s"}</div>
      </div>,
    );
  }
  segments.push(
    <div className="sg-cb-fnode" key="flight">
      <div className="sg-cb-fk">In flight</div>
      <div className="sg-cb-fv">{inFlight.length}<span className="sg-cb-fu"> building</span></div>
      <div className="sg-cb-fs">{inFlight[0] ? inFlight[0].name : "—"}</div>
    </div>,
    <div className="sg-cb-fnode" key="born">
      <div className="sg-cb-fk">Clusters born here</div>
      <div className="sg-cb-fv">{born.length}</div>
      <div className="sg-cb-fs">{lastBorn ? `last: ${lastBorn.name}` : "—"}</div>
    </div>,
  );
  return <div className="card sg-cb-flow">{segments}</div>;
}

// ---------------------------------------------------------------------------
// Infrastructure tab (unchanged behavior; concept card styling)
// ---------------------------------------------------------------------------

function InfraSection({ title, description, items, columns, onDelete, form, testAction }) {
  const [showForm, setShowForm] = useState(false);
  return (
    <div className="card sg-cb-infra-card">
      <div className="sg-cb-infra-head">
        <div>
          <h3>{title}</h3>
          <p className="muted">{description}</p>
        </div>
        <button className="btn-outline" onClick={() => setShowForm((v) => !v)}>
          {showForm ? "Close" : "Add"}
        </button>
      </div>
      {showForm ? form(() => setShowForm(false)) : null}
      {items.length === 0 ? (
        <p className="muted sg-cb-infra-empty">Nothing configured yet.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                {columns.map((c) => <th key={c.key}>{c.label}</th>)}
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  {columns.map((c) => (
                    <td key={c.key}>{c.render ? c.render(item) : item[c.key] ?? "—"}</td>
                  ))}
                  <td className="sg-cb-row-actions">
                    {testAction ? testAction(item) : null}
                    <button className="btn-ghost" onClick={() => onDelete(item)}>Remove</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

const EMPTY_REPO_FORM = {
  name: "",
  repoMode: "internet",
  k8sPkgRepoUrl: "",
  k8sImageRegistry: "",
  cniImageRegistry: "",
  addonImageRegistry: "",
  registryUsername: "",
  registryPassword: "",
  extraCaCertsPem: "",
  httpProxy: "",
  httpsProxy: "",
  noProxy: "",
  offlineBundlePath: "",
};

function InfraTab({ notify, infra, reloadInfra }) {
  const { vsphere, credentials, profiles, buildProfiles } = infra;
  const [vsForm, setVsForm] = useState({ name: "", baseUrl: "", username: "", password: "", skipTlsVerify: false });
  const [credForm, setCredForm] = useState({ name: "", username: "", authMethod: "key", secret: "", sudoMode: "nopasswd", sudoPassword: "", port: 22 });
  const [profForm, setProfForm] = useState({ name: "", credentialId: "", routeMode: "direct", bastionHost: "", bastionCredentialId: "", hostKeyPolicy: "tofu" });
  const [repoForm, setRepoForm] = useState({ ...EMPTY_REPO_FORM });
  const [useImageProxy, setUseImageProxy] = useState(false);
  const [sameImagePrefix, setSameImagePrefix] = useState(true);
  const [testHost, setTestHost] = useState("");

  const submit = async (fn, close) => {
    try {
      await fn();
      await reloadInfra();
      close();
    } catch (err) {
      notify(err.message || String(err), true);
    }
  };

  const imagePrefixError = (value, required = false) => {
    const prefix = String(value || "").trim();
    if (!prefix) return required ? "Enter a registry host or repository prefix." : "";
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(prefix)) return "Use an image prefix without http:// or https://.";
    if (/\s/.test(prefix)) return "Image prefixes cannot contain spaces.";
    return "";
  };

  const k8sImageError = useImageProxy
    ? imagePrefixError(repoForm.k8sImageRegistry, true)
    : "";
  const cniImageError = useImageProxy && !sameImagePrefix
    ? imagePrefixError(repoForm.cniImageRegistry)
    : "";
  const addonImageError = useImageProxy && !sameImagePrefix
    ? imagePrefixError(repoForm.addonImageRegistry)
    : "";
  const configuredRegistryAuthorities = new Set(
    (sameImagePrefix
      ? [repoForm.k8sImageRegistry]
      : [
        repoForm.k8sImageRegistry,
        repoForm.cniImageRegistry,
        repoForm.addonImageRegistry,
      ])
      .map((prefix) => prefix.trim().replace(/\/+$/, "").split("/", 1)[0])
      .filter(Boolean)
  );
  const hasRegistryAuth = Boolean(
    repoForm.registryUsername.trim() || repoForm.registryPassword
  );
  const registryAuthError = useImageProxy &&
    Boolean(repoForm.registryUsername.trim()) !== Boolean(repoForm.registryPassword)
    ? "Provide both username and password, or leave both blank for anonymous access."
    : useImageProxy && hasRegistryAuth && configuredRegistryAuthorities.size !== 1
      ? "One credential pair can only be used with one registry authority."
      : "";
  const forwardProxyError = (value) => {
    const raw = value.trim();
    if (!raw) return "";
    try {
      const parsed = new URL(raw);
      if (!["http:", "https:"].includes(parsed.protocol) || !parsed.hostname) {
        return "Use a valid http:// or https:// proxy URL.";
      }
      if (parsed.username || parsed.password) {
        return "Authenticated forward-proxy URLs are not supported; omit credentials.";
      }
      if ((parsed.pathname && parsed.pathname !== "/") || parsed.search || parsed.hash) {
        return "Use only scheme, host, and an optional port.";
      }
      return "";
    } catch {
      return "Use a valid http:// or https:// proxy URL.";
    }
  };
  const httpProxyError = forwardProxyError(repoForm.httpProxy);
  const httpsProxyError = forwardProxyError(repoForm.httpsProxy);
  const extraCaError = repoForm.extraCaCertsPem.trim() &&
    (!repoForm.extraCaCertsPem.includes("-----BEGIN CERTIFICATE-----") ||
      !repoForm.extraCaCertsPem.includes("-----END CERTIFICATE-----"))
    ? "Paste a PEM-encoded CA certificate."
    : "";
  const repoFormValid = Boolean(
    repoForm.name.trim() &&
    (repoForm.repoMode !== "mirror" || repoForm.k8sPkgRepoUrl.trim()) &&
    (repoForm.repoMode !== "offline" || repoForm.offlineBundlePath.trim()) &&
    !k8sImageError && !cniImageError && !addonImageError &&
    !registryAuthError && !extraCaError &&
    !httpProxyError && !httpsProxyError
  );

  const saveRepoProfile = (close) => {
    const sharedPrefix = repoForm.k8sImageRegistry.trim().replace(/\/+$/, "");
    const payload = {
      ...repoForm,
      name: repoForm.name.trim(),
      k8sPkgRepoUrl: repoForm.k8sPkgRepoUrl.trim(),
      offlineBundlePath: repoForm.offlineBundlePath.trim(),
      k8sImageRegistry: useImageProxy ? sharedPrefix : "",
      cniImageRegistry: useImageProxy
        ? (sameImagePrefix ? sharedPrefix : repoForm.cniImageRegistry.trim().replace(/\/+$/, ""))
        : "",
      addonImageRegistry: useImageProxy
        ? (sameImagePrefix ? sharedPrefix : repoForm.addonImageRegistry.trim().replace(/\/+$/, ""))
        : "",
      registryUsername: useImageProxy ? repoForm.registryUsername.trim() : "",
      registryPassword: useImageProxy ? repoForm.registryPassword : "",
      extraCaCertsPem: repoForm.extraCaCertsPem.trim(),
      httpProxy: repoForm.httpProxy.trim(),
      httpsProxy: repoForm.httpsProxy.trim(),
      noProxy: repoForm.noProxy.trim(),
    };
    submit(
      () => createBuildProfile(payload),
      () => {
        setRepoForm({ ...EMPTY_REPO_FORM });
        setUseImageProxy(false);
        setSameImagePrefix(true);
        close();
      }
    );
  };

  return (
    <div className="sg-cb-infra">
      <InfraSection
        title="vSphere connections"
        description="Read-only vCenter link that powers the VM picker and placement checks. The account only needs the Read-Only role."
        items={vsphere}
        columns={[
          { key: "name", label: "Name" },
          { key: "baseUrl", label: "vCenter" },
          { key: "username", label: "User" },
          {
            key: "lastConnectionStatus", label: "Last test",
            render: (i) => i.lastConnectionStatus ? <StatusPill status={i.lastConnectionStatus === "ok" ? "pass" : "fail"} /> : "—",
          },
        ]}
        onDelete={(item) => submit(() => deleteVSphereConnection(item.id), () => {})}
        testAction={(item) => (
          <button
            className="btn-ghost"
            onClick={async () => {
              try {
                const result = await testVSphereConnection(item.id);
                notify(result.status === "ok" ? `Connected — ${result.vmCount} VMs visible.` : result.error, result.status !== "ok");
                reloadInfra();
              } catch (err) { notify(err.message, true); }
            }}
          >
            Test
          </button>
        )}
        form={(close) => (
          <div className="sg-cb-form-grid">
            <Field label="Name"><input value={vsForm.name} onChange={(e) => setVsForm({ ...vsForm, name: e.target.value })} /></Field>
            <Field label="vCenter URL" hint="https://vcenter.example.com"><input value={vsForm.baseUrl} onChange={(e) => setVsForm({ ...vsForm, baseUrl: e.target.value })} /></Field>
            <Field label="Username"><input value={vsForm.username} onChange={(e) => setVsForm({ ...vsForm, username: e.target.value })} /></Field>
            <Field label="Password"><input type="password" value={vsForm.password} onChange={(e) => setVsForm({ ...vsForm, password: e.target.value })} /></Field>
            <Field label="Skip TLS verification">
              <input type="checkbox" checked={vsForm.skipTlsVerify} onChange={(e) => setVsForm({ ...vsForm, skipTlsVerify: e.target.checked })} />
            </Field>
            <div className="sg-cb-form-actions">
              <button className="primary" onClick={() => submit(() => createVSphereConnection(vsForm), close)}>Save connection</button>
            </div>
          </div>
        )}
      />

      <InfraSection
        title="SSH credentials"
        description="Who KubeSight logs in as. Recommended: a dedicated non-root user with passwordless sudo and key auth. Secrets are encrypted at rest and never shown again."
        items={credentials}
        columns={[
          { key: "name", label: "Name" },
          { key: "username", label: "User" },
          { key: "authMethod", label: "Auth" },
          { key: "sudoMode", label: "Sudo" },
        ]}
        onDelete={(item) => submit(() => deleteSshCredential(item.id), () => {})}
        form={(close) => (
          <div className="sg-cb-form-grid">
            <Field label="Name"><input value={credForm.name} onChange={(e) => setCredForm({ ...credForm, name: e.target.value })} /></Field>
            <Field label="Username"><input value={credForm.username} onChange={(e) => setCredForm({ ...credForm, username: e.target.value })} /></Field>
            <Field label="Auth method">
              <select value={credForm.authMethod} onChange={(e) => setCredForm({ ...credForm, authMethod: e.target.value })}>
                <option value="key">Private key</option>
                <option value="password">Password</option>
              </select>
            </Field>
            <Field label={credForm.authMethod === "key" ? "Private key (PEM)" : "Password"}>
              {credForm.authMethod === "key"
                ? <textarea rows={4} value={credForm.secret} onChange={(e) => setCredForm({ ...credForm, secret: e.target.value })} />
                : <input type="password" value={credForm.secret} onChange={(e) => setCredForm({ ...credForm, secret: e.target.value })} />}
            </Field>
            <Field label="Sudo mode">
              <select value={credForm.sudoMode} onChange={(e) => setCredForm({ ...credForm, sudoMode: e.target.value })}>
                <option value="nopasswd">Passwordless sudo</option>
                <option value="password">Sudo with password</option>
                <option value="root">Root login</option>
              </select>
            </Field>
            {credForm.sudoMode === "password" ? (
              <Field label="Sudo password"><input type="password" value={credForm.sudoPassword} onChange={(e) => setCredForm({ ...credForm, sudoPassword: e.target.value })} /></Field>
            ) : null}
            <Field label="SSH port"><input type="number" value={credForm.port} onChange={(e) => setCredForm({ ...credForm, port: Number(e.target.value) })} /></Field>
            <div className="sg-cb-form-actions">
              <button className="primary" onClick={() => submit(() => createSshCredential(credForm), close)}>Save credential</button>
            </div>
          </div>
        )}
      />

      <InfraSection
        title="SSH connection profiles"
        description="How hosts are reached: credential + direct or bastion route + host-key policy. Production should pin pre-approved fingerprints (strict/pinned)."
        items={profiles}
        columns={[
          { key: "name", label: "Name" },
          { key: "credentialName", label: "Credential" },
          { key: "routeMode", label: "Route", render: (i) => i.routeMode === "bastion" ? `via ${i.bastionHost}` : "direct" },
          { key: "hostKeyPolicy", label: "Host keys" },
        ]}
        onDelete={(item) => submit(() => deleteSshProfile(item.id), () => {})}
        testAction={(item) => (
          <span className="sg-cb-test-inline">
            <input placeholder="host to test" value={testHost} onChange={(e) => setTestHost(e.target.value)} />
            <button
              className="btn-ghost"
              onClick={async () => {
                try {
                  const result = await testSshProfile(item.id, testHost);
                  notify(
                    result.status === "ok"
                      ? `Connected as ${result.effectiveUser} (${result.kernel}, ${result.latencyMs} ms).`
                      : result.error,
                    result.status !== "ok"
                  );
                } catch (err) { notify(err.message, true); }
              }}
            >
              Test
            </button>
          </span>
        )}
        form={(close) => (
          <div className="sg-cb-form-grid">
            <Field label="Name"><input value={profForm.name} onChange={(e) => setProfForm({ ...profForm, name: e.target.value })} /></Field>
            <Field label="Credential">
              <select value={profForm.credentialId} onChange={(e) => setProfForm({ ...profForm, credentialId: e.target.value })}>
                <option value="">Select…</option>
                {credentials.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            </Field>
            <Field label="Route">
              <select value={profForm.routeMode} onChange={(e) => setProfForm({ ...profForm, routeMode: e.target.value })}>
                <option value="direct">Direct</option>
                <option value="bastion">Via bastion / jump host</option>
              </select>
            </Field>
            {profForm.routeMode === "bastion" ? (
              <>
                <Field label="Bastion host"><input value={profForm.bastionHost} onChange={(e) => setProfForm({ ...profForm, bastionHost: e.target.value })} /></Field>
                <Field label="Bastion credential">
                  <select value={profForm.bastionCredentialId} onChange={(e) => setProfForm({ ...profForm, bastionCredentialId: e.target.value })}>
                    <option value="">Select…</option>
                    {credentials.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                  </select>
                </Field>
              </>
            ) : null}
            <Field label="Host-key policy" hint="tofu records on first use; strict/pinned require pre-approved fingerprints">
              <select value={profForm.hostKeyPolicy} onChange={(e) => setProfForm({ ...profForm, hostKeyPolicy: e.target.value })}>
                <option value="tofu">Trust on first use</option>
                <option value="strict">Strict</option>
                <option value="pinned">Pinned (pre-approved only)</option>
              </select>
            </Field>
            <div className="sg-cb-form-actions">
              <button
                className="primary"
                onClick={() => submit(() => createSshProfile({
                  ...profForm,
                  credentialId: Number(profForm.credentialId) || undefined,
                  bastionCredentialId: profForm.bastionCredentialId ? Number(profForm.bastionCredentialId) : undefined,
                }), close)}
              >
                Save profile
              </button>
            </div>
          </div>
        )}
      />

      <InfraSection
        title="Repository profiles"
        description="Choose package and image sources independently, including a private registry proxy or fully offline bundle."
        items={buildProfiles}
        columns={[
          { key: "name", label: "Name" },
          { key: "repoMode", label: "Package source" },
          {
            key: "k8sImageRegistry",
            label: "Image source",
            render: (item) => item.k8sImageRegistry || "Upstream registries",
          },
        ]}
        onDelete={(item) => submit(() => deleteBuildProfile(item.id), () => {})}
        form={(close) => (
          <div className="sg-cb-form-grid">
            <Field label="Name">
              <input
                value={repoForm.name}
                onChange={(e) => setRepoForm({ ...repoForm, name: e.target.value })}
                placeholder="Production sources"
                required
              />
            </Field>
            <Field label="Package source">
              <select value={repoForm.repoMode} onChange={(e) => setRepoForm({ ...repoForm, repoMode: e.target.value })}>
                <option value="internet">Upstream internet repositories</option>
                <option value="mirror">Internal package mirror</option>
                <option value="offline">Offline bundle</option>
              </select>
            </Field>
            {repoForm.repoMode === "mirror" ? (
              <Field label="Kubernetes package repository" hint="{minor} is replaced, for example …/v{minor}/deb/.">
                <input
                  value={repoForm.k8sPkgRepoUrl}
                  onChange={(e) => setRepoForm({ ...repoForm, k8sPkgRepoUrl: e.target.value })}
                  placeholder="https://packages.example/kubernetes/v{minor}/deb/"
                  required
                />
              </Field>
            ) : null}
            {repoForm.repoMode === "offline" ? (
              <Field label="Offline bundle path">
                <input
                  value={repoForm.offlineBundlePath}
                  onChange={(e) => setRepoForm({ ...repoForm, offlineBundlePath: e.target.value })}
                  placeholder="/srv/kubesight/bundles/k8s"
                  required
                />
              </Field>
            ) : null}

            <section className="sg-cb-profile-section">
              <div className="sg-cb-profile-section-head">
                <div>
                  <h4>Image source</h4>
                  <p className="muted">Pull directly from upstream registries or rewrite builder-managed images through a registry proxy.</p>
                </div>
                <label className="sg-cb-toggle-row">
                  <input
                    type="checkbox"
                    checked={useImageProxy}
                    onChange={(e) => setUseImageProxy(e.target.checked)}
                  />
                  Use registry proxy
                </label>
              </div>
              {useImageProxy ? (
                <div className="sg-cb-profile-subgrid">
                  <Field
                    label="Kubernetes image prefix"
                    hint="Registry host or repository prefix, without a URL scheme."
                    error={k8sImageError}
                  >
                    <input
                      value={repoForm.k8sImageRegistry}
                      onChange={(e) => setRepoForm({ ...repoForm, k8sImageRegistry: e.target.value })}
                      placeholder="nexus.example:5000/kubernetes"
                      aria-invalid={Boolean(k8sImageError)}
                      required
                    />
                  </Field>
                  <label className="sg-cb-inline-check sg-cb-span-all">
                    <input
                      type="checkbox"
                      checked={sameImagePrefix}
                      onChange={(e) => setSameImagePrefix(e.target.checked)}
                    />
                    Use the same prefix for CNI and add-on images
                  </label>
                  <Field
                    label="CNI image prefix"
                    hint={sameImagePrefix
                      ? "Using the Kubernetes image prefix."
                      : "Optional; leave blank to keep the manifest's upstream registry."}
                    error={cniImageError}
                  >
                    <input
                      value={sameImagePrefix ? repoForm.k8sImageRegistry : repoForm.cniImageRegistry}
                      onChange={(e) => setRepoForm({ ...repoForm, cniImageRegistry: e.target.value })}
                      placeholder="nexus.example:5000/networking"
                      aria-invalid={Boolean(cniImageError)}
                      disabled={sameImagePrefix}
                    />
                  </Field>
                  <Field
                    label="Add-on image prefix"
                    hint={sameImagePrefix
                      ? "Using the Kubernetes image prefix."
                      : "Optional; used by selected cluster add-ons."}
                    error={addonImageError}
                  >
                    <input
                      value={sameImagePrefix ? repoForm.k8sImageRegistry : repoForm.addonImageRegistry}
                      onChange={(e) => setRepoForm({ ...repoForm, addonImageRegistry: e.target.value })}
                      placeholder="nexus.example:5000/addons"
                      aria-invalid={Boolean(addonImageError)}
                      disabled={sameImagePrefix}
                    />
                  </Field>
                  <Field label="Registry username (optional)" error={registryAuthError}>
                    <input
                      value={repoForm.registryUsername}
                      onChange={(e) => setRepoForm({ ...repoForm, registryUsername: e.target.value })}
                      autoComplete="off"
                      aria-invalid={Boolean(registryAuthError)}
                    />
                  </Field>
                  <Field label="Registry password (optional)">
                    <input
                      type="password"
                      value={repoForm.registryPassword}
                      onChange={(e) => setRepoForm({ ...repoForm, registryPassword: e.target.value })}
                      autoComplete="new-password"
                      aria-invalid={Boolean(registryAuthError)}
                    />
                  </Field>
                </div>
              ) : (
                <p className="muted sg-cb-profile-note">Images will use their standard upstream registries.</p>
              )}
            </section>

            <section className="sg-cb-profile-section">
              <div className="sg-cb-profile-section-head">
                <div>
                  <h4>Outbound network proxy</h4>
                  <p className="muted">Optional HTTP(S) egress settings for package and manifest downloads. This is separate from the image registry proxy.</p>
                </div>
              </div>
              <div className="sg-cb-profile-subgrid">
                <Field label="HTTP proxy (optional)" error={httpProxyError}>
                  <input
                    type="url"
                    value={repoForm.httpProxy}
                    onChange={(e) => setRepoForm({ ...repoForm, httpProxy: e.target.value })}
                    placeholder="http://proxy.example:3128"
                    aria-invalid={Boolean(httpProxyError)}
                  />
                </Field>
                <Field label="HTTPS proxy (optional)" error={httpsProxyError}>
                  <input
                    type="url"
                    value={repoForm.httpsProxy}
                    onChange={(e) => setRepoForm({ ...repoForm, httpsProxy: e.target.value })}
                    placeholder="http://proxy.example:3128"
                    aria-invalid={Boolean(httpsProxyError)}
                  />
                </Field>
                <Field label="NO_PROXY (optional)" hint="Comma-separated hosts, domains, or CIDRs that should bypass the outbound proxy.">
                  <input
                    value={repoForm.noProxy}
                    onChange={(e) => setRepoForm({ ...repoForm, noProxy: e.target.value })}
                    placeholder=".cluster.local,10.0.0.0/8"
                  />
                </Field>
                <Field
                  label="Trusted CA certificates (optional)"
                  hint="Trust an internal registry, package mirror, or TLS-inspecting outbound proxy."
                  error={extraCaError}
                >
                  <textarea
                    rows={4}
                    value={repoForm.extraCaCertsPem}
                    onChange={(e) => setRepoForm({ ...repoForm, extraCaCertsPem: e.target.value })}
                    placeholder={"-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----"}
                    aria-invalid={Boolean(extraCaError)}
                  />
                </Field>
              </div>
            </section>

            <div className="sg-cb-form-actions">
              <button className="primary" disabled={!repoFormValid} onClick={() => saveRepoProfile(close)}>Save profile</button>
            </div>
          </div>
        )}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Wizard
// ---------------------------------------------------------------------------

const EMPTY_BASICS = {
  name: "",
  k8sVersion: "",
  topologyType: "stacked_ha",
  endpointMode: "managed_haproxy",
  vipAddress: "",
  controlPlaneEndpoint: "",
  cniPlugin: "calico",
  podCidr: "10.244.0.0/16",
  serviceCidr: "10.96.0.0/12",
  addons: [],
  vsphereConnectionId: "",
  buildProfileId: "",
  connectionProfileId: "",
};

function topologyTargets(basics) {
  if (basics.topologyType === "single_cp") {
    return {
      control_plane: 1,
      loadbalancer: basics.endpointMode === "managed_haproxy" ? 1 : 0,
    };
  }
  return { control_plane: 3, loadbalancer: basics.endpointMode === "managed_haproxy" ? 2 : 0 };
}

/** Composer slot list per tier: picked VMs (with host) + manual nodes. */
function composerTiers({ picked, manualNodes, vms, targets }) {
  const vmByMoid = Object.fromEntries(vms.map((vm) => [vm.moid, vm]));
  const tiers = { loadbalancer: [], control_plane: [], worker: [] };
  Object.entries(picked).forEach(([moid, p]) => {
    const vm = vmByMoid[moid];
    tiers[p.role]?.push({
      key: `vm-${moid}`,
      name: vm?.name || moid,
      host: vm?.esxiHost || null,
    });
  });
  manualNodes.forEach((node, index) => {
    if (node.role && (node.address || node.hostname)) {
      tiers[node.role]?.push({
        key: `manual-${index}`,
        name: node.hostname || node.address,
        host: null,
      });
    }
  });
  // Anti-affinity ties: >1 node of an HA tier on the same ESXi host.
  ["loadbalancer", "control_plane"].forEach((role) => {
    const byHost = {};
    tiers[role].forEach((slot) => {
      if (slot.host) (byHost[slot.host] ||= []).push(slot);
    });
    Object.values(byHost).forEach((list) => {
      if (list.length > 1) list.forEach((slot) => { slot.tie = true; });
    });
  });
  const conflictHosts = [...new Set(
    [...tiers.loadbalancer, ...tiers.control_plane]
      .filter((slot) => slot.tie).map((slot) => slot.host)
  )];
  return { tiers, targets, conflictHosts };
}

function Composer({ basics, tiersInfo, countsOk, busy, onPreflight, nodesCount }) {
  const { tiers, targets, conflictHosts } = tiersInfo;
  const tierRow = (role, label, need) => {
    const have = tiers[role].length;
    const full = need ? have === need : have > 0;
    const slots = [...tiers[role]];
    const blanks = Math.max((need || (have ? 0 : 1)) - have, 0);
    return (
      <div className="sg-cb-tier" key={role}>
        <div className="sg-cb-tier-label">
          <span>{label}</span>
          <span className={`sg-cb-tier-n ${full ? "is-full" : ""}`}>{need ? `${have} / ${need}` : have}</span>
        </div>
        <div className="sg-cb-slots">
          {slots.map((slot) => (
            <div key={slot.key} className={`sg-cb-slot is-filled ${role === "control_plane" ? "is-cp" : ""} ${role === "loadbalancer" ? "is-lb" : ""} ${slot.tie ? "is-tie" : ""}`}>
              <span className="sg-cb-slot-nm">{slot.name}</span>
              <span className="sg-cb-slot-host">{slot.host || "manual"}</span>
            </div>
          ))}
          {Array.from({ length: blanks }).map((_, i) => (
            <div key={`e${i}`} className="sg-cb-slot is-empty">assign a VM</div>
          ))}
        </div>
      </div>
    );
  };

  return (
    <div className="card sg-cb-composer" aria-live="polite">
      <h3>Your cluster, taking shape</h3>
      <p className="sg-cb-composer-sub">
        {basics.topologyType === "stacked_ha" ? "Highly available" : "Single control plane"} · fills as you assign roles
      </p>
      {basics.endpointMode === "managed_haproxy" && basics.vipAddress ? (
        <div className="sg-cb-vipline">
          <span className="sg-cb-vipdot" />
          <span>VIP <b className="sg-cb-mono">{basics.vipAddress}</b>:6443</span>
          <span className="sg-cb-vipwire" />
        </div>
      ) : null}
      {targets.loadbalancer ? tierRow("loadbalancer", "Load balancers", targets.loadbalancer) : null}
      {tierRow("control_plane", "Control planes", targets.control_plane)}
      {tierRow("worker", "Workers", 0)}
      {conflictHosts.length ? (
        <div className="sg-cb-affnote is-bad">
          ⚠ Multiple HA nodes share {conflictHosts.map((h) => <b key={h} className="sg-cb-mono"> {h}</b>)} — one
          host failure would take the tier down. Move a VM (this becomes a hard preflight failure).
        </div>
      ) : countsOk ? (
        <div className="sg-cb-affnote is-good">✓ Placement is clean — every HA tier spans distinct ESXi hosts.</div>
      ) : null}
      <button className="primary sg-cb-composer-cta" disabled={!countsOk || busy || nodesCount === 0} onClick={onPreflight}>
        {busy ? "Running preflight…" : "Run preflight"}
      </button>
    </div>
  );
}

function WizardTab({ options, infra, notify, onBuildLaunched }) {
  const [step, setStep] = useState(0);
  const [basics, setBasics] = useState({ ...EMPTY_BASICS });
  const [vms, setVms] = useState([]);
  const [vmsLoading, setVmsLoading] = useState(false);
  const [vmSearch, setVmSearch] = useState("");
  const [picked, setPicked] = useState({});
  const [manualNodes, setManualNodes] = useState([]);
  const [buildId, setBuildId] = useState(null);
  const [preflightResult, setPreflightResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [acked, setAcked] = useState(false);

  useEffect(() => {
    if (options && !basics.k8sVersion) {
      setBasics((b) => ({ ...b, k8sVersion: options.k8sVersions[0] }));
    }
  }, [options, basics.k8sVersion]);

  useEffect(() => {
    let ignore = false;
    if (!basics.vsphereConnectionId) { setVms([]); return undefined; }
    setVmsLoading(true);
    listVSphereVms(basics.vsphereConnectionId)
      .then((data) => { if (!ignore) setVms(data.items || []); })
      .catch((err) => notify(`vCenter inventory failed: ${err.message}`, true))
      .finally(() => { if (!ignore) setVmsLoading(false); });
    return () => { ignore = true; };
  }, [basics.vsphereConnectionId, notify]);

  const targets = topologyTargets(basics);
  const counts = useMemo(() => {
    const all = { control_plane: 0, worker: 0, loadbalancer: 0 };
    Object.values(picked).forEach((p) => { all[p.role] += 1; });
    manualNodes.forEach((n) => { if (n.role) all[n.role] += 1; });
    return all;
  }, [picked, manualNodes]);

  const nodesPayload = useMemo(() => {
    const fromVms = Object.entries(picked).map(([moid, p]) => ({
      role: p.role,
      vsphereVmMoid: moid,
      address: p.address || undefined,
      hostname: p.hostname || undefined,
    }));
    const manual = manualNodes
      .filter((n) => n.address && n.role)
      .map((n) => ({ role: n.role, hostname: n.hostname, address: n.address }));
    return [...fromVms, ...manual];
  }, [picked, manualNodes]);

  const countsOk =
    counts.control_plane === targets.control_plane &&
    counts.loadbalancer === targets.loadbalancer;

  const tiersInfo = useMemo(
    () => composerTiers({ picked, manualNodes, vms, targets }),
    [picked, manualNodes, vms, targets]
  );

  const filteredVms = useMemo(() => {
    const query = vmSearch.trim().toLowerCase();
    if (!query) return vms;
    return vms.filter((vm) =>
      [vm.name, vm.guestHostname, vm.guestIp, vm.esxiHost]
        .filter(Boolean).some((v) => String(v).toLowerCase().includes(query))
    );
  }, [vms, vmSearch]);

  const runPreflight = async () => {
    setBusy(true);
    setAcked(false);
    try {
      let id = buildId;
      const payload = {
        ...basics,
        vsphereConnectionId: basics.vsphereConnectionId || undefined,
        buildProfileId: basics.buildProfileId || undefined,
        connectionProfileId: basics.connectionProfileId || undefined,
        nodes: nodesPayload,
      };
      if (id) {
        await updateClusterBuild(id, payload);
      } else {
        const created = await createClusterBuild(payload);
        id = created.id;
        setBuildId(id);
      }
      const result = await preflightClusterBuild(id);
      setPreflightResult(result);
      setStep(2);
    } catch (err) {
      notify(err.message || String(err), true);
    } finally {
      setBusy(false);
    }
  };

  const launch = async () => {
    setBusy(true);
    try {
      const hasWarnings = preflightResult?.status === "warn";
      await startClusterBuild(buildId, hasWarnings ? { ackWarnings: ["Acknowledged in wizard"] } : {});
      onBuildLaunched(buildId);
    } catch (err) {
      notify(err.message || String(err), true);
    } finally {
      setBusy(false);
    }
  };

  // Preflight summary numbers for the verdict ring.
  const verdict = useMemo(() => {
    if (!preflightResult) return null;
    let pass = 0, warn = 0, fail = 0;
    preflightResult.nodes.forEach((node) => node.checks.forEach((check) => {
      if (check.status === "pass") pass += 1;
      else if (check.status === "warn") warn += 1;
      else fail += 1;
    }));
    const total = pass + warn + fail;
    const failing = preflightResult.nodes.filter((n) => n.status === "fail");
    const warning = preflightResult.nodes.filter((n) => n.status === "warn");
    const passing = preflightResult.nodes.filter((n) => n.status === "pass");
    return { pass, warn, fail, total, failing, warning, passing };
  }, [preflightResult]);

  const ringStyle = verdict && verdict.total ? {
    background: `conic-gradient(var(--ok) 0 ${(verdict.pass / verdict.total) * 100}%, var(--warn) ${(verdict.pass / verdict.total) * 100}% ${((verdict.pass + verdict.warn) / verdict.total) * 100}%, var(--danger) ${((verdict.pass + verdict.warn) / verdict.total) * 100}% 100%)`,
  } : undefined;

  const renderPfNode = (node, open) => (
    <details key={node.nodeId} className="sg-cb-preflight-node" open={open}>
      <summary>
        <StatusPill status={node.status} />
        <strong>{node.hostname || node.address}</strong>
        <span className="muted">{ROLE_LABELS[node.role]} · {node.address}</span>
        <span className="sg-cb-chev">›</span>
      </summary>
      <div className="sg-cb-checks">
        {node.checks.filter((c) => open ? true : c.status !== "pass").map((check, index) => (
          <div key={`${check.id}-${index}`} className={`sg-cb-check is-${check.status}`}>
            <span className="sg-cb-check-status"><StatusPill status={check.status} /></span>
            <div>
              <div className="sg-cb-check-label">{check.label}</div>
              {check.detail ? <div className="muted">{check.detail}</div> : null}
              {check.hint && check.status !== "pass" ? (
                <div className={`sg-cb-fix ${check.status === "fail" ? "is-crit" : ""}`}>Fix: {check.hint}</div>
              ) : null}
            </div>
          </div>
        ))}
        {open ? null : (
          <div className="sg-cb-check">
            <span className="sg-cb-check-status"><StatusPill status="pass" /></span>
            <div className="muted">{node.checks.filter((c) => c.status === "pass").length} checks passed — collapsed.</div>
          </div>
        )}
      </div>
    </details>
  );

  const buildEnabled = preflightResult && preflightResult.status !== "fail" &&
    (preflightResult.status === "pass" || acked) && !busy;
  const selectedBuildProfile = infra.buildProfiles.find(
    (profile) => String(profile.id) === String(basics.buildProfileId)
  );

  return (
    <div className="sg-cb-wizard">
      <WizardSteps current={step} />

      {step === 0 && options ? (
        <div className="card sg-cb-card">
          <div className="sg-cb-form-grid sg-cb-basics">
            <Field label="Cluster name"><input value={basics.name} onChange={(e) => setBasics({ ...basics, name: e.target.value })} /></Field>
            <Field label="Kubernetes version">
              <select value={basics.k8sVersion} onChange={(e) => setBasics({ ...basics, k8sVersion: e.target.value })}>
                {options.k8sVersions.map((v) => <option key={v} value={v}>v{v}</option>)}
              </select>
            </Field>
            <Field label="Topology">
              <select value={basics.topologyType} onChange={(e) => setBasics({ ...basics, topologyType: e.target.value })}>
                {options.topologies.map((t) => <option key={t.id} value={t.id}>{t.label} — {t.shape}</option>)}
              </select>
            </Field>
            <Field label="API endpoint mode">
              <select
                value={basics.endpointMode}
                onChange={(e) => setBasics({ ...basics, endpointMode: e.target.value })}
              >
                {options.endpointModes.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
              </select>
            </Field>
            {basics.endpointMode === "managed_haproxy" ? (
              <Field
                label="VIP address"
                hint={
                  basics.topologyType === "single_cp"
                    ? "An unused IP on the same L2 network. KubeSight assigns it to the single managed LB; this lab shape has no LB failover."
                    : "An unused IP on the control-plane L2 segment. Keepalived floats it between the two LB VMs."
                }
              >
                <input value={basics.vipAddress} onChange={(e) => setBasics({ ...basics, vipAddress: e.target.value })} placeholder="10.0.0.100" />
              </Field>
            ) : (
              <Field label="Control-plane endpoint" hint="host:port. A stable endpoint is required even for single control plane — it keeps the HA migration path open.">
                <input value={basics.controlPlaneEndpoint} onChange={(e) => setBasics({ ...basics, controlPlaneEndpoint: e.target.value })} placeholder="k8s-api.example.com:6443" />
              </Field>
            )}
            <Field label="CNI">
              <select value={basics.cniPlugin} onChange={(e) => setBasics({ ...basics, cniPlugin: e.target.value })}>
                {options.cniPlugins.map((p) => (
                  <option key={p.id} value={p.id}>{p.displayName} ({p.supportTier})</option>
                ))}
              </select>
            </Field>
            <Field label="Pod CIDR"><input value={basics.podCidr} onChange={(e) => setBasics({ ...basics, podCidr: e.target.value })} /></Field>
            <Field label="Service CIDR"><input value={basics.serviceCidr} onChange={(e) => setBasics({ ...basics, serviceCidr: e.target.value })} /></Field>
            <AddonSelector
              catalog={options.addons || []}
              value={basics.addons}
              onChange={(addons) => setBasics({ ...basics, addons })}
            />
            <Field label="vSphere connection" hint="Optional — enables the VM picker">
              <select value={basics.vsphereConnectionId} onChange={(e) => setBasics({ ...basics, vsphereConnectionId: e.target.value })}>
                <option value="">None (manual hosts)</option>
                {infra.vsphere.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            </Field>
            <Field label="SSH connection profile">
              <select value={basics.connectionProfileId} onChange={(e) => setBasics({ ...basics, connectionProfileId: e.target.value })}>
                <option value="">Select…</option>
                {infra.profiles.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </Field>
            <Field label="Repository profile">
              <select value={basics.buildProfileId} onChange={(e) => setBasics({ ...basics, buildProfileId: e.target.value })}>
                <option value="">Internet defaults (dev/test)</option>
                {infra.buildProfiles.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.repoMode})</option>)}
              </select>
            </Field>
          </div>
          <div className="sg-cb-form-actions">
            <button
              className="primary"
              disabled={!basics.name || !basics.connectionProfileId ||
                (basics.endpointMode === "managed_haproxy" ? !basics.vipAddress : !basics.controlPlaneEndpoint)}
              onClick={() => setStep(1)}
            >
              Next: pick nodes
            </button>
          </div>
        </div>
      ) : null}

      {step === 1 ? (
        <div className="sg-cb-pick-layout">
          <div className="card sg-cb-card">
            {basics.vsphereConnectionId ? (
              <>
                <div className="sg-cb-vm-toolbar">
                  <input
                    placeholder={`Search ${vms.length} VMs — name, IP, ESXi host…`}
                    value={vmSearch}
                    onChange={(e) => setVmSearch(e.target.value)}
                  />
                  {vmsLoading ? <span className="muted">Loading inventory…</span> : <span className="muted">{vms.length} VMs</span>}
                </div>
                <div className="table-wrap sg-cb-vm-table">
                  <table>
                    <thead>
                      <tr>
                        <th>Role</th><th>VM</th><th>IP</th><th>vCPU</th><th>Mem</th>
                        <th>Guest OS</th><th>Tools</th><th>ESXi host</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredVms.map((vm) => {
                        const selection = picked[vm.moid];
                        const toolsOk = vm.toolsRunState === "RUNNING";
                        const tie = tiersInfo.conflictHosts.includes(vm.esxiHost) && selection &&
                          (selection.role === "control_plane" || selection.role === "loadbalancer");
                        return (
                          <tr key={vm.moid} className={selection ? "is-picked" : ""}>
                            <td>
                              <select
                                value={selection?.role || ""}
                                aria-label={`Role for ${vm.name}`}
                                onChange={(e) => {
                                  const role = e.target.value;
                                  setPicked((prev) => {
                                    const next = { ...prev };
                                    if (!role) delete next[vm.moid];
                                    else next[vm.moid] = { ...(next[vm.moid] || {}), role };
                                    return next;
                                  });
                                }}
                              >
                                <option value="">—</option>
                                {Object.entries(ROLE_LABELS).map(([role, label]) => (
                                  <option key={role} value={role}>{label}</option>
                                ))}
                              </select>
                            </td>
                            <td className="sg-cb-vm-name">{vm.name}
                              {vm.powerState !== "POWERED_ON" ? <span className="sg-cb-warn-text"> · off</span> : null}
                            </td>
                            <td>
                              {toolsOk && vm.guestIp ? <span className="sg-cb-mono">{vm.guestIp}</span> : (
                                selection ? (
                                  <input
                                    className="sg-cb-ip-override"
                                    placeholder="management IP"
                                    value={selection.address || ""}
                                    onChange={(e) => setPicked((prev) => ({
                                      ...prev, [vm.moid]: { ...prev[vm.moid], address: e.target.value },
                                    }))}
                                  />
                                ) : <span className="muted">no Tools IP</span>
                              )}
                            </td>
                            <td>{vm.cpuCount ?? "—"}</td>
                            <td>{vm.memoryMiB ? `${Math.round(vm.memoryMiB / 1024)} GiB` : "—"}</td>
                            <td className="sg-cb-ellipsis">{vm.guestOs || "—"}</td>
                            <td>{toolsOk ? "Running" : <span className="sg-cb-warn-text">None</span>}</td>
                            <td><span className={`sg-cb-hostchip ${tie ? "is-conflict" : ""}`}>{vm.esxiHost || "—"}</span></td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                <p className="muted sg-cb-tools-note">
                  VMs without VMware Tools stay pickable — the IP cell becomes an input. Tools is recommended, never required.
                </p>
              </>
            ) : null}

            <div className="sg-cb-manual">
              <h4>Manual hosts {basics.vsphereConnectionId ? "(non-vCenter nodes)" : ""}</h4>
              {manualNodes.map((node, index) => (
                <div key={index} className="sg-cb-manual-row">
                  <select value={node.role} onChange={(e) => setManualNodes((prev) => prev.map((n, i) => i === index ? { ...n, role: e.target.value } : n))}>
                    <option value="">Role…</option>
                    {Object.entries(ROLE_LABELS).map(([role, label]) => <option key={role} value={role}>{label}</option>)}
                  </select>
                  <input placeholder="hostname" value={node.hostname} onChange={(e) => setManualNodes((prev) => prev.map((n, i) => i === index ? { ...n, hostname: e.target.value } : n))} />
                  <input placeholder="IP address" value={node.address} onChange={(e) => setManualNodes((prev) => prev.map((n, i) => i === index ? { ...n, address: e.target.value } : n))} />
                  <button className="btn-ghost" onClick={() => setManualNodes((prev) => prev.filter((_, i) => i !== index))}>Remove</button>
                </div>
              ))}
              <button className="btn-outline" onClick={() => setManualNodes((prev) => [...prev, { role: "", hostname: "", address: "" }])}>
                Add manual host
              </button>
            </div>

            <div className="sg-cb-form-actions">
              <button className="btn-outline" onClick={() => setStep(0)}>Back</button>
            </div>
          </div>

          <Composer
            basics={basics}
            tiersInfo={tiersInfo}
            countsOk={countsOk}
            busy={busy}
            onPreflight={runPreflight}
            nodesCount={nodesPayload.length}
          />
        </div>
      ) : null}

      {step === 2 && preflightResult && verdict ? (
        <div className="sg-cb-vstack">
          <WizardConfigurationSummary
            basics={basics}
            addonCatalog={options.addons || []}
            buildProfile={selectedBuildProfile}
          />
          <div className="card sg-cb-verdict">
            <div className="sg-cb-ring" style={ringStyle} role="img"
                 aria-label={`${verdict.pass} of ${verdict.total} checks pass`}>
              <b>{verdict.total}</b>
            </div>
            <div>
              <h3>
                {verdict.fail
                  ? `${verdict.pass} of ${verdict.total} checks pass — ${verdict.fail} must be fixed`
                  : verdict.warn
                    ? `${verdict.pass} of ${verdict.total} checks pass — ${verdict.warn} warning${verdict.warn === 1 ? "" : "s"}`
                    : `All ${verdict.total} checks pass`}
              </h3>
              <p className="muted">
                {verdict.fail
                  ? "Fix the failures and re-run — failures are never acknowledgeable."
                  : verdict.warn
                    ? "Warnings can be acknowledged below; the acknowledgement is recorded."
                    : "Ready to build."}
              </p>
            </div>
            <button className="btn-outline sg-cb-rerun" disabled={busy} onClick={runPreflight}>Re-run preflight</button>
          </div>

          {(preflightResult.topologyWarnings || []).map((warning) => (
            <p key={warning} className="sg-cb-topology-warn">⚠ {warning}</p>
          ))}

          {verdict.failing.map((node) => renderPfNode(node, true))}
          {verdict.warning.map((node) => renderPfNode(node, true))}
          {verdict.passing.length ? (
            <details className="sg-cb-preflight-node">
              <summary>
                <StatusPill status="pass" />
                <strong>{verdict.passing.length} machine{verdict.passing.length === 1 ? "" : "s"}</strong>
                <span className="muted">every check green — grouped so the page stays about what needs attention</span>
                <span className="sg-cb-chev">›</span>
              </summary>
              <div className="sg-cb-checks">
                {verdict.passing.map((node) => (
                  <div key={node.nodeId} className="sg-cb-check">
                    <span className="sg-cb-check-status"><StatusPill status="pass" /></span>
                    <div>
                      <div className="sg-cb-check-label">{node.hostname || node.address}</div>
                      <div className="muted">{ROLE_LABELS[node.role]} · {node.address} · {node.checks.length} checks</div>
                    </div>
                  </div>
                ))}
              </div>
            </details>
          ) : null}

          {preflightResult.status === "warn" ? (
            <div className="sg-cb-ackbar">
              <label>
                <input type="checkbox" checked={acked} onChange={(e) => setAcked(e.target.checked)} />
                I understand the {verdict.warn} warning{verdict.warn === 1 ? "" : "s"} and want to proceed.
              </label>
              <button className="primary" disabled={!buildEnabled} onClick={launch}>Build cluster</button>
            </div>
          ) : (
            <div className="sg-cb-form-actions">
              <button className="btn-outline" onClick={() => setStep(1)}>Back to nodes</button>
              <button className="primary" disabled={!buildEnabled} onClick={launch}>Build cluster</button>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Build detail: phase rail + node lanes + log well + handoff receipt
// ---------------------------------------------------------------------------

function buildDuration(build) {
  if (!build.startedAt || !build.finishedAt) return null;
  const ms = parseApiTime(build.finishedAt) - parseApiTime(build.startedAt);
  if (!(ms > 0)) return null;
  const min = Math.round(ms / 60000);
  return min < 60 ? `${min} min` : `${Math.floor(min / 60)} h ${min % 60} min`;
}

/** Phases this build will actually run (steps are created lazily, so the
    progress denominator comes from the build's shape, not from the steps). */
function expectedPhases(build) {
  return PHASE_ORDER.filter((phase) => {
    if (phase === "loadbalancer") return build.endpointMode === "managed_haproxy";
    if (phase === "join_cp") return (build.nodeCounts?.controlPlane || 0) > 1;
    if (phase === "join_workers") return (build.nodeCounts?.worker || 0) > 0;
    if (phase === "addons") return (build.addons || []).length > 0;
    return true;
  });
}

function formatElapsed(ms) {
  if (!(ms > 0)) return "0 s";
  const totalSeconds = Math.floor(ms / 1000);
  if (totalSeconds < 60) return `${totalSeconds} s`;
  const min = Math.floor(totalSeconds / 60);
  if (min < 60) return `${min} min ${totalSeconds % 60} s`;
  return `${Math.floor(min / 60)} h ${min % 60} min`;
}

function BuildProgress({ build, now }) {
  const plan = expectedPhases(build);
  const steps = build.steps || [];
  const phaseSteps = (phase) => steps.filter((s) => s.phase === phase);
  const donePhases = plan.filter((phase) => {
    const list = phaseSteps(phase);
    return list.length > 0 &&
      list.every((s) => s.status === "completed" || s.status === "skipped");
  });
  const current = [...plan].reverse().find((phase) => phaseSteps(phase).length > 0);
  const currentDone = current ? donePhases.includes(current) : false;
  const label = current && !currentDone
    ? (PHASE_LABELS[current] || current)
    : current ? "Next phase starting…" : "Starting…";
  const position = current ? plan.indexOf(current) + 1 : 0;
  const pct = plan.length ? Math.round((donePhases.length / plan.length) * 100) : 0;
  const elapsed = build.startedAt ? now - parseApiTime(build.startedAt) : null;
  return (
    <div className="card sg-cb-progresscard" aria-live="polite">
      <div className="sg-cb-progress-row">
        <span className="sg-cb-livebadge"><i />Live</span>
        <strong className="sg-cb-progress-phase">{label}</strong>
        {position ? <span className="muted">phase {position} of {plan.length}</span> : null}
        {current && !currentDone && PHASE_NOTES[current]
          ? <span className="muted sg-cb-progress-note">{PHASE_NOTES[current]}</span>
          : null}
        {elapsed !== null
          ? <span className="sg-cb-mono sg-cb-progress-elapsed">{formatElapsed(elapsed)}</span>
          : null}
      </div>
      <div
        className="sg-cb-progressbar"
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${donePhases.length} of ${plan.length} phases complete`}
      >
        <i style={{ width: `${Math.max(pct, 2)}%` }} />
      </div>
    </div>
  );
}

const LOG_REFRESH_MS = 2500;
const DETAIL_POLL_INTERVAL_MS = 2500;

function BuildDetail({
  buildId,
  canExecute,
  notify,
  onBack,
  onDeleted,
  addonCatalog = [],
  buildProfiles = [],
}) {
  const [build, setBuild] = useState(null);
  const [logs, setLogs] = useState(null);
  // The step we're viewing: {id, nodeId, phase}.
  const [logStep, setLogStep] = useState(null);
  const timer = useRef(null);
  // Follow mode: while the build runs, the log panel tracks the first running
  // step until someone chooses a lane. A manual lane click always pins that
  // exact step; this matters for parallel phases where several nodes are
  // running and the generic live follower would otherwise jump back to the
  // first node on the next detail poll.
  const follow = useRef(true);
  const prevViewedStatus = useRef(null);
  const logEl = useRef(null);
  const stickToBottom = useRef(true);
  // Ticks once a second purely so the elapsed clock advances between polls.
  const [now, setNow] = useState(() => Date.now());

  const load = useCallback(async () => {
    try {
      const data = await getClusterBuild(buildId);
      setBuild(data);
      return data;
    } catch (err) {
      notify(err.message, true);
      return null;
    }
  }, [buildId, notify]);

  useEffect(() => {
    let stopped = false;
    const tick = async () => {
      const data = await load();
      if (stopped) return;
      if (data && (data.status === "building" || data.status === "preflighting")) {
        // Faster cadence than the list view: this is the screen someone watches
        // while the build runs, and phase hand-offs should feel immediate.
        timer.current = setTimeout(tick, DETAIL_POLL_INTERVAL_MS);
      }
    };
    tick();
    return () => { stopped = true; clearTimeout(timer.current); };
  }, [load]);

  const isRunning = build?.status === "building" || build?.status === "preflighting";

  useEffect(() => {
    if (!isRunning) return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [isRunning]);

  const fetchLog = useCallback(async (step) => {
    try {
      const data = await getClusterBuildLogs(buildId, step.nodeId || undefined);
      const match = (data.items || []).find((item) => item.id === step.id);
      setLogs(match
        ? { ...match, phase: step.phase }
        : { logTail: "(waiting for output…)", phase: step.phase });
    } catch (err) {
      notify(err.message, true);
    }
  }, [buildId, notify]);

  const openLogs = (step, { auto = false } = {}) => {
    // Manual selection always wins over the generic live follower. The pinned
    // step still refreshes below while it is running.
    if (!auto) follow.current = false;
    const meta = { id: step.id, nodeId: step.nodeId, phase: step.phase };
    prevViewedStatus.current = step.status;
    stickToBottom.current = true;  // a freshly opened step starts at the newest output
    setLogStep(meta);
    fetchLog(meta);
  };

  // Live-follow: while the build runs, keep the panel on the currently running
  // step — including advancing to the NEXT step when a phase completes.
  useEffect(() => {
    if (!build || build.status !== "building" || !follow.current) return;
    const running = (build.steps || []).find((s) => s.status === "running");
    if (running && (!logStep || logStep.id !== running.id)) {
      openLogs(running, { auto: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [build, logStep]);

  // When the viewed step stops running (completed or failed), fetch its log one
  // last time so the panel shows the final output, not a mid-stream tail.
  useEffect(() => {
    if (!logStep || !build) return;
    const step = (build.steps || []).find((s) => s.id === logStep.id);
    const status = step?.status || null;
    if (prevViewedStatus.current === "running" && status && status !== "running") {
      fetchLog(logStep);
    }
    prevViewedStatus.current = status;
  }, [build, logStep, fetchLog]);

  // Live-refresh the open log while its step is still running.
  useEffect(() => {
    if (!logStep || !build || build.status !== "building") return undefined;
    const step = (build.steps || []).find((s) => s.id === logStep.id);
    if (!step || step.status !== "running") return undefined;
    const id = setInterval(() => fetchLog(logStep), LOG_REFRESH_MS);
    return () => clearInterval(id);
  }, [logStep, build, fetchLog]);

  // Keep the log well pinned to the newest output, unless the user has
  // scrolled up to read something — then leave their position alone.
  useEffect(() => {
    const el = logEl.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [logs]);

  const onLogScroll = () => {
    const el = logEl.current;
    if (!el) return;
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  };

  const closeLogs = () => {
    follow.current = false;
    setLogs(null);
    setLogStep(null);
  };

  const logStepId = logStep?.id ?? null;
  const viewedStep = logStep ? (build?.steps || []).find((s) => s.id === logStep.id) : null;
  const logIsLive = viewedStep?.status === "running";

  if (!build) return <div className="card sg-cb-card"><p className="muted">Loading…</p></div>;

  const nodeById = Object.fromEntries((build.nodes || []).map((n) => [n.id, n]));
  // Show the whole plan, not just phases that have already started — the rail
  // is how someone sees what is still ahead while the build runs.
  const plannedPhases = expectedPhases(build);
  const phaseGroups = [...new Set([
    ...plannedPhases,
    ...PHASE_ORDER.filter((p) => (build.steps || []).some((s) => s.phase === p)),
  ])]
    .sort((a, b) => PHASE_ORDER.indexOf(a) - PHASE_ORDER.indexOf(b))
    .map((phase) => ({ phase, steps: (build.steps || []).filter((s) => s.phase === phase) }));

  const phaseState = (steps) => {
    if (steps.length === 0) return "todo";
    if (steps.some((s) => s.status === "failed")) return "fail";
    if (steps.some((s) => s.status === "running")) return "now";
    if (steps.every((s) => s.status === "completed" || s.status === "skipped")) return "done";
    return "todo";
  };

  const duration = buildDuration(build);
  const isDone = build.status === "completed";
  const buildProfile = buildProfiles.find(
    (profile) => String(profile.id) === String(build.buildProfileId)
  );

  return (
    <div className="sg-cb-detail">
      <div className="sg-cb-detail-head">
        <button className="btn-ghost" onClick={onBack}>← All builds</button>
        <h3>{build.name}</h3>
        {build.status === "building"
          ? <span className="sg-cb-livebadge"><i />Building</span>
          : <StatusPill status={build.status} />}
        <span className="muted sg-cb-mono">
          v{build.k8sVersion} · {build.topologyType === "stacked_ha" ? "HA" : "single CP"} ·
          {" "}{build.controlPlaneEndpoint} · {build.cniPlugin}
        </span>
        <span className="sg-cb-detail-actions">
          {canExecute && build.status === "building" ? (
            <button className="btn-outline" onClick={async () => { await cancelClusterBuild(build.id); load(); }}>Cancel build</button>
          ) : null}
          {canExecute && (build.status === "failed" || build.status === "cancelled") ? (
            <button className="primary" onClick={async () => {
              try { await retryClusterBuild(build.id); load(); }
              catch (err) { notify(err.message, true); }
            }}>
              {build.status === "cancelled"
                ? "Resume build — completed phases are kept"
                : "Retry — resets failed nodes, resumes here"}
            </button>
          ) : null}
          {build.status !== "building" ? (
            <button className="btn-danger" onClick={async () => {
              try { await deleteClusterBuild(build.id); onDeleted(); }
              catch (err) { notify(err.message, true); }
            }}>Delete</button>
          ) : null}
        </span>
      </div>

      {build.error ? <ErrorBanner message={build.error} /> : null}

      {build.status === "building" ? <BuildProgress build={build} now={now} /> : null}

      <div className="card sg-cb-config-summary sg-cb-detail-config">
        <div className="sg-cb-config-item">
          <span className="sg-cb-config-label">Add-ons</span>
          <AddonChips addons={build.addons || []} catalog={addonCatalog} />
        </div>
        <div className="sg-cb-config-item">
          <span className="sg-cb-config-label">Image source</span>
          <strong className="sg-cb-mono">{buildProfile?.k8sImageRegistry || "Upstream registries"}</strong>
          {buildProfile?.k8sImageRegistry ? <span className="muted">Registry proxy</span> : null}
        </div>
        <div className="sg-cb-config-item">
          <span className="sg-cb-config-label">Package source</span>
          <strong>{buildProfile?.name || "Internet defaults"}</strong>
          <span className="muted">{buildProfile?.repoMode || "internet"}</span>
        </div>
      </div>

      {isDone ? (
        <>
          <div className="card sg-cb-donecard">
            <div className="sg-cb-okring">✓</div>
            <div>
              <h3>{build.name} is alive</h3>
              <p className="muted">
                {(build.nodes || []).length} nodes · registered in KubeSight as{" "}
                <b>{build.name}</b> — already visible in
                Clusters, Dashboard and Inventory.
              </p>
            </div>
            <ShapeGlyph shape={build.nodeShape} buildStatus={build.status} />
          </div>
          <div className="sg-cb-factrow">
            <div className="sg-cb-fact"><div className="sg-cb-fk">Endpoint</div><div className="sg-cb-fv sg-cb-mono">{build.controlPlaneEndpoint}</div></div>
            {duration ? <div className="sg-cb-fact"><div className="sg-cb-fk">Build time</div><div className="sg-cb-fv">{duration}</div></div> : null}
            <div className="sg-cb-fact"><div className="sg-cb-fk">Network</div><div className="sg-cb-fv">{build.cniPlugin} · <span className="sg-cb-mono">{build.podCidr}</span></div></div>
            <div className="sg-cb-fact"><div className="sg-cb-fk">Join secrets</div><div className="sg-cb-fv">Destroyed after use</div></div>
          </div>
        </>
      ) : null}

      <div className="card sg-cb-card">
        <h4 className="sg-cb-h4">Phases</h4>
        <div className="sg-cb-rail">
          {phaseGroups.map(({ phase, steps }, index) => {
            const state = phaseState(steps);
            return (
              <div key={phase} className={`sg-cb-phase is-${state}`}>
                <div className="sg-cb-knot">{state === "done" ? "✓" : index + 1}</div>
                <div className="sg-cb-pname">
                  {PHASE_LABELS[phase] || phase}
                  <span className="sg-cb-pnote">{PHASE_NOTES[phase] || ""}</span>
                </div>
                <div className="sg-cb-lanes">
                  {steps.map((step) => {
                    const laneState =
                      step.status === "completed" ? "done"
                      : step.status === "running" ? "now"
                      : step.status === "failed" ? "fail" : "";
                    const who = step.nodeId
                      ? (nodeById[step.nodeId]?.hostname || nodeById[step.nodeId]?.address || `node ${step.nodeId}`)
                      : "cluster";
                    return (
                      <button
                        key={step.id}
                        className={`sg-cb-lane is-${laneState} ${logStepId === step.id ? "is-open" : ""}`}
                        onClick={() => openLogs(step)}
                        title="View log"
                      >
                        <i />{who}
                        {step.attempt > 1 ? <span className="sg-cb-attempt">×{step.attempt}</span> : null}
                      </button>
                    );
                  })}
                  {steps.length === 0 ? <span className="sg-cb-lane-waiting">not started</span> : null}
                </div>
              </div>
            );
          })}
          {phaseGroups.length === 0 ? <p className="muted">No phases recorded yet.</p> : null}
        </div>
      </div>

      {logs ? (
        <div className="card sg-cb-card sg-cb-logcard">
          <div className="sg-cb-log-head">
            <h4 className="sg-cb-h4">Step log</h4>
            <span className="muted sg-cb-mono">{PHASE_LABELS[logs.phase] || logs.phase}</span>
            {logIsLive ? <span className="sg-cb-livebadge"><i />live</span> : null}
            <span className="muted sg-cb-log-note">secrets scrubbed before anything is stored</span>
            <button className="btn-ghost" onClick={closeLogs}>Close</button>
          </div>
          {logs.error ? <ErrorBanner message={logs.error} /> : null}
          <pre className="sg-cb-log" ref={logEl} onScroll={onLogScroll}>
            {logs.logTail || (logIsLive ? "(waiting for output…)" : "(empty)")}
          </pre>
        </div>
      ) : null}

      <div className="card sg-cb-card">
        <h4 className="sg-cb-h4">Nodes</h4>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Node</th><th>Role</th><th>Address</th><th>OS</th><th>ESXi host</th><th>Status</th></tr>
            </thead>
            <tbody>
              {(build.nodes || []).map((node) => (
                <tr key={node.id}>
                  <td>{node.hostname || node.vsphereVmName || "—"}{node.isPrimaryCp ? " ★" : ""}{node.isLbMaster ? " (VRRP master)" : ""}</td>
                  <td>{ROLE_LABELS[node.role]}</td>
                  <td className="sg-cb-mono">{node.address}{node.addressSource === "manual" ? " (manual)" : ""}</td>
                  <td>{node.osFamily ? `${node.osFamily} ${node.osVersion || ""}` : "—"}</td>
                  <td>{node.vsphereHost || "—"}</td>
                  <td><StatusPill status={node.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function ClusterBuilderPage({ canCreate = false, canExecute = false, canManageInfra = false }) {
  const [tab, setTab] = useState("builds");
  const [builds, setBuilds] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [options, setOptions] = useState(null);
  const [selectedBuildId, setSelectedBuildId] = useState(null);
  const [infra, setInfra] = useState({ vsphere: [], credentials: [], profiles: [], buildProfiles: [] });

  const notify = useCallback((message, isError = false) => {
    if (isError) { setError(message); setNotice(""); }
    else { setNotice(message); setError(""); }
  }, []);

  const reloadBuilds = useCallback(async () => {
    try {
      const data = await listClusterBuilds();
      setBuilds(data.items || []);
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  const reloadInfra = useCallback(async () => {
    const [vsphere, credentials, profiles, buildProfiles] = await Promise.all([
      canManageInfra ? listVSphereConnections().catch(() => ({ items: [] })) : Promise.resolve({ items: [] }),
      canManageInfra ? listSshCredentials().catch(() => ({ items: [] })) : Promise.resolve({ items: [] }),
      canManageInfra ? listSshProfiles().catch(() => ({ items: [] })) : Promise.resolve({ items: [] }),
      listBuildProfiles().catch(() => ({ items: [] })),
    ]);
    setInfra({
      vsphere: vsphere.items || [],
      credentials: credentials.items || [],
      profiles: profiles.items || [],
      buildProfiles: buildProfiles.items || [],
    });
  }, [canManageInfra]);

  useEffect(() => {
    reloadBuilds();
    reloadInfra();
    getBuilderOptions().then(setOptions).catch((err) => setError(err.message));
  }, [reloadBuilds, reloadInfra]);

  useEffect(() => {
    const active = builds.some((b) => b.status === "building" || b.status === "preflighting");
    if (!active) return undefined;
    const id = setInterval(reloadBuilds, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [builds, reloadBuilds]);

  const tabs = [
    { id: "builds", label: "Builds" },
    ...(canCreate ? [{ id: "new", label: "New build" }] : []),
    ...(canManageInfra ? [{ id: "infra", label: "Infrastructure" }] : []),
  ];

  return (
    <div className="sg-cb-page">
      <PageTitle
        title="Cluster Builder"
        subtitle="Pick machines from vCenter, assign roles, and KubeSight builds the cluster."
      />
      {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}
      {notice ? <div className="sg-cb-notice">{notice}</div> : null}

      <div className="sg-cb-tabs">
        {tabs.map((t) => (
          <button
            key={t.id}
            className={`sg-cb-tab ${tab === t.id ? "is-active" : ""}`}
            onClick={() => { setTab(t.id); setSelectedBuildId(null); }}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "builds" && selectedBuildId ? (
        <BuildDetail
          buildId={selectedBuildId}
          canExecute={canExecute}
          notify={notify}
          onBack={() => { setSelectedBuildId(null); reloadBuilds(); }}
          onDeleted={() => { setSelectedBuildId(null); reloadBuilds(); }}
          addonCatalog={options?.addons || []}
          buildProfiles={infra.buildProfiles}
        />
      ) : null}

      {tab === "builds" && !selectedBuildId ? (
        loading ? <p className="muted">Loading…</p> : (
          <div className="sg-cb-vstack">
            <FlowStrip builds={builds} infra={infra} canManageInfra={canManageInfra} />
            {builds.length === 0 && !canCreate ? (
              <EmptyState title="No cluster builds yet" message="No builds have been created." />
            ) : (
              <div className="sg-cb-build-grid">
                {builds.map((build) => (
                  <button key={build.id} className="card sg-cb-build-card" onClick={() => setSelectedBuildId(build.id)}>
                    <div className="sg-cb-build-card-top">
                      <span className="sg-cb-build-name">{build.name}</span>
                      <StatusPill status={build.status} />
                    </div>
                    <div className="muted">
                      v{build.k8sVersion} · {build.topologyType === "stacked_ha" ? "HA" : "single CP"} · {build.cniPlugin}
                      {build.vipAddress ? <> · VIP <span className="sg-cb-mono">{build.vipAddress}</span></> : null}
                    </div>
                    <div className="sg-cb-build-card-config">
                      <span>
                        {(build.addons || []).length
                          ? addonSummary(build.addons, options?.addons || [], true)
                          : "No add-ons"}
                      </span>
                      {infra.buildProfiles.find(
                        (profile) => String(profile.id) === String(build.buildProfileId)
                      )?.k8sImageRegistry ? <span>Registry proxy</span> : null}
                    </div>
                    <ShapeGlyph shape={build.nodeShape} buildStatus={build.status} />
                    {build.resultClusterId ? (
                      <div className="sg-cb-build-card-result">→ Registered as {build.name} · open in Clusters</div>
                    ) : build.error ? (
                      <div className="sg-cb-build-card-err">{build.error}</div>
                    ) : build.status === "building" ? (
                      <div className="sg-cb-build-card-live">
                        <span className="sg-cb-livebadge"><i /></span>
                        {PHASE_LABELS[build.currentPhase] || "Starting…"}
                      </div>
                    ) : (
                      <div className="sg-cb-build-card-nodes">
                        {build.nodeCounts.loadbalancer ? `${build.nodeCounts.loadbalancer} LB · ` : ""}
                        {build.nodeCounts.controlPlane} CP · {build.nodeCounts.worker} worker{build.nodeCounts.worker === 1 ? "" : "s"}
                      </div>
                    )}
                  </button>
                ))}
                {canCreate ? (
                  <button className="card sg-cb-build-card sg-cb-newcard" onClick={() => setTab("new")}>
                    <div className="sg-cb-newcard-plus">+</div>
                    <div className="sg-cb-newcard-t">New cluster build</div>
                    <div className="muted">2 LB · 3 CP · N workers recommended</div>
                  </button>
                ) : null}
              </div>
            )}
          </div>
        )
      ) : null}

      {tab === "new" && canCreate ? (
        <WizardTab
          options={options}
          infra={infra}
          notify={notify}
          onBuildLaunched={(id) => { setTab("builds"); setSelectedBuildId(id); reloadBuilds(); }}
        />
      ) : null}

      {tab === "infra" && canManageInfra ? (
        <InfraTab notify={notify} infra={infra} reloadInfra={reloadInfra} />
      ) : null}
    </div>
  );
}
