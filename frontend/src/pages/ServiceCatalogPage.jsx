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
import PageTitle from "../components/common/PageTitle.jsx";
import BlueprintEditorModal from "../components/catalog/BlueprintEditorModal.jsx";
import DeployFromBlueprintWizard from "../components/catalog/DeployFromBlueprintWizard.jsx";

const STATUS_BADGE = {
  ready: "pass",
  draft: "pending",
  deprecated: "warning",
};

const CRITICALITY_BADGE = {
  critical: "fail",
  high: "warning",
  medium: "pending",
  low: "pass",
};

function Badge({ value, map, fallback = "pending" }) {
  if (!value) return null;
  return (
    <span className={`status-badge status-badge--${map[value] || fallback}`}>{value}</span>
  );
}

function BlueprintCard({ blueprint, onView }) {
  return (
    <button
      type="button"
      className="card"
      onClick={onView}
      style={{
        textAlign: "left",
        padding: "1.1rem",
        display: "flex",
        flexDirection: "column",
        gap: "0.6rem",
        cursor: "pointer",
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "0.75rem" }}>
        <h3 style={{ margin: 0, fontSize: "1.05rem" }}>{blueprint.name}</h3>
        <Badge value={blueprint.status} map={STATUS_BADGE} />
      </div>
      {blueprint.description && (
        <p className="muted" style={{ margin: 0, fontSize: "0.85rem" }}>
          {blueprint.description}
        </p>
      )}
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.4rem", fontSize: "0.75rem" }}>
        {blueprint.category && <span className="chip">{blueprint.category}</span>}
        {blueprint.ownerTeam && <span className="chip">{blueprint.ownerTeam}</span>}
        <Badge value={blueprint.criticality} map={CRITICALITY_BADGE} />
      </div>
      <div className="muted" style={{ fontSize: "0.8rem", display: "flex", gap: "1rem" }}>
        <span>{blueprint.componentCount} component{blueprint.componentCount !== 1 ? "s" : ""}</span>
        <span>{blueprint.dependencyCount} dependenc{blueprint.dependencyCount !== 1 ? "ies" : "y"}</span>
        {blueprint.appServiceCount > 0 && (
          <span>{blueprint.appServiceCount} deployed</span>
        )}
      </div>
    </button>
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
            v{detail.version} · <Badge value={detail.status} map={STATUS_BADGE} />
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
                {r.secret && <span className="status-badge status-badge--warning">secret</span>}
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

  return (
    <div className="ops-page">
      <PageTitle
        title="Service Catalog"
        subtitle="Reusable business service blueprints — deploy real app services from a logical design."
        actionLabel={canCreate ? "New blueprint" : undefined}
        onAction={canCreate ? openCreate : undefined}
      />

      {error && <ErrorBanner message={error} />}

      <div className="user-filters" style={{ marginBottom: "1rem", display: "flex", gap: "0.75rem" }}>
        <input
          type="search"
          className="form-input"
          placeholder="Search by name, category, or owner team…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ maxWidth: 320 }}
        />
        <select
          className="form-select"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          <option value="all">All statuses</option>
          <option value="draft">Draft</option>
          <option value="ready">Ready</option>
          <option value="deprecated">Deprecated</option>
        </select>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: detail ? "2fr 1fr" : "1fr",
          gap: "1.25rem",
          alignItems: "start",
        }}
      >
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
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
                gap: "1rem",
              }}
            >
              {filtered.map((bp) => (
                <BlueprintCard key={bp.id} blueprint={bp} onView={() => openDetail(bp.id)} />
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
