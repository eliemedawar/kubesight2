import { describe, expect, it } from "vitest";
import {
  countsError,
  datastoreFit,
  defaultPlacement,
  destroyStance,
  growthState,
  machineNames,
  minimumDisk,
  placementProblem,
  planGroups,
  previewMachines,
  provisionRail,
  provisioningPayload,
  resolvePlacement,
  shapeFor,
  shapeLabel,
  sizeErrors,
  totals,
  vmRows,
  vmwareNameError,
} from "./clusterProvisioning.js";
import { draftBlueprint, machinesBlueprint } from "./clusterBuilder.js";

const SMALL = { loadbalancer: 1, controlPlane: 1, worker: 2 };
const SIZES = {
  loadbalancer: { cpu: 2, memoryGb: 2, diskGb: 40 },
  controlPlane: { cpu: 4, memoryGb: 8, diskGb: 80 },
  worker: { cpu: 4, memoryGb: 8, diskGb: 100 },
};

const PLACEMENT = {
  datacenters: [{
    id: "datacenter-3",
    name: "DC-Beirut",
    clusters: [
      { id: "domain-c31", name: "Lab", hostCount: 1, drsEnabled: false, rootResourcePoolId: "resgroup-32", resourcePools: [] },
      { id: "domain-c8", name: "Prod", hostCount: 4, drsEnabled: true, rootResourcePoolId: "resgroup-9",
        resourcePools: [{ id: "resgroup-20", name: "k8s", path: "k8s" }] },
    ],
    folders: [{ id: "group-v22", path: "KubeSight" }],
    datastores: [
      { id: "ds-1", name: "small", capacityGb: 1000, freeGb: 100, accessible: true },
      { id: "ds-2", name: "big", capacityGb: 4000, freeGb: 2000, accessible: true },
    ],
    networks: [
      { id: "net-1", name: "DMZ" },
      { id: "net-2", name: "K8S" },
    ],
    templates: [
      { id: "vm-1", name: "win", compatibility: { status: "bad", checks: [{ status: "bad", label: "Guest OS", detail: "Windows" }] } },
      { id: "vm-2", name: "ubuntu", uuid: "u-2", disks: [{ sizeGb: 60 }], compatibility: { status: "ok", checks: [] } },
    ],
  }],
};
const RANGES = [{ id: 7, networkName: "K8S", size: 41, reservedCount: 0, inUseCount: 3 }];

describe("shape rules", () => {
  it("refuses even control planes and the wrong number of load balancers", () => {
    expect(countsError({ loadbalancer: 2, controlPlane: 2, worker: 1 })).toMatch(/1, 3 or 5/);
    expect(countsError({ loadbalancer: 1, controlPlane: 3, worker: 1 })).toMatch(/exactly 2/);
    expect(countsError({ loadbalancer: 2, controlPlane: 1, worker: 1 })).toMatch(/0 or 1/);
    expect(countsError({ loadbalancer: 0, controlPlane: 1, worker: 0 })).toMatch(/at least 1 worker/);
    expect(countsError(SMALL)).toBe("");
  });

  it("maps a shape to the builder's topology", () => {
    expect(shapeFor({ loadbalancer: 0, controlPlane: 1, worker: 1 }))
      .toEqual({ topologyType: "single_cp", endpointMode: "manual_endpoint" });
    expect(shapeFor(SMALL)).toEqual({ topologyType: "single_cp", endpointMode: "managed_haproxy" });
    expect(shapeFor({ loadbalancer: 2, controlPlane: 3, worker: 3 }).topologyType).toBe("stacked_ha");
  });

  it("checks sizes only for roles that have machines", () => {
    const lab = { loadbalancer: 0, controlPlane: 1, worker: 1 };
    expect(sizeErrors(lab, { ...SIZES, loadbalancer: { cpu: 0, memoryGb: 0, diskGb: 0 } })).toEqual([]);
    expect(sizeErrors(lab, { ...SIZES, controlPlane: { cpu: 1, memoryGb: 8, diskGb: 80 } }))
      .toEqual(["Control planes need at least 2 vCPU."]);
  });

  it("totals and labels a shape", () => {
    expect(totals(SMALL, SIZES)).toEqual({ machines: 4, cpu: 14, memoryGb: 26, diskGb: 320 });
    expect(shapeLabel(SMALL)).toBe("1 load balancer · 1 control plane · 2 workers");
  });
});

describe("names and addresses", () => {
  it("names machines the way the backend does", () => {
    expect(machineNames("uat-02", SMALL).map((m) => m.name))
      .toEqual(["uat-02-lb-1", "uat-02-cp-1", "uat-02-wk-1", "uat-02-wk-2"]);
  });

  it("hands the first address to the VIP when there is a load balancer", () => {
    const preview = previewMachines("c", SMALL, SIZES, ["10.0.0.50", "10.0.0.51", "10.0.0.52", "10.0.0.53", "10.0.0.54"]);
    expect(preview.vip).toBe("10.0.0.50");
    expect(preview.endpoint).toBe("10.0.0.50:6443");
    expect(preview.machines[1]).toMatchObject({ name: "c-cp-1", ip: "10.0.0.52", cpu: 4 });
  });

  it("uses the control plane's own address without a load balancer", () => {
    const preview = previewMachines("c", { loadbalancer: 0, controlPlane: 1, worker: 1 }, SIZES, ["10.0.0.50", "10.0.0.51"]);
    expect(preview.vip).toBeNull();
    expect(preview.endpoint).toBe("10.0.0.50:6443");
  });

  it("asks for DNS-style names", () => {
    expect(vmwareNameError("uat-02")).toBe("");
    expect(vmwareNameError("UAT 02")).toMatch(/Lowercase/);
  });
});

describe("placement", () => {
  it("picks sensible defaults", () => {
    const vm = defaultPlacement(PLACEMENT, undefined, RANGES);
    expect(vm).toMatchObject({
      datacenterId: "datacenter-3",
      clusterId: "domain-c8",      // DRS on, several hosts
      folderParentId: "group-v22", // a KubeSight folder exists
      datastoreId: "ds-2",         // most free space
      networkId: "net-2",          // the network with a range
      templateId: "vm-2",          // first compatible
    });
  });

  it("says what is missing", () => {
    const vm = defaultPlacement(PLACEMENT, undefined, RANGES);
    expect(placementProblem(resolvePlacement(PLACEMENT, vm, RANGES), SMALL)).toBe("");
    expect(placementProblem(resolvePlacement(PLACEMENT, { ...vm, networkId: "net-1" }, RANGES), SMALL))
      .toMatch(/DMZ has no address range/);
    expect(placementProblem(resolvePlacement(PLACEMENT, { ...vm, templateId: "vm-1" }, RANGES), SMALL))
      .toMatch(/win cannot be used/);
  });

  it("builds the provisioning payload with ids and names", () => {
    const vm = defaultPlacement(PLACEMENT, undefined, RANGES);
    const payload = provisioningPayload("3", resolvePlacement(PLACEMENT, vm, RANGES), vm, SMALL, SIZES);
    expect(payload).toMatchObject({
      vsphereConnectionId: 3, clusterId: "domain-c8", resourcePoolId: "resgroup-9",
      folderParent: "KubeSight", datastoreName: "big", networkName: "K8S", antiAffinity: true,
    });
    expect(payload.template.uuid).toBe("u-2");
  });

  it("knows a clone cannot shrink the template's disk", () => {
    expect(minimumDisk("worker", { disks: [{ sizeGb: 60 }] })).toBe(60);
    expect(minimumDisk("worker", { disks: [{ sizeGb: 20 }] })).toBe(40);
  });

  it("warns when thin disks could outgrow the datastore", () => {
    expect(datastoreFit({ capacityGb: 1000, freeGb: 100 }, 320).over).toBe(true);
    expect(datastoreFit({ capacityGb: 4000, freeGb: 2000 }, 320).over).toBe(false);
  });
});

describe("plans and progress", () => {
  it("groups a plan's resources, VMs first", () => {
    const groups = planGroups({ resources: [
      { kind: "folder", address: "f" }, { kind: "vm", address: "a" }, { kind: "rule", address: "r" },
    ] });
    expect(groups.map((g) => g.kind)).toEqual(["vm", "folder", "rule"]);
  });

  it("walks the rail through an apply", () => {
    const at = (status, buildStatus = "provisioning") => provisionRail({
      status: buildStatus, provisioning: { job: { operation: "create", status } },
    }).map((cell) => cell.state);
    expect(at("planned", "draft")).toEqual(["wait", "todo", "todo", "todo"]);
    expect(at("applying")).toEqual(["done", "now", "todo", "todo"]);
    expect(at("connect_failed", "provision_failed")).toEqual(["done", "done", "fail", "todo"]);
    expect(at("succeeded", "building")).toEqual(["done", "done", "done", "now"]);
    expect(at("succeeded", "completed")).toEqual(["done", "done", "done", "done"]);
  });

  it("ends a VMs-only rail at SSH", () => {
    const rail = (status, buildStatus) => provisionRail({
      status: buildStatus, vmsOnly: true, provisioning: { job: { operation: "create", status } },
    });
    expect(rail("applying", "provisioning").map((cell) => cell.key)).toEqual(["plan", "create", "ssh"]);
    expect(rail("succeeded", "vms_ready").map((cell) => cell.state)).toEqual(["done", "done", "done"]);
  });

  it("reads per-VM state from the job's progress", () => {
    const rows = vmRows({
      provisioning: {
        spec: { machines: [
          { name: "c-cp-1", role: "controlPlane", ip: "10.0.0.2", cpu: 4, memoryGb: 8, diskGb: 80 },
          { name: "c-wk-1", role: "worker", ip: "10.0.0.3", cpu: 4, memoryGb: 8, diskGb: 100 },
        ] },
        job: { progress: { vms: {
          "c-cp-1": { state: "created", elapsed: "1m12s" },
          "c-wk-1": { state: "failed", error: "Insufficient disk space" },
        } } },
        state: { vmCount: 1 },
      },
    });
    expect(rows[0]).toMatchObject({ name: "c-cp-1", tone: "ok", label: "Created" });
    expect(rows[1]).toMatchObject({ name: "c-wk-1", tone: "bad", error: "Insufficient disk space" });
  });

  it("knows who decides a destroy", () => {
    const job = { operation: "destroy", status: "awaiting_approval", requestedByUserId: 1 };
    expect(destroyStance(job, 1)).toBe("requester");
    expect(destroyStance(job, 2)).toBe("approver");
    expect(destroyStance({ ...job, status: "applying" }, 2)).toBe("none");
  });
});

describe("blueprints", () => {
  it("draws planned machines tier by tier", () => {
    const plan = machinesBlueprint({
      machines: [
        { name: "c-lb-1", role: "loadbalancer", ip: "10.0.0.51" },
        { name: "c-cp-1", role: "controlPlane", ip: "10.0.0.52" },
        { name: "c-wk-1", role: "worker", ip: "10.0.0.53" },
      ],
      vip: "10.0.0.50",
    });
    expect(plan.tiers.map((t) => [t.role, t.filled, t.target])).toEqual([
      ["loadbalancer", 1, 1], ["control_plane", 1, 1], ["worker", 1, 1],
    ]);
    expect(plan.bus).toMatchObject({ managed: true, address: "10.0.0.50" });
  });

  it("asks existing-machine builds for exactly the template's workers", () => {
    const plan = draftBlueprint({ basics: { counts: SMALL, endpointMode: "managed_haproxy" } });
    expect(plan.tiers.find((t) => t.role === "worker").target).toBe(2);
  });
});

describe("growing a running cluster", () => {
  const limits = (counts, cp = true, lb = true) => ({
    counts,
    controlPlane: { allowed: cp, reason: cp ? null : "The API address is a control plane's own address." },
    loadbalancer: { allowed: lb, reason: lb ? null : "Two load balancers is the most." },
  });

  it("offers control planes and a second balancer to a Small cluster", () => {
    const state = growthState({
      growthLimits: limits({ control_plane: 1, worker: 2, loadbalancer: 1 }),
      nodes: [],
    });
    expect(state.controlPlane.allowed).toBe(true);
    expect(state.loadbalancer.allowed).toBe(true);
    expect(state.oddProblem).toBe("");
  });

  it("asks for the second control plane before preflight", () => {
    const state = growthState({
      growthLimits: limits({ control_plane: 2, worker: 2, loadbalancer: 1 }),
      nodes: [{ role: "control_plane", status: "pending" }],
    });
    expect(state.running.control_plane).toBe(1);
    expect(state.queued.control_plane).toBe(1);
    expect(state.oddProblem).toMatch(/Queue one more/);
  });

  it("stops at a keepalived pair and at five control planes", () => {
    const state = growthState({
      growthLimits: limits({ control_plane: 5, worker: 3, loadbalancer: 2 }, true, false),
      nodes: [],
    });
    expect(state.loadbalancer.allowed).toBe(false);
    expect(state.controlPlane.allowed).toBe(false);
    expect(state.controlPlane.reason).toMatch(/Five/);
  });
});
