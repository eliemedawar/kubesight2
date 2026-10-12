import { describe, expect, it } from "vitest";
import {
  describeDraft,
  draftStorageKey,
  readStoredDraft,
  scrubSecrets,
  storableDraft,
  writeStoredDraft,
} from "./wizardDraft.js";

function memoryStorage() {
  const data = new Map();
  return {
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => data.set(key, String(value)),
    removeItem: (key) => data.delete(key),
    data,
  };
}

const draft = {
  step: 2,
  basics: {
    name: "lab-1",
    connectionProfileId: "3",
    vsphereConnectionId: "1",
    vm: { connectionId: "2", templateId: "t-1" },
    workloads: { registryConnectionId: 4, items: [] },
  },
  picked: {},
  manualNodes: [{ role: "worker", address: "192.0.2.10", hostname: "w1" }],
  buildId: 9,
  dirty: true,
  planBuild: { id: 9, provisioning: { job: { status: "planned" } } },
  workloadPlan: { counts: { images: 3 } },
};

describe("draftStorageKey", () => {
  it("is per user and absent without one", () => {
    expect(draftStorageKey(1)).not.toBe(draftStorageKey(2));
    expect(draftStorageKey(null)).toBe(null);
    expect(draftStorageKey("")).toBe(null);
  });
});

describe("scrubSecrets", () => {
  it("drops secret-looking keys at any depth", () => {
    const out = scrubSecrets({
      password: "x",
      nested: { sudoPassword: "y", apiToken: "z", privateKey: "k", keep: 1 },
      list: [{ secret: "s", name: "n" }],
    });
    expect(out).toEqual({ nested: { keep: 1 }, list: [{ name: "n" }] });
  });

  it("keeps references to stored credentials", () => {
    const out = scrubSecrets({ credentialId: 3, sshCredentialId: 4, registryConnectionId: 5 });
    expect(out).toEqual({ credentialId: 3, sshCredentialId: 4, registryConnectionId: 5 });
  });

  it("does not change the original", () => {
    const original = { password: "x" };
    scrubSecrets(original);
    expect(original.password).toBe("x");
  });
});

describe("storableDraft", () => {
  it("leaves the memory-only parts out", () => {
    const out = storableDraft(draft);
    expect(out.planBuild).toBeUndefined();
    expect(out.workloadPlan).toBeUndefined();
    expect(out.basics.name).toBe("lab-1");
    expect(out.manualNodes).toHaveLength(1);
  });
});

describe("stored draft round trip", () => {
  it("writes, reads back and removes", () => {
    const storage = memoryStorage();
    const key = draftStorageKey(7);
    writeStoredDraft(storage, key, draft);
    const back = readStoredDraft(storage, key);
    expect(back.basics.name).toBe("lab-1");
    expect(back.buildId).toBe(9);
    expect(back.planBuild).toBeUndefined();
    writeStoredDraft(storage, key, null);
    expect(readStoredDraft(storage, key)).toBe(null);
  });

  it("survives broken or blocked storage", () => {
    const storage = memoryStorage();
    storage.setItem("k", "{not json");
    expect(readStoredDraft(storage, "k")).toBe(null);
    storage.setItem("k", JSON.stringify({ step: 1 }));
    expect(readStoredDraft(storage, "k")).toBe(null);
    const throwing = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("full");
      },
      removeItem: () => {
        throw new Error("blocked");
      },
    };
    expect(readStoredDraft(throwing, "k")).toBe(null);
    expect(() => writeStoredDraft(throwing, "k", draft)).not.toThrow();
    expect(() => writeStoredDraft(throwing, "k", null)).not.toThrow();
    expect(readStoredDraft(null, "k")).toBe(null);
  });
});

describe("describeDraft", () => {
  it("names the build and the step", () => {
    expect(describeDraft(draft, ["Shape", "Machines", "Add-ons"])).toBe("“lab-1”, stopped at Add-ons");
    expect(describeDraft({ basics: { name: " " }, step: 0 })).toBe("an unnamed build");
    expect(describeDraft(null)).toBe("");
  });
});
