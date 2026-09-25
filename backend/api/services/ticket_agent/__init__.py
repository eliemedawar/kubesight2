"""Hermes ticket agent: Hermes handles each inbound ticket through KubeSight's MCP tools.

``engine`` hands tickets to Hermes and implements the tools Hermes calls;
``catalog`` is what it may deploy to; ``validator`` is the guard rail inside
those tools; ``telegram`` carries approvals; ``settings`` is the config row.
"""
