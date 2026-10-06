import { useEffect, useState } from "react";
import { attachSharedPipeline, getSharedPipelineCopyFrom, listCiServices, listSharedPipelines } from "../../../api/ciApi.js";
import SearchableSelect from "../../common/SearchableSelect.jsx";
import { applicationTypeLabel } from "../ciShared.jsx";
import { PlIcon } from "./icons.jsx";

/**
 * "Use a shared pipeline": pick one from the Pipelines page for this service.
 *
 * Attaching saves at once. The service keeps its own stages (they come back if
 * it stops using the shared one), so the dialog says that before the click.
 */
export function SharedPipelinePicker({ service, currentId, onClose, onAttached }) {
  const [items, setItems] = useState(null);
  const [picked, setPicked] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    listSharedPipelines()
      .then((data) => setItems((data.items || []).filter((item) => (item.stageNames || []).length)))
      .catch((err) => {
        setItems([]);
        setError(err.message || "Could not list the pipelines.");
      });
  }, []);

  const attach = async () => {
    if (!picked) return;
    setBusy(true);
    setError("");
    try {
      const result = await attachSharedPipeline(service.id, picked.id);
      onAttached(result, picked);
    } catch (err) {
      setError(err.message || "Could not use that pipeline.");
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal-card sp-picker"
        role="dialog"
        aria-label="Use a shared pipeline"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-card__header">
          <h3>Use a shared pipeline</h3>
          <p className="muted">
            From its next build, {service.name} runs that pipeline's stages against its own repository,
            Dockerfile and registry. Its own stages are kept and come back if it stops using it.
          </p>
        </div>
        {error && <p className="banner-message error">{error}</p>}
        {items === null ? (
          <p className="muted">Loading pipelines…</p>
        ) : items.length === 0 ? (
          <div className="sp-picker-empty">
            <p>
              No pipeline on the Pipelines page has stages yet.{" "}
              <a className="pl-link" href="#/pipelines">
                Create one there
              </a>
              , then come back.
            </p>
          </div>
        ) : (
          <ul className="sp-picker-list" role="listbox" aria-label="Pipelines">
            {items.map((item) => {
              const current = String(item.id) === String(currentId);
              const on = picked && String(picked.id) === String(item.id);
              return (
                <li key={item.id}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={Boolean(on)}
                    className={`btn-ghost sp-picker-item${on ? " is-on" : ""}`}
                    disabled={current || item.status !== "active"}
                    onClick={() => setPicked(item)}
                  >
                    <span className="sp-picker-glyph" aria-hidden="true">
                      <PlIcon name="stages" />
                    </span>
                    <span className="sp-picker-copy">
                      <strong>
                        {item.name}
                        {current && <span className="pl-tag is-info">In use</span>}
                        {item.status !== "active" && <span className="pl-tag">Turned off</span>}
                      </strong>
                      <small>{item.description || "No description"}</small>
                      <small className="sp-picker-stages">
                        {(item.stageNames || []).join(" → ")}
                      </small>
                    </span>
                    <span className="sp-picker-meta">
                      v{item.pipelineVersion}
                      <small>
                        {item.usedByCount ? `used by ${item.usedByCount}` : "not used yet"}
                      </small>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="primary sg-cat-new" disabled={!picked || busy} onClick={attach}>
            <PlIcon name="link" />
            {busy ? "Saving…" : picked ? `Build with ${picked.name}` : "Pick a pipeline"}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * "Copy stages from a CI service" (a pipeline on the Pipelines page): the
 * service's pipeline as an unsaved draft. Nothing is written until Save, and
 * the service itself is not changed.
 */
export function CopyFromServiceModal({ pipeline, onClose, onDraft }) {
  const [services, setServices] = useState(null);
  const [picked, setPicked] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    listCiServices()
      .then((data) => setServices(data.items || []))
      .catch(() => setServices([]));
  }, []);

  const copy = async () => {
    const service = (services || []).find((item) => String(item.id) === String(picked));
    if (!service) return;
    setBusy(true);
    setError("");
    try {
      const draft = await getSharedPipelineCopyFrom(pipeline.id, service.id);
      onDraft(draft, service);
    } catch (err) {
      setError(err.message || "Could not read that service's pipeline.");
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal-card sp-picker"
        role="dialog"
        aria-label="Copy stages from a CI service"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-card__header">
          <h3>Copy stages from a CI service</h3>
          <p className="muted">
            Its stages, build inputs and post actions replace what is on screen, as unsaved changes to review. The
            service itself is not changed.
          </p>
        </div>
        {error && <p className="banner-message error">{error}</p>}
        <div className="sp-start-detail">
          <span>CI service</span>
          <SearchableSelect
            aria-label="CI service"
            value={picked}
            onChange={(event) => setPicked(event.target.value)}
            disabled={services === null}
            placeholder={services === null ? "Loading…" : "Pick a service…"}
            searchPlaceholder="Search services…"
            options={(services || []).map((item) => ({
              value: item.id,
              label: (
                <span className="sp-option">
                  <span className="sp-option__name">{item.name}</span>
                  <span className="sp-option__meta">{applicationTypeLabel(item.applicationType)}</span>
                </span>
              ),
            }))}
          />
        </div>
        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="primary sg-cat-new" disabled={!picked || busy} onClick={copy}>
            <PlIcon name="copy" />
            {busy ? "Copying…" : "Copy as a draft"}
          </button>
        </div>
      </div>
    </div>
  );
}
