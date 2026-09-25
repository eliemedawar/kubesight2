// On a cluster that requires approvals, a write without a live approved
// deployment request is not refused: the backend queues it as a change bundle
// (HTTP 202, `pendingApproval: true`) and applies it automatically once it is
// approved. These helpers keep every screen from reporting that as "applied".

export function isPendingApproval(result) {
  return Boolean(result && result.pendingApproval);
}

export function pendingApprovalMessage(result) {
  if (!isPendingApproval(result)) return "";
  if (result.message) return result.message;
  const id = result.bundleId ? ` #${result.bundleId}` : "";
  return `Sent for approval as change bundle${id}. It will be applied automatically once it is approved.`;
}

/** Banner copy for a screen that is about to make a change on a gated cluster. */
export function approvalNotice(eligibility) {
  if (!eligibility || !eligibility.approvalRequired || eligibility.hasActiveApproval) return "";
  const n = eligibility.requiredApprovals;
  const needs = n ? ` (${n} approval${n === 1 ? "" : "s"})` : "";
  return (
    `This cluster requires approval${needs}. Your change will be sent to the approvers ` +
    "and applied automatically once it is approved."
  );
}
