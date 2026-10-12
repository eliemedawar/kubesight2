import { Component } from "react";

/**
 * Catches render errors. At the root it replaces the whole app; with
 * `scope="page"` it wraps just the page content, so the sidebar and topbar stay
 * usable and moving to another page (a new `resetKey`) clears the error.
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error("KubeSight UI error:", error, info);
  }

  componentDidUpdate(prevProps) {
    if (this.state.error && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  render() {
    const { error } = this.state;
    if (!error) {
      return this.props.children;
    }

    const details = (
      <pre
        style={{
          whiteSpace: "pre-wrap",
          fontSize: "0.85rem",
          background: "var(--bg-interactive)",
          padding: "0.75rem",
          borderRadius: "0.5rem",
        }}
      >
        {error?.message || String(error)}
      </pre>
    );

    if (this.props.scope === "page") {
      return (
        <section className="card" role="alert" style={{ maxWidth: "40rem" }}>
          <h2 style={{ marginTop: 0 }}>This page could not be displayed</h2>
          <p className="muted">
            Something on this page failed while rendering. The rest of KubeSight still works:
            pick another page, try this one again, or reload. If it keeps happening, send the
            message below to your administrator.
          </p>
          {details}
          <div style={{ display: "flex", gap: "0.5rem" }}>
            <button type="button" className="primary" onClick={() => this.setState({ error: null })}>
              Try again
            </button>
            <button type="button" className="btn-outline" onClick={() => window.location.reload()}>
              Reload
            </button>
          </div>
        </section>
      );
    }

    return (
      <div className="login-screen">
        <div className="card" style={{ maxWidth: "32rem", margin: "2rem auto" }}>
          <h1 style={{ marginTop: 0 }}>Something went wrong</h1>
          <p className="muted">
            KubeSight hit an unexpected error and could not render. Reload the page; if it keeps
            happening, send the message below to your administrator.
          </p>
          {details}
          <button type="button" className="primary" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      </div>
    );
  }
}
