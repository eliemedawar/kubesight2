import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.jsx";
import ErrorBoundary from "./components/common/ErrorBoundary.jsx";
import { AuthProvider } from "./context/AuthContext.jsx";
import { loadRuntimeConfig } from "./runtimeConfig.js";
import "./index.css";

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
          <App />
        </AuthProvider>
      </ErrorBoundary>
    </React.StrictMode>
  );
}

bootstrap();
