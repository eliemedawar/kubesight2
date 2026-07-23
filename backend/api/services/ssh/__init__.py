"""SSH transport for the Cluster Builder.

``transport`` — connect/run/put-file against a target (direct or via bastion),
with sudo escalation and secrets kept off the command line.
``hostkeys`` — strict | tofu | pinned host-key verification backed by the
``ssh_host_keys`` table.

paramiko is imported lazily inside the real transport so tests (which install
a fake transport via ``set_transport_factory``) and mock-mode deployments never
need it installed.
"""

from .transport import (  # noqa: F401
    SshCommandError,
    SshConnectionError,
    SshResult,
    SshTarget,
    get_transport,
    set_transport_factory,
)
