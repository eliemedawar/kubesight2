"""Security core for the OpenID Connect authorization-code flow.

Persistence and Flask routes intentionally live outside this module.  Keeping
discovery, PKCE, token validation, and claim policy pure makes the dangerous
parts independently testable before the OIDC tables are added by Alembic.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

import jwt
from jwt import PyJWKClient


_ASYMMETRIC_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
    }
)
_AUTHORIZATION_PARAMETER_NAMES = frozenset(
    {
        "client_id",
        "code_challenge",
        "code_challenge_method",
        "nonce",
        "redirect_uri",
        "response_type",
        "scope",
        "state",
    }
)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_MAX_DOCUMENT_BYTES = 1024 * 1024
_CLAIM_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_USERNAME_CHARACTER = re.compile(r"[^A-Za-z0-9._-]+")


class OidcConfigurationError(ValueError):
    """Raised when operator-provided OIDC configuration is unsafe."""


class OidcProtocolError(RuntimeError):
    """Raised when an OIDC provider response fails closed."""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def oidc_enabled(environ: Mapping[str, str] | None = None) -> bool:
    source = environ if environ is not None else os.environ
    return _truthy(source.get("OIDC_ENABLED"))


def _parse_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _canonical_domain(value: str) -> str:
    candidate = value.strip().lower().rstrip(".")
    if not candidate or "@" in candidate or "/" in candidate:
        raise OidcConfigurationError("OIDC_ALLOWED_DOMAINS contains an invalid domain.")
    try:
        encoded = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise OidcConfigurationError(
            "OIDC_ALLOWED_DOMAINS contains an invalid domain."
        ) from exc
    if "." not in encoded or any(not label for label in encoded.split(".")):
        raise OidcConfigurationError(
            "OIDC_ALLOWED_DOMAINS must contain fully qualified domains."
        )
    return encoded


def _validate_url(
    value: str,
    name: str,
    *,
    allow_insecure_localhost: bool,
    allow_query: bool,
) -> str:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc or not parsed.hostname:
        raise OidcConfigurationError(f"{name} must be an absolute URL.")
    insecure_local = (
        allow_insecure_localhost
        and parsed.scheme == "http"
        and parsed.hostname.lower() in _LOCAL_HOSTS
    )
    if parsed.scheme != "https" and not insecure_local:
        raise OidcConfigurationError(f"{name} must use HTTPS.")
    if parsed.username or parsed.password or parsed.fragment:
        raise OidcConfigurationError(
            f"{name} must not contain userinfo or a fragment."
        )
    if parsed.query and not allow_query:
        raise OidcConfigurationError(f"{name} must not contain a query string.")
    return value


def _parse_group_mapping(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OidcConfigurationError(
            "OIDC_GROUP_ROLE_MAPPINGS must be a JSON object."
        ) from exc
    if not isinstance(document, dict):
        raise OidcConfigurationError(
            "OIDC_GROUP_ROLE_MAPPINGS must be a JSON object."
        )
    mappings: dict[str, str] = {}
    for group, role in document.items():
        normalized_group = str(group).strip()
        normalized_role = str(role).strip()
        if not normalized_group or not normalized_role:
            raise OidcConfigurationError(
                "OIDC group and role names must be non-empty strings."
            )
        mappings[normalized_group] = normalized_role
    return mappings


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    allowed_domains: frozenset[str]
    group_role_mappings: Mapping[str, str]
    default_role: str | None
    groups_claim: str
    scopes: tuple[str, ...]
    token_endpoint_auth_method: str
    allowed_algorithms: tuple[str, ...]
    clock_skew_seconds: int
    http_timeout_seconds: int
    allow_insecure_localhost: bool

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "OidcConfig":
        source = environ if environ is not None else os.environ
        production = source.get("KUBESIGHT_ENV", "").strip().lower() == "production"
        allow_insecure = (
            not production and _truthy(source.get("OIDC_ALLOW_INSECURE_HTTP"))
        )
        issuer = (source.get("OIDC_ISSUER_URL") or "").strip()
        client_id = (source.get("OIDC_CLIENT_ID") or "").strip()
        client_secret = source.get("OIDC_CLIENT_SECRET") or ""
        redirect_uri = (source.get("OIDC_REDIRECT_URI") or "").strip()
        missing = [
            name
            for name, value in (
                ("OIDC_ISSUER_URL", issuer),
                ("OIDC_CLIENT_ID", client_id),
                ("OIDC_REDIRECT_URI", redirect_uri),
            )
            if not value
        ]
        if missing:
            raise OidcConfigurationError(
                "Missing required OIDC settings: " + ", ".join(missing)
            )
        _validate_url(
            issuer,
            "OIDC_ISSUER_URL",
            allow_insecure_localhost=allow_insecure,
            allow_query=False,
        )
        _validate_url(
            redirect_uri,
            "OIDC_REDIRECT_URI",
            allow_insecure_localhost=allow_insecure,
            allow_query=False,
        )

        auth_method = (
            source.get("OIDC_TOKEN_ENDPOINT_AUTH_METHOD")
            or "client_secret_basic"
        ).strip()
        if auth_method not in {"client_secret_basic", "client_secret_post"}:
            raise OidcConfigurationError(
                "OIDC_TOKEN_ENDPOINT_AUTH_METHOD must be client_secret_basic "
                "or client_secret_post."
            )
        if not client_secret:
            raise OidcConfigurationError("OIDC_CLIENT_SECRET must be set.")

        domains = frozenset(
            _canonical_domain(item)
            for item in _parse_csv(source.get("OIDC_ALLOWED_DOMAINS", ""))
        )
        if not domains:
            raise OidcConfigurationError(
                "OIDC_ALLOWED_DOMAINS must contain at least one verified domain."
            )
        mappings = _parse_group_mapping(
            source.get("OIDC_GROUP_ROLE_MAPPINGS", "")
        )
        default_role = (source.get("OIDC_DEFAULT_ROLE") or "").strip() or None
        if not mappings and not default_role:
            raise OidcConfigurationError(
                "Configure OIDC_GROUP_ROLE_MAPPINGS or OIDC_DEFAULT_ROLE."
            )

        groups_claim = (source.get("OIDC_GROUPS_CLAIM") or "groups").strip()
        if not _CLAIM_NAME.fullmatch(groups_claim):
            raise OidcConfigurationError("OIDC_GROUPS_CLAIM is invalid.")
        scopes = _parse_csv(source.get("OIDC_SCOPES", "openid,email,profile"))
        if "openid" not in scopes or "email" not in scopes:
            raise OidcConfigurationError(
                "OIDC_SCOPES must include openid and email."
            )

        algorithms = _parse_csv(source.get("OIDC_ALLOWED_ALGORITHMS", "RS256"))
        if not algorithms or any(
            algorithm not in _ASYMMETRIC_ALGORITHMS for algorithm in algorithms
        ):
            raise OidcConfigurationError(
                "OIDC_ALLOWED_ALGORITHMS may contain only approved asymmetric algorithms."
            )
        try:
            clock_skew = min(
                300, max(0, int(source.get("OIDC_CLOCK_SKEW_SECONDS", "60")))
            )
            timeout = min(
                30, max(1, int(source.get("OIDC_HTTP_TIMEOUT_SECONDS", "5")))
            )
        except ValueError as exc:
            raise OidcConfigurationError(
                "OIDC timeout and clock-skew settings must be integers."
            ) from exc

        return cls(
            issuer=issuer,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            allowed_domains=domains,
            group_role_mappings=mappings,
            default_role=default_role,
            groups_claim=groups_claim,
            scopes=scopes,
            token_endpoint_auth_method=auth_method,
            allowed_algorithms=algorithms,
            clock_skew_seconds=clock_skew,
            http_timeout_seconds=timeout,
            allow_insecure_localhost=allow_insecure,
        )


@dataclass(frozen=True)
class OidcDiscovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str

    @classmethod
    def from_document(
        cls, config: OidcConfig, document: Mapping[str, Any]
    ) -> "OidcDiscovery":
        issuer = str(document.get("issuer") or "")
        if issuer != config.issuer:
            raise OidcProtocolError(
                "OIDC discovery issuer does not exactly match OIDC_ISSUER_URL."
            )
        methods = document.get("code_challenge_methods_supported")
        if not isinstance(methods, list) or "S256" not in methods:
            raise OidcProtocolError(
                "OIDC provider metadata must advertise PKCE S256 support."
            )
        endpoints: dict[str, str] = {}
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = str(document.get(key) or "")
            try:
                endpoints[key] = _validate_url(
                    value,
                    f"OIDC discovery {key}",
                    allow_insecure_localhost=config.allow_insecure_localhost,
                    allow_query=True,
                )
            except OidcConfigurationError as exc:
                raise OidcProtocolError(str(exc)) from exc
        return cls(issuer=issuer, **endpoints)


def _read_json_response(response: Any, description: str) -> Mapping[str, Any]:
    raw = response.read(_MAX_DOCUMENT_BYTES + 1)
    if len(raw) > _MAX_DOCUMENT_BYTES:
        raise OidcProtocolError(f"{description} exceeded the response-size limit.")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OidcProtocolError(f"{description} was not valid JSON.") from exc
    if not isinstance(document, dict):
        raise OidcProtocolError(f"{description} must be a JSON object.")
    return document


def fetch_discovery(
    config: OidcConfig,
    *,
    opener: Callable[..., Any] = urlopen,
) -> OidcDiscovery:
    url = f"{config.issuer.rstrip('/')}/.well-known/openid-configuration"
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with opener(request, timeout=config.http_timeout_seconds) as response:
            document = _read_json_response(response, "OIDC discovery response")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise OidcProtocolError("OIDC discovery request failed.") from exc
    return OidcDiscovery.from_document(config, document)


def hash_transaction_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_return_to(value: str | None) -> str:
    candidate = (value or "/").strip()
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or "\\" in candidate
        or "\r" in candidate
        or "\n" in candidate
    ):
        raise OidcProtocolError("OIDC returnTo must be a local absolute path.")
    return candidate


@dataclass(frozen=True)
class OidcAuthorizationTransaction:
    state: str
    nonce: str
    code_verifier: str
    browser_binding: str
    return_to: str
    authorization_url: str


def begin_authorization(
    config: OidcConfig,
    discovery: OidcDiscovery,
    *,
    return_to: str | None = None,
) -> OidcAuthorizationTransaction:
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    browser_binding = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    parsed = urlparse(discovery.authorization_endpoint)
    existing = parse_qsl(parsed.query, keep_blank_values=True)
    if _AUTHORIZATION_PARAMETER_NAMES.intersection(key for key, _ in existing):
        raise OidcProtocolError(
            "OIDC authorization endpoint contains conflicting query parameters."
        )
    parameters = existing + [
        ("response_type", "code"),
        ("client_id", config.client_id),
        ("redirect_uri", config.redirect_uri),
        ("scope", " ".join(config.scopes)),
        ("state", state),
        ("nonce", nonce),
        ("code_challenge", challenge),
        ("code_challenge_method", "S256"),
    ]
    authorization_url = urlunparse(parsed._replace(query=urlencode(parameters)))
    return OidcAuthorizationTransaction(
        state=state,
        nonce=nonce,
        code_verifier=code_verifier,
        browser_binding=browser_binding,
        return_to=safe_return_to(return_to),
        authorization_url=authorization_url,
    )


def exchange_code(
    config: OidcConfig,
    discovery: OidcDiscovery,
    *,
    code: str,
    code_verifier: str,
    opener: Callable[..., Any] = urlopen,
) -> Mapping[str, Any]:
    if not code or not code_verifier:
        raise OidcProtocolError("OIDC code and PKCE verifier are required.")
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.redirect_uri,
        "code_verifier": code_verifier,
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if config.token_endpoint_auth_method == "client_secret_basic":
        credential = base64.b64encode(
            (
                f"{quote_plus(config.client_id)}:"
                f"{quote_plus(config.client_secret)}"
            ).encode("utf-8")
        ).decode("ascii")
        headers["Authorization"] = f"Basic {credential}"
        form["client_id"] = config.client_id
    else:
        form["client_id"] = config.client_id
        form["client_secret"] = config.client_secret
    request = Request(
        discovery.token_endpoint,
        data=urlencode(form).encode("ascii"),
        headers=headers,
        method="POST",
    )
    try:
        with opener(request, timeout=config.http_timeout_seconds) as response:
            document = _read_json_response(response, "OIDC token response")
    except HTTPError as exc:
        raise OidcProtocolError(
            f"OIDC token exchange failed with HTTP {exc.code}."
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise OidcProtocolError("OIDC token exchange failed.") from exc
    if not isinstance(document.get("id_token"), str) or not document["id_token"]:
        raise OidcProtocolError("OIDC token response did not contain an ID token.")
    return document


def validate_id_token(
    config: OidcConfig,
    discovery: OidcDiscovery,
    *,
    id_token: str,
    expected_nonce: str,
    jwk_client_factory: Callable[..., Any] = PyJWKClient,
) -> Mapping[str, Any]:
    try:
        header = jwt.get_unverified_header(id_token)
        algorithm = str(header.get("alg") or "")
        if algorithm not in config.allowed_algorithms:
            raise OidcProtocolError("OIDC ID token algorithm is not allowed.")
        if not header.get("kid"):
            raise OidcProtocolError("OIDC ID token is missing its key id.")
        jwk_client = jwk_client_factory(
            discovery.jwks_uri,
            timeout=config.http_timeout_seconds,
        )
        signing_key = jwk_client.get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(
            id_token,
            signing_key,
            algorithms=[algorithm],
            audience=config.client_id,
            issuer=config.issuer,
            leeway=config.clock_skew_seconds,
            options={
                "require": ["aud", "exp", "iat", "iss", "nonce", "sub"],
            },
        )
    except OidcProtocolError:
        raise
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise OidcProtocolError("OIDC ID token validation failed.") from exc

    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not hmac.compare_digest(
        nonce, expected_nonce
    ):
        raise OidcProtocolError("OIDC ID token nonce validation failed.")
    audiences = claims.get("aud")
    if isinstance(audiences, list) and len(audiences) > 1:
        if claims.get("azp") != config.client_id:
            raise OidcProtocolError(
                "OIDC ID token authorized-party validation failed."
            )
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise OidcProtocolError("OIDC ID token subject is invalid.")
    return claims


@dataclass(frozen=True)
class OidcPrincipal:
    subject: str
    email: str
    username: str
    full_name: str
    groups: tuple[str, ...]
    role_name: str


def principal_from_claims(
    config: OidcConfig, claims: Mapping[str, Any]
) -> OidcPrincipal:
    if claims.get("email_verified") is not True:
        raise OidcProtocolError("OIDC email claim is not verified.")
    raw_email = claims.get("email")
    if not isinstance(raw_email, str) or raw_email.count("@") != 1:
        raise OidcProtocolError("OIDC email claim is invalid.")
    local_part, raw_domain = raw_email.strip().rsplit("@", 1)
    if not local_part:
        raise OidcProtocolError("OIDC email claim is invalid.")
    try:
        domain = _canonical_domain(raw_domain)
    except OidcConfigurationError as exc:
        raise OidcProtocolError("OIDC email domain is invalid.") from exc
    if domain not in config.allowed_domains:
        raise OidcProtocolError("OIDC email domain is not allowed.")

    raw_groups = claims.get(config.groups_claim, [])
    if not isinstance(raw_groups, list) or any(
        not isinstance(group, str) for group in raw_groups
    ):
        raise OidcProtocolError("OIDC groups claim must be an array of strings.")
    groups = tuple(dict.fromkeys(group.strip() for group in raw_groups if group.strip()))
    mapped_roles = {
        config.group_role_mappings[group]
        for group in groups
        if group in config.group_role_mappings
    }
    if len(mapped_roles) > 1:
        raise OidcProtocolError(
            "OIDC groups map to multiple roles; refuse ambiguous privilege assignment."
        )
    role_name = next(iter(mapped_roles), config.default_role)
    if not role_name:
        raise OidcProtocolError("OIDC groups do not map to an authorized role.")

    preferred = claims.get("preferred_username")
    username_source = preferred if isinstance(preferred, str) else local_part
    username = _USERNAME_CHARACTER.sub("-", username_source.strip()).strip(".-_")
    if not username:
        username = f"oidc-{hash_transaction_secret(str(claims['sub']))[:12]}"
    full_name = claims.get("name")
    return OidcPrincipal(
        subject=str(claims["sub"]),
        email=f"{local_part}@{domain}".lower()[:255],
        username=username[:120],
        full_name=(full_name.strip() if isinstance(full_name, str) else "")[:255],
        groups=groups,
        role_name=role_name,
    )
