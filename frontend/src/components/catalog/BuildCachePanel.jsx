import { useCallback, useEffect, useRef, useState } from "react";
import {
  cleanCiCache,
  createCiCacheVolume,
  getCiCache,
  listCiServices,
  measureCiCache,
  setCiCacheEnabled,
} from "../../api/ciApi.js";

/**
 * The build cache, as one card on the Runners page.
 *
 * Without a cache every build re-downloads its whole dependency graph, which
 * is usually the largest single number in a build's duration. The cache is one
 * volume mounted at /cache by every stage, with each build tool pointed into
 * its own directory under it — so the interesting states are: is there a
 * volume, is it switched on, how big has it grown, and can I empty it.
 *
 * Create is offered only when there is nothing there: a bound volume cannot be
 * resized or re-pathed in place, so the form would be a lie. Emptying is per
 * service, because a service's cache is its own subtree and one bad dependency
 * should not cost everybody else their warm cache.
 */

const BACKINGS = [
  {
    value: "nfs",
    label: "NFS export",
    hint: "Any node can run builds. Matches this cluster's other volumes.",
  },
  {
    value: "local",
    label: "Directory on one node",
    hint: "Faster, but pins every build to that node and grows on its disk.",
  },
];

function phaseChip(cache) {
  if (!cache.enabled) return { text: "Off", tone: "" };
  if (cache.mode === "class") return { text: `Storage class ${cache.storageClass}`, tone: "ok" };
  if (cache.claim?.phase === "Bound") return { text: "On", tone: "ok" };
  return { text: "On, but not bound", tone: "warn" };
}

function backingLine(volume) {
  const backing = volume?.backing;
  if (!backing) return "";
  if (backing.type === "nfs") return `NFS ${backing.server}:${backing.path}`;
  if (backing.type === "local") {
    return `Node ${backing.node || "?"} at ${backing.path}`;
  }
  if (backing.type === "hostPath") return `Host path ${backing.path}`;
  return backing.type;
}

export default function BuildCachePanel({ canManage }) {
  const [cache, setCache] = useState(null);
  const [services, setServices] = useState([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState(null);
  const timer = useRef(null);

  const load = useCallback(async () => {
    try {
      const data = await getCiCache();
      setCache(data);
      setError("");
      return data;
    } catch (err) {
      setError(err.message || "Could not read the build cache state.");
      return null;
    }
  }, []);

  useEffect(() => {
    load();
    // Needed to turn a usage row into something cleanable: du knows the slug,
    // only the catalog knows the id behind it.
    listCiServices()
      .then((data) => setServices(data.items || []))
      .catch(() => setServices([]));
  }, [load]);

  // A measure or clean runs as a Job, so the answer arrives later. Poll only
  // while one is in flight, then stop.
  useEffect(() => {
    const running = cache?.maintenance?.phase === "running";
    if (!running) {
      if (timer.current) window.clearTimeout(timer.current);
      return undefined;
    }
    timer.current = window.setTimeout(load, 4000);
    return () => timer.current && window.clearTimeout(timer.current);
  }, [cache, load]);

  const run = async (key, action, successText) => {
    setBusy(key);
    setError("");
    setNotice("");
    try {
      await action();
      const next = await load();
      if (successText && next) setNotice(successText);
    } catch (err) {
      setError(err.message || "That did not work.");
    } finally {
      setBusy("");
    }
  };

  if (!cache) {
    return (
      <section className="sg-ci-cache">
        <h4 className="sg-ci-cache-title">Build cache</h4>
        <p className="muted sg-ci-run-note">{error || "Loading…"}</p>
      </section>
    );
  }

  const chip = phaseChip(cache);
  const hasVolume = Boolean(cache.claim?.exists);
  const usage = cache.maintenance?.usage || [];
  const total = usage.find((row) => row.service === "(total)");
  const perService = usage.filter((row) => row.service !== "(total)");
  const startCreate = () =>
    setForm({
      backing: cache.suggestions?.backing || "nfs",
      nfsServer: cache.suggestions?.nfsServer || "",
      nfsPath: cache.suggestions?.nfsPath || "",
      node: "",
      path: "/var/lib/kubesight/ci-cache",
      size: cache.suggestions?.size || "20Gi",
      namespace: cache.buildNamespace,
      claimName: cache.claimName,
    });

  return (
    <section className="sg-ci-cache">
      <div className="sg-ci-cache-head">
        <h4 className="sg-ci-cache-title">Build cache</h4>
        <span className={`chip${chip.tone ? ` is-${chip.tone}` : ""}`}>{chip.text}</span>
        {/* Which of the two configuration paths is in charge, so nobody hunts
            for a switch that a ConfigMap is overriding. */}
        <span className="sg-ci-cache-source muted">
          {cache.source === "settings" ? "set here" : "set by CI_CACHE_* env"}
        </span>
      </div>

      <p className="muted sg-ci-cache-lead">
        One volume mounted at {cache.mountPath} by every stage, with Maven, Gradle, npm, yarn,
        pnpm, pip, Go, Cargo, Composer and NuGet pointed into it. Each service caches under{" "}
        <code>{cache.mountPath}/&lt;service&gt;/</code>.
      </p>

      {error && <p className="banner-message error">{error}</p>}
      {notice && <p className="banner-message">{notice}</p>}
      {cache.warnings?.map((warning) => (
        <p key={warning} className="banner-message warning-banner">
          {warning}
        </p>
      ))}

      {hasVolume ? (
        <dl className="sg-ci-cache-facts">
          <div>
            <dt>Claim</dt>
            <dd>
              {cache.claimName} · {cache.claim.phase || "unknown"} ·{" "}
              {cache.claim.capacity || cache.size}
            </dd>
          </div>
          <div>
            <dt>Namespace</dt>
            <dd>{cache.namespace}</dd>
          </div>
          {cache.volume?.exists && (
            <div>
              <dt>Backing store</dt>
              <dd>{backingLine(cache.volume)}</dd>
            </div>
          )}
          {total && (
            <div>
              <dt>Used</dt>
              <dd>{total.size}</dd>
            </div>
          )}
        </dl>
      ) : (
        cache.clusterReachable !== false && (
          <p className="sg-ci-cache-empty">
            No cache volume yet — every build downloads its dependencies from scratch.
          </p>
        )
      )}

      {canManage && form && (
        <form
          className="sg-ci-cache-form"
          onSubmit={(event) => {
            event.preventDefault();
            setCreating(true);
            run("create", () => createCiCacheVolume(form), "Volume created.")
              .then(() => setForm(null))
              .finally(() => setCreating(false));
          }}
        >
          <fieldset className="sg-ci-cache-backing">
            <legend>Backing store</legend>
            {BACKINGS.map((option) => (
              <label key={option.value}>
                <input
                  type="radio"
                  name="backing"
                  value={option.value}
                  checked={form.backing === option.value}
                  onChange={() => setForm({ ...form, backing: option.value })}
                />
                <span>{option.label}</span>
                <em className="muted">{option.hint}</em>
              </label>
            ))}
          </fieldset>

          {form.backing === "nfs" ? (
            <div className="sg-ci-cache-fields">
              <label>
                NFS server
                <input
                  value={form.nfsServer}
                  placeholder="10.4.27.17"
                  onChange={(event) => setForm({ ...form, nfsServer: event.target.value })}
                  required
                />
              </label>
              <label>
                Export path
                <input
                  value={form.nfsPath}
                  placeholder="/datauat/NFS-DATA/ci-cache"
                  onChange={(event) => setForm({ ...form, nfsPath: event.target.value })}
                  required
                />
              </label>
            </div>
          ) : (
            <div className="sg-ci-cache-fields">
              <label>
                Node
                <input
                  value={form.node}
                  placeholder="worker-1"
                  onChange={(event) => setForm({ ...form, node: event.target.value })}
                  required
                />
              </label>
              <label>
                Directory on that node
                <input
                  value={form.path}
                  onChange={(event) => setForm({ ...form, path: event.target.value })}
                  required
                />
              </label>
            </div>
          )}

          <div className="sg-ci-cache-fields">
            <label>
              Size
              <input
                value={form.size}
                placeholder="20Gi"
                onChange={(event) => setForm({ ...form, size: event.target.value })}
              />
            </label>
            <label>
              Namespace
              <input
                value={form.namespace}
                onChange={(event) => setForm({ ...form, namespace: event.target.value })}
              />
            </label>
            <label>
              Claim name
              <input
                value={form.claimName}
                onChange={(event) => setForm({ ...form, claimName: event.target.value })}
              />
            </label>
          </div>

          {form.namespace !== cache.buildNamespace && (
            <p className="banner-message warning-banner">
              Builds run in {cache.buildNamespace}. A claim in {form.namespace} cannot be mounted
              by them, so the cache would be ignored.
            </p>
          )}

          <p className="muted sg-ci-cache-note">
            The directory must exist and belong to uid {cache.suggestions?.buildUid || 65532}{" "}
            before a build can write to it — on NFS, do it on the server, because root_squash
            refuses the chown Kubernetes would otherwise attempt.
          </p>

          <div className="sg-ci-cache-actions">
            <button type="submit" className="primary" disabled={creating}>
              {creating ? "Creating…" : "Create"}
            </button>
            <button
              type="button"
              className="btn-outline"
              disabled={creating}
              onClick={() => setForm(null)}
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {/* One row, one primary. Which action is primary depends on what is
          missing: with no volume the thing to do is create it, and the switch
          is the secondary control beside it. */}
      {canManage && !form && (
        <div className="sg-ci-cache-actions">
          {!hasVolume && (
            <button type="button" className="primary" onClick={startCreate}>
              Create cache volume
            </button>
          )}
          {/* Hidden only when we can see for certain that there is nothing to
              cache into: the switch would then just report that back. */}
          {(hasVolume || cache.enabled || cache.clusterReachable === false) && (
            <button
              type="button"
              className={hasVolume && !cache.enabled ? "primary" : "btn-outline"}
              aria-pressed={cache.enabled}
              disabled={Boolean(busy)}
              onClick={() =>
                run(
                  "toggle",
                  () => setCiCacheEnabled(!cache.enabled),
                  cache.enabled
                    ? "Caching off. Nothing was deleted — turning it back on picks up the same cache."
                    : "Caching on."
                )
              }
            >
              {busy === "toggle"
                ? "Saving…"
                : cache.enabled
                  ? "Turn caching off"
                  : "Turn caching on"}
            </button>
          )}

          {hasVolume && (
            <button
              type="button"
              className="btn-outline btn-compact"
              disabled={Boolean(busy) || cache.maintenance?.phase === "running"}
              title="Runs du against the volume; the result appears here when it finishes"
              onClick={() => run("measure", measureCiCache)}
            >
              {busy === "measure" ? "Starting…" : "Measure"}
            </button>
          )}
          {hasVolume && (
          <button
            type="button"
            className="btn-outline btn-compact danger"
            disabled={Boolean(busy) || cache.maintenance?.phase === "running"}
            onClick={() => {
              if (
                !window.confirm(
                  "Empty the cache for every service? The next build of each one re-downloads " +
                    "its dependencies and is slow once. Artifacts and logs are not touched."
                )
              )
                return;
              run("clean", () => cleanCiCache({ all: true }));
            }}
          >
            Clean all
          </button>
          )}
        </div>
      )}

      {cache.maintenance?.phase === "running" && (
        <p className="sg-ci-cache-note">
          {cache.maintenance.kind === "clean" ? "Emptying" : "Measuring"} the cache…
        </p>
      )}
      {cache.maintenance?.phase === "failed" && (
        <p className="banner-message error">
          The last cache {cache.maintenance.kind || "job"} failed.
          {cache.maintenance.output ? ` ${cache.maintenance.output.slice(-300)}` : ""}
        </p>
      )}

      {perService.length > 0 && (
        <table className="sg-ci-cache-usage">
          <thead>
            <tr>
              <th>Service</th>
              <th>Cache size</th>
              {canManage && <th aria-label="Actions" />}
            </tr>
          </thead>
          <tbody>
            {perService.map((row) => {
              const service = services.find((item) => item.slug === row.service);
              return (
                <tr key={row.service}>
                  <td>{service?.name || row.service}</td>
                  <td>{row.size}</td>
                  {canManage && (
                    <td>
                      {service ? (
                        <button
                          type="button"
                          className="btn-outline btn-compact"
                          disabled={Boolean(busy) || cache.maintenance?.phase === "running"}
                          onClick={() => {
                            if (
                              !window.confirm(
                                `Empty the cache for ${service.name}? Its next build ` +
                                  "re-downloads its dependencies and is slow once."
                              )
                            )
                              return;
                            run("clean", () => cleanCiCache({ serviceId: service.id }));
                          }}
                        >
                          Clean
                        </button>
                      ) : (
                        // A directory with no service behind it: the service was
                        // deleted and its cache outlived it. Clean all removes it.
                        <span className="muted">no such service</span>
                      )}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
