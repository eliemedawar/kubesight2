import { useState } from "react";

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
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const submit = async () => {
    if (!name.trim() || saving) return;
    setSaving(true);
    setError("");
    try {
      onRegistered(
        await register({ name: name.trim(), runnerType, maxConcurrent: Number(maxConcurrent) || 1 })
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

      <p className="muted sg-ci-run-note">
        The agent is <code>agent/kubesight-agent.py</code> in the KubeSight repository —
        one file, no dependencies beyond Python 3. It appears here as online within a
        few seconds of starting. Keep it running with {install.keepAliveWith || "a service manager"};
        the README beside it has a ready-made unit file.
      </p>

      <div className="modal-actions">
        <button type="button" className="primary btn-compact" onClick={onDone}>
          Done
        </button>
      </div>
    </section>
  );
}

AgentEnrolment.Token = Token;
