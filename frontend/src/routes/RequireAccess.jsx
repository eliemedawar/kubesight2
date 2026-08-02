import AccessDeniedPage from "../pages/AccessDeniedPage.jsx";
import { NAV_PAGES } from "../utils/authz.js";

/**
 * Route-level authorization.
 *
 * Before this, an unpermitted page key was quietly rewritten to the first page
 * the user *could* see, and the user was moved with no explanation. That was
 * survivable while navigation was internal state — you could only get there by
 * clicking, and the click simply appeared to do nothing.
 *
 * With real URLs it stops being survivable. A colleague pastes you a link to
 * `/admin/users`; you lack `users:view`; you land on the dashboard. Nothing says
 * the link was fine and your account was not, so the reasonable conclusion is
 * that the link is broken — and the person who sent it gets asked to re-send a
 * URL that was correct all along. Bookmarkable URLs are the point of this work,
 * and a URL that silently means something different for each reader is not one.
 *
 * So: say what happened, at the address it happened at. The URL stays put, back
 * still works, and the message names the page rather than leaving the operator
 * to guess which permission they are missing.
 */

const LABEL_BY_PAGE_KEY = new Map(NAV_PAGES.map((page) => [page.key, page.label]));

export function accessDeniedMessage(pageKey) {
  const label = LABEL_BY_PAGE_KEY.get(pageKey);
  return label
    ? `You do not have access to ${label}. Contact an administrator if you need it.`
    : "You do not have access to this page. Contact an administrator if you need it.";
}

export default function RequireAccess({ pageKey, isPageAllowed, children }) {
  if (!isPageAllowed(pageKey)) {
    return <AccessDeniedPage message={accessDeniedMessage(pageKey)} />;
  }
  return children;
}
