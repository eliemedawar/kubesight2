/** Sources → the section rail.
 *
 *  Sources used to be eight cards stacked down one page, so finding SSH meant
 *  scrolling past vCenter, cloud accounts, address ranges and two ticket
 *  profiles. The rail groups them by what they answer — where machines come
 *  from, how a build reaches and installs them, and what tickets may ask for —
 *  and shows one section at a time. Each row carries a count and turns red when
 *  something in it last failed, so a broken source is visible without opening it.
 */

import { accountState } from "../../utils/cloudProvisioning.js";
import { sourceProfileSummary } from "../../utils/clusterBuilder.js";

export const SOURCE_GROUPS = [
  { id: "machines", label: "Machines" },
  { id: "reach", label: "Reach & install" },
  { id: "tickets", label: "From tickets" },
];

const SECTIONS = [
  { id: "vcenter", group: "machines", label: "vCenter", perm: "vsphere" },
  { id: "cloud", group: "machines", label: "Cloud accounts", perm: "vsphere" },
  { id: "addresses", group: "machines", label: "VM addresses", perm: "vsphere" },
  { id: "opentofu", group: "machines", label: "OpenTofu", perm: "vsphere" },
  { id: "ssh", group: "reach", label: "SSH reach", perm: "ssh" },
  { id: "packages", group: "reach", label: "Packages & images", perm: "buildProfiles" },
  { id: "vm-requests", group: "tickets", label: "VMs from tickets", perm: "vsphere" },
  { id: "app-requests", group: "tickets", label: "Apps from tickets", perm: "apps" },
];

function tally(rows, failed) {
  const list = rows || [];
  return { count: list.length, failing: list.filter(failed).length };
}

/** The sections this user may manage, in rail order, with count + failures. */
export function sourceSections(infra = {}, perms = {}) {
  const stats = {
    vcenter: tally(
      infra.vsphere,
      (row) => Boolean(row.lastConnectionStatus) && row.lastConnectionStatus !== "ok"
    ),
    cloud: tally(infra.cloudAccounts, (row) => accountState(row) === "failed"),
    ssh: tally(infra.profiles, (row) => row.lastTestStatus === "failed"),
    packages: tally(infra.buildProfiles, (row) => Boolean(sourceProfileSummary(row).incomplete)),
  };
  return SECTIONS
    .filter((section) => perms[section.perm])
    .map((section) => ({
      ...section,
      count: stats[section.id] ? stats[section.id].count : null,
      failing: stats[section.id] ? stats[section.id].failing : 0,
    }));
}

/** The section to show: the asked-for one if visible, else the first. */
export function activeSection(sections, requested) {
  if (sections.some((section) => section.id === requested)) return requested;
  return sections[0]?.id || null;
}
