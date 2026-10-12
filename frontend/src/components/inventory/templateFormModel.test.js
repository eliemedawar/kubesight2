import { describe, expect, it } from "vitest";

import {
  buildTemplatePayload,
  describeKeptExtras,
  formFromTemplate,
  freshForm,
  joinExtras,
  mergeTemplateEdit,
  validateTemplateForm,
} from "./templateFormModel.js";

// A template richer than the form: a sidecar, a NodePort Service on another
// port, a manual PV on one claim, probe timeouts, an ingress block, keys the
// form has never heard of.
function richTemplate() {
  return {
    id: "payments-api",
    custom: true,
    name: "Payments API",
    description: "Imported from YAML (Deployment)",
    category: "Imported",
    workloadType: "Deployment",
    containers: [
      { name: "api", image: "registry/payments", tag: "1.4.2", pullPolicy: "Always", ports: [8080, 9090], args: ["--serve"] },
      { name: "log-shipper", image: "fluent/fluent-bit", tag: "2.2", pullPolicy: "IfNotPresent", ports: [2020] },
    ],
    resources: { cpuRequest: "250m", cpuLimit: "1", memoryRequest: "256Mi", memoryLimit: "1Gi", ephemeralStorage: "2Gi" },
    scaling: { replicas: 3 },
    networking: {
      service: {
        enabled: true,
        type: "NodePort",
        protocol: "TCP",
        port: 80,
        targetPort: 8080,
        ports: [
          { name: "http", port: 80, targetPort: 8080, nodePort: 30080, protocol: "TCP" },
          { name: "metrics", port: 9090, targetPort: 9090, protocol: "TCP" },
        ],
      },
      ingress: { enabled: true, host: "pay.example.com", path: "/" },
    },
    storage: {
      pvcMode: "new",
      newPvc: { name: "pin-claim", size: "1Gi", accessMode: "ReadWriteOnce" },
      newPvcs: [
        { name: "pin-claim", size: "1Gi", accessMode: "ReadWriteOnce" },
        {
          name: "logs-claim",
          size: "10Gi",
          accessMode: "ReadWriteOnce",
          advanced: { createManualPv: true, storageType: "nfs", nfsServer: "10.0.0.5", nfsPath: "/exports/logs" },
        },
      ],
      volumeMounts: [
        { name: "data", mountPath: "/app/pin", readOnly: false, pvcName: "pin-claim" },
        { name: "data-2", mountPath: "/app/logs", readOnly: false, pvcName: "logs-claim" },
      ],
    },
    healthChecks: {
      readiness: { enabled: true, type: "http", path: "/ready", port: 8080, initialDelaySeconds: 5, periodSeconds: 10, timeoutSeconds: 3, failureThreshold: 6 },
    },
    schema: {
      overrides: { tag: true, replicas: true },
      env: [
        { key: "LOG_LEVEL", required: false, sensitive: false, kind: "value", default: "info", allowedSources: ["value"], description: "Root log level" },
        { key: "DB_PASSWORD", required: true, sensitive: true, kind: "secret", allowedSources: ["existingSecret"] },
      ],
      imagePullSecret: { required: true },
    },
    podAnnotations: { "prometheus.io/scrape": "true" },
    warnings: ["Workload has 2 containers; …"],
  };
}

// Open the form on `stored`, let `edit` change it, save.
function saveAfter(stored, edit) {
  const initial = formFromTemplate(stored);
  const form = edit(JSON.parse(JSON.stringify(initial)));
  return mergeTemplateEdit(stored, initial, form);
}

const READ_ONLY = ["id", "custom", "warnings"];
function withoutReadOnly(template) {
  const copy = JSON.parse(JSON.stringify(template));
  for (const key of READ_ONLY) delete copy[key];
  return copy;
}

describe("mergeTemplateEdit", () => {
  it("saves an untouched template exactly as stored (minus read-only keys)", () => {
    const stored = richTemplate();
    expect(saveAfter(stored, (form) => form)).toEqual(withoutReadOnly(stored));
  });

  it("keeps every extra container when the first one is edited", () => {
    const stored = richTemplate();
    const saved = saveAfter(stored, (form) => ({ ...form, tag: "1.5.0" }));
    expect(saved.containers).toHaveLength(2);
    expect(saved.containers[0]).toEqual({ ...stored.containers[0], tag: "1.5.0" });
    expect(saved.containers[1]).toEqual(stored.containers[1]);
    // Nothing else moved.
    expect({ ...saved, containers: null }).toEqual({ ...withoutReadOnly(stored), containers: null });
  });

  it("keeps extra container ports and moves only the Service port that followed the container", () => {
    const stored = richTemplate();
    const saved = saveAfter(stored, (form) => ({ ...form, port: "8081" }));
    expect(saved.containers[0].ports).toEqual([8081, 9090]);
    const service = saved.networking.service;
    expect(service.type).toBe("NodePort");
    expect(service.port).toBe(80);
    expect(service.targetPort).toBe(8081);
    expect(service.ports[0]).toEqual({ name: "http", port: 80, targetPort: 8081, nodePort: 30080, protocol: "TCP" });
    expect(service.ports[1]).toEqual(stored.networking.service.ports[1]);
    expect(saved.networking.ingress).toEqual(stored.networking.ingress);
  });

  it("keeps a per-claim manual PV when another volume's size is edited", () => {
    const stored = richTemplate();
    const saved = saveAfter(stored, (form) => ({
      ...form,
      storageVolumes: form.storageVolumes.map((row, i) => (i === 0 ? { ...row, size: "2Gi" } : row)),
    }));
    expect(saved.storage.newPvcs[0]).toEqual({ name: "pin-claim", size: "2Gi", accessMode: "ReadWriteOnce" });
    expect(saved.storage.newPvcs[1]).toEqual(stored.storage.newPvcs[1]);
    expect(saved.storage.newPvc).toEqual(saved.storage.newPvcs[0]);
    expect(saved.storage.volumeMounts).toEqual(stored.storage.volumeMounts);
  });

  it("drops a removed volume's claim but keeps the manual PV of the one left", () => {
    const stored = richTemplate();
    const saved = saveAfter(stored, (form) => ({ ...form, storageVolumes: form.storageVolumes.slice(1) }));
    expect(saved.storage.volumeMounts).toEqual([stored.storage.volumeMounts[1]]);
    expect(saved.storage.newPvcs).toEqual([stored.storage.newPvcs[1]]);
    expect(saved.storage.newPvc.advanced.createManualPv).toBe(true);
  });

  it("adds a new volume with its own non-colliding claim", () => {
    const stored = richTemplate();
    const saved = saveAfter(stored, (form) => ({
      ...form,
      storageVolumes: [...form.storageVolumes, { size: "5Gi", mountPath: "/cache" }],
    }));
    expect(saved.storage.newPvcs).toHaveLength(3);
    const added = saved.storage.newPvcs[2];
    expect(added).toMatchObject({ name: "payments-api-data", size: "5Gi" });
    expect(saved.storage.volumeMounts[2]).toMatchObject({ mountPath: "/cache", pvcName: "payments-api-data", name: "data-3" });
  });

  it("keeps unknown keys at every level", () => {
    const stored = richTemplate();
    const saved = saveAfter(stored, (form) => ({ ...form, description: "Edited", cpuLimit: "2" }));
    expect(saved.description).toBe("Edited");
    expect(saved.podAnnotations).toEqual(stored.podAnnotations);
    expect(saved.resources).toEqual({ ...stored.resources, cpuLimit: "2" });
    expect(saved.schema.imagePullSecret).toEqual({ required: true });
    expect(saved.containers[0].args).toEqual(["--serve"]);
    expect(saved).not.toHaveProperty("id");
    expect(saved).not.toHaveProperty("warnings");
  });

  it("edits a probe field by field, keeping its timeout and threshold", () => {
    const stored = richTemplate();
    const saved = saveAfter(stored, (form) => ({
      ...form,
      probes: { ...form.probes, readiness: { ...form.probes.readiness, path: "/healthz" } },
    }));
    expect(saved.healthChecks.readiness).toEqual({ ...stored.healthChecks.readiness, path: "/healthz" });
  });

  it("keeps env-row keys the form doesn't show and a narrowed source list", () => {
    const stored = richTemplate();
    const saved = saveAfter(stored, (form) => ({
      ...form,
      envVars: [...form.envVars.map((row) => (row.key === "LOG_LEVEL" ? { ...row, default: "debug" } : row)), { key: "NEW_FLAG", required: false, sensitive: false, kind: "value", default: "" }],
    }));
    expect(saved.schema.env[0]).toEqual({ ...stored.schema.env[0], default: "debug" });
    expect(saved.schema.env[1]).toEqual(stored.schema.env[1]);
    expect(saved.schema.env[2]).toEqual({ key: "NEW_FLAG", required: false, sensitive: false, kind: "value", allowedSources: ["value"] });
    expect(saved.environment).toEqual({ envVars: [{ name: "LOG_LEVEL", value: "debug" }] });
  });

  it("removes a field the form models when the user removes it", () => {
    const stored = richTemplate();
    const saved = saveAfter(stored, (form) => ({
      ...form,
      serviceEnabled: false,
      probes: { ...form.probes, readiness: { ...form.probes.readiness, enabled: false } },
      envVars: form.envVars.filter((row) => row.key !== "LOG_LEVEL"),
      overrides: { ...form.overrides, tag: false },
    }));
    // The Service goes; the ingress block the form doesn't model stays.
    expect(saved.networking).toEqual({ ingress: stored.networking.ingress });
    expect(saved).not.toHaveProperty("healthChecks");
    expect(saved.schema.env).toEqual([stored.schema.env[1]]);
    expect(saved.schema.overrides).toEqual({ replicas: true });
  });

  it("switching storage off removes the storage block", () => {
    const stored = richTemplate();
    const saved = saveAfter(stored, (form) => ({ ...form, storageEnabled: false }));
    expect(saved).not.toHaveProperty("storage");
  });

  it("turning a disabled Service back on keeps its stored type and ports", () => {
    const stored = richTemplate();
    stored.networking.service.enabled = false;
    const saved = saveAfter(stored, (form) => ({ ...form, serviceEnabled: true }));
    expect(saved.networking.service).toEqual({ ...stored.networking.service, enabled: true });
  });

  it("builds the whole template from the form when there is nothing stored", () => {
    const form = { ...freshForm("Web"), name: "Hello", image: "nginx" };
    expect(mergeTemplateEdit(null, null, form)).toEqual(buildTemplatePayload(form));
  });

  it("an import draft keeps its sidecar on create", () => {
    const draft = richTemplate();
    delete draft.id;
    const saved = saveAfter(draft, (form) => ({ ...form, name: "payments" }));
    expect(saved.name).toBe("payments");
    expect(saved.containers.map((c) => c.name)).toEqual(["api", "log-shipper"]);
    expect(saved).not.toHaveProperty("warnings");
  });
});

describe("describeKeptExtras", () => {
  it("names what the form can't show", () => {
    const extras = describeKeptExtras(richTemplate());
    const text = joinExtras(extras);
    expect(text).toContain("1 more container (log-shipper)");
    expect(text).toContain("1 more container port");
    expect(text).toContain("Service settings (type NodePort, 2 ports, port 80)");
    expect(text).toContain("ingress networking");
    expect(text).toContain("manual PersistentVolume settings for 1 volume");
    expect(text).toContain("advanced probe settings");
    expect(text).toContain("schema imagePullSecret");
    expect(text).toContain("other settings (podAnnotations)");
    expect(text).toContain("resource settings (ephemeralStorage)");
  });

  it("is empty for a template the form fully shows", () => {
    const form = { ...freshForm("Web"), name: "Hello", image: "nginx" };
    expect(describeKeptExtras(buildTemplatePayload(form))).toEqual([]);
    expect(describeKeptExtras(null)).toEqual([]);
  });

  it("joins with commas and a final 'and'", () => {
    expect(joinExtras(["a"])).toBe("a");
    expect(joinExtras(["a", "b", "c"])).toBe("a, b and c");
  });
});

describe("validateTemplateForm", () => {
  it("asks for the name, category and image in order", () => {
    const form = freshForm("Web");
    expect(validateTemplateForm(form)).toBe("Template name is required.");
    expect(validateTemplateForm({ ...form, name: "x" })).toBe("Container image is required.");
    expect(validateTemplateForm({ ...form, name: "x", category: "", image: "nginx" })).toMatch(/category/);
    expect(validateTemplateForm({ ...form, name: "x", image: "nginx" })).toBe("");
  });
});
