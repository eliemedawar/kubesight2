/**
 * The Approval stage's model: its blank settings, how it reads in one line,
 * what a save would refuse, and how a build's wait reads in the drawer.
 *
 * Pure functions, like stageModel.js. The backend (services/ci/approval_config.py
 * and approval_stage.py) is the validator and the judge; these say out loud,
 * before Save or before a click, what it would say after.
 */

import { parseApiTime } from "../../../lib/apiTime.js";

export const APPROVE_PERMISSION = "ci_builds:approve";
export const MAX_MIN_APPROVALS = 10;
export const MAX_INSTRUCTIONS_CHARS = 2000;
/** An approval waits for people, not machines: four hours, not thirty minutes. */
export const DEFAULT_APPROVAL_TIMEOUT_SECONDS = 4 * 3600;

export const blankApproval = () => ({
  instructions: "",
  users: [],
  anyoneWithPermission: true,
  minApprovals: 1,
  allowSelfApproval: false,
  notify: false,
});

/** "alice, bob or anyone with ci_builds:approve" — who may answer. */
export function describeApprovers(approval) {
  const names = (approval?.users || []).map((user) => user.username || `user #${user.id}`);
  const parts = [];
  if (names.length) parts.push(names.join(", "));
  if (approval?.anyoneWithPermission) parts.push(`anyone with ${APPROVE_PERMISSION}`);
  return parts.join(" or ") || "nobody";
}

export function approvalSummary(stage) {
  const approval = stage.approval;
  if (!approval) return "Nobody picked yet";
  const count = Number(approval.minApprovals) || 1;
  return `Waits for ${count} approval${count === 1 ? "" : "s"} from ${describeApprovers(approval)}`;
}

/** What a save would refuse. Mirrors approval_config.normalize. */
export function approvalProblems(approval) {
  const problems = [];
  const add = (message) => problems.push({ field: "approval", message });
  if (!approval) {
    add("Say who may approve this stage.");
    return problems;
  }
  const users = approval.users || [];
  if (!users.length && !approval.anyoneWithPermission) {
    add(`Nobody may approve this stage. Name the approvers, or let anyone with ${APPROVE_PERMISSION} approve.`);
  }
  const min = Number(approval.minApprovals);
  if (!Number.isInteger(min) || min < 1 || min > MAX_MIN_APPROVALS) {
    add(`The number of approvals must be a whole number from 1 to ${MAX_MIN_APPROVALS}.`);
  } else if (!approval.anyoneWithPermission && users.length && min > users.length) {
    add(
      `Needs ${min} approvals but names only ${users.length} approver${users.length === 1 ? "" : "s"}, so it could never pass.`
    );
  }
  if (String(approval.instructions || "").length > MAX_INSTRUCTIONS_CHARS) {
    add(`The message to approvers is longer than ${MAX_INSTRUCTIONS_CHARS} characters.`);
  }
  return problems;
}

/** Distinct people who approved, in order. */
export function approvalsSoFar(state) {
  const seen = new Set();
  const out = [];
  for (const decision of state?.decisions || []) {
    if (decision.decision !== "approve" || seen.has(decision.userId)) continue;
    seen.add(decision.userId);
    out.push(decision);
  }
  return out;
}

/** The outcome of an Approval stage in a build, in words and a tone. */
export function approvalOutcome(state) {
  if (!state) return null;
  switch (state.outcome) {
    case "approved":
      return { tone: "success", label: "Approved" };
    case "rejected":
      return { tone: "error", label: "Rejected" };
    case "timed_out":
      return { tone: "error", label: "Not approved in time" };
    case "cancelled":
      return { tone: "muted", label: "Cancelled" };
    case "skipped":
      return { tone: "muted", label: "Not asked" };
    case "failed":
      return { tone: "error", label: "Failed" };
    default:
      break;
  }
  if (state.phase === "waiting_approval") {
    const required = Number(state.required) || 1;
    return { tone: "warn", label: `Waiting for approval · ${approvalsSoFar(state).length} of ${required}` };
  }
  return { tone: "info", label: "Starting" };
}

/** "in 3 h 20 min" until an approval runs out; "any moment" once it has. */
export function timeLeft(deadlineIso, now = Date.now()) {
  const deadline = parseApiTime(deadlineIso);
  if (Number.isNaN(deadline)) return "";
  const seconds = Math.round((deadline - now) / 1000);
  if (seconds <= 0) return "any moment";
  if (seconds < 60) return "in under a minute";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `in ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `in ${hours} h ${rest} min` : `in ${hours} h`;
}
