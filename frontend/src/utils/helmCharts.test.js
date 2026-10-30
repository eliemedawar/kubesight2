import { describe, expect, it } from "vitest";

import { chartLabelVersion, matchStoredChart } from "./helmCharts.js";

const charts = [
  { id: "ingress", name: "ingress" },
  { id: "ingress-nginx", name: "ingress-nginx" },
  { id: "payments", name: "Payments" },
];

describe("matchStoredChart", () => {
  it("matches the chart name in front of the version", () => {
    expect(matchStoredChart("payments-1.2.0", charts)?.id).toBe("payments");
  });

  it("prefers the longest name when chart names contain dashes", () => {
    expect(matchStoredChart("ingress-nginx-4.10.1", charts)?.id).toBe("ingress-nginx");
    expect(matchStoredChart("ingress-0.3.0", charts)?.id).toBe("ingress");
  });

  it("does not match a different chart that shares a prefix", () => {
    expect(matchStoredChart("ingress-controller-1.0.0", charts)).toBeNull();
    expect(matchStoredChart("", charts)).toBeNull();
  });
});

describe("chartLabelVersion", () => {
  it("reads the version off a Helm chart label", () => {
    expect(chartLabelVersion("ingress-nginx-4.10.1")).toBe("4.10.1");
    expect(chartLabelVersion("payments-1.0.0-rc.1")).toBe("1.0.0-rc.1");
    expect(chartLabelVersion("nameonly")).toBe("");
  });
});
