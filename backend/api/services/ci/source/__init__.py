"""Source providers.

The port every repository host implements. Bitbucket is the only provider in
Phase 1; the indirection exists because adding GitLab or GitHub should be one
new module and one registry line, not a sweep through the catalog, the pipeline
editor and the build engine looking for hardcoded Bitbucket assumptions.

Credentials are never passed in as raw values by callers above this layer — a
provider is handed a ``BitbucketCredentialProfile`` row and decrypts what it
needs itself, so the plaintext's lifetime stays inside the provider call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


class SourceError(RuntimeError):
    """A source host could not be reached or refused the credential.

    Messages are safe to show a user: they never echo a token or a URL that
    embeds one.
    """


@dataclass
class RepositoryRef:
    """A normalized repository address."""

    provider: str
    url: str
    workspace: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.workspace}/{self.name}"


@dataclass
class RevisionOption:
    """A branch, tag, or commit a build can be pinned to."""

    value: str
    label: str
    kind: str  # branch | tag | commit
    commit: str = ""


@dataclass
class CheckoutSpec:
    """What a runner needs to clone. Assembled at dispatch, never persisted."""

    url: str
    revision: str
    # Injected as environment variables by the runner, never interpolated into
    # a command string or a remote URL.
    credential_env: Dict[str, str] = field(default_factory=dict)
    working_directory: Optional[str] = None


@runtime_checkable
class SourceProvider(Protocol):
    provider: str

    def parse_repository_url(self, url: str) -> RepositoryRef:
        """Normalize and validate a repository URL. Raises ValueError."""

    def list_revisions(self, ref: RepositoryRef, credential) -> List[RevisionOption]:
        """Branches, tags, and recent commits. Raises :class:`SourceError`."""

    def verify_access(self, ref: RepositoryRef, credential) -> Dict[str, Any]:
        """Confirm the credential can read the repository."""

    def checkout_spec(
        self, ref: RepositoryRef, credential, revision: str, working_directory=None
    ) -> CheckoutSpec:
        """Everything a runner needs to fetch the source."""


_PROVIDERS: Dict[str, SourceProvider] = {}


def register_provider(provider: SourceProvider) -> None:
    _PROVIDERS[provider.provider] = provider


def get_provider(name: str) -> SourceProvider:
    provider = _PROVIDERS.get((name or "bitbucket").strip().lower())
    if provider is None:
        raise SourceError(f"Source provider '{name}' is not supported.")
    return provider


def supported_providers() -> List[str]:
    return sorted(_PROVIDERS)


from .bitbucket import BitbucketSourceProvider  # noqa: E402

register_provider(BitbucketSourceProvider())
