import { Link } from "react-router-dom";

/**
 * A real not-found page.
 *
 * Before routing, an unrecognised page key fell through the `default:` arm of
 * App's switch and silently rendered the dashboard, so a mistyped or stale link
 * looked like it had worked. Now that URLs are shareable and bookmarkable, a
 * wrong one has to say so — otherwise the operator reads the dashboard as the
 * answer to a question they did not ask.
 */
export default function NotFoundPage() {
  return (
    <section className="card empty-state-card">
      <h2>Page not found</h2>
      <p className="muted">
        There is nothing at this address. The link may be out of date, or the
        page may have been renamed.
      </p>
      <p>
        <Link to="/">Go to the dashboard</Link>
      </p>
    </section>
  );
}
