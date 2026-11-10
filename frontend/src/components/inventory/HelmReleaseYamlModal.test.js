import { describe, expect, it } from "vitest";

import { manifestResources } from "./HelmReleaseYamlModal.jsx";

const HELM_MANIFEST = `---
# Source: api/templates/service.yaml
apiVersion: v1
kind: Service
metadata:
  labels:
    name: not-this-one
  name: api
spec:
  ports: []
---
# Source: api/templates/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: "api-worker"
  annotations:
    app.kubernetes.io/name: also-not-this
`;

// The redacted form: yaml.dump output, no comments, no leading ---.
const REDACTED = `apiVersion: v1
kind: Secret
metadata:
  name: db
__warning: Secret values redacted from preview

---
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
`;

describe("manifestResources", () => {
  it("splits a Helm manifest and names each resource by its own metadata.name", () => {
    const items = manifestResources(HELM_MANIFEST);
    expect(items.map((item) => item.label)).toEqual(["Service / api", "Deployment / api-worker"]);
    expect(items[0].source).toBe("api/templates/service.yaml");
    expect(items[1].text.startsWith("# Source: api/templates/deployment.yaml\n")).toBe(true);
  });

  it("reads the redacted form, which has no Source comments", () => {
    const items = manifestResources(REDACTED);
    expect(items.map((item) => item.label)).toEqual(["Secret / db", "ConfigMap / settings"]);
    expect(items[0].source).toBe("");
  });

  it("skips empty and comment-only documents", () => {
    expect(manifestResources("---\n# Source: x/templates/empty.yaml\n---\n")).toEqual([]);
    expect(manifestResources("")).toEqual([]);
  });
});
