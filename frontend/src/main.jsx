import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.jsx";
import ErrorBoundary from "./components/common/ErrorBoundary.jsx";
import { AuthProvider } from "./context/AuthContext.jsx";
import { ChangeBundleProvider } from "./context/ChangeBundleContext.jsx";
import { loadRuntimeConfig } from "./runtimeConfig.js";
import "./styles/fonts.css";
import "./index.css";
import "./styles/ui-polish.css";
import "./styles/premium.css";
import "./styles/signal/screens.css";
import "./styles/signal/overview.css";
import "./styles/signal/clusters.css";
import "./styles/signal/users.css";
import "./styles/signal/catalog.css";
import "./styles/signal/alerts.css";
import "./styles/signal/requests.css";
import "./styles/signal/topology.css";
import "./styles/signal/zoho.css";
import "./styles/signal/settings.css";

async function bootstrap() {
  await loadRuntimeConfig();

  const rootElement = document.getElementById("root");
  if (!rootElement) {
    document.body.innerHTML =
      "<p style='padding:1rem;font-family:system-ui,sans-serif'>Missing #root element. Rebuild the frontend.</p>";
    return;
  }

  ReactDOM.createRoot(rootElement).render(
    <React.StrictMode>
      <ErrorBoundary>
        <AuthProvider>
          <ChangeBundleProvider>
            <App />
          </ChangeBundleProvider>
        </AuthProvider>
      </ErrorBoundary>
    </React.StrictMode>
  );
}

bootstrap();
