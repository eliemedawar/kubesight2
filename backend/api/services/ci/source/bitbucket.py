"""Bitbucket Cloud source provider.

Thin adapter over the read-only metadata client Application Intelligence
already ships (``application_intelligence_bitbucket``) and the URL validator in
``application_intelligence_security``. Both are reused rather than reimplemented
— they already enforce HTTPS, reject credentials embedded in URLs, pin the API
origin, and cap response sizes. Reusing them is also why CI inherits those
protections for free.

Note the direction of the dependency: this module imports two *stateless
security/HTTP helpers* from the Application Intelligence package. It does not
touch analyses, Hermes, or any AI code path, and nothing here requires an
``IntelligenceApplication`` to exist.
"""

from __future__ import annotations

import fnmatch

from typing import Any, Dict, List, Optional

from ....secret_encryption import decrypt_secret
from ...application_intelligence_bitbucket import (
    BitbucketMetadataError,
    fetch_file,
    list_revisions,
    list_tree,
)
from ...application_intelligence_security import validate_relative_path, validate_repository_url
from . import CheckoutSpec, RepositoryRef, RevisionOption, SourceError, TreeListing
from . import bitbucket_status

# The port's verdict vocabulary, in Bitbucket's words.
_BITBUCKET_STATES = {
    "running": bitbucket_status.STATE_INPROGRESS,
    "passed": bitbucket_status.STATE_SUCCESSFUL,
    "failed": bitbucket_status.STATE_FAILED,
}


def _pattern_covers(restriction: Dict[str, Any], branch: str) -> bool:
    """Whether one branch restriction applies to one destination branch.

    Bitbucket matches either a glob pattern or a branching-model TYPE
    ("production", "development", ...). A type-matched restriction cannot be
    resolved from the pattern alone — the model says which branch is which — so
    it is treated as covering. Reporting "not enforced" for a repository that IS
    protected by its branching model would train people to ignore the warning,
    which costs more than the rare false reassurance.
    """
    if str(restriction.get("matchKind") or "glob") == "branching_model":
        return True
    pattern = str(restriction.get("pattern") or "")
    if not pattern:
        return False
    return fnmatch.fnmatch(branch, pattern)


class BitbucketSourceProvider:
    provider = "bitbucket"

    def parse_repository_url(self, url: str) -> RepositoryRef:
        normalized, ref = validate_repository_url(url)
        workspace, name = ref.split("/", 1)
        return RepositoryRef(
            provider=self.provider, url=normalized, workspace=workspace, name=name
        )

    def _credential_parts(self, credential) -> tuple:
        if credential is None:
            raise SourceError("No source credential is configured for this service.")
        if not credential.enabled:
            raise SourceError(f"Credential profile '{credential.name}' is disabled.")
        token = decrypt_secret(credential.secret_cipher or "")
        if not token:
            raise SourceError(
                f"Credential profile '{credential.name}' has no usable secret. "
                "Re-enter it and try again."
            )
        return token, credential.credential_type, (credential.principal or "")

    def list_revisions(
        self, ref: RepositoryRef, credential, kinds: tuple = ()
    ) -> List[RevisionOption]:
        token, credential_type, principal = self._credential_parts(credential)
        try:
            payload = list_revisions(
                ref.full_name,
                token,
                credential_type,
                principal,
                kinds=tuple(kinds) or ("branch", "tag", "commit"),
            )
        except BitbucketMetadataError as exc:
            raise SourceError(str(exc)) from exc
        except ValueError as exc:
            raise SourceError(str(exc)) from exc
        return [
            RevisionOption(
                value=item.get("value", ""),
                label=item.get("label", ""),
                kind=item.get("type", "branch"),
                commit=item.get("commit", "") or "",
            )
            for item in payload.get("items", [])
            if item.get("value")
        ]

    def read_file(self, ref: RepositoryRef, credential, revision: str, path: str) -> str:
        """One file's text at one revision, for reading configuration out of a
        repository without cloning it."""
        token, credential_type, principal = self._credential_parts(credential)
        try:
            return fetch_file(
                ref.full_name, token, revision, path, credential_type, principal
            )
        except BitbucketMetadataError as exc:
            raise SourceError(str(exc)) from exc
        except ValueError as exc:
            raise SourceError(str(exc)) from exc

    def list_tree(self, ref: RepositoryRef, credential, revision: str) -> TreeListing:
        """Every file path at one revision, in one paginated API walk.

        This is what lets repository analysis see the SHAPE of a project — a
        Gradle wrapper, an ``app/`` module, three ``pom.xml`` files — without a
        clone, a workspace, or a Kubernetes Job.
        """
        token, credential_type, principal = self._credential_parts(credential)
        try:
            payload = list_tree(
                ref.full_name, token, revision, credential_type, principal
            )
        except BitbucketMetadataError as exc:
            raise SourceError(str(exc)) from exc
        except ValueError as exc:
            raise SourceError(str(exc)) from exc
        return TreeListing(
            revision=payload.get("revision", revision),
            paths=list(payload.get("paths") or []),
            truncated=bool(payload.get("truncated")),
        )

    def verify_access(self, ref: RepositoryRef, credential) -> Dict[str, Any]:
        """Read the ref list as a liveness + authorization probe.

        Listing refs is the cheapest call that proves all three things a build
        needs: the repository exists, the credential is accepted, and it has
        read scope.
        """
        revisions = self.list_revisions(ref, credential)
        branches = [item for item in revisions if item.kind == "branch"]
        return {
            "ok": True,
            "repository": ref.full_name,
            "branchCount": len(branches),
            "branches": [item.value for item in branches[:50]],
            "message": f"Connected to {ref.full_name} — {len(branches)} branches visible.",
        }

    def checkout_spec(
        self,
        ref: RepositoryRef,
        credential,
        revision: str,
        working_directory: Optional[str] = None,
    ) -> CheckoutSpec:
        token, credential_type, principal = self._credential_parts(credential)
        # Credentials travel as environment variables so they never enter a
        # command line (visible in `ps`), a remote URL, or the build log.
        credential_env = {
            "KUBESIGHT_GIT_TOKEN": token,
            "KUBESIGHT_GIT_CREDENTIAL_TYPE": credential_type,
            "KUBESIGHT_GIT_PRINCIPAL": principal,
        }
        return CheckoutSpec(
            url=ref.url,
            revision=revision,
            credential_env=credential_env,
            working_directory=validate_relative_path(
                working_directory, "Working directory"
            ),
        )

    # --- Writes ------------------------------------------------------------
    # The only two calls in CI that change anything on the source host. Both
    # belong to merge checks: a verdict nobody is told is not a gate.

    def post_check_verdict(
        self,
        ref: RepositoryRef,
        credential,
        *,
        commit_sha: str,
        status_key: str,
        state: str,
        name: str,
        description: str,
        url: str,
    ) -> None:
        token, credential_type, principal = self._credential_parts(credential)
        if credential.read_only:
            # Caught here rather than as a 403 eight seconds later, because the
            # answer ("use a credential that can write") is the same and this
            # way it arrives before the checks have run.
            raise SourceError(
                f"Credential profile '{credential.name}' is marked read-only. "
                "Reporting a merge check verdict writes a build status, which "
                "needs a credential with write access.",
                retryable=False,
            )
        try:
            bitbucket_status.post_build_status(
                repository_ref=ref.full_name,
                commit_sha=commit_sha,
                key=status_key,
                state=_BITBUCKET_STATES.get(state, bitbucket_status.STATE_INPROGRESS),
                name=name,
                description=description,
                url=url,
                token=token,
                credential_type=credential_type,
                principal=principal,
            )
        except bitbucket_status.StatusWriteError as exc:
            raise SourceError(str(exc), retryable=exc.retryable) from exc

    def post_pull_request_note(
        self, ref: RepositoryRef, credential, *, pull_request_id: str, markdown: str
    ) -> None:
        token, credential_type, principal = self._credential_parts(credential)
        try:
            bitbucket_status.post_pull_request_comment(
                repository_ref=ref.full_name,
                pull_request_id=pull_request_id,
                markdown=markdown,
                token=token,
                credential_type=credential_type,
                principal=principal,
            )
        except bitbucket_status.StatusWriteError as exc:
            raise SourceError(str(exc), retryable=exc.retryable) from exc

    def check_merge_enforcement(
        self, ref: RepositoryRef, credential, *, branches: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Whether Bitbucket will actually refuse the merge on a failed check.

        The one question a merge gate has to be able to answer about itself.
        KubeSight posting a red build status is necessary and not sufficient:
        until a branch restriction requires passing builds, that red status is
        decoration, and the merge goes through.

        ``branches`` is the list of destination branches the gate watches (empty
        = all). A restriction is only reassuring if it covers those, so a
        repository that protects ``release/*`` while the gate watches ``master``
        is reported as NOT enforced rather than as "a restriction exists".
        """
        token, credential_type, principal = self._credential_parts(credential)
        try:
            restrictions = bitbucket_status.fetch_build_restrictions(
                repository_ref=ref.full_name,
                token=token,
                credential_type=credential_type,
                principal=principal,
            )
        except bitbucket_status.StatusWriteError as exc:
            raise SourceError(str(exc), retryable=exc.retryable) from exc

        active = [item for item in restrictions if item["minimum"] > 0]
        watched = [branch for branch in (branches or []) if branch]
        covered, uncovered = [], []
        for branch in watched:
            if any(_pattern_covers(item, branch) for item in active):
                covered.append(branch)
            else:
                uncovered.append(branch)

        return {
            "restrictions": active,
            "covered": covered,
            "uncovered": uncovered,
            # With no watched branches the gate applies to every branch, and no
            # realistic set of patterns covers "every branch" — so the honest
            # answer is "there is a restriction", not "you are covered".
            "enforced": bool(active) and not uncovered,
            "checkedBranches": watched,
        }
