/**
 * Table configs for the kinds added to the Resources page after the first
 * eleven: events, autoscalers, network policies, volume claims, volumes and
 * storage classes. Same shape as ResourcesPage's own `tableConfig` entries:
 * { title, columns, rows }, where a row may carry `searchText` for the cells
 * that render as elements (links, pills) and so cannot be searched as text.
 */
import ResourceRowActions from "./ResourceRowActions.jsx";
import { tabKeyForApiKind } from "../../lib/resourceTypes.js";

const HEALTH_PILL = { ok: "ok", warn: "warn", crit: "danger" };

function HealthPill({ health, text }) {
  return <span className={`status-pill ${HEALTH_PILL[health] || "info"}`}>{text || "-"}</span>;
}

/** A cell that opens another kind's list, searched for this object. */
function JumpLink({ tabKey, query, children, onJump, canJump }) {
  if (!tabKey || !query || !canJump(tabKey)) {
    return <span className="resources-mono">{children}</span>;
  }
  return (
    <button
      type="button"
      className="resources-link resources-mono"
      onClick={(event) => {
        event.stopPropagation();
        onJump(tabKey, query);
      }}
      title={`Show ${query}`}
    >
      {children}
    </button>
  );
}

function readOnlyActions(resource, kind, onResourceAction) {
  return (
    <ResourceRowActions
      actions={resource.actions || ["describe", "yaml"]}
      onAction={(actionId) => onResourceAction(actionId, resource, kind)}
    />
  );
}

function eventTime(event) {
  const value = Date.parse(event.lastTimestamp || event.firstTimestamp || "");
  return Number.isFinite(value) ? value : 0;
}

/**
 * @param {object} args
 * @param {object} args.resources   RBAC-filtered lists, by list key
 * @param {string} args.clusterId
 * @param {string} args.namespace
 * @param {function} args.onResourceAction  (actionId, resource, kind)
 * @param {function} args.onJump    (tabKey, searchText) — open a kind searched for one object
 * @param {function} args.canJump   (tabKey) => whether this user can open that tab
 */
export function buildExtraKindTables({
  resources,
  clusterId,
  namespace,
  onResourceAction,
  onJump,
  canJump,
}) {
  const link = (tabKey, query, text) => (
    <JumpLink tabKey={tabKey} query={query} onJump={onJump} canJump={canJump}>
      {text}
    </JumpLink>
  );
  const withScope = (item) => ({
    ...item,
    cluster: item.cluster || clusterId,
    namespace: item.namespace || namespace,
  });

  const events = [...(resources.events || [])].sort((a, b) => {
    const aWarn = String(a.type || "").toLowerCase() === "warning" ? 0 : 1;
    const bWarn = String(b.type || "").toLowerCase() === "warning" ? 0 : 1;
    return aWarn - bWarn || eventTime(b) - eventTime(a);
  });

  return {
    events: {
      title: "Events",
      columns: [
        { key: "typeCell", label: "Type" },
        { key: "reason", label: "Reason" },
        { key: "object", label: "Object" },
        { key: "message", label: "Message" },
        { key: "countText", label: "Count" },
        { key: "age", label: "Last seen" },
      ],
      rows: events.map((event, index) => {
        const kind = event.involvedKind || "";
        const name = event.involvedName || "";
        const warning = String(event.type || "").toLowerCase() === "warning";
        return {
          ...event,
          id: `${kind}/${name}/${event.reason || ""}/${event.lastTimestamp || ""}/${index}`,
          typeCell: (
            <span className={`status-pill ${warning ? "warn" : "info"}`}>{event.type || "-"}</span>
          ),
          reason: event.reason || "-",
          object: link(tabKeyForApiKind(kind), name, kind && name ? `${kind}/${name}` : name || "-"),
          message: event.message || "-",
          countText: `×${event.count ?? 1}`,
          age: event.age || "-",
          searchText: [event.type, event.reason, kind, name, event.message].join(" "),
        };
      }),
    },

    hpas: {
      title: "Autoscalers",
      columns: [
        { key: "name", label: "Autoscaler" },
        { key: "target", label: "Scales" },
        { key: "replicas", label: "Min / now / max" },
        { key: "metrics", label: "Now / target" },
        { key: "statusCell", label: "Status" },
        { key: "age", label: "Age" },
        { key: "actions", label: "Actions" },
      ],
      rows: (resources.hpas || []).map((hpa) => {
        const targetText = hpa.targetKind ? `${hpa.targetKind}/${hpa.targetName}` : hpa.targetName || "-";
        return {
          ...withScope(hpa),
          target: link(tabKeyForApiKind(hpa.targetKind), hpa.targetName, targetText),
          replicas: `${hpa.minReplicas ?? "-"} / ${hpa.currentReplicas ?? "-"} / ${hpa.maxReplicas ?? "-"}`,
          metrics: hpa.metrics || "-",
          statusCell: <HealthPill health={hpa.health} text={hpa.status} />,
          age: hpa.age || "-",
          actions: readOnlyActions(withScope(hpa), "hpa", onResourceAction),
          searchText: [hpa.name, targetText, hpa.status, hpa.metrics].join(" "),
        };
      }),
    },

    networkPolicies: {
      title: "Network policies",
      columns: [
        { key: "name", label: "Policy" },
        { key: "appliesTo", label: "Applies to" },
        { key: "direction", label: "Direction" },
        { key: "allows", label: "Allows" },
        { key: "age", label: "Age" },
        { key: "actions", label: "Actions" },
      ],
      rows: (resources.networkpolicies || []).map((policy) => {
        const lines = [
          ...(policy.ingressRules || []).map((text) => ({ dir: "In", text })),
          ...(policy.egressRules || []).map((text) => ({ dir: "Out", text })),
        ];
        const types = policy.policyTypes || [];
        return {
          ...withScope(policy),
          appliesTo: policy.appliesTo || "All pods",
          direction:
            types.length === 2 ? "In + out" : types[0] === "Egress" ? "Out" : types.length ? "In" : "-",
          allows: lines.length ? (
            <ul className="resources-rules">
              {lines.map((line, index) => (
                <li key={`${line.dir}-${index}`}>
                  <span className="resources-rules__dir">{line.dir}</span>
                  {line.text}
                </li>
              ))}
            </ul>
          ) : (
            "-"
          ),
          age: policy.age || "-",
          actions: readOnlyActions(withScope(policy), "networkpolicy", onResourceAction),
          searchText: [policy.name, policy.appliesTo, ...lines.map((line) => line.text)].join(" "),
        };
      }),
    },

    pvcs: {
      title: "Volume claims",
      columns: [
        { key: "name", label: "Claim" },
        { key: "statusCell", label: "Status" },
        { key: "capacity", label: "Size" },
        { key: "accessModes", label: "Access" },
        { key: "storageClass", label: "Storage class" },
        { key: "volumeCell", label: "Volume" },
        { key: "usedByCell", label: "Used by" },
        { key: "age", label: "Age" },
        { key: "actions", label: "Actions" },
      ],
      rows: (resources.pvcs || []).map((pvc) => {
        const users = pvc.usedBy || [];
        return {
          ...withScope(pvc),
          statusCell: <HealthPill health={pvc.health} text={pvc.status} />,
          capacity: pvc.capacity || "-",
          accessModes: pvc.accessModes || "-",
          storageClass: pvc.storageClass || "-",
          volumeCell: pvc.volume ? link("pvs", pvc.volume, pvc.volume) : "-",
          usedByCell: users.length ? (
            <span className="resources-link-list">
              {users.slice(0, 2).map((pod) => (
                <span key={pod}>{link("pods", pod, pod)}</span>
              ))}
              {users.length > 2 ? (
                <span className="resources-muted">+{users.length - 2}</span>
              ) : null}
            </span>
          ) : (
            "-"
          ),
          age: pvc.age || "-",
          actions: readOnlyActions(withScope(pvc), "pvc", onResourceAction),
          searchText: [pvc.name, pvc.status, pvc.storageClass, pvc.volume, ...users].join(" "),
        };
      }),
    },

    pvs: {
      title: "Volumes",
      columns: [
        { key: "name", label: "Volume" },
        { key: "statusCell", label: "Status" },
        { key: "capacity", label: "Size" },
        { key: "accessModes", label: "Access" },
        { key: "reclaimPolicy", label: "Reclaim" },
        { key: "claimCell", label: "Claim" },
        { key: "storageClass", label: "Storage class" },
        { key: "age", label: "Age" },
        { key: "actions", label: "Actions" },
      ],
      rows: (resources.pvs || []).map((pv) => {
        const claim = pv.claimName ? `${pv.claimNamespace}/${pv.claimName}` : "";
        const claimedHere = pv.claimNamespace === namespace;
        return {
          ...pv,
          cluster: clusterId,
          // Describe/YAML go through the current namespace's URL; the server
          // checks the volume's claim, not this namespace.
          namespace,
          statusCell: (
            <HealthPill
              health={claimedHere ? pv.health : pv.health === "ok" ? "ok" : "info"}
              text={pv.status}
            />
          ),
          capacity: pv.capacity || "-",
          accessModes: pv.accessModes || "-",
          reclaimPolicy: pv.reclaimPolicy || "-",
          claimCell: !claim ? (
            <span className="resources-muted">Unclaimed</span>
          ) : claimedHere ? (
            link("pvcs", pv.claimName, claim)
          ) : (
            <span className="resources-mono">{claim}</span>
          ),
          storageClass: pv.storageClass || "-",
          age: pv.age || "-",
          actions: readOnlyActions({ ...pv, namespace }, "pv", onResourceAction),
          searchText: [pv.name, pv.status, claim, pv.storageClass].join(" "),
        };
      }),
    },

    storageClasses: {
      title: "Storage classes",
      columns: [
        { key: "nameCell", label: "Storage class" },
        { key: "provisioner", label: "Provisioner" },
        { key: "reclaimPolicy", label: "Reclaim" },
        { key: "bindingMode", label: "Binding" },
        { key: "expansion", label: "Can grow" },
        { key: "age", label: "Age" },
        { key: "actions", label: "Actions" },
      ],
      rows: (resources.storageclasses || []).map((sc) => ({
        ...sc,
        cluster: clusterId,
        namespace,
        nameCell: (
          <span className="resources-name-with-tag">
            <span>{sc.name}</span>
            {sc.default ? <span className="status-pill info">Default</span> : null}
          </span>
        ),
        provisioner: sc.provisioner || "-",
        reclaimPolicy: sc.reclaimPolicy || "-",
        bindingMode: sc.bindingMode || "-",
        expansion: sc.allowExpansion ? "Yes" : "No",
        age: sc.age || "-",
        actions: readOnlyActions({ ...sc, namespace }, "storageclass", onResourceAction),
        searchText: [sc.name, sc.provisioner, sc.default ? "default" : ""].join(" "),
      })),
    },
  };
}
