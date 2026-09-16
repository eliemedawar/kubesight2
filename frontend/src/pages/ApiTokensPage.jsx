import { useCallback, useEffect, useState } from "react";
import { createApiToken, listApiTokens, revokeApiToken } from "../api/apiTokensApi.js";
import { useAuth } from "../context/AuthContext";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import { parseApiTime } from "../lib/apiTime.js";

/**
 * API tokens — how a machine authenticates as a person.
 *
 * The whole page is built around one fact: a token is visible exactly once, at
 * the moment it is created. Only a prefix is stored afterwards, so there is no
 * "show again" to offer and no endpoint that could implement one. That is why
 * the new token gets a panel of its own rather than a toast — a copyable value
 * that disappears on the next render is a value somebody loses.
 *
 * A token carries its owner's permissions, not its own. Saying so on the page
 * matters more than it looks: the usual mistake is to mint one from an admin
 * account for a job that only needs to read.
 */

// parseApiTime returns epoch milliseconds (or NaN), not a Date — the backend
// serializes naive UTC, and that helper is where the missing "Z" gets added.
const relative = (value) => {
  const ms = parseApiTime(value);
  if (!Number.isFinite(ms)) return "—";
  return new Date(ms).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
};

const isExpired = (token) => {
  const ms = parseApiTime(token.expires_at);
  return Number.isFinite(ms) && ms < Date.now();
};

function NewTokenPanel({ token, onDismiss }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(token.token);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard is blocked in some browsers and over plain HTTP. The value is
      // on screen and selectable, so this is a convenience, not the mechanism.
    }
  };

  return (
    <section className="sg-tok-new" aria-label="New API token">
      <header>
        <h4>Copy this token now</h4>
        <p className="muted">
          It is shown once and cannot be retrieved. KubeSight stores only its
          first characters, so if you lose it you will need to create another.
        </p>
      </header>
      <div className="sg-tok-value">
        <code>{token.token}</code>
        <button type="button" className="primary btn-compact" onClick={copy}>
          {copied ? "Copied ✓" : "Copy"}
        </button>
      </div>
      <button type="button" className="btn-outline btn-compact" onClick={onDismiss}>
        I have saved it
      </button>
    </section>
  );
}

export default function ApiTokensPage() {
  const { user } = useAuth();
  const [tokens, setTokens] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [issued, setIssued] = useState(null);

  const load = useCallback(async () => {
    try {
      // Scoped to the caller by the API. There is deliberately no "everyone"
      // listing — a token is its owner's, and an admin wanting one belonging to
      // somebody else revokes it through that user rather than browsing them.
      const data = await listApiTokens();
      setTokens(data.tokens || []);
      setError("");
    } catch (err) {
      setError(err.message || "Could not load API tokens.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const create = async () => {
    if (!name.trim()) return;
    setCreating(true);
    setError("");
    try {
      const payload = { name: name.trim() };
      if (expiresAt) {
        // The API takes ISO 8601; a date input gives YYYY-MM-DD.
        payload.expires_at = new Date(`${expiresAt}T00:00:00Z`).toISOString();
      }
      const created = await createApiToken(payload);
      setIssued(created);
      setName("");
      setExpiresAt("");
      load();
    } catch (err) {
      setError(err.message || "Could not create the token.");
    } finally {
      setCreating(false);
    }
  };

  const revoke = async (token) => {
    if (
      !window.confirm(
        `Revoke "${token.name}"? Anything using it stops working immediately, and ` +
          "this cannot be undone."
      )
    )
      return;
    try {
      await revokeApiToken(token.id);
      load();
    } catch (err) {
      setError(err.message || "Could not revoke the token.");
    }
  };

  if (loading) return <LoadingState label="Loading API tokens…" />;

  const active = tokens.filter((item) => item.is_active && !isExpired(item));

  return (
    <div className="ops-page">
      <div className="sg-ph">
        <div>
          <h2>API tokens</h2>
          <p className="sg-ph-sub">
            For scripts, CI, and agents. A token acts as{" "}
            <strong>{user?.username || "you"}</strong> and carries exactly your
            permissions — nothing more.
          </p>
        </div>
      </div>

      {error && <ErrorBanner message={error} />}

      {issued && <NewTokenPanel token={issued} onDismiss={() => setIssued(null)} />}

      <section className="form-section sg-tok-create">
        <h4>Create a token</h4>
        <div className="form-grid">
          <label>
            Name *
            <input
              value={name}
              maxLength={120}
              placeholder="e.g. hermes-mcp"
              onChange={(event) => setName(event.target.value)}
            />
            <span className="field-hint">
              What will use it. This is all you will see of it afterwards.
            </span>
          </label>
          <label>
            Expires
            <input
              type="date"
              value={expiresAt}
              onChange={(event) => setExpiresAt(event.target.value)}
            />
            <span className="field-hint">
              Optional. A token with no expiry works until it is revoked.
            </span>
          </label>
          <div className="form-grid__full">
            <button
              type="button"
              className="primary"
              onClick={create}
              disabled={creating || !name.trim()}
            >
              {creating ? "Creating…" : "Create token"}
            </button>
          </div>
        </div>
      </section>

      <section className="form-section">
        <div className="sg-tok-head">
          <h4>
            {active.length} active {active.length === 1 ? "token" : "tokens"}
          </h4>
        </div>

        {tokens.length === 0 ? (
          <EmptyState
            title="No API tokens yet"
            message="Create one above to let a script, a pipeline or an agent authenticate as you."
          />
        ) : (
          <table className="sg-tok-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Token</th>
                <th>Created</th>
                <th>Expires</th>
                <th>Last used</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {tokens.map((token) => {
                const dead = !token.is_active || isExpired(token);
                return (
                  <tr key={token.id} className={dead ? "is-dead" : ""}>
                    <td>{token.name}</td>
                    <td>
                      <code>{token.prefix}…</code>
                    </td>
                    <td>{relative(token.created_at)}</td>
                    <td>
                      {token.expires_at ? (
                        <span className={isExpired(token) ? "sg-tok-expired" : ""}>
                          {relative(token.expires_at)}
                        </span>
                      ) : (
                        "Never"
                      )}
                    </td>
                    <td>
                      {/* Never used is worth seeing: it is how you find the one
                          you minted, pasted wrong, and forgot about. */}
                      {token.last_used_at ? relative(token.last_used_at) : "Never used"}
                    </td>
                    <td className="sg-tok-actions">
                      {!token.is_active ? (
                        <span className="muted">Revoked</span>
                      ) : (
                        <button
                          type="button"
                          className="btn-outline btn-compact sg-ci-danger"
                          onClick={() => revoke(token)}
                        >
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
