import { useState } from "react";

/**
 * What KubeSight believes this application is — and where it read it.
 *
 * Every detected value carries the file it came from, because the difference
 * between "Java 17" and "Java 17, from the toolchain block in build.gradle" is
 * the difference between a claim and a fact. A reader who can check the
 * evidence stops having to trust the feature, which is the only way they ever
 * start trusting it.
 *
 * Anything here is editable. A value the user changes is marked as theirs and
 * stays theirs through a regenerate, so nobody makes the same correction twice.
 */

const FIELDS = [
  ["language", "Language", "languageVersion"],
  ["framework", "Framework", "frameworkVersion"],
  ["buildSystem", "Build system", "buildSystemVersion"],
  ["packaging", "Packaging", null],
  ["packageManager", "Package manager", null],
  ["projectStructure", "Project structure", null],
];

const LANGUAGES = [
  "java", "kotlin", "scala", "groovy", "javascript", "typescript", "python",
  "go", "csharp", "php", "ruby", "rust", "dart", "swift", "objective-c",
  "shell", "other",
];
const BUILD_SYSTEMS = [
  "gradle", "maven", "ant", "npm", "yarn", "pnpm", "pip", "poetry", "pipenv",
  "setuptools", "go", "dotnet", "composer", "bundler", "cargo", "flutter",
  "xcodebuild", "swiftpm", "make", "docker", "none", "other",
];
const PACKAGING = [
  "jar", "war", "ear", "apk", "aab", "ipa", "wheel", "sdist", "tarball", "zip",
  "static-site", "bundle", "binary", "container-image", "none", "other",
];
const PACKAGE_MANAGERS = [
  "", "npm", "yarn", "pnpm", "pip", "poetry", "pipenv", "cocoapods", "swiftpm",
  "composer", "bundler", "cargo", "go", "other",
];
const STRUCTURES = ["single", "multi-module", "monorepo"];

const CHOICES = {
  language: LANGUAGES,
  buildSystem: BUILD_SYSTEMS,
  packaging: PACKAGING,
  packageManager: PACKAGE_MANAGERS,
  projectStructure: STRUCTURES,
};

const titleCase = (value) =>
  String(value || "").replace(/(^|[\s-])\w/g, (match) => match.toUpperCase());

/** A fact the analysis established without asking anybody. */
function Signal({ ok, children }) {
  return (
    <li className={ok ? "is-ok" : "is-absent"}>
      <span aria-hidden="true">{ok ? "✓" : "—"}</span>
      {children}
    </li>
  );
}

function Evidence({ entry }) {
  if (!entry?.source) return null;
  return (
    <span className="sg-ci-profile-evidence" title={entry.detail || ""}>
      {entry.confidence === "Confirmed" ? "read from" : `${entry.confidence} · from`}{" "}
      <code>{entry.source}</code>
    </span>
  );
}

export default function ApplicationProfileCard({
  profile,
  editable = false,
  onChange,
  onRegenerate,
  regenerating = false,
  compact = false,
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(profile || {});
  const [dirtyFields, setDirtyFields] = useState({});

  if (!profile) return null;

  const evidence = profile.evidence || {};
  const overrides = profile.overrides || {};
  const unknown = new Set(profile.unknown || []);

  const startEditing = () => {
    setDraft({ ...profile });
    setDirtyFields({});
    setEditing(true);
  };

  const set = (field, value) => {
    setDraft((prev) => ({ ...prev, [field]: value }));
    setDirtyFields((prev) => ({ ...prev, [field]: true }));
  };

  const apply = () => {
    // Only what actually changed travels as an override — sending the whole
    // profile back would mark every field as a user decision, and a "user
    // overridden" badge on a value nobody touched is a lie.
    const changed = {};
    Object.keys(dirtyFields).forEach((field) => {
      if (String(draft[field] ?? "") !== String(profile[field] ?? "")) {
        changed[field] = draft[field];
      }
    });
    setEditing(false);
    if (Object.keys(changed).length) onChange?.(changed);
  };

  const rows = FIELDS.map(([field, label, versionField]) => {
    const value = profile[field];
    const version = versionField ? profile[versionField] : "";
    const overridden = Boolean(overrides[field] || (versionField && overrides[versionField]));
    return { field, label, versionField, value, version, overridden };
  }).filter((row) => !compact || row.value);

  return (
    <section className="sg-ci-profile" aria-label="Application profile">
      <header className="sg-ci-profile-head">
        <div>
          <h4>Application</h4>
          {profile.source === "manual" && (
            <span className="sg-ci-profile-source">Configured by hand</span>
          )}
        </div>
        {editable && !editing && (
          <div className="sg-ci-profile-actions">
            <button type="button" className="btn-outline btn-compact" onClick={startEditing}>
              Edit application profile
            </button>
            {onRegenerate && (
              <button
                type="button"
                className="btn-outline btn-compact"
                onClick={onRegenerate}
                disabled={regenerating}
                title="Ask Hermes to build a pipeline around the profile as it stands now"
              >
                {regenerating ? "Regenerating…" : "Regenerate pipeline"}
              </button>
            )}
          </div>
        )}
      </header>

      {editing ? (
        <div className="form-grid sg-ci-profile-edit">
          {FIELDS.map(([field, label, versionField]) => (
            <label key={field}>
              {label}
              {CHOICES[field] ? (
                <select value={draft[field] || ""} onChange={(e) => set(field, e.target.value)}>
                  {CHOICES[field].map((option) => (
                    <option key={option || "none"} value={option}>
                      {option || "Not applicable"}
                    </option>
                  ))}
                </select>
              ) : (
                <input value={draft[field] || ""} onChange={(e) => set(field, e.target.value)} />
              )}
              {versionField && (
                <input
                  className="sg-ci-profile-version"
                  value={draft[versionField] || ""}
                  placeholder="version"
                  onChange={(e) => set(versionField, e.target.value)}
                />
              )}
            </label>
          ))}
          <label className="sg-ci-profile-check">
            <input
              type="checkbox"
              checked={Boolean(draft.usesBuildWrapper)}
              onChange={(e) => set("usesBuildWrapper", e.target.checked)}
            />
            Project ships a build wrapper (./gradlew, ./mvnw)
          </label>
          <label className="sg-ci-profile-check">
            <input
              type="checkbox"
              checked={Boolean(draft.testsDetected)}
              onChange={(e) => set("testsDetected", e.target.checked)}
            />
            Project has tests
          </label>
          <div className="form-grid__full sg-ci-profile-editactions">
            <button type="button" className="btn-outline" onClick={() => setEditing(false)}>
              Cancel
            </button>
            <button type="button" className="primary" onClick={apply}>
              Apply changes
            </button>
          </div>
          <p className="field-hint form-grid__full">
            Changing the build system or the language changes what a regenerated
            pipeline would run. Nothing already saved is rewritten.
          </p>
        </div>
      ) : (
        <>
          <dl className="sg-ci-profile-grid">
            {rows.map((row) => (
              <div key={row.field}>
                <dt>{row.label}</dt>
                <dd>
                  {row.value ? (
                    <>
                      <strong>{titleCase(row.value)}</strong>
                      {row.version && <span className="sg-ci-profile-ver">{row.version}</span>}
                      {row.overridden && (
                        <span
                          className="sg-ci-profile-badge"
                          title="You set this. A regenerate will build around it rather than detect over it."
                        >
                          user set
                        </span>
                      )}
                      {!row.overridden && (
                        <Evidence entry={evidence[row.versionField] || evidence[row.field]} />
                      )}
                    </>
                  ) : (
                    <span className="muted">
                      {unknown.has(row.field) || unknown.has(row.versionField)
                        ? "Could not be determined"
                        : "Not applicable"}
                    </span>
                  )}
                </dd>
              </div>
            ))}
          </dl>

          <ul className="sg-ci-profile-signals">
            <Signal ok={Boolean(profile.usesBuildWrapper)}>
              {profile.usesBuildWrapper ? "Build wrapper detected" : "No build wrapper"}
            </Signal>
            <Signal ok={Boolean(profile.testsDetected)}>
              {profile.testsDetected ? "Tests detected" : "No tests found"}
            </Signal>
            <Signal ok={profile.containerization?.type === "dockerfile"}>
              {profile.containerization?.type === "dockerfile"
                ? `Dockerfile detected${
                    profile.containerization.dockerfilePath
                      ? ` (${profile.containerization.dockerfilePath})`
                      : ""
                  }`
                : "No Dockerfile"}
            </Signal>
            {(profile.modules || []).length > 0 && (
              <Signal ok>
                {profile.modules.length} modules: {profile.modules.slice(0, 4).join(", ")}
                {profile.modules.length > 4 ? "…" : ""}
              </Signal>
            )}
          </ul>

          {/* Stated rather than hidden: a field nobody could establish is a
              question for the user, and pretending otherwise is how a wrong
              build image gets chosen. */}
          {unknown.size > 0 && (
            <p className="sg-ci-profile-unknown">
              Could not be determined from the repository:{" "}
              {[...unknown].join(", ")}. Edit the profile to supply them.
            </p>
          )}
        </>
      )}
    </section>
  );
}
