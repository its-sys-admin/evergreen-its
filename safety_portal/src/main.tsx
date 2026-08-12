import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { AuthProvider } from "./lib/auth";
import "./styles/tokens.css";
import "./styles/global.css";
// Layout layer for the two surfaces that render time as space (the schedule timeline) and a
// long authored document (the weekly production report). Loaded last; every selector is
// namespaced .sched-* / .wpr-* / .u-* so it never competes with the shared kit above.
import "./styles/schedule-report.css";

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("Root element #root not found");

createRoot(rootEl).render(
  <StrictMode>
    <AuthProvider>
      <App />
    </AuthProvider>
  </StrictMode>,
);
