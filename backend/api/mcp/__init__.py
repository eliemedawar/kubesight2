"""KubeSight as a set of tools an agent can call.

Everywhere else in this codebase, a model is handed evidence and asked a
question. This is the other direction: an agent — Hermes, Claude Code, anything
that speaks MCP — connects and asks KubeSight questions of its own. "Which
services are failing?", "why did build 214 stop?", "what does the payment
service's pipeline actually run?"

Three decisions shape it, and each is a boundary rather than a preference.

**It is read-only.** Every tool here answers; none of them change anything. An
agent that can trigger a build or rewrite a pipeline is a different feature with
a different risk, and mixing the two would mean every future read-only tool
inherited that risk. Writes stay on the ordinary API, behind the ordinary UI,
with a person pressing the button.

**It authenticates as a person.** There is no MCP-specific credential and no
service bypass: a caller presents an ordinary KubeSight API token, and every
tool runs as that token's user under the same RBAC every route uses. An agent
holding a viewer's token sees exactly what a viewer sees. Giving this its own
identity would have created a second, invisible permission system.

**It serves over HTTP from the backend itself.** Not a stdio subprocess: Hermes
runs in its own container, and a stdio server would have to be packaged into it
and kept in step. One Flask blueprint, no new process, no second copy of the
data access.

The protocol is implemented directly rather than through the ``mcp`` SDK. The
server half of Streamable HTTP is a handful of JSON-RPC methods over a POST, the
backend already has no dependency it does not need, and the alternative was
adding an SDK to the production image to save eighty lines.
"""
