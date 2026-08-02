# OpenID Connect and authentication recovery

KubeSight supports one OpenID Connect provider using the authorization-code
flow with PKCE S256. OIDC supplements local authentication; it does not remove
the local administrator account used for recovery. SAML and SCIM are not part
of this release.

## Security model

- The browser receives only KubeSight's HttpOnly session cookies. Provider
  authorization codes, ID tokens, access tokens, client secrets, PKCE
  verifiers, and refresh tokens are never returned to frontend JavaScript.
- Every login has independent high-entropy state, nonce, PKCE verifier, and an
  HttpOnly browser-binding cookie. Authorization state is single-use and
  expires after 10 minutes.
- KubeSight requires an exact discovery issuer match, PKCE S256 advertisement,
  HTTPS endpoints, an asymmetric allowlisted ID-token algorithm, signature,
  issuer, audience, authorized-party, lifetime, subject, and nonce validation.
- Email must be asserted with `email_verified: true` and its canonical domain
  must be explicitly allowlisted.
- Group-to-role mapping refuses login if the presented groups resolve to more
  than one distinct role. This prevents mapping order from silently deciding a
  user's privilege.
- Linking an OIDC subject to an existing account by matching email is disabled
  by default. It must be explicitly enabled after the operator verifies the
  provider's account and domain controls.

These choices follow [OpenID Connect Core 1.0](https://openid.net/specs/openid-connect-core-1_0-final.html),
[PKCE RFC 7636](https://www.rfc-editor.org/rfc/rfc7636.html), and the
[OAuth 2.0 Security BCP, RFC 9700](https://www.rfc-editor.org/rfc/rfc9700.html).

## Identity-provider registration

Register KubeSight as a confidential web client. Enable only the authorization
code response type and PKCE S256. Register one exact callback URI; do not use a
wildcard:

```text
https://kubesight.example.com/api/auth/oidc/callback
```

The ID token must include `sub`, `email`, `email_verified`, and the configured
groups claim. `preferred_username` and `name` are optional. The discovery
document must advertise `code_challenge_methods_supported` containing `S256`.

## Environment configuration

| Setting | Required | Meaning |
|---|---:|---|
| `OIDC_ENABLED` | yes | Set `true` to expose OIDC login. |
| `OIDC_ISSUER_URL` | yes | Exact issuer identifier from provider metadata. A trailing slash is identity-significant. |
| `OIDC_CLIENT_ID` | yes | Registered confidential client ID. |
| `OIDC_CLIENT_SECRET` | yes | Provider client secret. Keep separate from KubeSight signing and encryption keys. |
| `OIDC_REDIRECT_URI` | yes | Exact registered callback URI. |
| `OIDC_ALLOWED_DOMAINS` | yes | Comma-separated verified email domains; no wildcards. |
| `OIDC_GROUP_ROLE_MAPPINGS` | conditional | JSON object mapping exact group values to existing KubeSight role names. |
| `OIDC_DEFAULT_ROLE` | conditional | Existing role used when no group mapping matches. Omit to reject unmapped users. |
| `OIDC_GROUPS_CLAIM` | no | Claim containing the string array of groups; default `groups`. |
| `OIDC_SCOPES` | no | Comma-separated scopes; default `openid,email,profile`. Must retain `openid` and `email`. |
| `OIDC_ALLOWED_ALGORITHMS` | no | Comma-separated asymmetric algorithms; default `RS256`. HMAC ID tokens are refused. |
| `OIDC_TOKEN_ENDPOINT_AUTH_METHOD` | no | `client_secret_basic` (default) or `client_secret_post`. |
| `OIDC_AUTO_PROVISION` | no | Default `true`; create a local user only after domain and role policy succeeds. |
| `OIDC_LINK_BY_EMAIL` | no | Default `false`; permit a new subject to bind to an existing verified-email account. |
| `OIDC_CLOCK_SKEW_SECONDS` | no | Token-validation leeway, clamped to 0–300 seconds; default 60. |
| `OIDC_HTTP_TIMEOUT_SECONDS` | no | Discovery/JWKS/token timeout, clamped to 1–30 seconds; default 5. |

Example:

```dotenv
OIDC_ENABLED=true
OIDC_ISSUER_URL=https://idp.example.com/realms/platform
OIDC_CLIENT_ID=kubesight-production
OIDC_CLIENT_SECRET=<secret-from-provider>
OIDC_REDIRECT_URI=https://kubesight.example.com/api/auth/oidc/callback
OIDC_ALLOWED_DOMAINS=example.com
OIDC_GROUP_ROLE_MAPPINGS={"kubesight-admins":"admin","kubesight-operators":"operator","kubesight-viewers":"viewer"}
OIDC_AUTO_PROVISION=true
OIDC_LINK_BY_EMAIL=false
```

When `KUBESIGHT_ENV=production` and OIDC is enabled, startup refuses incomplete
or insecure OIDC configuration. It also refuses an OIDC client secret reused as
`JWT_SECRET_KEY` or `ALERT_ROUTING_SECRET_KEY`.

Plain HTTP is accepted only for `localhost`, `127.0.0.1`, or `::1` in a
non-production process when `OIDC_ALLOW_INSECURE_HTTP=true` is explicit.

## Browser contract

- `GET /api/auth/oidc/status` returns only enablement and the issuer.
- Navigate the browser to
  `GET /api/auth/oidc/login?returnTo=/local/path`. `returnTo` must be a local
  absolute path; external and scheme-relative redirects are rejected.
- KubeSight owns the callback. On success it installs the normal access,
  refresh, and CSRF cookies, then redirects to `returnTo`. On failure it
  redirects to the fixed `/login?oidc=failed` path.
- No provider token is present in either response.

## Role and account lifecycle

The `(issuer, sub)` pair is the durable identity key. Email is profile data, not
the primary key. On every successful login KubeSight re-evaluates group mapping
and synchronizes the bound user's role. Disabled users, service accounts, and
accounts with interactive login disabled are refused.

Auto-provisioning creates an active interactive user with an unusable random
local password. If an existing user already has the same email, login is
refused unless `OIDC_LINK_BY_EMAIL=true`; this avoids treating email coincidence
as proof of account ownership.

## MFA recovery codes

Completing first TOTP enrollment returns ten recovery codes exactly once. Each
code has 80 bits of randomness and is stored only as a SHA-256 hash. Codes are
single-use; concurrent reuse has one winner.

- During an MFA challenge, submit `POST /api/auth/mfa/recover` with
  `{ "recoveryCode": "...." }`.
- An authenticated user can inspect only the remaining count with
  `GET /api/auth/mfa/recovery-codes`.
- Regenerate with `POST /api/auth/mfa/recovery-codes` and
  `{ "code": "current TOTP" }`. Regeneration invalidates every older code and
  returns the replacement set exactly once.

Operators should instruct users to download or print the initial set and keep
it outside the device running their authenticator.

## Administrator break-glass recovery

OIDC remains additive so an IdP outage does not eliminate local recovery. From
an authenticated host with database access, mint a one-time grant:

```powershell
python backend/manage.py admin-recovery admin --minutes 10
```

The command accepts only an active, interactive administrator and prints one
raw token once. KubeSight stores only its hash. Submit the username and token to
`POST /api/auth/admin-recovery`; the token is atomically consumed, all existing
sessions are revoked, password/MFA lock state is cleared, and the administrator
is placed directly into TOTP re-enrollment. Abandoned or expired grants cannot
be reused. Minting a new grant invalidates older unused grants.

The local administrator should have its own strong, rotated password and an
offline recovery-code copy. Do not use a shared OIDC account as the break-glass
identity.

## Audit events

OIDC start, success, failure, provisioning, identity linking, and role changes
are audited without state, codes, tokens, nonces, PKCE material, or provider
secrets. Recovery-code generation/use/rejection and administrator-grant
creation/use/rejection are also audited without raw or hashed credential
material.
