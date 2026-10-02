import { CONDITIONAL_STAGE_TYPES, IMAGE_SCAN_THRESHOLDS } from "../ciShared.jsx";
import { ChipsInput, Field, Segmented } from "./controls.jsx";
import { PlIcon } from "./icons.jsx";
import {
  defaultScan,
  SBOM_FORMATS,
  SCAN_ON_FAIL,
  SCAN_TOOLS,
  scanArtifact,
  scanToolPatch,
  TRIVY_SCANNERS,
} from "./scanModel.js";

/**
 * A Scan stage: pick a scanner, set its gate, see what it leaves on the build.
 *
 * There is no image or command to edit — KubeSight writes both from the tool
 * (services/ci/scan_stage.py) — so the sheet asks only the questions a person
 * actually has an answer to, and says where the scanner runs and what it needs
 * mirrored. `gate` is the code scan quality gate, rendered by the caller,
 * shown for Semgrep exactly as on a command stage.
 */
export default function ScanStageFields({ ids, stage, index, scanTools, secretKeys, editable, onChange, error, gate }) {
  const scan = stage.scan || null;
  const tool = scan?.tool || "";
  const catalog = (scanTools || []).find((item) => item.tool === tool) || null;
  const setScan = (patch) => onChange({ scan: { ...(defaultScan(tool) || {}), ...(scan || {}), ...patch } });
  const artifact = scanArtifact(scan, index);

  return (
    <>
      <Field label="Scan with" error={!tool ? error : undefined}>
        <div className="pl-scan-tools" role="radiogroup" aria-label="Scanner">
          {SCAN_TOOLS.map((item) => {
            const on = item.value === tool;
            return (
              <button
                key={item.value}
                type="button"
                role="radio"
                aria-checked={on}
                disabled={!editable}
                className={`btn-ghost pl-scan-tool${on ? " is-on" : ""}`}
                onClick={() => onChange(scanToolPatch(stage, item.value))}
              >
                <span className="pl-scan-tool-head">
                  <PlIcon name={item.value === "syft" ? "file" : "scan"} />
                  <strong>{item.label}</strong>
                  {on && <PlIcon name="check" className="pl-scan-tool-check" />}
                </span>
                <small>{item.sentence}</small>
              </button>
            );
          })}
        </div>
      </Field>

      {tool === "trivy_fs" && <TrivyOptions ids={ids} scan={scan} editable={editable} setScan={setScan} />}
      {tool === "semgrep" && (
        <>
          <Field
            label="Rules"
            optional
            hint={
              <>
                Empty uses{" "}
                <code>{(catalog?.defaultRules || ["p/default"]).join(" ")}</code> for this service&apos;s
                application type — or rules committed as <code>.semgrep.yml</code> /{" "}
                <code>.semgrep/</code> when the repository has them. Rules named here are used as named.
              </>
            }
          >
            <ChipsInput
              label="Semgrep rules"
              value={scan.rules || []}
              lowercase={false}
              placeholder={(catalog?.defaultRules || ["p/default"]).join(", ")}
              disabled={!editable}
              onChange={(rules) => setScan({ rules })}
            />
          </Field>
          {gate}
        </>
      )}
      {tool === "dependency_check" && (
        <DependencyCheckOptions ids={ids} scan={scan} secretKeys={secretKeys} editable={editable} setScan={setScan} />
      )}
      {tool === "syft" && (
        <>
          <Field label="SBOM format">
            <Segmented
              label="SBOM format"
              value={scan.format || "cyclonedx-json"}
              options={SBOM_FORMATS}
              disabled={!editable}
              onChange={(format) => setScan({ format })}
            />
          </Field>
          <div className="pl-note is-muted">
            <PlIcon name="file" />
            <p>
              <strong>Describes the source tree.</strong> An SBOM of the image this build pushes is
              not offered: reading the image back would put the registry&apos;s push credential in a
              scanner container, and KubeSight keeps it in the one stage that pushes. For the
              image&apos;s own vulnerabilities, turn on the scan gate on the container image stage.
            </p>
          </div>
        </>
      )}

      {tool && (
        <div className="pl-scan-facts">
          {artifact && (
            <p>
              <PlIcon name="file" />
              <span>
                Leaves <code>{artifact.name}</code> on the build as a{" "}
                {artifact.type === "sbom" ? "SBOM" : "scan report"} — kept even when the scan fails the
                stage{tool === "semgrep" ? ", with the PDF report and Send dialog" : ""}.
              </span>
            </p>
          )}
          <p className={catalog && !catalog.configured ? "is-warn" : undefined}>
            <PlIcon name={catalog && !catalog.configured ? "alert" : "server"} />
            <span>
              {catalog?.image ? (
                <>
                  Runs in <code>{catalog.image}</code>. Mirror it to the cluster&apos;s registry
                  {catalog.imageVariable ? (
                    <>
                      {" "}or point <code>{catalog.imageVariable}</code> at your mirror
                    </>
                  ) : null}
                  .
                </>
              ) : (
                <>
                  No image is configured for this scanner
                  {catalog?.imageVariable ? (
                    <>
                      {" "}— set <code>{catalog.imageVariable}</code>
                    </>
                  ) : null}
                  ; until then the stage fails and says so.
                </>
              )}
            </span>
          </p>
          <p>
            <PlIcon name="clock" />
            <span>{CONDITIONAL_STAGE_TYPES.scan} Applies from the next build.</span>
          </p>
        </div>
      )}
    </>
  );
}

function TrivyOptions({ ids, scan, editable, setScan }) {
  const scanners = scan.scanners?.length ? scan.scanners : ["vuln", "secret"];
  const toggle = (value, on) => {
    const next = TRIVY_SCANNERS.map((item) => item.value).filter((item) =>
      item === value ? on : scanners.includes(item)
    );
    // Never none: an empty list would scan for the defaults on save anyway.
    if (next.length) setScan({ scanners: next });
  };
  return (
    <div className="pl-scan pl-scan-gate is-on">
      <div className="pl-scan-body is-flush">
        <Field label="Look for">
          <div className="pl-scan-checks">
            {TRIVY_SCANNERS.map((item) => (
              <label key={item.value} className="pl-check">
                <input
                  type="checkbox"
                  checked={scanners.includes(item.value)}
                  disabled={!editable || (scanners.length === 1 && scanners.includes(item.value))}
                  onChange={(event) => toggle(item.value, event.target.checked)}
                />
                <span>
                  <strong>{item.label}</strong>
                </span>
              </label>
            ))}
          </div>
        </Field>
        <Field label="Gate at" hint="Every severity is in the report either way; this is what the gate counts.">
          <Segmented
            label="Severity the gate counts"
            value={scan.threshold || "critical"}
            options={IMAGE_SCAN_THRESHOLDS.map((item) => ({ value: item.value, label: item.label }))}
            disabled={!editable}
            onChange={(threshold) => setScan({ threshold })}
          />
        </Field>
        <Field
          label="When something is found"
          hint={
            (scan.onFail || "block") === "block"
              ? "The stage fails, and so does the build unless it keeps going."
              : "The report is kept and the stage passes — a gate that only reports."
          }
        >
          <Segmented
            label="When something is found"
            value={scan.onFail || "block"}
            options={SCAN_ON_FAIL}
            disabled={!editable}
            onChange={(onFail) => setScan({ onFail })}
          />
        </Field>
        {scanners.includes("vuln") && (
          <label className="pl-check">
            <input
              type="checkbox"
              checked={Boolean(scan.ignoreUnfixed)}
              disabled={!editable}
              onChange={(event) => setScan({ ignoreUnfixed: event.target.checked })}
            />
            <span>
              <strong>Ignore vulnerabilities with no fix available</strong>
              <small>Nothing to upgrade to means nothing anybody can do in this build. Off by default.</small>
            </span>
          </label>
        )}
        <Field
          label="Skip"
          htmlFor={`${ids}-skipdirs`}
          optional
          hint="Directories or globs Trivy does not walk. node_modules is described by the lockfile already."
        >
          <ChipsInput
            label="Directories to skip"
            value={scan.skipDirs || []}
            lowercase={false}
            placeholder="**/node_modules, **/.git"
            disabled={!editable}
            onChange={(skipDirs) => setScan({ skipDirs })}
          />
        </Field>
      </div>
    </div>
  );
}

function DependencyCheckOptions({ ids, scan, secretKeys, editable, setScan }) {
  const score = scan.failOnCvss === "" ? "" : scan.failOnCvss ?? 7;
  const hasDefaultKey = (secretKeys || []).some((item) => item.key === "NVD_API_KEY");
  return (
    <div className="pl-scan pl-scan-gate is-on">
      <div className="pl-scan-body is-flush">
        <div className="pl-grid">
          <Field
            label="Fail on CVSS"
            htmlFor={`${ids}-cvss`}
            hint={`Any vulnerability scoring ${Number(score || 0).toFixed(1)} or more trips the gate. 7 is "high"; 9 is "critical".`}
          >
            <input
              id={`${ids}-cvss`}
              type="number"
              min={0}
              max={10}
              step={0.1}
              className="pl-scan-score"
              value={score}
              disabled={!editable}
              onChange={(event) =>
                setScan({ failOnCvss: event.target.value === "" ? "" : Number(event.target.value) })
              }
            />
          </Field>
          <Field label="When something is found">
            <Segmented
              label="When something is found"
              value={scan.onFail || "block"}
              options={SCAN_ON_FAIL}
              disabled={!editable}
              onChange={(onFail) => setScan({ onFail })}
            />
          </Field>
          <Field
            label="NVD API key"
            htmlFor={`${ids}-nvdkey`}
            optional
            hint="Without a key NVD rate-limits the database download, and the first run takes far longer."
          >
            <select
              id={`${ids}-nvdkey`}
              value={scan.nvdApiKeySecret || ""}
              disabled={!editable}
              onChange={(event) => setScan({ nvdApiKeySecret: event.target.value })}
            >
              <option value="">
                {hasDefaultKey ? "NVD_API_KEY (this service's secret)" : "None — use NVD_API_KEY if it is added"}
              </option>
              {(secretKeys || [])
                .filter((item) => item.key !== "NVD_API_KEY")
                .map((item) => (
                  <option key={item.key} value={item.key}>
                    {item.key}
                  </option>
                ))}
            </select>
          </Field>
          <Field
            label="NVD data mirror"
            htmlFor={`${ids}-nvdmirror`}
            optional
            hint="For a cluster with no route to nvd.nist.gov. A NVD_DATAFEED_URL secret wins over this."
          >
            <input
              id={`${ids}-nvdmirror`}
              className="is-mono"
              value={scan.nvdDatafeedUrl || ""}
              placeholder="https://nexus.example.com/nvd/"
              disabled={!editable}
              spellCheck={false}
              onChange={(event) => setScan({ nvdDatafeedUrl: event.target.value })}
            />
          </Field>
        </div>
        <p className="pl-field-hint">
          Shares the NVD database (and its one-scan-at-a-time lock) with merge checks, in the build
          cache. Java services: put this stage after the one that builds — Dependency-Check reads
          jars, not the build file.
        </p>
      </div>
    </div>
  );
}
