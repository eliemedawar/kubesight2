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

from typing import Any, Dict, List, Optional

from ....secret_encryption import decrypt_secret
from ...application_intelligence_bitbucket import BitbucketMetadataError, list_revisions
from ...application_intelligence_security import validate_relative_path, validate_repository_url
from . import CheckoutSpec, RepositoryRef, RevisionOption, SourceError


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

    def list_revisions(self, ref: RepositoryRef, credential) -> List[RevisionOption]:
        token, credential_type, principal = self._credential_parts(credential)
        try:
            payload = list_revisions(
                ref.full_name, token, credential_type, principal
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
