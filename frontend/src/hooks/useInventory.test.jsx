// @vitest-environment jsdom
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const listInventory = vi.fn();
const getInventoryDetail = vi.fn();

vi.mock("../api/inventoryApi.js", () => ({
  listInventory: (...a) => listInventory(...a),
  getInventoryDetail: (...a) => getInventoryDetail(...a),
}));

const { useApplicationDetail, useInventoryClusterOptions } = await import("./useInventory.js");

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  listInventory.mockResolvedValue([]);
  getInventoryDetail.mockResolvedValue({});
});

let latest = null;
function Probe({ hook, ...props }) {
  latest = hook(props);
  return null;
}

const deferred = () => {
  let resolve;
  const promise = new Promise((res) => {
    resolve = res;
  });
  return { promise, resolve };
};

describe("cluster options", () => {
  const allowedClusters = [{ id: "prod-eu", name: "Production EU" }];

  it("lists the clusters the user can reach", async () => {
    render(<Probe hook={useInventoryClusterOptions} allowedClusters={allowedClusters} />);
    await waitFor(() => expect(latest.options).toHaveLength(1));
    expect(latest.options[0]).toEqual({ id: "prod-eu", name: "Production EU" });
  });

  // The one thing the inventory fetch earns: an application can live in a
  // cluster that is not in the allowed list, and the dropdown should name it
  // rather than showing a blank.
  it("adds clusters that only appear in the inventory", async () => {
    listInventory.mockResolvedValue([{ cluster: "legacy-dc" }]);
    render(<Probe hook={useInventoryClusterOptions} allowedClusters={allowedClusters} />);
    await waitFor(() => expect(latest.options).toHaveLength(2));
    expect(latest.options.map((o) => o.id).sort()).toEqual(["legacy-dc", "prod-eu"]);
  });

  it("does not duplicate a cluster present in both", async () => {
    listInventory.mockResolvedValue([{ cluster: "prod-eu" }, { clusterId: "prod-eu" }]);
    render(<Probe hook={useInventoryClusterOptions} allowedClusters={allowedClusters} />);
    await waitFor(() => expect(latest.options).toHaveLength(1));
  });

  it("still lists reachable clusters when the inventory call fails", async () => {
    listInventory.mockRejectedValue(new Error("boom"));
    render(<Probe hook={useInventoryClusterOptions} allowedClusters={allowedClusters} />);
    await waitFor(() => expect(latest.options).toHaveLength(1));
  });

  it("skips the fetch when disabled", async () => {
    render(
      <Probe hook={useInventoryClusterOptions} allowedClusters={allowedClusters} enabled={false} />
    );
    await waitFor(() => expect(latest.options).toHaveLength(1));
    expect(listInventory).not.toHaveBeenCalled();
  });
});

describe("application detail", () => {
  it("loads the application named in the URL", async () => {
    getInventoryDetail.mockResolvedValue({ summary: { name: "payments-api" } });
    render(<Probe hook={useApplicationDetail} />);
    // The hook takes the id positionally; render with it directly.
    cleanup();

    function IdProbe({ id }) {
      latest = useApplicationDetail(id);
      return null;
    }
    render(<IdProbe id="42" />);
    await waitFor(() => expect(latest.detail?.summary?.name).toBe("payments-api"));
    expect(getInventoryDetail).toHaveBeenCalledWith("42");
  });

  // The id comes from the path now, so it can change as fast as a click. A slow
  // response for the previous application must not paint under the new name.
  it("discards a response for an application that is no longer open", async () => {
    function IdProbe({ id }) {
      latest = useApplicationDetail(id);
      return null;
    }

    const slow = deferred();
    getInventoryDetail.mockReturnValueOnce(slow.promise);
    const { rerender } = render(<IdProbe id="42" />);

    getInventoryDetail.mockResolvedValueOnce({ summary: { name: "billing" } });
    await act(async () => rerender(<IdProbe id="43" />));
    await waitFor(() => expect(latest.detail?.summary?.name).toBe("billing"));

    await act(async () => {
      slow.resolve({ summary: { name: "payments-api" } });
      await slow.promise;
    });

    expect(latest.detail.summary.name).toBe("billing");
  });

  it("clears the previous application while the next one loads", async () => {
    function IdProbe({ id }) {
      latest = useApplicationDetail(id);
      return null;
    }
    getInventoryDetail.mockResolvedValueOnce({ summary: { name: "payments-api" } });
    const { rerender } = render(<IdProbe id="42" />);
    await waitFor(() => expect(latest.detail).toBeTruthy());

    getInventoryDetail.mockReturnValueOnce(deferred().promise);
    await act(async () => rerender(<IdProbe id="43" />));

    expect(latest.detail).toBeNull();
  });

  it("fetches nothing without an id", async () => {
    function IdProbe({ id }) {
      latest = useApplicationDetail(id);
      return null;
    }
    render(<IdProbe id="" />);
    await waitFor(() => expect(latest.detail).toBeNull());
    expect(getInventoryDetail).not.toHaveBeenCalled();
  });

  it("surfaces a load failure", async () => {
    function IdProbe({ id }) {
      latest = useApplicationDetail(id);
      return null;
    }
    getInventoryDetail.mockRejectedValue(new Error("Something broke"));
    render(<IdProbe id="42" />);
    await waitFor(() => expect(latest.error).toBe("Something broke"));
    expect(latest.detail).toBeNull();
  });
});
