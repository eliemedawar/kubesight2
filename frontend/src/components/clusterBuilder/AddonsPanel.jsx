/** Day two: installing add-ons on a cluster that is already running.
 *
 *  The wizard's shelf, with what the cluster already runs locked in place.
 *  Only the new selections are sent; the backend applies those alone and
 *  proves them the same way the build does — nothing already installed is
 *  re-applied, and no other phase runs.
 */

import { useState } from "react";
import { AddonShelf } from "./Wizard.jsx";
import { addonSelectionError } from "../../utils/addonConfig.js";
import { addonDisplayName } from "../../utils/clusterBuilder.js";
import { addClusterBuildAddons } from "../../api/clusterBuildsApi.js";

/** What each add-on changes on a live cluster — said before, not discovered after. */
const LIVE_NOTES = {
  "metrics-server":
    "Metrics Server needs CA-signed kubelet certificates. If this cluster was built "
    + "without it, every kubelet is switched to serving-certificate bootstrap and "
    + "restarted once — running pods are not touched.",
  "nginx-ingress": "NGINX Ingress is exposed as a NodePort Service on every node.",
  metallb:
    "MetalLB hands out addresses from the pool to LoadBalancer Services — pick a "
    + "range nothing on this network already uses.",
};

export default function AddonsPanel({
  build, catalog, canExecute, notify, onChanged, onClose,
}) {
  const installed = build.addons || [];
  const [value, setValue] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const configError = addonSelectionError(value, catalog);
  const remaining = catalog.filter(
    (entry) => !installed.some((addon) => addon.id === entry.id)
  );
  const canApply = Boolean(value.length && canExecute && !busy && !configError);
  const names = value.map((addon) => addonDisplayName(addon, catalog));

  const apply = async () => {
    setBusy(true);
    setError("");
    try {
      await addClusterBuildAddons(build.id, value);
      notify(`Installing ${names.join(", ")} on ${build.name}.`);
      onChanged();
      onClose();
    } catch (exception) {
      // Inline as well as page-level: this panel sits below the fold.
      const message = exception.message || String(exception);
      setError(message);
      notify(message, true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card sg-cb-grow">
      <div className="sg-cb-sect">
        <h2>Add plugins</h2>
        <span className="sg-cb-sect-right">
          <button className="btn-ghost btn-sm" type="button" onClick={onClose}>Close</button>
        </span>
      </div>
      {error ? <p className="sg-cb-grow-error" role="alert">{error}</p> : null}

      <p className="muted sg-cb-grow-lede">
        The same pinned, digest-checked plugins the wizard offers, installed on this
        running cluster over the build&apos;s own SSH route. Only what you tick here is
        applied — plugins already on the cluster are left exactly as they are — and
        each one must answer for real before it counts as installed.
      </p>

      <AddonShelf
        catalog={catalog}
        value={value}
        onChange={(next) => { setValue(next); setError(""); }}
        k8sVersion={build.k8sVersion}
        installed={installed}
      />

      {value.some((addon) => LIVE_NOTES[addon.id]) ? (
        <ul className="sg-cb-proofs">
          {value.filter((addon) => LIVE_NOTES[addon.id]).map((addon) => (
            <li className="sg-cb-proof is-todo" key={addon.id}>
              <span className="tick" aria-hidden="true">!</span>
              <span className="what">{addonDisplayName(addon, catalog)}</span>
              <span className="how">{LIVE_NOTES[addon.id]}</span>
            </li>
          ))}
        </ul>
      ) : null}

      <div className="sg-cb-actions">
        {configError ? <span className="sg-cb-field-error">{configError}</span> : null}
        <button className="primary" type="button" disabled={!canApply} onClick={apply}>
          {busy
            ? "Installing…"
            : value.length
              ? `Install ${names.join(", ")} on ${build.name}`
              : remaining.length
                ? "Select a plugin to install"
                : "Every plugin is already installed"}
        </button>
        {!canExecute ? (
          <span className="muted">
            Installing plugins needs the build-execute permission.
          </span>
        ) : null}
      </div>
    </div>
  );
}
