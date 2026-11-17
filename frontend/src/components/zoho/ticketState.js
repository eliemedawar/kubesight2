// When a ticket still needs someone, shared by the Tickets tab filter and the
// flow strip's "N unresolved" box so the two always agree.
//
// Without the agent, a ticket is resolved when its dropdowns matched a
// deployment. With the agent on, tickets are free text and Hermes may answer,
// execute or park one without the dropdowns ever being filled, so `resolved`
// says nothing: what matters is whether Hermes has handled it. A ticket is
// unresolved while it has no first reading (`handle` task) that got anywhere,
// i.e. none yet, or only ones that errored or were superseded.
const NOT_HANDLED = new Set(["error", "superseded"]);

export function isUnresolved(ticket, agentActive) {
  if (!agentActive) return !ticket.resolved;
  return !(ticket.agentTasks || []).some(
    (task) => task.kind === "handle" && !NOT_HANDLED.has(task.status)
  );
}
