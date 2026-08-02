import { buildNavTree } from "../../routes/navigation.js";
import { pathForPageKey } from "../../routes/paths.js";

/**
 * What Ctrl/Cmd+K can find, and how it ranks.
 *
 * Permission-aware by construction rather than by filtering afterwards: pages
 * come from `visiblePages`, which is already `getVisiblePages()` output, and
 * clusters and namespaces come from the scoped lists the shell holds, which are
 * the RBAC-filtered ones. There is no path here that can surface a destination
 * the user could not otherwise reach — a search box that reveals the existence
 * of a page by name is an information leak even when the link 403s.
 */

export const RESULT_KIND = {
  PAGE: "page",
  CLUSTER: "cluster",
  NAMESPACE: "namespace",
};

const KIND_LABEL = {
  page: "Page",
  cluster: "Cluster",
  namespace: "Namespace",
};

export function kindLabel(kind) {
  return KIND_LABEL[kind] || "";
}

/**
 * Score a candidate against a query.
 *
 * Prefix beats word-start beats substring. Nothing fuzzier: on a list this
 * small, fuzzy matching mostly produces confident-looking wrong answers, and
 * the top result is the one that gets hit with Enter without being read.
 *
 * Returns -1 for no match.
 */
export function score(text, query) {
  const haystack = String(text || "").toLowerCase();
  const needle = String(query || "").trim().toLowerCase();
  if (!needle) {
    return 0;
  }
  if (!haystack) {
    return -1;
  }
  if (haystack === needle) {
    return 100;
  }
  if (haystack.startsWith(needle)) {
    return 80 - Math.min(haystack.length - needle.length, 20);
  }
  const wordStart = haystack.split(/[\s\-_/.]+/).some((word) => word.startsWith(needle));
  if (wordStart) {
    return 60;
  }
  if (haystack.includes(needle)) {
    return 40;
  }
  return -1;
}

function pageCandidates(visiblePages) {
  return buildNavTree(visiblePages).flatMap((group) =>
    group.items.map((entry) => ({
      id: `page:${entry.pageKey}`,
      kind: RESULT_KIND.PAGE,
      label: entry.label,
      hint: group.label,
      href: entry.href,
      // Group name is searchable too, so "operate" surfaces that whole area.
      haystacks: [entry.label, group.label],
    }))
  );
}

function clusterCandidates(clusters) {
  return (clusters || []).map((cluster) => ({
    id: `cluster:${cluster.id}`,
    kind: RESULT_KIND.CLUSTER,
    label: cluster.name || cluster.id,
    hint: cluster.name && cluster.name !== cluster.id ? cluster.id : "",
    href: pathForPageKey("clusterOverview", { clusterId: cluster.id }),
    haystacks: [cluster.name, cluster.id],
  }));
}

function namespaceCandidates(namespaces, clusterId) {
  if (!clusterId) {
    return [];
  }
  return (namespaces || []).map((ns) => {
    const name = typeof ns === "string" ? ns : ns?.name;
    return {
      id: `namespace:${clusterId}:${name}`,
      kind: RESULT_KIND.NAMESPACE,
      label: name,
      hint: clusterId,
      href: pathForPageKey("resources", { clusterId, namespace: name }),
      haystacks: [name],
    };
  });
}

/**
 * Search everything reachable.
 *
 * With no query this returns the pages only, as a jump list — offering every
 * namespace in a large cluster before the user has typed anything is noise.
 */
export function searchCommands(
  query,
  { visiblePages = [], clusters = [], namespaces = [], clusterId = "", limit = 12 } = {}
) {
  const trimmed = String(query || "").trim();

  const candidates = trimmed
    ? [
        ...pageCandidates(visiblePages),
        ...clusterCandidates(clusters),
        ...namespaceCandidates(namespaces, clusterId),
      ]
    : pageCandidates(visiblePages);

  const scored = candidates
    .map((candidate) => ({
      ...candidate,
      score: Math.max(...candidate.haystacks.map((text) => score(text, trimmed))),
    }))
    .filter((candidate) => candidate.score >= 0 && candidate.href);

  scored.sort((a, b) => b.score - a.score || a.label.localeCompare(b.label));
  return scored.slice(0, limit);
}
