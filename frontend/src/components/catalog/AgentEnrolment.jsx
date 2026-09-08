import { useState } from "react";
import AgentInstallHelp from "./AgentInstallHelp.jsx";

const PLATFORMS = [
  ["agent_linux", "Linux", "A build VM or host — licensed tools, Docker, anything a pod cannot give."],
  ["agent_macos", "macOS", "Required for iOS: Apple's toolchain only runs on Apple hardware."],
];

/**
 * Registering an agent.
 *
 * Capabilities are deliberately NOT asked for here — the agent reports what it
 * actually finds installed on every heartbeat. A list typed into a form goes
 * stale the first time somebody upgrades Xcode, and a stale list routes builds
 * to a machine that can no longer run them.
 */
export default function AgentEnrolment({ register, onRegistered, onCancel }) {
  const [name, setName] = useState("");
  const [runnerType, setRunnerType] = useState("agent_linux");
  const [maxConcurrent, setMaxConcurrent] = useState(1);
  const [workspaceRoot, setWorkspaceRoot] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [showHelp, setShowHelp] = useState(false);

  const submit = async () => {
    if (!name.trim() || saving) return;
    setSaving(true);
    setError("");
    try {
      onRegistered(
        await register({
          name: name.trim(),
          runnerType,
          maxConcurrent: Number(maxConcurrent) || 1,
          workspaceRoot: workspaceRoot.trim(),
        })
      );
    } catch (err) {
      setError(err.message || "Could not register the agent.");
      setSaving(false);
    }
  };

  return (
    <section className="sg-ci-agent-form">
      {error && <p className="banner-message error">{error}</p>}

      <div className="form-grid">
        <label>
          Agent name *
          <input
            value={name}
            placeholder="mac-mini-build-01"
            autoFocus
            maxLength={120}
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => event.key === "Enter" && submit()}
          />
          <span className="field-hint">Name it after the machine — that is what you look for later.</span>
        </label>
        <label>
          Concurrent builds
          <input
            type="number"
            min={1}
            max={20}
            value={maxConcurrent}
            onChange={(event) => setMaxConcurrent(event.target.value)}
          />
          <span className="field-hint">How many stages this machine runs at once.</span>
        </label>

        <label className="form-grid__full">
          Build directory
          <input
            value={workspaceRoot}
            placeholder={
              runnerType === "agent_macos" ? "/Users/builder/kubesight" : "/var/lib/kubesight-agent"
            }
            maxLength={512}
            onChange={(event) => setWorkspaceRoot(event.target.value)}
          />
          <span className="field-hint">
            Where the agent checks out builds, one directory per build. Leave empty for
            the agent's own default (<code>~/kubesight-agent</code>). KubeSight cannot
            check this path exists — the agent applies it and reports back if it cannot
            write there.
          </span>
        </label>
      </div>

      <div className="sg-ci-agent-platforms" role="radiogroup" aria-label="Platform">
        {PLATFORMS.map(([value, label, why]) => (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={runnerType === value}
            className={`sg-ci-agent-platform${runnerType === value ? " is-on" : ""}`}
            onClick={() => setRunnerType(value)}
          >
            <strong>{label}</strong>
            <span>{why}</span>
          </button>
        ))}
      </div>

      <p className="muted sg-ci-run-note">
        What the machine can do is reported by the agent itself, so there is nothing
        to list here.
      </p>

      <button
        type="button"
        className="sg-ci-help-toggle"
        aria-expanded={showHelp}
        onClick={() => setShowHelp((prev) => !prev)}
      >
        {showHelp
          ? "Hide install steps"
          : `How do I install this on ${runnerType === "agent_macos" ? "a Mac" : "Linux"}?`}
      </button>
      {showHelp && (
        // Readable before registering too: seeing what the work involves is
        // part of deciding to do it.
        <AgentInstallHelp runnerType={runnerType} workspaceRoot={workspaceRoot.trim()} />
      )}

      <div className="modal-actions">
        <button type="button" className="btn-outline btn-compact" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
        <button
          type="button"
          className="primary btn-compact"
          onClick={submit}
          disabled={saving || !name.trim()}
        >
          {saving ? "Registering…" : "Register agent"}
        </button>
      </div>
    </section>
  );
}

/**
 * The token, shown once.
 *
 * Only its hash is stored, so this is genuinely the only time it exists. The
 * command to run is here too, because this is the moment somebody has to go and
 * do something on another computer — sending them to find documentation is how
 * an agent ends up never installed.
 */
function Token({ issued, onDone }) {
  const [copied, setCopied] = useState("");
  // Open by default here: the token is on screen once, and the unit file
  // below already has it filled in — this is the moment to copy both.
  const [showHelp, setShowHelp] = useState(true);
  const install = issued.install || {};
  const command = `python3 kubesight-agent.py --url ${install.url || ""} --token ${issued.token}`;

  const copy = async (text, what) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(what);
    } catch {
      // Clipboard is blocked in some contexts; the value is on screen anyway.
      setCopied("");
    }
  };

  return (
    <section className="sg-ci-agent-token">
      <h4>{issued.name} registered</h4>
      <p className="muted">
        This token is shown once — only its hash is stored. Copy it now; if it is
        lost, issue a new one from this dialog.
      </p>

      <div className="sg-ci-agent-token-row">
        <code>{issued.token}</code>
        <button type="button" className="btn-outline btn-compact" onClick={() => copy(issued.token, "token")}>
          {copied === "token" ? "Copied" : "Copy"}
        </button>
      </div>

      <p className="form-label">Then, on {issued.runnerType === "agent_macos" ? "the Mac" : "the machine"}:</p>
      <div className="sg-ci-agent-token-row">
        <code>{command}</code>
        <button type="button" className="btn-outline btn-compact" onClick={() => copy(command, "command")}>
          {copied === "command" ? "Copied" : "Copy"}
        </button>
      </div>

      <button
        type="button"
        className="sg-ci-help-toggle"
        aria-expanded={showHelp}
        onClick={() => setShowHelp((prev) => !prev)}
      >
        {showHelp ? "Hide install steps" : "Show install steps"}
      </button>

      {showHelp && (
        <AgentInstallHelp
          runnerType={issued.runnerType}
          url={install.url}
          token={issued.token}
          workspaceRoot={issued.workspaceRoot}
        />
      )}

      <div className="modal-actions">
        <button type="button" className="primary btn-compact" onClick={onDone}>
          Done
        </button>
      </div>
    </section>
  );
}

AgentEnrolment.Token = Token;
