import PageTitle from "./PageTitle.jsx";

export { default as Breadcrumbs } from "./Breadcrumbs.jsx";

/**
 * Alias for PageTitle.
 *
 * These were briefly two components rendering the same block, which is the
 * duplication this layer exists to remove. PageTitle is the one with nineteen
 * callers, so it won; this stays as the name the newer screens already import,
 * and takes the same props.
 *
 * `pageKey` and `params` are accepted and ignored: the trail is derived from
 * the current route, so passing them was always redundant.
 */
export default function PageHeader({ pageKey, params, showBreadcrumbs = true, ...rest }) {
  return <PageTitle breadcrumbs={showBreadcrumbs} {...rest} />;
}
