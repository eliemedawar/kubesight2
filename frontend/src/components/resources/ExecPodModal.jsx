import { useEffect, useRef, useState } from "react";
import { execInPod } from "../../api/clustersApi.js";
import { formatAccessError } from "../../utils/authz.js";

// Pseudo-terminal modal: each submitted command runs as a fresh `kubectl exec
// -- sh -c <command>` (no persistent shell state between commands). The
// scrollback shows the prompt, command, and captured stdout/stderr.
export default function ExecPodModal({ open, clusterId, namespace, pod, containers = [], onClose }) {
  const containerList = Array.isArray(containers) ? containers.filter(Boolean) : [];
  const [container, setContainer] = useState(containerList[0] || "");
  const [command, setCommand] = useState("");
  const [running, setRunning] = useState(false);
  const [history, setHistory] = useState([]);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (open) {
      setHistory([]);
      setCommand("");
      setContainer(containerList[0] || "");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, pod]);

  useEffect(() => {
    if (open) {
      inputRef.current?.focus();
    }
  }, [open]);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [history, running]);

  if (!open) {
    return null;
  }

  const promptLabel = `${pod}${container ? `:${container}` : ""} $`;

  const runCommand = async (event) => {
    event.preventDefault();
    const trimmed = command.trim();
    if (!trimmed || running) {
      return;
    }
    setHistory((prev) => [...prev, { type: "command", text: trimmed, prompt: promptLabel }]);
    setCommand("");
    setRunning(true);
    try {
      const payload = await execInPod({
        clusterId,
        namespace,
        podName: pod,
        command: trimmed,
        container: container || undefined,
      });
      const output = (payload.output ?? "").replace(/\n$/, "");
      setHistory((prev) => [...prev, { type: "output", text: output }]);
    } catch (err) {
      setHistory((prev) => [
        ...prev,
        { type: "error", text: formatAccessError(err.message) || err.message || "Command failed" },
      ]);
    } finally {
      setRunning(false);
      inputRef.current?.focus();
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel modal-card--wide exec-pod-modal"
        role="dialog"
        aria-labelledby="exec-pod-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <h3 id="exec-pod-title">Exec — {pod}</h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" /></svg>
          </button>
        </header>

        {containerList.length > 1 ? (
          <label className="exec-pod-modal__container">
            <span className="muted">Container</span>
            <select value={container} onChange={(event) => setContainer(event.target.value)}>
              {containerList.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
        ) : null}

        <div className="exec-pod-modal__terminal" ref={scrollRef}>
          {history.length === 0 ? (
            <p className="exec-pod-modal__hint muted">
              Each command runs in a fresh shell (no persistent state). Try{" "}
              <code>ls</code>, <code>env</code>, or <code>cat /etc/hostname</code>.
            </p>
          ) : null}
          {history.map((entry, index) => {
            if (entry.type === "command") {
              return (
                <div key={index} className="exec-pod-modal__line exec-pod-modal__line--command">
                  <span className="exec-pod-modal__prompt">{entry.prompt}</span> {entry.text}
                </div>
              );
            }
            return (
              <pre
                key={index}
                className={`exec-pod-modal__line exec-pod-modal__line--${entry.type}`}
              >
                {entry.text}
              </pre>
            );
          })}
          {running ? <div className="exec-pod-modal__line muted">Running…</div> : null}
        </div>

        <form className="exec-pod-modal__form" onSubmit={runCommand}>
          <span className="exec-pod-modal__prompt" aria-hidden="true">
            {promptLabel}
          </span>
          <input
            ref={inputRef}
            type="text"
            className="exec-pod-modal__input"
            value={command}
            onChange={(event) => setCommand(event.target.value)}
            placeholder="Type a command and press Enter…"
            aria-label="Command to run in pod"
            autoComplete="off"
            spellCheck="false"
            disabled={running}
          />
          <button type="submit" className="btn-primary btn-sm" disabled={running || !command.trim()}>
            Run
          </button>
        </form>

        <footer className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose}>
            Close
          </button>
        </footer>
      </section>
    </div>
  );
}
