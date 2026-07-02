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

// Direction of the direct deployment↔client link (egress only).
const DIRECTION_OPTIONS = [
  { value: "outbound", label: "Outbound — deployment → client" },
  { value: "inbound", label: "Inbound — client → deployment" },
  { value: "both", label: "Both — bidirectional" },
];

// Modal to configure the client-specific connectivity overlay for one
// client↔service link. Nothing here touches the reusable service topology.
export default function EditConnectionModal({
  clientName,
  serviceName,
  connection,
  onClose,
  onSave,
  saving,
  error,
  heading = "Edit Connection",
  subtitle,
  showDirection = false,
}) {
  const c = connection || {};
  const [sourceIp, setSourceIp] = useState(c.sourceIp || "");
  const [destinationIp, setDestinationIp] = useState(c.destinationIp || "");
  const [transportType, setTransportType] = useState(c.transportType || "");
  const [transportName, setTransportName] = useState(c.transportName || "");
  const [transportNotes, setTransportNotes] = useState(c.transportNotes || "");
  const [direction, setDirection] = useState(c.direction || "outbound");
  const [status, setStatus] = useState(c.status || "active");

  const isOther = transportType === "Other";
  const otherMissing = isOther && !transportName.trim();

  const handleSubmit = () => {
    if (otherMissing) return;
    onSave({
      sourceIp: sourceIp.trim(),
      destinationIp: destinationIp.trim(),
      transportType: transportType || "",
      transportName: transportName.trim(),
      transportNotes: transportNotes.trim(),
      status: status || "active",
      ...(showDirection ? { direction } : {}),
    });
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div className="modal-card modal-card--wide" role="dialog" onClick={(e) => e.stopPropagation()}>
        <div className="modal-card__header">
          <h3>{heading}</h3>
          <p className="muted">
            {subtitle || (
              <>Client-specific connectivity for <strong>{clientName}</strong> → <strong>{serviceName}</strong>.</>
            )}
          </p>
        </div>

        {error && <p className="banner-message error">{error}</p>}

        <section className="form-section">
          <h4>Connectivity</h4>
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
            {showDirection && (
              <label>
                Direction
                <SearchableSelect
                  options={DIRECTION_OPTIONS}
                  value={direction}
                  onChange={(e) => setDirection(e.target.value || "outbound")}
                  placeholder="Select direction…"
                />
              </label>
            )}
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

        <section className="form-section">
          <h4>Status</h4>
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
