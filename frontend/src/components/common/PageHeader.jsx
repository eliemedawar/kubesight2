import { Fragment } from "react";
import { Link } from "react-router-dom";
import { breadcrumbsFor } from "../../routes/breadcrumbs.js";

/**
 * The standard page header: breadcrumbs, title, optional subtitle and actions.
 *
 * Supersedes PageTitle, which had no trail and a single hardcoded primary
 * button. Pages migrate to this as they are touched; both render the same
 * `.page-title` block, so the two look identical while the move happens.
 */

export function Breadcrumbs({ pageKey, params, currentLabel, trail }) {
  const crumbs = trail || breadcrumbsFor(pageKey, { params, currentLabel });
  if (crumbs.length < 2) {
    // A single crumb is just the page title again.
    return null;
  }

  return (
    <nav className="breadcrumbs" aria-label="Breadcrumb">
      <ol>
        {crumbs.map((crumb, index) => (
          <Fragment key={`${crumb.label}-${index}`}>
            {index > 0 ? (
              <li className="breadcrumb-sep" aria-hidden="true">
                /
              </li>
            ) : null}
            <li className={crumb.isGroup ? "breadcrumb-group" : undefined}>
              {crumb.href ? (
                <Link to={crumb.href}>{crumb.label}</Link>
              ) : (
                <span aria-current={crumb.isCurrent ? "page" : undefined}>{crumb.label}</span>
              )}
            </li>
          </Fragment>
        ))}
      </ol>
    </nav>
  );
}

export default function PageHeader({
  pageKey,
  params,
  title,
  subtitle,
  currentLabel,
  showBreadcrumbs = true,
  actions,
  meta,
}) {
  return (
    <header className="page-title">
      {showBreadcrumbs && pageKey ? (
        <Breadcrumbs pageKey={pageKey} params={params} currentLabel={currentLabel || title} />
      ) : null}
      <div className="page-title-content">
        <div className="page-title-text">
          <h2>{title}</h2>
          {subtitle ? <p>{subtitle}</p> : null}
          {meta ? <div className="page-title-meta">{meta}</div> : null}
        </div>
        {actions ? <div className="page-title-actions">{actions}</div> : null}
      </div>
    </header>
  );
}
