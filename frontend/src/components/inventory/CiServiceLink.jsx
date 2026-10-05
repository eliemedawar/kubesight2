import { useEffect, useState } from "react";
import "../../styles/signal/sharedPipelines.css";
import { createCiDeploymentLink, listCiServices } from "../../api/ciApi.js";
import { useAuth } from "../../context/AuthContext";
import { buildRoute } from "../../routes/routeUrl.js";
import SearchableSelect from "../common/SearchableSelect.jsx";

const CiGlyph = () => (
  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
    <path d="M3 4.5h6M3 8h10M3 11.5h7" strokeLinecap="round" />
    <circle cx="12" cy="4.5" r="1.5" />
  </svg>
);

/**
 * "Built by <CI service>" on an inventory row: the service linked to this
 * deployment (services/ci/deployment_links.py). A link, so a person goes from
 * what is running straight to what builds it.
 */
export function BuiltByChip({ ciService }) {
  if (!ciService) return null;
  return (
    <a
      className="sp-builtby"
      href={buildRoute({ key: "serviceDetail", params: { serviceId: String(ciService.id), tab: "overview" } })}
      title={`Built by the CI service ${ciService.name}${ciService.environment ? ` (${ciService.environment})` : ""}`}
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => event.stopPropagation()}
    >
      <CiGlyph />
      {ciService.name}
      {ciService.environment ? ` · ${ciService.environment}` : ""}
    </a>
  );
}

/**
 * The detail page's "Built by" line: the linked service, or — for someone who
 * may edit CI services — a way to link one from here.
 */
export default function CiServiceLink({ ciService, clusterId, namespace, workloadName }) {
  const { hasPermission } = useAuth();
  const canLink = hasPermission("ci_services:edit") && hasPermission("ci_services:view");
  const [linked, setLinked] = useState(ciService || null);
  const [picking, setPicking] = useState(false);
  const [services, setServices] = useState(null);
  const [picked, setPicked] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => setLinked(ciService || null), [ciService]);

  useEffect(() => {
    if (!picking || services !== null) return;
    listCiServices()
      .then((data) => setServices(data.items || []))
      .catch(() => setServices([]));
  }, [picking, services]);

  const link = async () => {
    const service = (services || []).find((item) => String(item.id) === String(picked));
    if (!service) return;
    setBusy(true);
    setError("");
    try {
      const created = await createCiDeploymentLink(service.id, {
        clusterId,
        namespace,
        workloadName,
        source: "inventory",
      });
      setLinked({ id: service.id, name: service.name, slug: service.slug, environment: created.environment || "" });
      setPicking(false);
    } catch (err) {
      setError(err.message || "Could not link the CI service.");
    } finally {
      setBusy(false);
    }
  };

  if (linked) return <BuiltByChip ciService={linked} />;
  if (!canLink || !workloadName) return <span className="muted">Not linked to a CI service</span>;
  if (!picking) {
    return (
      <button type="button" className="btn-outline btn-compact" onClick={() => setPicking(true)}>
        Link to a CI service
      </button>
    );
  }
  return (
    <div className="sp-inventory-link">
      <SearchableSelect
        id="inventory-ci-service"
        aria-label="CI service"
        value={picked}
        placeholder={services === null ? "Loading services…" : "Pick the CI service that builds it…"}
        searchPlaceholder="Search services…"
        options={(services || []).map((item) => ({
          value: String(item.id),
          label: item.name,
        }))}
        onChange={(event) => setPicked(event.target.value)}
      />
      <button type="button" className="primary btn-compact" disabled={!picked || busy} onClick={link}>
        {busy ? "Linking…" : "Link"}
      </button>
      <button type="button" className="btn-outline btn-compact" onClick={() => setPicking(false)} disabled={busy}>
        Cancel
      </button>
      {error && <p className="banner-message error">{error}</p>}
    </div>
  );
}
