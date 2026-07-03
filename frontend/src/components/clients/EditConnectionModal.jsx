import { useState } from "react";
import SearchableSelect from "../common/SearchableSelect.jsx";

export const TRANSPORT_TYPES = [
  "VPN",
  "Leased Line",
  "MPLS",
  "Internet",
  "Private Link",
  "Direct Connect",
  "Internal Network",
  "Other",
];

const STATUS_OPTIONS = ["active", "inactive", "degraded", "planned"];

// Direction of the connectivity link (client ↔ service).
const DIRECTION_OPTIONS = [
  { value: "inbound", label: "Inbound — client → service" },
  { value: "outbound", label: "Outbound — service → client" },
  { value: "both", label: "Both — bidirectional" },
];

// Heading for the component picker, phrased for the chosen direction.
function componentHeading(direction) {
  if (direction === "outbound") return "Components that connect to the client";
  if (direction === "both") return "Components in this connection";
  return "Components the client connects to";
}

// Modal to configure the client-specific connectivity overlay for one
// client↔service link: direction, transport, the service component(s) the
// connection attaches to (each with its own source/destination IP), and status.
// Nothing here touches the reusable service topology.
export default function EditConnectionModal({
  clientName,
  serviceName,
  connection,
  components = [],
  onClose,
  onSave,
  saving,
  error,
  heading = "Edit Connection",
  subtitle,
}) {
  const c = connection || {};
  const [direction, setDirection] = useState(c.direction || "inbound");
  const [transportType, setTransportType] = useState(c.transportType || "");
  const [transportName, setTransportName] = useState(c.transportName || "");
  const [transportNotes, setTransportNotes] = useState(c.transportNotes || "");
  const [status, setStatus] = useState(c.status || "active");
  // Connection-level IPs — only used when the service has no components to attach to.
  const [sourceIp, setSourceIp] = useState(c.sourceIp || "");
  const [destinationIp, setDestinationIp] = useState(c.destinationIp || "");
  // Per-component selection + addressing: { [ref]: { sourceIp, destinationIp } }.
  const [compData, setCompData] = useState(() => {
    const init = {};
    (c.componentRefs || []).forEach((r) => {
      init[String(r.ref)] = { sourceIp: r.sourceIp || "", destinationIp: r.destinationIp || "" };
    });
    return init;
  });

  const isOther = transportType === "Other";
  const otherMissing = isOther && !transportName.trim();
  const hasComponents = components.length > 0;

  const toggleRef = (ref) => {
    setCompData((prev) => {
      const next = { ...prev };
      if (next[ref]) delete next[ref];
      else next[ref] = { sourceIp: "", destinationIp: "" };
      return next;
    });
  };

  const setCompField = (ref, field, val) => {
    setCompData((prev) => ({ ...prev, [ref]: { ...prev[ref], [field]: val } }));
  };

  const handleSubmit = () => {
    if (otherMissing) return;
    onSave({
      direction,
      transportType: transportType || "",
      transportName: transportName.trim(),
      transportNotes: transportNotes.trim(),
      status: status || "active",
      // Connection-level IPs matter only for the no-component (entrypoint) case.
      sourceIp: hasComponents ? "" : sourceIp.trim(),
      destinationIp: hasComponents ? "" : destinationIp.trim(),
      componentRefs: Object.entries(compData).map(([ref, v]) => ({
        ref,
        sourceIp: (v.sourceIp || "").trim(),
        destinationIp: (v.destinationIp || "").trim(),
      })),
    });
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div className="modal-card modal-card--wide" role="dialog" onClick={(e) => e.stopPropagation()}>
        <div className="modal-card__header">
          <h3>{heading}</h3>
          <p className="muted">
            {subtitle || (
              <>Client-specific connectivity for <strong>{clientName}</strong> ↔ <strong>{serviceName}</strong>.</>
            )}
          </p>
        </div>

        {error && <p className="banner-message error">{error}</p>}

        <section className="form-section">
          <h4>Direction &amp; transport</h4>
          <div className="form-grid">
            <label>
              Direction
              <SearchableSelect
                options={DIRECTION_OPTIONS}
                value={direction}
                onChange={(e) => setDirection(e.target.value || "inbound")}
                placeholder="Select direction…"
              />
            </label>
            <label>
              Transport type
              <SearchableSelect
                options={TRANSPORT_TYPES.map((t) => ({ value: t, label: t }))}
                value={transportType}
                onChange={(e) => setTransportType(e.target.value)}
                placeholder="Select transport…"
              />
            </label>
            <label>
              Transport name/details {isOther && <span style={{ color: "var(--danger)" }}>*</span>}
              <input
                value={transportName}
                onChange={(e) => setTransportName(e.target.value)}
                maxLength={255}
                placeholder={isOther ? "Custom transport (required)" : "e.g. Circuit ID / provider"}
              />
            </label>
          </div>
        </section>

        <section className="form-section">
          <h4>{componentHeading(direction)}</h4>
          {!hasComponents ? (
            <>
              <p className="muted" style={{ fontSize: "0.85rem", margin: "0 0 0.75rem" }}>
                This service has no topology components. The connection attaches to the service entrypoint.
              </p>
              <div className="form-grid">
                <label>
                  Source IP
                  <input value={sourceIp} onChange={(e) => setSourceIp(e.target.value)} maxLength={64} placeholder="e.g. 196.10.20.5" />
                </label>
                <label>
                  Destination IP
                  <input value={destinationIp} onChange={(e) => setDestinationIp(e.target.value)} maxLength={64} placeholder="e.g. 10.4.12.50" />
                </label>
              </div>
            </>
          ) : (
            <>
              <p className="muted" style={{ fontSize: "0.8125rem", margin: "0 0 0.5rem" }}>
                Select one or more components — each has its own source &amp; destination IP. Leave all unchecked to attach to the service entrypoint.
              </p>
              <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
                {components.map((comp) => {
                  const ref = String(comp.ref);
                  const selected = Boolean(compData[ref]);
                  return (
                    <div
                      key={ref}
                      style={{
                        border: "1px solid var(--border)",
                        borderRadius: "0.5rem",
                        padding: "0.6rem 0.75rem",
                        background: selected ? "var(--bg-panel-strong, var(--bg-inset))" : "transparent",
                      }}
                    >
                      <label style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.875rem", cursor: "pointer", margin: 0 }}>
                        <input
                          type="checkbox"
                          checked={selected}
                          onChange={() => toggleRef(ref)}
                          style={{ width: "auto", margin: 0 }}
                        />
                        <span style={{ fontWeight: 600 }}>
                          {comp.name}
                          {comp.type && <span className="muted" style={{ fontWeight: 400 }}> · {comp.type}</span>}
                        </span>
                      </label>
                      {selected && (
                        <div className="form-grid" style={{ marginTop: "0.6rem" }}>
                          <label>
                            Source IP
                            <input
                              value={compData[ref].sourceIp}
                              onChange={(e) => setCompField(ref, "sourceIp", e.target.value)}
                              maxLength={64}
                              placeholder="e.g. 196.10.20.5"
                            />
                          </label>
                          <label>
                            Destination IP
                            <input
                              value={compData[ref].destinationIp}
                              onChange={(e) => setCompField(ref, "destinationIp", e.target.value)}
                              maxLength={64}
                              placeholder="e.g. 10.4.12.50"
                            />
                          </label>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </section>

        <section className="form-section">
          <h4>Status &amp; notes</h4>
          <div className="form-grid">
            <label>
              Status
              <SearchableSelect
                options={STATUS_OPTIONS.map((s) => ({ value: s, label: s }))}
                value={status}
                onChange={(e) => setStatus(e.target.value)}
                placeholder="Select status…"
              />
            </label>
            <label className="form-grid__full">
              Notes
              <textarea
                value={transportNotes}
                onChange={(e) => setTransportNotes(e.target.value)}
                rows={2}
                style={{ resize: "vertical" }}
                placeholder="Optional notes"
              />
            </label>
          </div>
        </section>

        {otherMissing && (
          <p className="banner-message error">Transport name is required when transport type is “Other”.</p>
        )}

        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button type="button" className="primary" onClick={handleSubmit} disabled={saving || otherMissing}>
            {saving ? "Saving…" : "Save connection"}
          </button>
        </div>
      </div>
    </div>
  );
}
