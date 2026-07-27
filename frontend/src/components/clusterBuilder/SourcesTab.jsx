/** Sources — everything a build consumes.
 *
 *  Was "Infrastructure": four stacked tables with Add and Remove columns. Each
 *  record's name, route and health now read as one line, and the freshness of
 *  its last proof is shown, because a route that passed a month ago is not the
 *  same as one that passed an hour ago.
 *
 *  The repository-profile form — image prefixes, a same-prefix checkbox,
 *  registry credentials, two proxies, NO_PROXY and a PEM textarea — becomes the
 *  three questions it was always asking.
 */

import { useState } from "react";
import { Field, StatusPill } from "./common.jsx";
import {
  bundleCoverage,
  freshness,
  sourceProfileSummary,
  sshPosture,
  timeAgo,
} from "../../utils/clusterBuilder.js";
import {
  createBuildProfile,
  createSshCredential,
  createSshProfile,
  createVSphereConnection,
  deleteBuildProfile,
  deleteSshCredential,
  deleteSshProfile,
  deleteVSphereConnection,
  testSshProfile,
  testVSphereConnection,
} from "../../api/clusterBuildsApi.js";

const POLICY_TONE = { pinned: "is-ok", strict: "is-ok", tofu: "is-warn" };

function Group({ title, description, children, action }) {
  return (
    <section className="card sg-cb-conn">
      <header className="sg-cb-conn-head">
        <div>
          <h3>{title}</h3>
          <p className="muted">{description}</p>
        </div>
        {action}
      </header>
      {children}
    </section>
  );
}

function Entry({ name, sub, right, tone = "" }) {
  return (
    <div className={`sg-cb-entry ${tone}`}>
      <span className="sg-cb-entry-id">
        <span className="en">{name}</span>
        <span className="ea sg-cb-mono">{sub}</span>
      </span>
      <span className="sg-cb-entry-right">{right}</span>
    </div>
  );
}

function Freshness({ at, status }) {
  const fresh = freshness(at);
  if (status === "failed") {
    return <span className="sg-cb-fresh is-bad">last test failed {timeAgo(at)}</span>;
  }
  if (fresh.never) return <span className="sg-cb-fresh is-stale">never tested</span>;
  return (
    <span className={`sg-cb-fresh ${fresh.stale ? "is-stale" : ""}`}>{fresh.text}</span>
  );
}

const EMPTY_VS = { name: "", baseUrl: "", username: "", password: "", skipTlsVerify: false };
const EMPTY_CRED = {
  name: "", username: "", authMethod: "key", secret: "",
  sudoMode: "nopasswd", sudoPassword: "", port: 22,
};
const EMPTY_ROUTE = {
  name: "", credentialId: "", routeMode: "direct",
  bastionHost: "", bastionCredentialId: "", hostKeyPolicy: "tofu",
};
const EMPTY_SOURCE = {
  name: "",
  repoMode: "internet",
  k8sPkgRepoUrl: "",
  offlineBundlePath: "",
  k8sImageRegistry: "",
  cniImageRegistry: "",
  addonImageRegistry: "",
  registryUsername: "",
  registryPassword: "",
  httpProxy: "",
  httpsProxy: "",
  noProxy: "",
  extraCaCertsPem: "",
};

function imagePrefixError(value, required = false) {
  const prefix = String(value || "").trim();
  if (!prefix) return required ? "Enter a registry host or repository prefix." : "";
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(prefix)) return "Use an image prefix without http:// or https://.";
  if (/\s/.test(prefix)) return "Image prefixes cannot contain spaces.";
  return "";
}

function forwardProxyError(value) {
  const raw = String(value || "").trim();
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
}

/** The three questions, in the order someone actually answers them. */
function SourceProfileForm({ onSave, onClose, busy }) {
  const [form, setForm] = useState({ ...EMPTY_SOURCE });
  const [useProxy, setUseProxy] = useState(false);
  const [samePrefix, setSamePrefix] = useState(true);
  const set = (key, value) => setForm((previous) => ({ ...previous, [key]: value }));

  const k8sImageError = useProxy ? imagePrefixError(form.k8sImageRegistry, true) : "";
  const cniImageError = useProxy && !samePrefix ? imagePrefixError(form.cniImageRegistry) : "";
  const addonImageError = useProxy && !samePrefix ? imagePrefixError(form.addonImageRegistry) : "";
  const authorities = new Set(
    (samePrefix
      ? [form.k8sImageRegistry]
      : [form.k8sImageRegistry, form.cniImageRegistry, form.addonImageRegistry])
      .map((prefix) => String(prefix || "").trim().replace(/\/+$/, "").split("/", 1)[0])
      .filter(Boolean)
  );
  const hasAuth = Boolean(form.registryUsername.trim() || form.registryPassword);
  const registryAuthError = useProxy
    && Boolean(form.registryUsername.trim()) !== Boolean(form.registryPassword)
    ? "Provide both username and password, or leave both blank for anonymous access."
    : useProxy && hasAuth && authorities.size !== 1
      ? "One credential pair can only be used with one registry authority."
      : "";
  const httpProxyError = forwardProxyError(form.httpProxy);
  const httpsProxyError = forwardProxyError(form.httpsProxy);
  const extraCaError = form.extraCaCertsPem.trim()
    && (!form.extraCaCertsPem.includes("-----BEGIN CERTIFICATE-----")
      || !form.extraCaCertsPem.includes("-----END CERTIFICATE-----"))
    ? "Paste a PEM-encoded CA certificate."
    : "";

  const valid = Boolean(
    form.name.trim()
    && (form.repoMode !== "mirror" || form.k8sPkgRepoUrl.trim())
    && (form.repoMode !== "offline" || form.offlineBundlePath.trim())
    && !k8sImageError && !cniImageError && !addonImageError
    && !registryAuthError && !extraCaError && !httpProxyError && !httpsProxyError
  );

  const save = () => {
    const shared = form.k8sImageRegistry.trim().replace(/\/+$/, "");
    onSave({
      ...form,
      name: form.name.trim(),
      k8sPkgRepoUrl: form.k8sPkgRepoUrl.trim(),
      offlineBundlePath: form.offlineBundlePath.trim(),
      k8sImageRegistry: useProxy ? shared : "",
      cniImageRegistry: useProxy
        ? (samePrefix ? shared : form.cniImageRegistry.trim().replace(/\/+$/, ""))
        : "",
      addonImageRegistry: useProxy
        ? (samePrefix ? shared : form.addonImageRegistry.trim().replace(/\/+$/, ""))
        : "",
      registryUsername: useProxy ? form.registryUsername.trim() : "",
      registryPassword: useProxy ? form.registryPassword : "",
      extraCaCertsPem: form.extraCaCertsPem.trim(),
      httpProxy: form.httpProxy.trim(),
      httpsProxy: form.httpsProxy.trim(),
      noProxy: form.noProxy.trim(),
    });
  };

  return (
    <div className="sg-cb-qform">
      <Field label="Name this profile" htmlFor="src-name">
        <input
          id="src-name"
          className="sg-cb-input"
          value={form.name}
          onChange={(event) => set("name", event.target.value)}
          placeholder="Production sources"
        />
      </Field>

      <div className="sg-cb-qa">
        <div className="q">Where do packages come from?</div>
        <div className="sg-cb-seg" role="group" aria-label="Package source">
          {[
            ["internet", "Upstream internet"],
            ["mirror", "Internal mirror"],
            ["offline", "Offline bundle"],
          ].map(([value, label]) => (
            <button
              key={value}
              type="button"
              aria-pressed={form.repoMode === value}
              onClick={() => set("repoMode", value)}
            >
              {label}
            </button>
          ))}
        </div>
        {form.repoMode === "mirror" ? (
          <Field
            htmlFor="src-pkg"
            hint="{minor} is replaced per Kubernetes minor, for example …/v1.32/deb/."
          >
            <input
              id="src-pkg"
              className="sg-cb-input sg-cb-mono"
              value={form.k8sPkgRepoUrl}
              onChange={(event) => set("k8sPkgRepoUrl", event.target.value)}
              placeholder="https://packages.example/kubernetes/v{minor}/deb/"
            />
          </Field>
        ) : null}
        {form.repoMode === "offline" ? (
          <Field htmlFor="src-bundle" hint="A directory on the KubeSight host holding the packages.">
            <input
              id="src-bundle"
              className="sg-cb-input sg-cb-mono"
              value={form.offlineBundlePath}
              onChange={(event) => set("offlineBundlePath", event.target.value)}
              placeholder="/srv/kubesight/bundles/k8s"
            />
          </Field>
        ) : null}
      </div>

      <div className="sg-cb-qa">
        <div className="q">Where do container images come from?</div>
        <div className="sg-cb-seg" role="group" aria-label="Image source">
          <button type="button" aria-pressed={!useProxy} onClick={() => setUseProxy(false)}>
            Their upstream registries
          </button>
          <button type="button" aria-pressed={useProxy} onClick={() => setUseProxy(true)}>
            A registry proxy
          </button>
        </div>
        {useProxy ? (
          <div className="sg-cb-qgrid">
            <Field
              label="Kubernetes image prefix"
              htmlFor="src-k8simg"
              hint="Registry host or repository prefix, without a URL scheme."
              error={k8sImageError}
            >
              <input
                id="src-k8simg"
                className="sg-cb-input sg-cb-mono"
                value={form.k8sImageRegistry}
                onChange={(event) => set("k8sImageRegistry", event.target.value)}
                placeholder="nexus.example:5000/kubernetes"
                aria-invalid={Boolean(k8sImageError)}
              />
            </Field>
            <label className="sg-cb-inlinecheck sg-cb-span">
              <input
                type="checkbox"
                checked={samePrefix}
                onChange={(event) => setSamePrefix(event.target.checked)}
              />
              Use the same prefix for CNI and add-on images
            </label>
            {!samePrefix ? (
              <>
                <Field label="CNI image prefix" htmlFor="src-cniimg" error={cniImageError}>
                  <input
                    id="src-cniimg"
                    className="sg-cb-input sg-cb-mono"
                    value={form.cniImageRegistry}
                    onChange={(event) => set("cniImageRegistry", event.target.value)}
                    placeholder="nexus.example:5000/networking"
                    aria-invalid={Boolean(cniImageError)}
                  />
                </Field>
                <Field label="Add-on image prefix" htmlFor="src-addonimg" error={addonImageError}>
                  <input
                    id="src-addonimg"
                    className="sg-cb-input sg-cb-mono"
                    value={form.addonImageRegistry}
                    onChange={(event) => set("addonImageRegistry", event.target.value)}
                    placeholder="nexus.example:5000/addons"
                    aria-invalid={Boolean(addonImageError)}
                  />
                </Field>
              </>
            ) : null}
            <Field label="Registry username (optional)" htmlFor="src-ruser" error={registryAuthError}>
              <input
                id="src-ruser"
                className="sg-cb-input"
                value={form.registryUsername}
                onChange={(event) => set("registryUsername", event.target.value)}
                autoComplete="off"
                aria-invalid={Boolean(registryAuthError)}
              />
            </Field>
            <Field label="Registry password (optional)" htmlFor="src-rpass">
              <input
                id="src-rpass"
                type="password"
                className="sg-cb-input"
                value={form.registryPassword}
                onChange={(event) => set("registryPassword", event.target.value)}
                autoComplete="new-password"
                aria-invalid={Boolean(registryAuthError)}
              />
            </Field>
          </div>
        ) : null}
      </div>

      <div className="sg-cb-qa">
        <div className="q">Anything in the way?</div>
        <p className="a muted">
          Corporate TLS inspection needs its CA here, or downloads fail with a certificate error.
        </p>
        <div className="sg-cb-qgrid">
          <Field label="HTTP proxy (optional)" htmlFor="src-hp" error={httpProxyError}>
            <input
              id="src-hp"
              type="url"
              className="sg-cb-input sg-cb-mono"
              value={form.httpProxy}
              onChange={(event) => set("httpProxy", event.target.value)}
              placeholder="http://proxy.example:3128"
              aria-invalid={Boolean(httpProxyError)}
            />
          </Field>
          <Field label="HTTPS proxy (optional)" htmlFor="src-hps" error={httpsProxyError}>
            <input
              id="src-hps"
              type="url"
              className="sg-cb-input sg-cb-mono"
              value={form.httpsProxy}
              onChange={(event) => set("httpsProxy", event.target.value)}
              placeholder="http://proxy.example:3128"
              aria-invalid={Boolean(httpsProxyError)}
            />
          </Field>
          <Field
            label="NO_PROXY (optional)"
            htmlFor="src-np"
            hint="Comma-separated hosts, domains or CIDRs that bypass the proxy."
          >
            <input
              id="src-np"
              className="sg-cb-input sg-cb-mono"
              value={form.noProxy}
              onChange={(event) => set("noProxy", event.target.value)}
              placeholder=".cluster.local,10.0.0.0/8"
            />
          </Field>
          <Field
            label="Trusted CA certificates (optional)"
            htmlFor="src-ca"
            error={extraCaError}
          >
            <textarea
              id="src-ca"
              rows={4}
              className="sg-cb-input sg-cb-mono"
              value={form.extraCaCertsPem}
              onChange={(event) => set("extraCaCertsPem", event.target.value)}
              placeholder={"-----BEGIN CERTIFICATE-----\n…\n-----END CERTIFICATE-----"}
              aria-invalid={Boolean(extraCaError)}
            />
          </Field>
        </div>
      </div>

      <div className="sg-cb-actions">
        <button className="btn-ghost" type="button" onClick={onClose}>Cancel</button>
        <button className="primary" type="button" disabled={!valid || busy} onClick={save}>
          Save profile
        </button>
      </div>
    </div>
  );
}

export default function SourcesTab({ infra, reloadInfra, notify, addonCatalog = [] }) {
  const { vsphere, credentials, profiles, buildProfiles } = infra;
  const [open, setOpen] = useState(null);
  const [busy, setBusy] = useState(false);
  const [vsForm, setVsForm] = useState({ ...EMPTY_VS });
  const [credForm, setCredForm] = useState({ ...EMPTY_CRED });
  const [routeForm, setRouteForm] = useState({ ...EMPTY_ROUTE });
  const [testHosts, setTestHosts] = useState({});

  const run = async (fn, after) => {
    setBusy(true);
    try {
      await fn();
      await reloadInfra();
      if (after) after();
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };

  const coverage = bundleCoverage(addonCatalog);

  return (
    <div className="sg-cb-sources">
      <Group
        title="vCenter"
        description="A read-only link that powers the machine picker and the placement checks. The account needs nothing beyond the Read-Only role."
        action={(
          <button
            className="btn-outline"
            type="button"
            onClick={() => setOpen(open === "vsphere" ? null : "vsphere")}
          >
            {open === "vsphere" ? "Close" : "Add a vCenter"}
          </button>
        )}
      >
        {open === "vsphere" ? (
          <div className="sg-cb-qgrid sg-cb-addform">
            <Field label="Name" htmlFor="vs-name">
              <input id="vs-name" className="sg-cb-input" value={vsForm.name}
                     onChange={(e) => setVsForm({ ...vsForm, name: e.target.value })} />
            </Field>
            <Field label="vCenter URL" htmlFor="vs-url" hint="https://vcenter.example.com">
              <input id="vs-url" className="sg-cb-input sg-cb-mono" value={vsForm.baseUrl}
                     onChange={(e) => setVsForm({ ...vsForm, baseUrl: e.target.value })} />
            </Field>
            <Field label="Username" htmlFor="vs-user">
              <input id="vs-user" className="sg-cb-input" value={vsForm.username}
                     onChange={(e) => setVsForm({ ...vsForm, username: e.target.value })} />
            </Field>
            <Field label="Password" htmlFor="vs-pass">
              <input id="vs-pass" type="password" className="sg-cb-input" value={vsForm.password}
                     onChange={(e) => setVsForm({ ...vsForm, password: e.target.value })} />
            </Field>
            <label className="sg-cb-inlinecheck sg-cb-span">
              <input type="checkbox" checked={vsForm.skipTlsVerify}
                     onChange={(e) => setVsForm({ ...vsForm, skipTlsVerify: e.target.checked })} />
              Skip TLS verification
            </label>
            <div className="sg-cb-actions sg-cb-span">
              <button className="primary" type="button" disabled={busy} onClick={() => run(
                () => createVSphereConnection(vsForm),
                () => { setVsForm({ ...EMPTY_VS }); setOpen(null); }
              )}>
                Save connection
              </button>
            </div>
          </div>
        ) : null}

        {vsphere.length ? vsphere.map((row) => (
          <Entry
            key={row.id}
            name={row.name}
            sub={`${row.baseUrl} · ${row.username}`}
            tone={row.lastConnectionStatus && row.lastConnectionStatus !== "ok" ? "is-bad" : ""}
            right={(
              <>
                <Freshness at={row.lastTestedAt} status={row.lastConnectionStatus === "ok" ? "ok" : row.lastConnectionStatus ? "failed" : null} />
                <button className="btn-ghost btn-sm" type="button" onClick={async () => {
                  try {
                    const result = await testVSphereConnection(row.id);
                    notify(
                      result.status === "ok"
                        ? `Connected — ${result.vmCount} machines visible.`
                        : result.error,
                      result.status !== "ok"
                    );
                    reloadInfra();
                  } catch (error) { notify(error.message, true); }
                }}>
                  Test now
                </button>
                <button className="btn-ghost btn-sm" type="button"
                        onClick={() => run(() => deleteVSphereConnection(row.id))}>
                  Remove
                </button>
              </>
            )}
          />
        )) : <p className="muted">Nothing configured yet — machines can still be entered by hand.</p>}
      </Group>

      <Group
        title="SSH reach"
        description="Who KubeSight logs in as, and the route it takes. Secrets are encrypted at rest and never shown again. A route that has never been proved is the usual cause of a build dying in node preparation."
        action={(
          <span className="sg-cb-conn-acts">
            <button className="btn-outline" type="button"
                    onClick={() => setOpen(open === "cred" ? null : "cred")}>
              {open === "cred" ? "Close" : "Add a credential"}
            </button>
            <button className="btn-outline" type="button"
                    onClick={() => setOpen(open === "route" ? null : "route")}>
              {open === "route" ? "Close" : "Add a route"}
            </button>
          </span>
        )}
      >
        {open === "cred" ? (
          <div className="sg-cb-qgrid sg-cb-addform">
            <Field label="Name" htmlFor="cr-name">
              <input id="cr-name" className="sg-cb-input" value={credForm.name}
                     onChange={(e) => setCredForm({ ...credForm, name: e.target.value })} />
            </Field>
            <Field label="Username" htmlFor="cr-user">
              <input id="cr-user" className="sg-cb-input" value={credForm.username}
                     onChange={(e) => setCredForm({ ...credForm, username: e.target.value })} />
            </Field>
            <Field label="Auth method" htmlFor="cr-auth">
              <select id="cr-auth" className="sg-cb-input" value={credForm.authMethod}
                      onChange={(e) => setCredForm({ ...credForm, authMethod: e.target.value })}>
                <option value="key">Private key</option>
                <option value="password">Password</option>
              </select>
            </Field>
            <Field
              label={credForm.authMethod === "key" ? "Private key (PEM)" : "Password"}
              htmlFor="cr-secret"
            >
              {credForm.authMethod === "key" ? (
                <textarea id="cr-secret" rows={4} className="sg-cb-input sg-cb-mono"
                          value={credForm.secret}
                          onChange={(e) => setCredForm({ ...credForm, secret: e.target.value })} />
              ) : (
                <input id="cr-secret" type="password" className="sg-cb-input" value={credForm.secret}
                       onChange={(e) => setCredForm({ ...credForm, secret: e.target.value })} />
              )}
            </Field>
            <Field label="Escalation" htmlFor="cr-sudo">
              <select id="cr-sudo" className="sg-cb-input" value={credForm.sudoMode}
                      onChange={(e) => setCredForm({ ...credForm, sudoMode: e.target.value })}>
                <option value="nopasswd">Passwordless sudo</option>
                <option value="password">Sudo with password</option>
                <option value="root">Root login</option>
              </select>
            </Field>
            {credForm.sudoMode === "password" ? (
              <Field label="Sudo password" htmlFor="cr-sudopass">
                <input id="cr-sudopass" type="password" className="sg-cb-input"
                       value={credForm.sudoPassword}
                       onChange={(e) => setCredForm({ ...credForm, sudoPassword: e.target.value })} />
              </Field>
            ) : null}
            <Field label="SSH port" htmlFor="cr-port">
              <input id="cr-port" type="number" className="sg-cb-input" value={credForm.port}
                     onChange={(e) => setCredForm({ ...credForm, port: Number(e.target.value) })} />
            </Field>
            <div className="sg-cb-actions sg-cb-span">
              <button className="primary" type="button" disabled={busy} onClick={() => run(
                () => createSshCredential(credForm),
                () => { setCredForm({ ...EMPTY_CRED }); setOpen(null); }
              )}>
                Save credential
              </button>
            </div>
          </div>
        ) : null}

        {open === "route" ? (
          <div className="sg-cb-qgrid sg-cb-addform">
            <Field label="Name" htmlFor="rt-name">
              <input id="rt-name" className="sg-cb-input" value={routeForm.name}
                     onChange={(e) => setRouteForm({ ...routeForm, name: e.target.value })} />
            </Field>
            <Field label="Credential" htmlFor="rt-cred">
              <select id="rt-cred" className="sg-cb-input" value={routeForm.credentialId}
                      onChange={(e) => setRouteForm({ ...routeForm, credentialId: e.target.value })}>
                <option value="">Select…</option>
                {credentials.map((row) => (
                  <option key={row.id} value={row.id}>{row.name}</option>
                ))}
              </select>
            </Field>
            <Field label="Route" htmlFor="rt-mode">
              <select id="rt-mode" className="sg-cb-input" value={routeForm.routeMode}
                      onChange={(e) => setRouteForm({ ...routeForm, routeMode: e.target.value })}>
                <option value="direct">Direct</option>
                <option value="bastion">Via a bastion / jump host</option>
              </select>
            </Field>
            {routeForm.routeMode === "bastion" ? (
              <>
                <Field label="Bastion host" htmlFor="rt-bhost">
                  <input id="rt-bhost" className="sg-cb-input sg-cb-mono" value={routeForm.bastionHost}
                         onChange={(e) => setRouteForm({ ...routeForm, bastionHost: e.target.value })} />
                </Field>
                <Field label="Bastion credential" htmlFor="rt-bcred">
                  <select id="rt-bcred" className="sg-cb-input" value={routeForm.bastionCredentialId}
                          onChange={(e) => setRouteForm({ ...routeForm, bastionCredentialId: e.target.value })}>
                    <option value="">Select…</option>
                    {credentials.map((row) => (
                      <option key={row.id} value={row.id}>{row.name}</option>
                    ))}
                  </select>
                </Field>
              </>
            ) : null}
            <Field
              label="Host-key policy"
              htmlFor="rt-policy"
              hint="Production should pin pre-approved fingerprints; trust-on-first-use records whatever answers the first time."
            >
              <select id="rt-policy" className="sg-cb-input" value={routeForm.hostKeyPolicy}
                      onChange={(e) => setRouteForm({ ...routeForm, hostKeyPolicy: e.target.value })}>
                <option value="tofu">Trust on first use</option>
                <option value="strict">Strict</option>
                <option value="pinned">Pinned (pre-approved only)</option>
              </select>
            </Field>
            <div className="sg-cb-actions sg-cb-span">
              <button className="primary" type="button" disabled={busy} onClick={() => run(
                () => createSshProfile({
                  ...routeForm,
                  credentialId: Number(routeForm.credentialId) || undefined,
                  bastionCredentialId: routeForm.bastionCredentialId
                    ? Number(routeForm.bastionCredentialId)
                    : undefined,
                }),
                () => { setRouteForm({ ...EMPTY_ROUTE }); setOpen(null); }
              )}>
                Save route
              </button>
            </div>
          </div>
        ) : null}

        {profiles.length ? profiles.map((row) => (
          <Entry
            key={row.id}
            name={row.name}
            sub={[
              row.routeMode === "bastion" ? `via ${row.bastionHost}` : "direct",
              sshPosture(row, credentials),
            ].filter(Boolean).join(" · ")}
            tone={row.lastTestStatus === "failed" ? "is-bad" : ""}
            right={(
              <>
                <span className={`sg-cb-pill ${POLICY_TONE[row.hostKeyPolicy] || "is-muted"}`}>
                  {row.hostKeyPolicy === "tofu" ? "trust on first use" : row.hostKeyPolicy}
                </span>
                <Freshness at={row.lastTestAt} status={row.lastTestStatus} />
                <span className="sg-cb-testinline">
                  <input
                    className="sg-cb-input sg-cb-mono"
                    placeholder="host to prove"
                    aria-label={`Host to test ${row.name} against`}
                    value={testHosts[row.id] || ""}
                    onChange={(e) => setTestHosts({ ...testHosts, [row.id]: e.target.value })}
                  />
                  <button className="btn-ghost btn-sm" type="button" onClick={async () => {
                    try {
                      const result = await testSshProfile(row.id, testHosts[row.id] || "");
                      notify(
                        result.status === "ok"
                          ? `Connected as ${result.effectiveUser} (${result.kernel}, ${result.latencyMs} ms).`
                          : result.error,
                        result.status !== "ok"
                      );
                      reloadInfra();
                    } catch (error) { notify(error.message, true); }
                  }}>
                    Test
                  </button>
                </span>
                <button className="btn-ghost btn-sm" type="button"
                        onClick={() => run(() => deleteSshProfile(row.id))}>
                  Remove
                </button>
              </>
            )}
          />
        )) : <p className="muted">No routes yet — a build cannot reach a machine without one.</p>}

        {credentials.length ? (
          <details className="sg-cb-subtle">
            <summary>
              {credentials.length} credential{credentials.length === 1 ? "" : "s"}
              <span className="muted">the identities routes log in with</span>
              <span className="sg-cb-chev" aria-hidden="true">›</span>
            </summary>
            <div className="sg-cb-subtle-body">
              {credentials.map((row) => (
                <Entry
                  key={row.id}
                  name={row.name}
                  sub={`${row.username} · ${row.authMethod === "key" ? "private key" : "password"} · port ${row.port}`}
                  right={(
                    <button className="btn-ghost btn-sm" type="button"
                            onClick={() => run(() => deleteSshCredential(row.id))}>
                      Remove
                    </button>
                  )}
                />
              ))}
            </div>
          </details>
        ) : null}
      </Group>

      <Group
        title="Packages & images"
        description="Three questions decide a source profile: where packages come from, where images come from, and what stands in the way."
        action={(
          <button className="btn-outline" type="button"
                  onClick={() => setOpen(open === "source" ? null : "source")}>
            {open === "source" ? "Close" : "Add a profile"}
          </button>
        )}
      >
        {open === "source" ? (
          <SourceProfileForm
            busy={busy}
            onClose={() => setOpen(null)}
            onSave={(payload) => run(() => createBuildProfile(payload), () => setOpen(null))}
          />
        ) : null}

        {buildProfiles.map((row) => {
          const summary = sourceProfileSummary(row);
          return (
            <div className="sg-cb-srcprofile" key={row.id}>
              <Entry
                name={row.name}
                sub={`${summary.packages.toLowerCase()} · ${summary.images.toLowerCase()}`}
                tone={summary.incomplete ? "is-bad" : ""}
                right={(
                  <>
                    {summary.incomplete
                      ? <StatusPill status="fail">{summary.incomplete}</StatusPill>
                      : <StatusPill status="ok">complete</StatusPill>}
                    <button className="btn-ghost btn-sm" type="button"
                            onClick={() => run(() => deleteBuildProfile(row.id))}>
                      Remove
                    </button>
                  </>
                )}
              />
              <dl className="sg-cb-answers">
                <div>
                  <dt>Packages</dt>
                  <dd>
                    {summary.packages}
                    {summary.packagesDetail
                      ? <span className="sg-cb-mono"> {summary.packagesDetail}</span>
                      : null}
                  </dd>
                </div>
                <div>
                  <dt>Images</dt>
                  <dd>
                    {summary.images}
                    {summary.imagesDetail
                      ? <span className="sg-cb-mono"> {summary.imagesDetail}</span>
                      : null}
                  </dd>
                </div>
                <div>
                  <dt>In the way</dt>
                  <dd>{summary.obstacles.join(" · ")}</dd>
                </div>
              </dl>
            </div>
          );
        })}

        <Entry
          name="Internet defaults"
          sub="upstream packages and registries · dev and test only"
          right={<span className="sg-cb-fresh">always available</span>}
        />

        {coverage.total ? (
          <p className="muted sg-cb-bundleline">
            Add-on bundles: <b>{coverage.bundled} of {coverage.total}</b> vendored on this host.
            {coverage.complete
              ? " Every add-on can install with no internet access at all."
              : " An offline build can only install the bundled ones — run tools/fetch_cluster_build_bundles.py for the rest."}
          </p>
        ) : null}
      </Group>
    </div>
  );
}
