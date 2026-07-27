import { describe, expect, it } from "vitest";

import { fieldActions, fieldRole, isSyncOwned, linesToValues, valuesToLines } from "./zohoFieldMeta";

const CONFIG = {
  environmentFieldId: "201",
  appFieldId: "202",
  variableFieldId: "203",
  syncVariables: true,
};

const pick = (id) => ({ id, isPicklist: true, type: "Picklist" });

describe("fieldRole", () => {
  it("recognises the three sync-owned fields", () => {
    expect(fieldRole(pick("201"), CONFIG)).toBe("environment");
    expect(fieldRole(pick("202"), CONFIG)).toBe("application");
    expect(fieldRole(pick("203"), CONFIG)).toBe("variable");
  });

  it("treats the Variable field as an ordinary picklist while its sync is off", () => {
    // With syncVariables off nothing publishes to it, so the operator must keep
    // being able to manage its options by hand.
    expect(fieldRole(pick("203"), { ...CONFIG, syncVariables: false })).toBe("picklist");
  });

  it("classifies unmanaged fields by type", () => {
    expect(fieldRole(pick("999"), CONFIG)).toBe("picklist");
    expect(fieldRole({ id: "1", type: "Text" }, CONFIG)).toBe("text");
    expect(fieldRole({ id: "2", type: "Textarea" }, CONFIG)).toBe("text");
    expect(fieldRole({ id: "3", type: "LookUp" }, CONFIG)).toBe("plain");
  });

  it("does not match a field with no id against blank config", () => {
    expect(fieldRole({ isPicklist: true }, {})).toBe("picklist");
    expect(fieldRole({ id: "", type: "Text" }, {})).toBe("text");
  });

  it("matches numeric and string ids alike", () => {
    expect(fieldRole({ id: 201, isPicklist: true }, CONFIG)).toBe("environment");
  });

  it("marks an operator-bound picklist as bound", () => {
    const bound = { ...pick("999"), binding: { sourceKind: "clusters", locked: false } };
    expect(fieldRole(bound, CONFIG)).toBe("bound");
  });

  it("keeps the three sync-owned roles ahead of their locked binding", () => {
    // The synthesized binding rides along on those fields too; their own role
    // is what decides the affordances (Environment keeps "Choose namespaces").
    const env = { ...pick("201"), binding: { sourceKind: "namespaces", locked: true } };
    expect(fieldRole(env, CONFIG)).toBe("environment");
  });

  it("reads a locked binding as the sync-owned role even without matching ids", () => {
    // Same claim from the backend, useful when the cached config ids are stale.
    const env = {
      ...pick("777"),
      binding: { sourceKind: "namespaces", locked: true, enabled: true },
    };
    expect(fieldRole(env, {})).toBe("environment");
  });

  it("treats a locked binding that is switched off as an ordinary picklist", () => {
    // Nothing publishes to it, so the operator must keep managing it by hand.
    const off = {
      ...pick("777"),
      binding: { sourceKind: "env_vars", locked: true, enabled: false },
    };
    expect(fieldRole(off, {})).toBe("picklist");
  });
});

describe("isSyncOwned", () => {
  it("covers exactly the fields the sync publishes", () => {
    expect(["environment", "application", "variable", "bound"].every(isSyncOwned)).toBe(true);
    expect(["picklist", "text", "plain"].some(isSyncOwned)).toBe(false);
  });
});

describe("fieldActions", () => {
  it("gives Environment the source picker and everything an Edit", () => {
    expect(fieldActions(pick("201"), "environment", true).map((a) => a.key)).toEqual([
      "source",
      "edit",
    ]);
  });

  it("hides option editing on the auto-derived fields", () => {
    expect(fieldActions(pick("202"), "application", true).map((a) => a.key)).toEqual(["edit"]);
    expect(fieldActions(pick("203"), "variable", true).map((a) => a.key)).toEqual(["edit"]);
  });

  // Conversion is a per-provider capability (Zoho Desk has the flow, Jira does
  // not), so the tests that expect it pass the capability in explicitly.
  const CONVERTS = { convertField: true, deleteField: true };

  it("offers Manage options on a plain picklist and conversion on text", () => {
    expect(fieldActions(pick("9"), "picklist", true).map((a) => a.key)).toEqual([
      "options",
      "bind",
      "edit",
    ]);
    expect(
      fieldActions({ id: "9", type: "Text" }, "text", true, CONVERTS).map((a) => a.key)
    ).toEqual(["convert", "edit"]);
  });

  it("drops manual option editing once a field is bound to a live source", () => {
    // The next sync would overwrite anything typed there.
    expect(fieldActions(pick("9"), "bound", true).map((a) => a.key)).toEqual(["bind", "edit"]);
  });

  it("offers nothing without manage permission", () => {
    expect(fieldActions(pick("201"), "environment", false)).toEqual([]);
  });

  it("offers deletion only for removable custom fields that are not sync-owned", () => {
    const custom = { id: "9", type: "Text", custom: true, removable: true };
    expect(fieldActions(custom, "text", true, CONVERTS).map((a) => a.key)).toEqual([
      "convert",
      "edit",
      "delete",
    ]);
    expect(
      fieldActions({ ...custom, removable: false }, "text", true, CONVERTS).map((a) => a.key)
    ).toEqual(["convert", "edit"]);
    expect(fieldActions({ ...custom, isPicklist: true }, "bound", true).map((a) => a.key)).toEqual([
      "bind",
      "edit",
    ]);
  });
});

describe("value line helpers", () => {
  it("round-trips, dropping blanks and the -None- placeholder", () => {
    expect(linesToValues(" a \n\n-None-\nb\n")).toEqual(["a", "b"]);
    expect(valuesToLines(["-None-", "a", "b"])).toBe("a\nb");
    expect(linesToValues("")).toEqual([]);
    expect(valuesToLines(null)).toBe("");
  });
});
