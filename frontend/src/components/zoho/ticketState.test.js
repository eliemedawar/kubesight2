import { describe, expect, it } from "vitest";
import { isUnresolved } from "./ticketState.js";

const handle = (status) => ({ kind: "handle", status });

describe("isUnresolved", () => {
  it("without the agent, follows the dropdown resolution", () => {
    expect(isUnresolved({ resolved: false }, false)).toBe(true);
    expect(isUnresolved({ resolved: true }, false)).toBe(false);
  });

  it("with the agent, a ticket Hermes answered is not unresolved even with empty dropdowns", () => {
    expect(isUnresolved({ resolved: false, agentTasks: [handle("done")] }, true)).toBe(false);
    expect(isUnresolved({ resolved: false, agentTasks: [handle("impediment")] }, true)).toBe(false);
    expect(isUnresolved({ resolved: false, agentTasks: [handle("running")] }, true)).toBe(false);
  });

  it("with the agent, no reading yet or only failed ones is unresolved", () => {
    expect(isUnresolved({ resolved: true }, true)).toBe(true);
    expect(isUnresolved({ resolved: true, agentTasks: [handle("error"), handle("error")] }, true)).toBe(true);
    expect(isUnresolved({ resolved: true, agentTasks: [handle("superseded")] }, true)).toBe(true);
    // A follow-up alone is not a reading of the ticket.
    expect(isUnresolved({ agentTasks: [{ kind: "followup", status: "done" }] }, true)).toBe(true);
  });

  it("a later successful reading clears earlier errors", () => {
    expect(isUnresolved({ agentTasks: [handle("error"), handle("done")] }, true)).toBe(false);
  });
});
