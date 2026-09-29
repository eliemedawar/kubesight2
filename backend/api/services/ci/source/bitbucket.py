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
    "stopped": bitbucket_status.STATE_STOPPED,
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


def _without_query(url: str) -> str:
    return str(url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()


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

    def find_webhook(
        self, ref: RepositoryRef, credential, *, url: str
    ) -> Optional[Dict[str, Any]]:
        """The repository webhook that already points at ``url``, if any.

        Matched on the URL without its query string, so a webhook set up by hand
        with the ``?secret=`` fallback is recognised and corrected rather than
        left beside a second one that fires the same checks twice.
        """
        token, credential_type, principal = self._credential_parts(credential)
        try:
            hooks = bitbucket_status.list_webhooks(
                repository_ref=ref.full_name,
                token=token,
                credential_type=credential_type,
                principal=principal,
            )
        except bitbucket_status.StatusWriteError as exc:
            raise SourceError(str(exc), retryable=exc.retryable) from exc
        wanted = _without_query(url)
        for hook in hooks:
            if _without_query(hook["url"]) == wanted:
                return hook
        return None

    def ensure_webhook(
        self,
        ref: RepositoryRef,
        credential,
        *,
        url: str,
        secret: str,
        events: List[str],
        description: str,
    ) -> Dict[str, Any]:
        """Create the webhook, or update the one already pointing at ``url``."""
        if credential is not None and credential.read_only:
            raise SourceError(
                f"Credential profile '{credential.name}' is marked read-only. "
                "Creating a webhook is a write, so it needs a credential with "
                "write access.",
                retryable=False,
            )
        existing = self.find_webhook(ref, credential, url=url)
        token, credential_type, principal = self._credential_parts(credential)
        try:
            saved = bitbucket_status.save_webhook(
                repository_ref=ref.full_name,
                uid=existing["uuid"] if existing else "",
                url=url,
                secret=secret,
                events=events,
                description=description,
                token=token,
                credential_type=credential_type,
                principal=principal,
            )
        except bitbucket_status.StatusWriteError as exc:
            raise SourceError(str(exc), retryable=exc.retryable) from exc
        return {
            "action": "updated" if existing else "created",
            "uuid": str(saved.get("uuid") or (existing or {}).get("uuid") or ""),
            "url": url,
            "events": list(events),
        }

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

        builds = [
            item for item in restrictions
            if item.get("kind", bitbucket_status.BUILD_RESTRICTION_KIND)
            == bitbucket_status.BUILD_RESTRICTION_KIND
        ]
        hard = [
            item for item in restrictions
            if item.get("kind") == bitbucket_status.ENFORCE_CHECKS_KIND
        ]
        active = [item for item in builds if item["minimum"] > 0]
        watched = [branch for branch in (branches or []) if branch]
        covered, uncovered = [], []
        for branch in watched:
            if any(_pattern_covers(item, branch) for item in active):
                covered.append(branch)
            else:
                uncovered.append(branch)
        # Whether a failed check REFUSES the merge rather than warning beside it.
        # Bitbucket Cloud only refuses with "enforce merge checks" (Premium).
        hard_covered = bool(watched) and all(
            any(_pattern_covers(item, branch) for item in hard) for branch in watched
        )

        return {
            "restrictions": active,
            "covered": covered,
            "uncovered": uncovered,
            # With no watched branches the gate applies to every branch, and no
            # realistic set of patterns covers "every branch" — so the honest
            # answer is "there is a restriction", not "you are covered".
            "enforced": bool(active) and not uncovered,
            "hardBlock": bool(active) and not uncovered and hard_covered,
            "checkedBranches": watched,
        }

    def ensure_merge_protection(
        self,
        ref: RepositoryRef,
        credential,
        *,
        branches: List[str],
        block_direct_push: bool = False,
    ) -> Dict[str, Any]:
        """Make each branch wait for a passing build before it can be merged.

        Per branch pattern, idempotently:

        * ``require_passing_builds_to_merge`` with at least one — KubeSight
          files INPROGRESS the moment a pull request arrives and FAILED when it
          blocks, and Bitbucket counts "no failed and no in-progress builds", so
          a pull request cannot merge before KubeSight has said yes;
        * ``enforce_merge_checks`` — the Premium switch that makes that a
          refusal instead of a warning. Refused on other plans, which is
          reported, not raised: the first restriction is still worth having;
        * optionally ``push`` with nobody allowed, so the branch cannot be
          changed except by merging a pull request. An existing push
          restriction is never touched — somebody chose who may push.
        """
        if credential is not None and credential.read_only:
            raise SourceError(
                f"Credential profile '{credential.name}' is marked read-only. "
                "Changing branch restrictions is a write.",
                retryable=False,
            )
        token, credential_type, principal = self._credential_parts(credential)
        auth = {
            "repository_ref": ref.full_name,
            "token": token,
            "credential_type": credential_type,
            "principal": principal,
        }

        def existing(kind: str) -> Dict[str, Dict[str, Any]]:
            items = bitbucket_status.list_branch_restrictions(kind=kind, **auth)
            return {
                item["pattern"]: item for item in items if item["matchKind"] == "glob"
            }

        try:
            builds = existing(bitbucket_status.BUILD_RESTRICTION_KIND)
            hard = existing(bitbucket_status.ENFORCE_CHECKS_KIND)
            pushes = existing(bitbucket_status.PUSH_KIND) if block_direct_push else {}
            results = []
            hard_error = ""
            for branch in branches:
                row: Dict[str, Any] = {"branch": branch}

                current = builds.get(branch)
                if current is None:
                    bitbucket_status.save_branch_restriction(
                        kind=bitbucket_status.BUILD_RESTRICTION_KIND,
                        pattern=branch, value=1, **auth,
                    )
                    row["passingBuilds"] = "created"
                elif int(current.get("value") or 0) < 1:
                    bitbucket_status.save_branch_restriction(
                        restriction_id=str(current["id"]),
                        kind=bitbucket_status.BUILD_RESTRICTION_KIND,
                        pattern=branch, value=1, **auth,
                    )
                    row["passingBuilds"] = "updated"
                else:
                    row["passingBuilds"] = "existing"

                if branch in hard:
                    row["enforceChecks"] = "existing"
                elif hard_error:
                    row["enforceChecks"] = "unavailable"
                else:
                    try:
                        bitbucket_status.save_branch_restriction(
                            kind=bitbucket_status.ENFORCE_CHECKS_KIND,
                            pattern=branch, **auth,
                        )
                        row["enforceChecks"] = "created"
                    except bitbucket_status.StatusWriteError as exc:
                        if exc.retryable or exc.status in (401, 403):
                            raise
                        hard_error = str(exc)
                        row["enforceChecks"] = "unavailable"

                if not block_direct_push:
                    row["directPush"] = "skipped"
                elif branch in pushes:
                    row["directPush"] = "existing"
                else:
                    bitbucket_status.save_branch_restriction(
                        kind=bitbucket_status.PUSH_KIND, pattern=branch, **auth,
                    )
                    row["directPush"] = "created"
                results.append(row)
        except bitbucket_status.StatusWriteError as exc:
            raise SourceError(str(exc), retryable=exc.retryable) from exc

        return {
            "branches": results,
            "hardBlock": not hard_error,
            "hardBlockError": hard_error,
        }
