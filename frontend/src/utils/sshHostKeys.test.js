import { describe, expect, it } from "vitest";
import {
  fingerprintCommand,
  fingerprintError,
  scanVerdict,
  sourceLabel,
} from "./sshHostKeys.js";
import { storageDetail, storageValue, workloadsValue } from "./clusterOverview.js";

describe("fingerprintError", () => {
  it("accepts the OpenSSH SHA256 form and 64-hex digests", () => {
    expect(fingerprintError("SHA256:" + "A".repeat(43))).toBe("");
    expect(fingerprintError("ab".repeat(32))).toBe("");
    expect(fingerprintError("ab:".repeat(31) + "ab")).toBe("");
  });

  it("rejects empty, MD5 and garbage", () => {
    expect(fingerprintError("")).toMatch(/Paste/);
    expect(fingerprintError("MD5:12:34")).toMatch(/MD5/);
    expect(fingerprintError("16:27:ac:a5:76:28:2d:36:63:1b:56:4d:eb:df:a6:48")).toMatch(/MD5/);
    expect(fingerprintError("SHA256:short")).toMatch(/SHA256/);
  });
});

describe("scanVerdict", () => {
  it("flags a changed key as bad", () => {
    expect(scanVerdict({ status: "changed" })[1]).toBe("is-bad");
  });
  it("distinguishes pinned from TOFU matches", () => {
    expect(scanVerdict({ status: "match", recorded: { source: "preapproved" } })[0]).toBe("pinned");
    expect(scanVerdict({ status: "match", recorded: { source: "tofu" } })[1]).toBe("is-warn");
  });
  it("treats unknown as not recorded", () => {
    expect(scanVerdict({ status: "unknown" })[0]).toBe("not recorded");
    expect(scanVerdict(null)).toBeNull();
  });
});

describe("host key labels", () => {
  it("labels sources and suggests the on-host command", () => {
    expect(sourceLabel("preapproved")).toBe("pinned");
    expect(sourceLabel("tofu")).toBe("trust on first use");
    expect(fingerprintCommand("ssh-ed25519")).toBe("ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub");
    expect(fingerprintCommand("ecdsa-sha2-nistp256")).toContain("ssh_host_ecdsa_key.pub");
  });
});

describe("cluster overview formatting", () => {
  it("shows a dash, never 0, for unknown values", () => {
    expect(workloadsValue({ deployments: 4, statefulsets: null, daemonsets: 2 })).toBe("4 / — / 2");
    expect(workloadsValue(null)).toBe("—");
    expect(storageValue({ usedGiB: null, capacityGiB: null, claimedGiB: null })).toBe("—");
    expect(storageValue(undefined)).toBe("—");
  });

  it("prefers used, then claimed, over provisioned capacity", () => {
    expect(storageValue({ usedGiB: 14200, capacityGiB: 20000 })).toBe("14200 GiB / 20000 GiB");
    expect(storageValue({ usedGiB: null, capacityGiB: 50, claimedGiB: 12.5 })).toBe("12.5 GiB / 50.0 GiB");
    expect(storageValue({ usedGiB: null, capacityGiB: 50, claimedGiB: null })).toBe("50.0 GiB");
    expect(storageDetail({ usedGiB: null, claimedGiB: 3 })).toMatch(/Claimed/);
  });
});
