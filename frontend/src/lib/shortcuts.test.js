import { describe, expect, it } from "vitest";
import { altShortcutLetter } from "./shortcuts.js";

const key = (code, extra = {}) => ({ code, altKey: true, ctrlKey: false, metaKey: false, shiftKey: false, ...extra });

describe("Alt+letter shortcuts", () => {
  it("reads the letter from the key's position, not the character it types", () => {
    expect(altShortcutLetter(key("KeyK"))).toBe("K");
    expect(altShortcutLetter(key("KeyG", { key: "©" }))).toBe("G");
  });

  it("ignores anything that is not plain Alt+letter", () => {
    expect(altShortcutLetter(key("KeyK", { altKey: false }))).toBe("");
    expect(altShortcutLetter(key("KeyK", { ctrlKey: true }))).toBe("");
    expect(altShortcutLetter(key("KeyK", { shiftKey: true }))).toBe("");
    expect(altShortcutLetter(key("Digit1"))).toBe("");
    expect(altShortcutLetter(key("KeyK", { defaultPrevented: true }))).toBe("");
  });
});
