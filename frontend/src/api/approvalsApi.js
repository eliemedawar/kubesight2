import { request } from "./client";

// The Approvals page: one inbox across every kind of approval, its history,
// every rule in one place, and the approver groups. Votes are not here — each
// inbox item carries the URL of its own kind's approve/reject route.

/** scope "mine" = what I may decide now; "all" = everything pending I can see. */
export const getApprovalsInbox = (scope = "mine") =>
  request("/api/approvals/inbox", { query: { scope } });

export const getApprovalsCount = () => request("/api/approvals/count");

/** Newest first. `before` (a previous page's `nextBefore`) returns the page
 *  after it; `nextBefore` is null on the last page. */
export const getApprovalsHistory = (kind, { limit, before } = {}) =>
  request("/api/approvals/history", {
    query: { kind: kind || undefined, limit: limit || undefined, before: before || undefined },
  });

export const getApprovalRules = () => request("/api/approvals/rules");

export const listApproverGroups = () => request("/api/approvals/groups");

export const createApproverGroup = (payload) =>
  request("/api/approvals/groups", { method: "POST", body: payload });

export const updateApproverGroup = (id, payload) =>
  request(`/api/approvals/groups/${encodeURIComponent(id)}`, { method: "PUT", body: payload });

export const deleteApproverGroup = (id) =>
  request(`/api/approvals/groups/${encodeURIComponent(id)}`, { method: "DELETE" });

/** Approve or reject one inbox item through its own route. */
export function decideApproval(item, action, text = "") {
  const actions = item.actions || {};
  const url = action === "approve" ? actions.approve : actions.reject;
  const field = action === "approve" ? actions.approveField : actions.rejectField;
  const body = field && text ? { [field]: text } : {};
  return request(url, { method: "POST", body });
}
