import { Component } from "react";

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

  render() {
    const { error } = this.state;
    if (!error) {
      return this.props.children;
    }

    return (
      <div className="login-screen">
        <div className="card" style={{ maxWidth: "32rem", margin: "2rem auto" }}>
          <h1 style={{ marginTop: 0 }}>Something went wrong</h1>
          <p className="muted">
            The app hit a JavaScript error and could not render. This often happens when the page
            is opened from a saved HTML file or the dev server is not running.
          </p>
          <p>
            <strong>Use:</strong> start the backend (<code>python app.py</code>), then open{" "}
            <code>http://127.0.0.1:5000</code> or the Vite dev URL{" "}
            <code>http://localhost:5173</code>.
          </p>
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
          <button type="button" className="primary-btn" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      </div>
    );
  }
}
