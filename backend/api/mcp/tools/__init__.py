"""Every tool an agent may call, grouped by domain.

Importing this package registers all of them. The domain modules have no other
entry point and are never imported individually — a module missing from the list
below is a tool surface that silently does not exist, which is why the imports
are explicit rather than a directory scan: a scan hides the mistake, a list
makes it a one-line diff.

The seven domains, and what each answers:

======================  =====================================================
``ci``                  services, pipelines, builds, logs, runners, source
``clusters``            clusters, nodes, namespaces, resources, events, topology
``workloads``           what is running, and restart / scale / rollback / exec
``deploys``             apply, dry run, diff, approvals, change bundles, Helm
``observability``       pod logs, alerts, alert policies, audit, the dashboard
``apps``                application intelligence, app services, clients
``platform``            registries, ticketing, mobile releases, users, roles
======================  =====================================================

The same seven names split the ``kubesight`` skill into reference files, so an
agent reads one page about builds rather than every page about everything. That
is the whole reason the grouping exists, and it is why a new tool belongs in an
existing domain wherever it plausibly can.
"""

from __future__ import annotations

from .registry import (  # noqa: F401  (the package's public surface)
    DOMAINS,
    call,
    definitions,
    domain_of,
    is_write,
    known_names,
    tool,
)

# Registration happens on import. Ordered as the table above reads.
from . import ci  # noqa: F401,E402
from . import clusters  # noqa: F401,E402
from . import workloads  # noqa: F401,E402
from . import deploys  # noqa: F401,E402
from . import observability  # noqa: F401,E402
from . import apps  # noqa: F401,E402
from . import platform  # noqa: F401,E402

__all__ = [
    "DOMAINS",
    "call",
    "definitions",
    "domain_of",
    "is_write",
    "known_names",
    "tool",
]
