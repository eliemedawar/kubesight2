"""KubeSight as a set of tools an agent can call.

Everywhere else in this codebase, a model is handed evidence and asked a
question. This is the other direction: an agent — Hermes, Claude Code, anything
that speaks MCP — connects and asks KubeSight questions of its own. "Which
services are failing?", "why did build 214 stop?", "what does the payment
service's pipeline actually run?"

Three decisions shape it, and each is a boundary rather than a preference.

**It reads widely and writes narrowly.** Most tools answer: the catalog, the
pipelines, the builds, the logs, and the source a service builds from. Four of
them write, and all four write the same single thing — a service's pipeline.
Everything else stays on the ordinary API, behind the ordinary UI, with a person
pressing the button.

The line is drawn where a change stops being reviewable. A pipeline edit is
stored, versioned, audited, and a running build is unaffected because it
snapshots the pipeline it started with — so an edit somebody disagrees with can
be read afterwards and put back. Starting a build, writing a secret or deleting
a service cannot be, so none of them are here. "Read-only" would have been the
easier boundary to hold, but it made the agent a commentator on a pipeline it
could see was wrong.

**It authenticates as a person.** There is no MCP-specific credential and no
service bypass: a caller presents an ordinary KubeSight API token, and every
tool runs as that token's user under the same RBAC every route uses. An agent
holding a viewer's token sees exactly what a viewer sees, and changes nothing.
Giving this its own identity would have created a second, invisible permission
system — which is also why there is no separate "allow agent writes" switch: the
write tools need ``ci_pipelines:edit``, the same permission the editor screen
needs, so an installation decides by minting the token it means to hand over.

**It serves over HTTP from the backend itself.** Not a stdio subprocess: Hermes
runs in its own container, and a stdio server would have to be packaged into it
and kept in step. One Flask blueprint, no new process, no second copy of the
data access.

The protocol is implemented directly rather than through the ``mcp`` SDK. The
server half of Streamable HTTP is a handful of JSON-RPC methods over a POST, the
backend already has no dependency it does not need, and the alternative was
adding an SDK to the production image to save eighty lines.
"""
