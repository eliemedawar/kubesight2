import { useEffect, useState } from "react";
import {
  createServiceBlueprint,
  deleteServiceBlueprint,
  getServiceBlueprint,
  listServiceBlueprints,
  updateServiceBlueprint,
} from "../api/serviceBlueprintsApi.js";
import { useAuth } from "../context/AuthContext";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import BlueprintEditorModal from "../components/catalog/BlueprintEditorModal.jsx";
import DeployFromBlueprintWizard from "../components/catalog/DeployFromBlueprintWizard.jsx";

// Real blueprint states → existing status-pill tones (Signal: certified=ok / caution=warn / new=muted)
const STATUS_PILL_TONE = {
  ready: "ok",
  draft: "unknown",
  deprecated: "warn",
};

const STATUS_TABS = [
  ["all", "All"],
  ["ready", "Ready"],
  ["draft", "Draft"],
  ["deprecated", "Deprecated"],
];

/* ── Inline stroke icons (no emoji, tokens only via currentColor) ── */
function IconBase({ children, ...props }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  );
}

const BoxIcon = () => (
  <IconBase>
    <path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z" />
    <path d="m3.3 7 8.7 5 8.7-5" />
    <path d="M12 22V12" />
  </IconBase>
);

const RocketIcon = () => (
  <IconBase>
    <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z" />
    <path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z" />
    <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0" />
    <path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" />
  </IconBase>
);

const PlusIcon = () => (
  <IconBase>
    <path d="M12 5v14" />
    <path d="M5 12h14" />
  </IconBase>
);

const SearchIcon = () => (
  <IconBase>
    <circle cx="11" cy="11" r="7" />
    <path d="m21 21-4.35-4.35" />
  </IconBase>
);

function StatusPill({ status }) {
  if (!status) return null;
  return (
    <span className={`status-pill ${STATUS_PILL_TONE[status] || "unknown"}`}>{status}</span>
  );
}

const teamInitials = (name) =>
  name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0])
    .join("")
    .toUpperCase();

function BlueprintCard({ blueprint, active, onView, onDeploy, canDeploy }) {
  const iconTone =
    blueprint.criticality === "critical" || blueprint.criticality === "high"
      ? "sg-ico--accent"
      : "sg-ico--muted";
  const showDeploy = canDeploy && blueprint.status !== "deprecated";

  const handleKeyDown = (e) => {
    // Only act on the card itself — not on keys bubbling from the Deploy button.
    if (e.target !== e.currentTarget) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onView();
    }
  };

  return (
    <article
      className={`sg-ccard sg-ccard--clickable sg-bp${active ? " sg-bp--active" : ""}`}
      role="button"
      tabIndex={0}
      onClick={onView}
      onKeyDown={handleKeyDown}
      aria-label={`View blueprint ${blueprint.name}`}
    >
      <header>
        <span className={`sg-ico ${iconTone}`}>
          <BoxIcon />
        </span>
        <div className="sg-bp-id">
          <b>{blueprint.name}</b>
          <span className="sg-ccard-sub">
            v{blueprint.version}
            {blueprint.category ? ` · ${blueprint.category}` : ""}
          </span>
        </div>
        <StatusPill status={blueprint.status} />
      </header>

      {blueprint.description && <p className="sg-ccard-desc">{blueprint.description}</p>}

      {(blueprint.ownerTeam || blueprint.appServiceCount > 0) && (
        <div className="sg-ccard-meta">
          {blueprint.ownerTeam && (
            <>
              <span className="sg-avatar sg-avatar--sm">{teamInitials(blueprint.ownerTeam)}</span>
              <span className="sg-bp-team">{blueprint.ownerTeam}</span>
            </>
          )}
          {blueprint.appServiceCount > 0 && (
            <span className="sg-bp-deploys">
              {blueprint.appServiceCount} deploy{blueprint.appServiceCount !== 1 ? "s" : ""}
            </span>
          )}
        </div>
      )}

      <footer>
        <span className="sg-tag">
          {blueprint.componentCount} component{blueprint.componentCount !== 1 ? "s" : ""}
        </span>
        <span className="sg-tag">
          {blueprint.dependencyCount} dependenc{blueprint.dependencyCount !== 1 ? "ies" : "y"}
        </span>
        {blueprint.criticality && <span className="sg-tag">{blueprint.criticality}</span>}
        {showDeploy && (
          <button
            type="button"
            className="primary btn-compact sg-bp-deploy"
            onClick={(e) => {
              e.stopPropagation();
              onDeploy();
            }}
          >
            <RocketIcon />
            Deploy
          </button>
        )}
      </footer>
    </article>
  );
}

function BlueprintDetail({ detail, onClose, onEdit, onDelete, onDeploy, canUpdate, canDelete, canDeploy }) {
  const componentName = (id) =>
    detail.components.find((c) => c.id === id)?.name || `#${id}`;

  return (
    <div className="card" style={{ padding: "1.25rem", position: "sticky", top: "1rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "1rem" }}>
        <div>
          <h3 style={{ margin: 0 }}>{detail.name}</h3>
          <p className="muted" style={{ marginTop: "0.25rem", fontSize: "0.85rem" }}>
            v{detail.version} · <StatusPill status={detail.status} />
          </p>
        </div>
        <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap", justifyContent: "flex-end" }}>
          {canDeploy && detail.status !== "deprecated" && (
            <button type="button" className="primary btn-compact" onClick={onDeploy}>Deploy</button>
          )}
          {canUpdate && (
            <button type="button" className="btn-outline btn-compact" onClick={onEdit}>Edit</button>
          )}
          {canDelete && (
            <button type="button" className="btn-outline btn-compact danger" onClick={onDelete}>Delete</button>
          )}
          <button type="button" className="btn-outline btn-compact" onClick={onClose}>Close</button>
        </div>
      </div>

      {detail.description && (
        <p style={{ marginTop: "0.75rem", fontSize: "0.875rem" }}>{detail.description}</p>
      )}

      <section style={{ marginTop: "1rem" }}>
        <p className="form-label">Components ({detail.components.length})</p>
        <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem" }}>
          {detail.components.map((c) => (
            <div key={c.id} style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.85rem" }}>
              <strong>{c.name}</strong>
              <span className="chip">{c.componentType}</span>
              {c.role && <span className="muted">{c.role}</span>}
              {!c.required && <span className="muted" style={{ fontSize: "0.75rem" }}>optional</span>}
            </div>
          ))}
        </div>
      </section>

      {detail.connections.length > 0 && (
        <section style={{ marginTop: "1rem" }}>
          <p className="form-label">Topology ({detail.connections.length})</p>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem", fontSize: "0.85rem" }}>
            {detail.connections.map((cn) => (
              <div key={cn.id} className="muted">
                {componentName(cn.sourceComponentId)} → {componentName(cn.targetComponentId)}
                {cn.protocol ? ` (${cn.protocol}${cn.port ? `:${cn.port}` : ""})` : ""}
              </div>
            ))}
          </div>
        </section>
      )}

      {detail.requirements.length > 0 && (
        <section style={{ marginTop: "1rem" }}>
          <p className="form-label">Requirements ({detail.requirements.length})</p>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem", fontSize: "0.85rem" }}>
            {detail.requirements.map((r) => (
              <div key={r.id} style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                <code>{r.key}</code>
                <span className="chip">{r.requirementType}</span>
                {r.secret && <span className="status-pill warn">secret</span>}
                {r.autoGenerate && <span className="muted" style={{ fontSize: "0.75rem" }}>auto</span>}
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

export default function ServiceCatalogPage({ clusters = [] }) {
  const { hasPermission } = useAuth();
  const canView = hasPermission("service_blueprints:view");
  const canCreate = hasPermission("service_blueprints:create");
  const canUpdate = hasPermission("service_blueprints:update");
  const canDelete = hasPermission("service_blueprints:delete");
  const canDeploy = hasPermission("service_blueprints:deploy");

  const [blueprints, setBlueprints] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorBlueprint, setEditorBlueprint] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [deployBlueprint, setDeployBlueprint] = useState(null);

  const loadData = async () => {
    setLoading(true);
    setError("");
    try {
      const res = await listServiceBlueprints();
      setBlueprints(res.items || []);
    } catch (err) {
      setError(err.message || "Failed to load service blueprints.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (canView) loadData();
  }, [canView]);

  const openDetail = async (id) => {
    setDetailLoading(true);
    try {
      const data = await getServiceBlueprint(id);
      setDetail(data);
    } catch (err) {
      setError(err.message || "Failed to load blueprint.");
    } finally {
      setDetailLoading(false);
    }
  };

  const openCreate = () => {
    setEditorBlueprint(null);
    setSaveError("");
    setEditorOpen(true);
  };

  const openEdit = async () => {
    // The detail panel already holds the full blueprint; reuse it.
    setEditorBlueprint(detail);
    setSaveError("");
    setEditorOpen(true);
  };

  const handleSave = async (payload) => {
    setSaving(true);
    setSaveError("");
    try {
      const saved = editorBlueprint?.id
        ? await updateServiceBlueprint(editorBlueprint.id, payload)
        : await createServiceBlueprint(payload);
      setEditorOpen(false);
      setEditorBlueprint(null);
      setDetail(saved);
      await loadData();
    } catch (err) {
      setSaveError(err.message || "Save failed.");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!detail) return;
    if (!window.confirm(`Delete blueprint "${detail.name}"? This cannot be undone.`)) return;
    try {
      await deleteServiceBlueprint(detail.id);
      setDetail(null);
      await loadData();
    } catch (err) {
      setError(err.message || "Delete failed.");
    }
  };

  if (!canView) return <AccessDeniedPage />;

  const filtered = blueprints.filter((bp) => {
    if (statusFilter !== "all" && bp.status !== statusFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      return (
        bp.name.toLowerCase().includes(q) ||
        (bp.category || "").toLowerCase().includes(q) ||
        (bp.ownerTeam || "").toLowerCase().includes(q)
      );
    }
    return true;
  });

  const subtitle = loading
    ? "Reusable business service blueprints — deploy real app services from a logical design."
    : `${blueprints.length} blueprint${blueprints.length === 1 ? "" : "s"} · deploy real app services from a logical design`;

  return (
    <div className="ops-page">
      <div className="sg-ph">
        <div>
          <h2>Service Catalog</h2>
          <p className="sg-ph-sub">{subtitle}</p>
        </div>
        {canCreate && (
          <div className="sg-ph-actions">
            <button type="button" className="primary sg-cat-new" onClick={openCreate}>
              <PlusIcon />
              New blueprint
            </button>
          </div>
        )}
      </div>

      {error && <ErrorBanner message={error} />}

      <div className="sg-cat-toolbar">
        <div className="sg-cat-tabs" role="group" aria-label="Filter by status">
          {STATUS_TABS.map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={`sg-cat-tab${statusFilter === value ? " is-on" : ""}`}
              aria-pressed={statusFilter === value}
              onClick={() => setStatusFilter(value)}
            >
              {label}
            </button>
          ))}
        </div>
        <label className="sg-cat-search">
          <SearchIcon />
          <input
            type="search"
            placeholder="Search by name, category, or owner team…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search blueprints"
          />
        </label>
      </div>

      <div className={`sg-cat-layout${detail ? " sg-cat-layout--split" : ""}`}>
        <div>
          {loading ? (
            <LoadingState label="Loading service catalog…" />
          ) : filtered.length === 0 ? (
            <EmptyState
              message="No service blueprints found."
              hint={
                blueprints.length > 0
                  ? "Try adjusting the search or filter."
                  : "Service blueprints define reusable service designs that can be deployed per client/environment."
              }
            />
          ) : (
            <div className="sg-card-grid">
              {filtered.map((bp) => (
                <BlueprintCard
                  key={bp.id}
                  blueprint={bp}
                  active={detail?.id === bp.id}
                  onView={() => openDetail(bp.id)}
                  onDeploy={() => setDeployBlueprint(bp)}
                  canDeploy={canDeploy}
                />
              ))}
            </div>
          )}
        </div>

        {detail && (
          <div>
            {detailLoading ? (
              <LoadingState label="Loading blueprint…" />
            ) : (
              <BlueprintDetail
                detail={detail}
                onClose={() => setDetail(null)}
                onEdit={openEdit}
                onDelete={handleDelete}
                onDeploy={() => setDeployBlueprint(detail)}
                canUpdate={canUpdate}
                canDelete={canDelete}
                canDeploy={canDeploy}
              />
            )}
          </div>
        )}
      </div>

      {editorOpen && (
        <BlueprintEditorModal
          blueprint={editorBlueprint}
          categories={[...new Set(blueprints.map((b) => b.category).filter(Boolean))]}
          onClose={() => { setEditorOpen(false); setEditorBlueprint(null); }}
          onSave={handleSave}
          saving={saving}
          error={saveError}
        />
      )}

      {deployBlueprint && (
        <DeployFromBlueprintWizard
          blueprintId={deployBlueprint.id}
          blueprintName={deployBlueprint.name}
          clusters={clusters}
          onClose={() => setDeployBlueprint(null)}
          onDeployed={() => loadData()}
        />
      )}
    </div>
  );
}
