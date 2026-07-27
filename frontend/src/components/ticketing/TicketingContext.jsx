import { createContext, useContext, useMemo } from "react";
import { makeTicketingApi } from "../../api/ticketingApi.js";

// One provider's identity + its bound API client, handed down to the whole
// workspace. The alternative — threading an `api` prop through the page, four
// tabs, the layout editor, and the four modals under it — would touch every
// component signature for something none of them actually decide.
//
// `descriptor` is the row from GET /api/ticketing/providers: name, formNoun
// ("layout" vs "screen") and the capability flags a component uses to decide
// whether to render an action at all (Jira has no text->dropdown conversion; Zoho
// Desk cannot create a section). Capabilities are read here rather than inferred
// from the provider key so a new provider never needs a UI change.
const TicketingContext = createContext(null);

export function TicketingProvider({ descriptor, children }) {
  const value = useMemo(() => {
    const key = descriptor?.key || "";
    const capabilities = descriptor?.capabilities || {};
    return {
      key,
      name: descriptor?.name || key,
      // The vendor's own word for the ticket form, used verbatim in UI copy so
      // it matches what the operator sees in their admin console.
      formNoun: descriptor?.formNoun || "layout",
      capabilities,
      can: (capability) => Boolean(capabilities[capability]),
      descriptor: descriptor || {},
      api: makeTicketingApi(key),
    };
  }, [descriptor]);

  return <TicketingContext.Provider value={value}>{children}</TicketingContext.Provider>;
}

export function useTicketing() {
  const value = useContext(TicketingContext);
  if (!value) {
    throw new Error("useTicketing must be used inside a <TicketingProvider>.");
  }
  return value;
}

/** Just the bound API client — the common case. */
export function useTicketingApi() {
  return useTicketing().api;
}

/** Sentence-cased form noun for copy that starts a sentence ("Screen writes…"). */
export function titleCase(text) {
  const s = String(text || "");
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}
