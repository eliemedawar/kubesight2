import { describe, expect, it } from "vitest";
import {
  EMPTY_CLOUD,
  accountFormError,
  accountPayload,
  awsManagedSubnetError,
  cidrError,
  cloudBus,
  cloudFromSpec,
  cloudMachines,
  cloudProblem,
  cloudProvisioningPayload,
  cloudShapeLabel,
  cloudShapesForVms,
  cloudTotals,
  cloudWhere,
  defaultCloudChoices,
  isCloud,
  managedBlueprint,
  parseCidrs,
  resolveCloud,
  whereCreated,
  withCloudBus,
} from "./cloudProvisioning.js";

const INVENTORY = {
  region: "eu-west-1",
  zones: [{ id: "eu-west-1a", name: "eu-west-1a" }, { id: "eu-west-1b", name: "eu-west-1b" }],
  networks: [
    { id: "vpc-empty", name: "empty", cidr: "10.9.0.0/16", subnets: [] },
    {
      id: "vpc-1", name: "core", cidr: "10.20.0.0/16",
      subnets: [
        { id: "subnet-a", name: "private-a", cidr: "10.20.1.0/24", zone: "eu-west-1a" },
        { id: "subnet-b", name: "private-b", cidr: "10.20.2.0/24", zone: "eu-west-1b" },
        { id: "subnet-a2", name: "private-a2", cidr: "10.20.3.0/24", zone: "eu-west-1a" },
      ],
    },
  ],
  images: [
    { id: "ami-rhel", name: "RHEL 9", osFamily: "rhel" },
    { id: "ami-ubuntu", name: "Ubuntu 24.04", osFamily: "debian" },
  ],
  machineTypes: [{ id: "m6i.xlarge", cpu: 4, memoryGb: 16 }],
  kubernetesVersions: ["1.33", "1.32"],
  compartments: [],
};

describe("cidrs", () => {
  it("splits on commas, spaces and new lines without duplicates", () => {
    expect(parseCidrs("10.0.0.0/8, 192.168.1.0/24\n10.0.0.0/8  172.16.0.0/12")).toEqual([
      "10.0.0.0/8", "192.168.1.0/24", "172.16.0.0/12",
    ]);
  });

  it("refuses nothing, a bad network and the whole internet", () => {
    expect(cidrError("")).toMatch(/at least one network/);
    expect(cidrError("10.0.0.0")).toMatch(/not an IPv4 network/);
    expect(cidrError("10.300.0.0/16")).toMatch(/not an IPv4 network/);
    expect(cidrError(["0.0.0.0/0"])).toMatch(/everyone/);
    expect(cidrError("10.20.0.0/16, 10.30.0.0/16")).toBe("");
  });
});

describe("awsManagedSubnetError", () => {
  const subnets = INVENTORY.networks[1].subnets;
  it("needs two subnets in two zones", () => {
    expect(awsManagedSubnetError(subnets, ["subnet-a"])).toMatch(/at least 2 subnets/);
    expect(awsManagedSubnetError(subnets, ["subnet-a", "subnet-a2"])).toMatch(/different availability zones/);
    expect(awsManagedSubnetError(subnets, ["subnet-a", "subnet-b"])).toBe("");
  });
});

describe("defaultCloudChoices", () => {
  it("picks a network with subnets, the subnet's zone, a Debian-family image and the newest version", () => {
    const next = defaultCloudChoices(INVENTORY, { ...EMPTY_CLOUD, accountId: "3", region: "eu-west-1" }, "aws");
    expect(next.networkId).toBe("vpc-1");
    expect(next.subnetId).toBe("subnet-a");
    expect(next.zone).toBe("eu-west-1a");
    expect(next.imageId).toBe("ami-ubuntu");
    expect(next.kubernetesVersion).toBe("1.33");
    expect(next.allowedCidrs).toBe("10.20.0.0/16");
    expect(next.diskType).toBe("gp3");
  });

  it("keeps what the user typed for allowed networks", () => {
    const next = defaultCloudChoices(INVENTORY, { ...EMPTY_CLOUD, allowedCidrs: "10.1.0.0/16" }, "aws");
    expect(next.allowedCidrs).toBe("10.1.0.0/16");
  });

  it("follows a zonal subnet's zone", () => {
    const next = defaultCloudChoices(INVENTORY, { ...EMPTY_CLOUD, networkId: "vpc-1", subnetId: "subnet-b", zone: "eu-west-1a" }, "aws");
    expect(next.zone).toBe("eu-west-1b");
  });
});

describe("cloudProblem", () => {
  const base = defaultCloudChoices(INVENTORY, { ...EMPTY_CLOUD, accountId: "3", region: "eu-west-1" }, "aws");
  const account = { id: 3, provider: "aws" };
  const problem = (cloud, extra = {}) => cloudProblem({
    provider: "aws", account, cloud, inventory: INVENTORY, resolved: resolveCloud(INVENTORY, cloud), ...extra,
  });

  it("walks the choices in order", () => {
    expect(cloudProblem({ provider: "aws", account: null, cloud: base, inventory: null, resolved: {} }))
      .toMatch(/cloud account/);
    expect(problem({ ...base, region: "" })).toMatch(/region/);
    expect(problem({ ...base, networkId: "" })).toMatch(/VPC/);
    expect(problem({ ...base, imageId: "" })).toMatch(/image/);
    expect(problem({ ...base, allowedCidrs: "" })).toMatch(/at least one network/);
    expect(problem(base)).toBe("");
  });

  it("asks a managed AWS cluster for two zones' subnets, not an image", () => {
    const managed = { ...base, kind: "managed", imageId: "" };
    expect(problem(managed)).toMatch(/2 subnets/);
    expect(problem({ ...managed, subnetIds: ["subnet-a", "subnet-b"] })).toBe("");
  });

  it("asks OCI for a compartment", () => {
    expect(cloudProblem({
      provider: "oci", account: { id: 1 }, cloud: { ...base, compartmentId: "" },
      inventory: INVENTORY, resolved: resolveCloud(INVENTORY, base),
    })).toMatch(/compartment/);
  });
});

describe("cloudProvisioningPayload", () => {
  const cloud = defaultCloudChoices(INVENTORY, { ...EMPTY_CLOUD, accountId: "3", region: "eu-west-1" }, "aws");
  const resolved = resolveCloud(INVENTORY, cloud);
  const counts = { loadbalancer: 2, controlPlane: 3, worker: 3 };
  const sizes = { controlPlane: { cpu: 4, memoryGb: 8, diskGb: 80 }, worker: { cpu: 8, memoryGb: 16, diskGb: 120 } };

  it("builds a self-managed AWS spec", () => {
    const payload = cloudProvisioningPayload({ provider: "aws", cloud: { ...cloud, machineTypes: { worker: "m6i.2xlarge", controlPlane: "" } }, resolved, counts, sizes });
    expect(payload).toMatchObject({
      cloudAccountId: 3, region: "eu-west-1", zone: "eu-west-1a", kind: "vms",
      networkId: "vpc-1", subnetId: "subnet-a", subnetCidr: "10.20.1.0/24",
      image: { id: "ami-ubuntu", osFamily: "debian" }, counts, vmCount: null,
      machineTypes: { worker: "m6i.2xlarge" }, allowedCidrs: ["10.20.0.0/16"],
      managed: null, diskType: "gp3",
    });
    expect(payload.subnetIds).toBeUndefined();
  });

  it("builds a managed spec with only workers", () => {
    const payload = cloudProvisioningPayload({
      provider: "aws", cloud: { ...cloud, kind: "managed", subnetIds: ["subnet-a", "subnet-b"] }, resolved, counts, sizes,
    });
    expect(payload.kind).toBe("managed");
    expect(payload.image).toBeNull();
    expect(payload.counts).toEqual({ loadbalancer: 0, controlPlane: 0, worker: 3 });
    expect(payload.sizes).toEqual({ worker: sizes.worker });
    expect(payload.managed).toEqual({ kubernetesVersion: "1.33" });
    expect(payload.subnetIds).toEqual(["subnet-a", "subnet-b"]);
  });

  it("a VMs-only build is never managed and says how many", () => {
    const payload = cloudProvisioningPayload({
      provider: "gcp", cloud: { ...cloud, kind: "managed" }, resolved, vmsOnly: true,
      counts: { vm: 2 }, sizes: { vm: { cpu: 2, memoryGb: 4, diskGb: 40 } },
    });
    expect(payload.kind).toBe("vms");
    expect(payload.vmCount).toBe(2);
    expect(payload.counts).toBeNull();
    expect(payload.diskType).toBe("pd-balanced");
    expect(payload.masterCidr).toBeUndefined();
  });

  it("carries OCI's compartment and shape", () => {
    const payload = cloudProvisioningPayload({
      provider: "oci", cloud: { ...cloud, compartmentId: "ocid1.compartment.oc1..x" }, resolved, counts, sizes,
    });
    expect(payload.compartmentId).toBe("ocid1.compartment.oc1..x");
    expect(payload.shape).toBe("VM.Standard.E5.Flex");
  });

  it("round-trips through cloudFromSpec", () => {
    const payload = cloudProvisioningPayload({ provider: "aws", cloud, resolved, counts, sizes });
    const back = cloudFromSpec(payload);
    expect(back.accountId).toBe("3");
    expect(back.subnetId).toBe("subnet-a");
    expect(back.imageId).toBe("ami-ubuntu");
    expect(back.allowedCidrs).toBe("10.20.0.0/16");
  });
});

describe("machines and drawings", () => {
  const counts = { loadbalancer: 2, controlPlane: 3, worker: 2 };
  const sizes = { controlPlane: { cpu: 4, memoryGb: 8, diskGb: 80 }, worker: { cpu: 8, memoryGb: 16, diskGb: 100 } };

  it("never makes load-balancer VMs", () => {
    const machines = cloudMachines("prod", counts, sizes, { worker: { type: "m6i.2xlarge" } });
    expect(machines.map((m) => m.name)).toEqual(["prod-cp-1", "prod-cp-2", "prod-cp-3", "prod-wk-1", "prod-wk-2"]);
    expect(machines[3].machineType).toBe("m6i.2xlarge");
    expect(cloudTotals(counts, sizes)).toEqual({ machines: 5, cpu: 28, memoryGb: 56, diskGb: 440 });
    expect(cloudShapeLabel(counts)).toBe("3 control planes · 2 workers · cloud load balancer");
  });

  it("draws the cloud load balancer on the API bar", () => {
    expect(cloudBus({ loadBalancer: true })).toMatchObject({ label: "Cloud LB", address: "cloud load balancer", port: "6443" });
    expect(cloudBus({ loadBalancer: true, endpoint: "internal-x.elb.amazonaws.com:6443" }).address)
      .toBe("internal-x.elb.amazonaws.com");
    expect(cloudBus({ loadBalancer: false }).label).toBe("Endpoint");
    const plan = withCloudBus({ tiers: [{ role: "loadbalancer" }, { role: "control_plane" }], bus: {} }, { loadBalancer: true });
    expect(plan.tiers.map((t) => t.role)).toEqual(["control_plane"]);
  });

  it("draws a managed cluster as the cloud's control plane and a node pool", () => {
    const plan = managedBlueprint({ provider: "gcp", count: 3, machineType: "e2-standard-4" });
    expect(plan.tiers[0].slots[0].name).toBe("Google GKE");
    expect(plan.tiers[1]).toMatchObject({ label: "Node pool", target: 3, filled: 3 });
    expect(plan.tiers[1].slots[0].sub).toBe("e2-standard-4");
  });

  it("offers only load-balancer-free shapes for cloud VMs", () => {
    const shapes = [
      { key: "a", counts: { loadbalancer: 0, controlPlane: 1, worker: 1 } },
      { key: "b", counts: { loadbalancer: 1, controlPlane: 1, worker: 0 } },
    ];
    expect(cloudShapesForVms(shapes).map((s) => s.key)).toEqual(["a"]);
  });
});

describe("labels", () => {
  it("knows clouds and where things live", () => {
    expect(isCloud("aws")).toBe(true);
    expect(isCloud("vmware")).toBe(false);
    expect(cloudWhere({ provider: "oci", region: "me-dubai-1", zone: "AD-1" })).toBe("Oracle Cloud · me-dubai-1 · AD-1");
    expect(whereCreated({ cloud: { provider: "gcp" } })).toBe("in Google Cloud");
    expect(whereCreated({})).toBe("in vCenter");
  });
});

describe("account form", () => {
  const fields = [
    { key: "accessKeyId", label: "Access key ID", secret: false, required: true },
    { key: "secretAccessKey", label: "Secret access key", secret: true, required: true },
    { key: "sessionToken", label: "Session token", secret: true, required: false },
  ];
  const form = { name: "prod", provider: "aws", region: "eu-west-1", values: { accessKeyId: "AKIA1", secretAccessKey: "" } };

  it("requires a new secret but keeps a stored one when editing", () => {
    expect(accountFormError(form, fields)).toMatch(/Secret access key is required/);
    expect(accountFormError(form, fields, { editing: true, secretsSet: { secretAccessKey: true } })).toBe("");
  });

  it("splits settings from secrets and drops blank secrets", () => {
    expect(accountPayload({ ...form, values: { accessKeyId: " AKIA1 ", secretAccessKey: "s3cret", sessionToken: "" } }, fields))
      .toEqual({
        name: "prod", provider: "aws", region: "eu-west-1",
        settings: { accessKeyId: "AKIA1" }, secrets: { secretAccessKey: "s3cret" }, caPem: "",
      });
  });
});

describe("shared provisioning helpers on a cloud", async () => {
  const { planGroups, provisionRail } = await import("./clusterProvisioning.js");
  const { deletePolicy } = await import("./clusterBuilder.js");

  it("keeps a cloud plan's load balancer, network and managed rows", () => {
    const groups = planGroups({
      resources: [
        { kind: "cluster", address: "aws_eks_cluster.cluster" },
        { kind: "nodepool", address: "aws_eks_node_group.workers" },
        { kind: "iam", address: "aws_iam_role.cluster" },
        { kind: "lb", address: "aws_lb.api" },
        { kind: "network", address: "aws_security_group.cluster" },
        { kind: "vm", address: 'aws_instance.node["a-cp-1"]' },
      ],
    });
    expect(groups.map((g) => g.kind)).toEqual(["cluster", "nodepool", "vm", "lb", "network", "iam"]);
  });

  it("draws a managed build's rail as plan, create, register", () => {
    const cells = provisionRail({ cloud: { kind: "managed" }, provisioning: { job: { status: "connecting" } } });
    expect(cells.map((c) => [c.label, c.state])).toEqual([
      ["Plan", "done"], ["Create the cluster", "done"], ["Register in KubeSight", "now"],
    ]);
  });

  it("asks for a destroy before deleting a cloud build that still has resources", () => {
    expect(deletePolicy({ status: "vms_ready", machineSource: "aws", canDestroy: true }).destroyFirst).toBe(true);
    expect(deletePolicy({ status: "completed", machineSource: "gcp", cloud: { kind: "managed" }, canDestroy: true }).reason)
      .toMatch(/cluster still exists/);
  });
});

describe("needsZone", () => {
  it("asks for a zone for VMs and for a zonal GKE cluster only", async () => {
    const { needsZone } = await import("./cloudProvisioning.js");
    expect(needsZone("aws", false)).toBe(true);
    expect(needsZone("gcp", true, { regional: false })).toBe(true);
    expect(needsZone("gcp", true, { regional: true })).toBe(false);
    expect(needsZone("aws", true)).toBe(false);
    expect(needsZone("oci", true)).toBe(false);
  });
});
