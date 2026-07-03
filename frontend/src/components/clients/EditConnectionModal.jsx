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
// connection attaches to, and source/destination IPs. Nothing here touches the
// reusable service topology.
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
  const [sourceIp, setSourceIp] = useState(c.sourceIp || "");
  const [destinationIp, setDestinationIp] = useState(c.destinationIp || "");
  const [status, setStatus] = useState(c.status || "active");
  const [selectedRefs, setSelectedRefs] = useState(
    () => new Set((c.componentRefs || []).map((r) => String(r.ref)))
  );

  const isOther = transportType === "Other";
  const otherMissing = isOther && !transportName.trim();

  const toggleRef = (ref) => {
    setSelectedRefs((prev) => {
      const next = new Set(prev);
      if (next.has(ref)) next.delete(ref);
      else next.add(ref);
      return next;
    });
  };

  const handleSubmit = () => {
    if (otherMissing) return;
    onSave({
      direction,
      transportType: transportType || "",
      transportName: transportName.trim(),
      transportNotes: transportNotes.trim(),
      sourceIp: sourceIp.trim(),
      destinationIp: destinationIp.trim(),
      status: status || "active",
      componentRefs: [...selectedRefs],
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
          {components.length === 0 ? (
            <p className="muted" style={{ fontSize: "0.85rem", margin: 0 }}>
              This service has no topology components. The connection will attach to the service entrypoint.
            </p>
          ) : (
            <>
              <p className="muted" style={{ fontSize: "0.8125rem", margin: "0 0 0.5rem" }}>
                Select one or more. Leave empty to attach to the service entrypoint.
              </p>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
                  gap: "0.4rem 1rem",
                  maxHeight: 220,
                  overflowY: "auto",
                }}
              >
                {components.map((comp) => {
                  const ref = String(comp.ref);
                  return (
                    <label
                      key={ref}
                      style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.85rem", cursor: "pointer" }}
                    >
                      <input
                        type="checkbox"
                        checked={selectedRefs.has(ref)}
                        onChange={() => toggleRef(ref)}
                        style={{ width: "auto", margin: 0 }}
                      />
                      <span>
                        {comp.name}
                        {comp.type && <span className="muted"> · {comp.type}</span>}
                      </span>
                    </label>
                  );
                })}
              </div>
            </>
          )}
        </section>

        <section className="form-section">
          <h4>Addressing</h4>
          <div className="form-grid">
            <label>
              Source IP
              <input
                value={sourceIp}
                onChange={(e) => setSourceIp(e.target.value)}
                maxLength={64}
                placeholder="e.g. 196.10.20.5"
              />
            </label>
            <label>
              Destination IP
              <input
                value={destinationIp}
                onChange={(e) => setDestinationIp(e.target.value)}
                maxLength={64}
                placeholder="e.g. 10.4.12.50"
              />
            </label>
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
