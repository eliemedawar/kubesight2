import { useEffect, useMemo, useState } from "react";
import { listCiApproverCandidates } from "../../../api/ciApi.js";
import SearchableSelect from "../../common/SearchableSelect.jsx";
import { DraftTextarea, Field, Switch } from "./controls.jsx";
import { APPROVE_PERMISSION, MAX_MIN_APPROVALS, blankApproval, describeApprovers } from "./approvalModel.js";
import { PlIcon } from "./icons.jsx";
import { timeoutLabel } from "./stageModel.js";

/**
 * An Approval stage: who must say yes before the stages after it run.
 *
 * Read top to bottom it answers what an approver is asked, who may answer, how
 * many must, and what happens when nobody does — so "it just waits forever"
 * or "I approved my own release" are answered on the page, not discovered.
 */
export default function ApprovalStageFields({ ids, stage, stages, index, editable, onChange, error }) {
  const approval = stage.approval || blankApproval();
  const set = (patch) => onChange({ approval: { ...approval, ...patch } });
  const [candidates, setCandidates] = useState({ items: [], error: "" });

  useEffect(() => {
    // Only an editor picks approvers; a saved stage already names its own.
    if (!editable) return undefined;
    let cancelled = false;
    listCiApproverCandidates()
      .then((data) => !cancelled && setCandidates({ items: data?.items || [], error: "" }))
      .catch((err) => !cancelled && setCandidates({ items: [], error: err.message || "Could not list users." }));
    return () => {
      cancelled = true;
    };
  }, [editable]);

  const chosen = approval.users || [];
  const chosenIds = new Set(chosen.map((user) => user.id));
  const options = useMemo(
    () =>
      candidates.items
        .filter((user) => !chosenIds.has(user.id))
        .map((user) => ({
          value: String(user.id),
          label: (
            <span className="pl-approval-option">
              <span>{user.username}</span>
              <small>
                {user.fullName ? `${user.fullName} · ` : ""}
                {user.holdsApprove ? `holds ${APPROVE_PERMISSION}` : `no ${APPROVE_PERMISSION}`}
              </small>
            </span>
          ),
        })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [candidates.items, chosen]
  );
  const holders = candidates.items.filter((user) => user.holdsApprove).length;
  const minimum = approval.minApprovals ?? 1;
  const after = stages.slice(index + 1).filter((other) => other.enabled !== false);

  return (
    <div className="pl-approval">
      <Field
        label="Message to approvers"
        htmlFor={`${ids}-instructions`}
        optional
        hint="Shown on the build and in the email — what to check before saying yes."
      >
        <DraftTextarea
          id={`${ids}-instructions`}
          rows={3}
          maxLength={2000}
          value={approval.instructions || ""}
          disabled={!editable}
          placeholder="Check the release notes and the UAT sign-off before approving."
          onChangeText={(instructions) => set({ instructions })}
        />
      </Field>

      {/* ── Who may approve ─────────────────────────────────────────── */}
      <div className="pl-approval-who" role="group" aria-label="Who may approve">
        <div className="pl-approval-row">
          <span className="pl-approval-row-icon" aria-hidden="true">
            <PlIcon name="key" />
          </span>
          <div>
            <strong>Anyone with the {APPROVE_PERMISSION} permission</strong>
            <small>
              Given with Retry builds to operators and cluster admins.
              {candidates.items.length ? ` ${holders} active user${holders === 1 ? "" : "s"} hold it now.` : ""}
            </small>
          </div>
          <Switch
            checked={Boolean(approval.anyoneWithPermission)}
            disabled={!editable}
            label={approval.anyoneWithPermission ? "Yes" : "No"}
            onChange={(anyoneWithPermission) => set({ anyoneWithPermission })}
          />
        </div>

        <Field
          label={approval.anyoneWithPermission ? "And these people" : "These people"}
          hint="Named approvers do not need the permission. Their account must be active."
          error={candidates.error}
        >
          {chosen.length > 0 && (
            <ul className="pl-approval-chips" aria-label="Named approvers">
              {chosen.map((user) => (
                <li key={user.id}>
                  <PlIcon name="approval" />
                  <span>{user.username || `user #${user.id}`}</span>
                  {editable && (
                    <button
                      type="button"
                      className="btn-ghost pl-approval-remove"
                      aria-label={`Remove ${user.username || `user #${user.id}`}`}
                      onClick={() => set({ users: chosen.filter((item) => item.id !== user.id) })}
                    >
                      <PlIcon name="x" />
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
          {editable && (
            <SearchableSelect
              id={`${ids}-approver`}
              aria-label="Add an approver"
              value=""
              placeholder="Add an approver…"
              searchPlaceholder="Search users…"
              options={options}
              onChange={(event) => {
                const id = Number(event.target.value);
                const user = candidates.items.find((item) => item.id === id);
                if (user) set({ users: [...chosen, { id: user.id, username: user.username }] });
              }}
            />
          )}
        </Field>
      </div>

      <div className="pl-grid pl-approval-rules">
        <Field
          label="Approvals needed"
          htmlFor={`${ids}-min`}
          hint={`Different people. 1 to ${MAX_MIN_APPROVALS}.`}
        >
          <input
            id={`${ids}-min`}
            type="number"
            min={1}
            max={MAX_MIN_APPROVALS}
            value={minimum}
            disabled={!editable}
            onChange={(event) =>
              set({ minApprovals: event.target.value === "" ? "" : Number(event.target.value) })
            }
          />
        </Field>
        <div className="pl-approval-checks">
          <label className="pl-check">
            <input
              type="checkbox"
              checked={Boolean(approval.allowSelfApproval)}
              disabled={!editable}
              onChange={(event) => set({ allowSelfApproval: event.target.checked })}
            />
            <span>
              <strong>The person who started the build may approve it</strong>
              <small>
                Off by default — the same rule as cluster approvals. A scheduled build counts as
                started by the schedule's owner; a webhook build has nobody behind it.
              </small>
            </span>
          </label>
          <label className="pl-check">
            <input
              type="checkbox"
              checked={Boolean(approval.notify)}
              disabled={!editable}
              onChange={(event) => set({ notify: event.target.checked })}
            />
            <span>
              <strong>Email the approvers when a build starts waiting</strong>
              <small>Through the SMTP relay in Settings. A mail that cannot be sent never fails the build.</small>
            </span>
          </label>
        </div>
      </div>

      {error && (
        <p className="pl-field-error" role="alert">
          <PlIcon name="alert" /> {error}
        </p>
      )}

      <ul className="pl-deploy-guards" aria-label="How this stage behaves">
        <li className="pl-deploy-guard">
          <span className="pl-deploy-guard-icon" aria-hidden="true">
            <PlIcon name="clock" />
          </span>
          <span>
            <strong>Fails “not approved” after {timeoutLabel(stage.timeoutSeconds)}</strong>
            <small>
              A wait never turns into a pass. Change how long it waits under “If it fails or hangs”
              (up to 24 h).
            </small>
          </span>
        </li>
        <li className="pl-deploy-guard">
          <span className="pl-deploy-guard-icon" aria-hidden="true">
            <PlIcon name="thumbsDown" />
          </span>
          <span>
            <strong>One rejection stops the build</strong>
            <small>
              {after.length
                ? `${after.map((other) => `“${other.name || "unnamed"}”`).join(", ")} ${after.length === 1 ? "runs" : "run"} only once ${describeApprovers(approval)} ${Number(minimum) === 1 ? "approves" : `give ${minimum} approvals`}.`
                : "Put the Deploy or store upload this guards after it — nothing follows it yet."}
            </small>
          </span>
        </li>
      </ul>
    </div>
  );
}
