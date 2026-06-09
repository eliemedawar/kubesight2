export default function EffectiveAccessPreview({ preview }) {
  if (!preview) return null;

  const previewStamp = [
    preview.roleName,
    preview.clusters?.join("|") ?? "",
    preview.namespaces?.join("|") ?? "",
    preview.resources?.join("|") ?? "",
    preview.allowedActions?.join("|") ?? "",
    preview.counts?.total ?? 0,
  ].join("::");

  return (
    <section
      className="form-section effective-access-panel"
      data-preview-stamp={previewStamp}
    >
      <h4>Effective access</h4>
      <p className="muted">Live preview of what this user will be able to do after saving.</p>
      <dl className="effective-access-dl">
        <dt>Role</dt>
        <dd>
          <strong>{preview.roleName}</strong>
          {preview.roleDescription ? (
            <p className="muted role-desc-inline">{preview.roleDescription}</p>
          ) : null}
        </dd>

        <dt>Clusters</dt>
        <dd>
          {preview.clusters.length ? (
            <ul>
              {preview.clusters.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          ) : (
            <span className="muted">None assigned</span>
          )}
        </dd>

        <dt>Namespaces</dt>
        <dd>
          {preview.namespaces.length ? (
            <ul>
              {preview.namespaces.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          ) : (
            <span className="muted">—</span>
          )}
        </dd>

        <dt>Resources</dt>
        <dd>
          {preview.resources.length ? (
            <ul>
              {preview.resources.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          ) : (
            <span className="muted">—</span>
          )}
        </dd>

        {preview.counts?.total > 0 ? (
          <>
            <dt>Selection counts</dt>
            <dd>
              Pods: {preview.counts.pods}, Deployments: {preview.counts.deployments}, Services:{" "}
              {preview.counts.services}
            </dd>
          </>
        ) : null}

        <dt>Allowed actions</dt>
        <dd>
          {preview.allowedActions.length ? (
            <ul className="permission-status-list permission-status-list--allowed">
              {preview.allowedActions.map((label) => (
                <li key={label}>
                  <span className="perm-icon perm-icon--ok">✓</span>
                  {label}
                </li>
              ))}
            </ul>
          ) : (
            <span className="muted">—</span>
          )}
        </dd>
      </dl>
    </section>
  );
}
