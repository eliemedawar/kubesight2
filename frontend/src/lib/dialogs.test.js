import { afterEach, describe, expect, it, vi } from "vitest";
import {
  confirmDialog,
  getDialogEntries,
  normalizeConfirmOptions,
  normalizePromptOptions,
  promptBlocker,
  promptDialog,
  registerDialogHost,
  settleDialog,
  subscribeDialogs,
} from "./dialogs.js";

let unregister = null;
afterEach(() => {
  unregister?.();
  unregister = null;
  vi.unstubAllGlobals();
});

describe("normalizeConfirmOptions", () => {
  it("takes a bare string as the message", () => {
    expect(normalizeConfirmOptions("Delete it?")).toMatchObject({
      kind: "confirm",
      message: "Delete it?",
      title: "Are you sure?",
      confirmLabel: "Confirm",
      cancelLabel: "Cancel",
      tone: "default",
    });
  });

  it("defaults a danger confirm to Delete", () => {
    expect(normalizeConfirmOptions({ tone: "danger" }).confirmLabel).toBe("Delete");
    expect(normalizeConfirmOptions({ tone: "danger", confirmLabel: "Remove" }).confirmLabel).toBe("Remove");
  });

  it("ignores an unknown tone", () => {
    expect(normalizeConfirmOptions({ tone: "loud" }).tone).toBe("default");
  });
});

describe("promptBlocker", () => {
  it("lets an optional prompt confirm empty", () => {
    expect(promptBlocker(normalizePromptOptions({}), "")).toBe("");
  });

  it("refuses a blank required value", () => {
    const options = normalizePromptOptions({ required: true });
    expect(promptBlocker(options, "")).not.toBe("");
    expect(promptBlocker(options, "   ")).not.toBe("");
    expect(promptBlocker(options, "because")).toBe("");
  });

  it("uses the message validate returns", () => {
    const options = normalizePromptOptions({ validate: (v) => (v === "prod" ? "" : "Type prod") });
    expect(promptBlocker(options, "pro")).toBe("Type prod");
    expect(promptBlocker(options, "prod")).toBe("");
  });

  it("treats validate returning false as a refusal", () => {
    const options = normalizePromptOptions({ validate: () => false });
    expect(promptBlocker(options, "x")).not.toBe("");
  });
});

describe("confirmDialog / promptDialog with a host", () => {
  it("queues a request and resolves it when settled", async () => {
    unregister = registerDialogHost();
    const seen = [];
    const unsubscribe = subscribeDialogs((entries) => seen.push(entries.length));
    const answer = confirmDialog({ title: "Delete", tone: "danger" });
    const [entry] = getDialogEntries();
    expect(entry.options).toMatchObject({ title: "Delete", tone: "danger" });
    settleDialog(entry.id, true);
    await expect(answer).resolves.toBe(true);
    expect(getDialogEntries()).toEqual([]);
    expect(seen).toEqual([1, 0]);
    unsubscribe();
  });

  it("stacks requests, newest last", async () => {
    unregister = registerDialogHost();
    const first = confirmDialog("one");
    const second = promptDialog({ title: "Why?" });
    const [a, b] = getDialogEntries();
    expect(a.options.message).toBe("one");
    expect(b.options.kind).toBe("prompt");
    settleDialog(b.id, "because");
    settleDialog(a.id, false);
    await expect(second).resolves.toBe("because");
    await expect(first).resolves.toBe(false);
  });

  it("cancels what is open when the last host goes away", async () => {
    unregister = registerDialogHost();
    const confirm = confirmDialog("x");
    const prompt = promptDialog("y");
    unregister();
    unregister = null;
    await expect(confirm).resolves.toBe(false);
    await expect(prompt).resolves.toBe(null);
    expect(getDialogEntries()).toEqual([]);
  });

  it("ignores a second settle of the same dialog", async () => {
    unregister = registerDialogHost();
    const answer = confirmDialog("x");
    const [entry] = getDialogEntries();
    settleDialog(entry.id, true);
    settleDialog(entry.id, false);
    await expect(answer).resolves.toBe(true);
  });
});

describe("without a host", () => {
  it("falls back to the browser box", async () => {
    const confirm = vi.fn(() => true);
    const prompt = vi.fn(() => "  ");
    vi.stubGlobal("window", { confirm, prompt });
    await expect(confirmDialog({ title: "T", message: "M" })).resolves.toBe(true);
    expect(confirm).toHaveBeenCalledWith("T\n\nM");
    // A required prompt answered blank counts as cancelled.
    await expect(promptDialog({ title: "Why?", required: true })).resolves.toBe(null);
    await expect(promptDialog({ title: "Why?" })).resolves.toBe("  ");
  });
});
