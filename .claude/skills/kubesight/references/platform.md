# Platform — registries, tickets, mobile releases, and who can do what

The surfaces a question lands on a few times a week. What they have in common is
that they are **configuration and provenance** rather than running state — so
when an answer somewhere else ends in "…and it was refused", the reason is often
a row in here.

## Registries and images

```
kubesight_registries_list
kubesight_image_check {image: "registry.areeba.com/payment-api:4.2.1"}
```

**`kubesight_image_check` is the fastest answer to an `ImagePullBackOff`**, and
to "will this deploy work". A manifest referencing an image that was never
pushed looks like a cluster problem from inside the cluster and stays looking
like one until somebody checks the registry.

Run it before telling anybody a deploy will succeed. `kubesight_deploy_apply`
runs the same check itself and refuses — but finding out here costs one call and
no failed deploy.

## Ticketing and deploy automation

```
kubesight_ticketing_providers                              ← the provider keys
kubesight_tickets_list        {provider: "zoho"}
kubesight_automation_runs_list{provider: "zoho"}
```

Every other ticketing tool needs a provider key from the first call — `zoho` or
`jira`, depending on what the installation has connected.

`kubesight_automation_runs_list` is the place to look when somebody says **"the
ticket was approved but nothing happened"**: it shows what KubeSight did in
response to each ticket and how far the run got, including the stage it stopped
at and the error.

```
kubesight_automation_run_start  {provider: "zoho", ticketRecordId: 88}
kubesight_automation_run_cancel {provider: "zoho", runId: 412}
```

**Starting a run is a deploy.** It deploys what the ticket asks for, into the
cluster the ticket names, through the ordinary deploy path — so an
approval-gated cluster still gates it, and the refusal will be the one described
in [deploys.md](deploys.md). Read the ticket first with `kubesight_tickets_list`
and say what it is about to deploy and where.

**Cancelling stops a run; it does not undo one.** Anything already applied stays
applied. Say that, rather than letting "cancelled" be heard as "reverted".

## Handling a ticket (the ticket agent)

When the ticket agent is on, KubeSight hands you each new ticket as a task
("handle_new_ticket", with its `ticketRecordId`, the ticket and the catalog) and
you handle it end to end. You write every comment the requester sees.

```
kubesight_ticket_get               {ticketRecordId: 88}   ← ticket + catalog + runs
kubesight_ticket_execute           {ticketRecordId, action, environment, application,
                                    tag | variable+value, confidence, understanding, comment}
kubesight_ticket_request_approval  {…same…, reasons, comment, commentOnApprove}
kubesight_ticket_set_status        {ticketRecordId, status, comment}
kubesight_ticket_comment           {ticketRecordId, comment}
```

**Call exactly one of the three settling tools per new ticket.**
`kubesight_ticket_execute` when you understand it exactly and are confident: it
starts the deploy (`deploy_image`, `set_env_var` or `restart`) and moves the
ticket to In Progress. `kubesight_ticket_request_approval` when you understand it
but are not sure: a DevOps engineer approves it on Telegram, and approving runs
exactly what you proposed. `kubesight_ticket_set_status` with `impediment` when
the ticket is unclear, impossible, or missing something — the comment says what
and asks.

**`environment` and `application` are copied from the catalog, never guessed.**
Execute refuses anything that is not one catalog entry, and turns a request
under the confidence bar, one that contradicts the ticket's dropdowns, or one
with `concerns` into "request approval instead" — then call the approval tool
with the same action.

**Only these tools change a ticketed deploy.** Never reach for
`kubesight_automation_run_start`, a workload restart or `kubesight_deploy_apply`
to carry out a ticket: they skip the ticket's status and the approval.

**Follow-ups.** When the run finishes (or an approval is rejected or expires)
you get a "followup" task describing it. Write the requester a comment and move
the ticket: `done` when the change is live, `failed` when it failed,
`impediment` when the approval was refused. `done` is refused while the run is
still going.

## Mobile releases

```
kubesight_mobile_apps_list
kubesight_mobile_builds_list   {appId: 3}
kubesight_mobile_publishes_list{appId: 3}
```

Read-only. Builds are the binaries KubeSight holds — platform, version, build
number, whether it is signed. Publishes are what went to Google Play or App
Store Connect, and whether the store accepted it.

An unsigned build cannot be published, and the builds listing says so per build.
That is usually the answer to "why can't we publish this".

## Users, roles and permissions

```
kubesight_users_list
kubesight_roles_list
kubesight_settings_get
```

**`kubesight_roles_list` is what turns a permission error into something
actionable.** When any tool refuses with

> `'X' needs the 'Y' permission, which this token does not have.`

look up which roles grant `Y` and name them: "that needs `helm:uninstall`, which
the `platform-admin` role has — ask an administrator to grant it or run it
themselves." A bare "I don't have permission" leaves the person nowhere.

`kubesight_users_list` shows account state — active, locked, MFA enrolled. It is
read-only: unlocking an account, resetting MFA and changing a role are
administrator actions in the UI, not tools here.

`kubesight_settings_get` is thin, and mostly useful for one thing: `defaultCluster`
tells you which cluster somebody means when they say "the cluster".
