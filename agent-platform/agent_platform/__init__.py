"""KubeSight's agent platform: PydanticAI agents run durably on Temporal.

KubeSight stays the system of record and the approval authority. The platform
only reasons and waits: it reads what KubeSight hands it, acts through
KubeSight's MCP server with its own narrow token, and reports back.

Two processes share this package:

* ``agent_platform.api``    — the HTTP front door KubeSight calls (start or
  signal a ticket workflow; an OpenAI-compatible completions endpoint).
* ``agent_platform.worker`` — the Temporal worker that runs the workflows and
  the agents' model and tool activities, for one department's task queue.
"""
