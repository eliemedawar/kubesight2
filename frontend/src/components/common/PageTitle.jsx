import { useLocation } from "react-router-dom";
import Breadcrumbs from "./Breadcrumbs.jsx";
import { matchPath } from "../../routes/paths.js";

/**
 * The standard page heading.
 *
 * Breadcrumbs are derived from the current route rather than passed in, so
 * every page that already renders a PageTitle gets them without being touched.
 * That was the alternative to migrating nineteen pages by hand onto a second
 * component: churn with a regression surface, to ship something the route table
 * can work out on its own.
 *
 * The trail comes from the route's `parent` chain — the same field that decides
 * which nav entry is highlighted. One fact with two consumers, rather than a
 * hand-written trail per page free to disagree with the menu.
 *
 * `breadcrumbs={false}` opts out, for headings rendered where a page-level
 * trail would be wrong.
 */
export default function PageTitle({
  title,
  subtitle,
  actionLabel,
  onAction,
  breadcrumbs = true,
  currentLabel,
  actions,
  meta,
}) {
  const location = useLocation();
  const match = matchPath(location.pathname);

  return (
    <header className="page-title">
      {breadcrumbs && match ? (
        <Breadcrumbs
          pageKey={match.pageKey}
          params={match.params}
          currentLabel={currentLabel || title}
        />
      ) : null}
      <div className="page-title-content">
        <div className="page-title-text">
          <h2>{title}</h2>
          {subtitle ? <p>{subtitle}</p> : null}
          {meta ? <div className="page-title-meta">{meta}</div> : null}
        </div>
        {actions || (actionLabel ? (
          <button type="button" className="primary page-title-action" onClick={onAction}>
            {actionLabel}
          </button>
        ) : null)}
      </div>
    </header>
  );
}
