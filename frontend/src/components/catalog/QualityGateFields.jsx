/**
 * The quality gate's fields, in one component used in two places.
 *
 * The installation policy (Settings → Merge checks) and a service's override
 * (the Merge Checks tab) hold exactly the same fields and mean the same thing
 * by them. Two copies of this form would drift the moment a knob is added, and
 * the drift would be silent — a field present in one place and absent in the
 * other reads as "this installation does not have that setting".
 *
 * The layout follows the shape of the decision, which is one number and then
 * four refinements of it:
 *
 *   The TOTAL is the gate. It gets a field of its own, sized like a number
 *   rather than like a paragraph, and a sentence beside it that says in words
 *   what the number does — because "5" alone does not tell you whether 5 passes.
 *
 *   Each CHECK is one row: its limit and what it counts, together. Those two
 *   used to sit in different sections, so answering "what does ESLint block on"
 *   meant reading two halves of the form and holding one in your head.
 *
 * An empty box is a real value: "no limit" on the policy, "inherit" on a
 * service. Stated in the placeholder rather than implied, because a blank field
 * that quietly became 0 would block every merge in the installation.
 */

export const TOOLS = [
  {
    key: "eslint",
    capKey: "maxEslintProblems",
    label: "ESLint",
    hint: "Lints with the ESLint and config the project itself pins.",
    // What this check counts as a problem. ESLint is the odd one out: it grades
    // findings as error or warning rather than by severity, so its control is a
    // toggle where the others get a floor.
    counts: { type: "toggle", key: "eslintCountWarnings", label: "Count warnings too" },
  },
  {
    key: "semgrep",
    capKey: "maxSemgrepProblems",
    label: "Semgrep",
    hint: "Static analysis in the build container — no server to run.",
    counts: { type: "severity", key: "semgrepMinSeverity", fallback: "medium" },
  },
  {
    key: "sonar",
    capKey: "maxSonarProblems",
    label: "SonarQube",
    hint: "Counts open issues from your SonarQube server.",
    counts: { type: "severity", key: "sonarMinSeverity", fallback: "medium" },
  },
  {
    key: "dependency_check",
    capKey: "maxDependencyProblems",
    label: "Dependency-Check",
    hint: "OWASP dependency scan. The first run is slow.",
    counts: { type: "severity", key: "dependencyMinSeverity", fallback: "high" },
  },
];

const SEVERITIES = ["critical", "high", "medium", "low", "info"];

/** "" for null/undefined, so a blank box round-trips as "no opinion". */
const asText = (value) => (value === null || value === undefined ? "" : String(value));

/** "" back to null — never to 0. See the module comment. */
const asNumber = (raw) => (String(raw).trim() === "" ? null : Number(raw));

export default function QualityGateFields({
  values,
  disabled = false,
  onChange,
  /**
   * The resolved gate this form sits on top of. On a service that is the
   * policy's numbers, shown as the placeholder in each empty box so "inherit"
   * has a visible value rather than being an empty field and a guess.
   */
  inheritedFrom = null,
  /** Copy that differs between the policy form and a service's override. */
  scope = "service",
}) {
  const blankMeans = scope === "policy" ? "no limit" : "inherit";

  /** The value actually in force for a field, whether typed here or inherited. */
  const effective = (key) => {
    const own = values[key];
    if (own !== null && own !== undefined && own !== "") return own;
    return inheritedFrom ? inheritedFrom[key] : null;
  };

  const placeholder = (key) => {
    const inherited = inheritedFrom?.[key];
    return inherited === null || inherited === undefined
      ? blankMeans
      : `${inherited}`;
  };

  const total = effective("maxTotalProblems");

  return (
    <div className="sg-qg">
      {/* ── The number the whole gate is ─────────────────────────────── */}
      <div className="sg-qg-total">
        <label className="sg-qg-total-field">
          <span className="sg-qg-total-label">Maximum problems in total</span>
          <input
            type="number"
            min="0"
            inputMode="numeric"
            className="sg-qg-num sg-qg-num--hero"
            value={asText(values.maxTotalProblems)}
            disabled={disabled}
            placeholder={placeholder("maxTotalProblems")}
            onChange={(event) => onChange("maxTotalProblems", asNumber(event.target.value))}
          />
        </label>
        {/* The consequence in words, live. A cap is a maximum that PASSES, and
            that is the one thing about this field people get wrong. */}
        <p className="sg-qg-sentence">
          {total === null || total === undefined ? (
            <>
              No limit is set, so the checks report their findings and{" "}
              <strong>nothing is blocked</strong>.
            </>
          ) : (
            <>
              A pull request with <b className="sg-qg-n">{total}</b>{" "}
              {total === 1 ? "problem" : "problems"} passes;{" "}
              <b className="sg-qg-n">{Number(total) + 1}</b> blocks the merge.
            </>
          )}
        </p>
      </div>

      {/* ── One row per check ────────────────────────────────────────── */}
      <div className="sg-qg-section">
        <h5>Per-check limits</h5>
        <p className="muted">
          Applied on top of the total. Leave a limit blank and that check is bound
          only by the total above.
        </p>
      </div>

      <div className="sg-qg-table" role="table" aria-label="Per-check limits">
        <div className="sg-qg-row sg-qg-row--head" role="row">
          <span role="columnheader">Check</span>
          <span role="columnheader">Limit</span>
          <span role="columnheader">Counts</span>
        </div>

        {TOOLS.map(({ key, capKey, label, hint, counts }) => (
          <div className="sg-qg-row" role="row" key={key}>
            <div className="sg-qg-check" role="cell">
              <span className="sg-qg-name">{label}</span>
              <span className="sg-qg-hint">{hint}</span>
            </div>

            <div role="cell">
              <input
                type="number"
                min="0"
                inputMode="numeric"
                className="sg-qg-num"
                aria-label={`Maximum problems from ${label}`}
                value={asText(values[capKey])}
                disabled={disabled}
                placeholder={placeholder(capKey)}
                onChange={(event) => onChange(capKey, asNumber(event.target.value))}
              />
            </div>

            <div role="cell">
              {counts.type === "toggle" ? (
                <label className="sg-qg-toggle">
                  <input
                    type="checkbox"
                    checked={Boolean(effective(counts.key))}
                    disabled={disabled}
                    onChange={(event) => onChange(counts.key, event.target.checked)}
                  />
                  {counts.label}
                </label>
              ) : (
                <>
                  <select
                    className="sg-qg-sev"
                    aria-label={`${label} severity floor`}
                    value={asText(effective(counts.key)) || counts.fallback}
                    disabled={disabled}
                    onChange={(event) => onChange(counts.key, event.target.value)}
                  >
                    {SEVERITIES.map((item) => (
                      <option key={item} value={item}>
                        {item} and above
                      </option>
                    ))}
                  </select>
                </>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* ── The one that is not a number ─────────────────────────────── */}
      <label className="sg-qg-block">
        <input
          type="checkbox"
          checked={values.blockOnToolError !== false}
          disabled={disabled}
          onChange={(event) => onChange("blockOnToolError", event.target.checked)}
        />
        <span>
          <strong>Block the merge when a check could not run at all</strong>
          <span className="sg-qg-hint">
            On is the safe reading: a crashed scanner found nothing because it did
            not look, which is not the same as finding nothing.
          </span>
        </span>
      </label>
    </div>
  );
}
