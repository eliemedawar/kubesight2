import { Fragment } from "react";
import { Link } from "react-router-dom";
import { breadcrumbsFor } from "../../routes/breadcrumbs.js";

/**
 * The trail above a page heading, derived from the route table's parent chain.
 *
 * Renders nothing for a single crumb: that is just the page title again.
 */
export default function Breadcrumbs({ pageKey, params, currentLabel, trail }) {
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
