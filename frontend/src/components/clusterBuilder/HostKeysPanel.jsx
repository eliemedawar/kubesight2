/** Host keys — what makes the "strict" and "pinned" host-key policies usable.
 *
 *  Without this panel a pinned route could never connect (nothing could mark a
 *  key pre-approved) and a strict route only worked after a TOFU run had
 *  already trusted whatever answered. The flow is: fetch the fingerprint the
 *  host presents (nothing is trusted by fetching), compare it on the host with
 *  ssh-keygen, then pin it. Every pin, replacement and removal is audit-logged
 *  server-side.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Field } from "./common.jsx";
import {
  deleteSshHostKey,
  listSshHostKeys,
  pinSshHostKey,
  scanSshHostKey,
} from "../../api/clusterBuildsApi.js";
import {
  fingerprintCommand,
  fingerprintError,
  scanVerdict,
  sourceLabel,
  sourceTone,
} from "../../utils/sshHostKeys.js";
import { timeAgo } from "../../utils/clusterBuilder.js";

const EMPTY_SCAN = { host: "", port: 22, profileId: "" };
const EMPTY_MANUAL = { host: "", port: 22, keyType: "ssh-ed25519", fingerprint: "" };

export default function HostKeysPanel({ profiles = [], notify }) {
  const [keys, setKeys] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [scanForm, setScanForm] = useState({ ...EMPTY_SCAN });
  const [scan, setScan] = useState(null);
  const [manualOpen, setManualOpen] = useState(false);
  const [manual, setManual] = useState({ ...EMPTY_MANUAL });

  // A ref, so a parent that re-creates `notify` each render cannot turn the
  // mount-time load into a fetch loop.
  const notifyRef = useRef(notify);
  notifyRef.current = notify;

  const reload = useCallback(async () => {
    try {
      const data = await listSshHostKeys();
      setKeys(data.items || []);
    } catch (error) {
      notifyRef.current?.(error.message || String(error), true);
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => { reload(); }, [reload]);

  const act = async (fn, after) => {
    setBusy(true);
    try {
      await fn();
      await reload();
      if (after) after();
    } catch (error) {
      notify?.(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };

  const runScan = () => act(async () => {
    setScan(null);
    const result = await scanSshHostKey({
      host: scanForm.host.trim(),
      port: Number(scanForm.port) || 22,
      profileId: scanForm.profileId ? Number(scanForm.profileId) : undefined,
    });
    setScan(result);
  });

  const pinScanned = () => {
    if (!scan) return;
    const replace = scan.status === "changed";
    if (replace && !window.confirm(
      `Replace the recorded ${scan.keyType} key for ${scan.host}:${scan.port}? `
      + "Only do this if the machine was legitimately rebuilt."
    )) return;
    act(
      () => pinSshHostKey({
        host: scan.host,
        port: scan.port,
        keyType: scan.keyType,
        fingerprint: scan.fingerprintSha256,
        replace,
      }),
      () => {
        notify?.(`Pinned ${scan.keyType} for ${scan.host}:${scan.port}.`);
        setScan(null);
      },
    );
  };

  const manualError = manualOpen ? fingerprintError(manual.fingerprint) : "";
  const manualValid = manual.host.trim() && manual.keyType.trim() && !manualError;

  const verdict = scanVerdict(scan);
  const bastionRoutes = profiles.filter((row) => row.routeMode === "bastion");

  return (
    <details className="sg-cb-subtle">
      <summary>
        Host keys
        <span className="muted">
          {keys.length
            ? `${keys.filter((k) => k.source === "preapproved").length} pinned · ${keys.filter((k) => k.source !== "preapproved").length} trusted on first use`
            : "pin fingerprints so strict and pinned routes can connect"}
        </span>
        <span className="sg-cb-chev" aria-hidden="true">›</span>
      </summary>
      <div className="sg-cb-subtle-body">
        <div className="sg-cb-qgrid sg-cb-addform">
          <Field label="Host" htmlFor="hk-host" hint="The address the route will connect to.">
            <input id="hk-host" className="sg-cb-input sg-cb-mono" value={scanForm.host}
                   placeholder="10.0.0.11"
                   onChange={(e) => setScanForm({ ...scanForm, host: e.target.value })} />
          </Field>
          <Field label="SSH port" htmlFor="hk-port">
            <input id="hk-port" type="number" className="sg-cb-input" value={scanForm.port}
                   onChange={(e) => setScanForm({ ...scanForm, port: e.target.value })} />
          </Field>
          {bastionRoutes.length ? (
            <Field label="Reach it via" htmlFor="hk-route"
                   hint="Only needed when the host is behind a bastion.">
              <select id="hk-route" className="sg-cb-input" value={scanForm.profileId}
                      onChange={(e) => setScanForm({ ...scanForm, profileId: e.target.value })}>
                <option value="">Direct</option>
                {bastionRoutes.map((row) => (
                  <option key={row.id} value={row.id}>{row.name} (via {row.bastionHost})</option>
                ))}
              </select>
            </Field>
          ) : null}
          <div className="sg-cb-actions sg-cb-span">
            <button className="btn-ghost" type="button"
                    onClick={() => setManualOpen(!manualOpen)}>
              {manualOpen ? "Close manual entry" : "Enter a fingerprint by hand"}
            </button>
            <button className="primary" type="button"
                    disabled={busy || !scanForm.host.trim()} onClick={runScan}>
              Fetch fingerprint
            </button>
          </div>
        </div>

        {scan && verdict ? (
          <div className={`sg-cb-entry ${scan.status === "changed" ? "is-bad" : ""}`}
               role="status" aria-live="polite">
            <span className="sg-cb-entry-id">
              <span className="en">{scan.host}:{scan.port} · {scan.keyType}</span>
              <span className="ea sg-cb-mono">{scan.fingerprint}</span>
              <span className="ea">
                {verdict[2]} On the host: <span className="sg-cb-mono">{fingerprintCommand(scan.keyType)}</span>
              </span>
              {scan.status === "changed" && scan.recorded ? (
                <span className="ea sg-cb-mono">recorded: {scan.recorded.fingerprint}</span>
              ) : null}
            </span>
            <span className="sg-cb-entry-right">
              <span className={`sg-cb-pill ${verdict[1]}`}>{verdict[0]}</span>
              {scan.status === "match" && scan.recorded?.source === "preapproved" ? null : (
                <button className="btn-outline btn-sm" type="button" disabled={busy}
                        onClick={pinScanned}>
                  {scan.status === "changed" ? "Replace and pin" : "Pin this fingerprint"}
                </button>
              )}
              <button className="btn-ghost btn-sm" type="button" onClick={() => setScan(null)}>
                Dismiss
              </button>
            </span>
          </div>
        ) : null}

        {manualOpen ? (
          <div className="sg-cb-qgrid sg-cb-addform">
            <Field label="Host" htmlFor="hk-m-host">
              <input id="hk-m-host" className="sg-cb-input sg-cb-mono" value={manual.host}
                     onChange={(e) => setManual({ ...manual, host: e.target.value })} />
            </Field>
            <Field label="SSH port" htmlFor="hk-m-port">
              <input id="hk-m-port" type="number" className="sg-cb-input" value={manual.port}
                     onChange={(e) => setManual({ ...manual, port: e.target.value })} />
            </Field>
            <Field label="Key type" htmlFor="hk-m-type"
                   hint="As the host presents it: ssh-ed25519, ecdsa-sha2-nistp256, ssh-rsa.">
              <input id="hk-m-type" className="sg-cb-input sg-cb-mono" value={manual.keyType}
                     onChange={(e) => setManual({ ...manual, keyType: e.target.value })} />
            </Field>
            <Field label="Fingerprint" htmlFor="hk-m-fp" error={manual.fingerprint ? manualError : ""}
                   hint="SHA256:… exactly as ssh-keygen -lf prints it.">
              <input id="hk-m-fp" className="sg-cb-input sg-cb-mono" value={manual.fingerprint}
                     aria-invalid={Boolean(manual.fingerprint && manualError)}
                     onChange={(e) => setManual({ ...manual, fingerprint: e.target.value })} />
            </Field>
            <div className="sg-cb-actions sg-cb-span">
              <button className="primary" type="button" disabled={busy || !manualValid}
                      onClick={() => act(
                        () => pinSshHostKey({
                          host: manual.host.trim(),
                          port: Number(manual.port) || 22,
                          keyType: manual.keyType.trim(),
                          fingerprint: manual.fingerprint.trim(),
                        }),
                        () => { setManual({ ...EMPTY_MANUAL }); setManualOpen(false); },
                      )}>
                Pin fingerprint
              </button>
            </div>
          </div>
        ) : null}

        {keys.length ? keys.map((row) => (
          <div className="sg-cb-entry" key={row.id}>
            <span className="sg-cb-entry-id">
              <span className="en">{row.host}:{row.port} · {row.keyType}</span>
              <span className="ea sg-cb-mono">{row.fingerprint}</span>
            </span>
            <span className="sg-cb-entry-right">
              <span className={`sg-cb-pill ${sourceTone(row.source)}`}>{sourceLabel(row.source)}</span>
              <span className="sg-cb-fresh">
                {row.source === "preapproved"
                  ? `pinned${row.approvedBy ? ` by ${row.approvedBy}` : ""} ${timeAgo(row.approvedAt)}`
                  : `first seen ${timeAgo(row.createdAt)}`}
              </span>
              {row.source !== "preapproved" ? (
                <button className="btn-ghost btn-sm" type="button" disabled={busy}
                        title="Approve the key recorded on first use as-is. Compare it on the host first."
                        onClick={() => act(() => pinSshHostKey({
                          host: row.host,
                          port: row.port,
                          keyType: row.keyType,
                          fingerprint: row.fingerprintSha256,
                        }))}>
                  Pin
                </button>
              ) : null}
              <button className="btn-ghost btn-sm" type="button" disabled={busy}
                      onClick={() => {
                        if (window.confirm(
                          `Forget the ${row.keyType} key for ${row.host}:${row.port}? `
                          + "Strict and pinned routes will refuse the host until it is pinned again."
                        )) act(() => deleteSshHostKey(row.id));
                      }}>
                Remove
              </button>
            </span>
          </div>
        )) : loaded ? (
          <p className="muted">
            No host keys recorded. Trust-on-first-use routes record them as they connect;
            strict and pinned routes need one pinned here first.
          </p>
        ) : null}
      </div>
    </details>
  );
}
