"""Native KubeSight CI.

    CiService   WHAT we build   catalog.py
    CiPipeline  HOW we build it pipelines.py
    CiRunner    WHERE we build  scheduler.py + runners/
    CiArtifact  WHAT came out   artifacts.py

The engine (engine.py) owns the build state machine and is the only module that
mutates a build's status. It talks to runners exclusively through the adapter
port in ``runners/base.py``, which is why the same engine drives a Kubernetes
Job, a Linux agent, and a Mac agent without knowing the difference.

This package does not import Hermes, Application Intelligence analyses, or any
AI code path. It reuses two stateless helpers from that package — a Bitbucket
metadata client and a set of URL/redaction validators — and nothing else.
"""

from .engine import advance_ci_builds  # noqa: F401
