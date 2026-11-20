import { describe, expect, it } from "vitest";
import { fittingServiceDefaults, ownDefaultFor, withServiceDefaults } from "./BuildParameters.jsx";

const VARIANT = { name: "APP_VARIANT", type: "choice", choices: ["issuing", "acquiring"], default: "issuing" };
const DEBUG = { name: "DEBUG", type: "boolean", default: "false" };
const TAG = { name: "IMAGE_TAG", type: "text", default: "" };

describe("a linked service's own defaults for shared build inputs", () => {
  it("starts each input at the service's default where it has one", () => {
    const effective = withServiceDefaults([VARIANT, DEBUG, TAG], { APP_VARIANT: "acquiring", DEBUG: true });
    expect(effective.map((p) => p.default)).toEqual(["acquiring", "true", ""]);
  });

  it("ignores a default the shared pipeline no longer offers, like the server", () => {
    expect(ownDefaultFor(VARIANT, { APP_VARIANT: "prod" })).toBeNull();
    expect(withServiceDefaults([VARIANT], { APP_VARIANT: "prod" })[0].default).toBe("issuing");
  });

  it("saves only entries that apply and differ from the shared default", () => {
    expect(
      fittingServiceDefaults([VARIANT, DEBUG, TAG], {
        APP_VARIANT: "issuing",
        DEBUG: "true",
        IMAGE_TAG: "  v2 ",
        GONE: "x",
      })
    ).toEqual({ DEBUG: "true", IMAGE_TAG: "v2" });
    expect(fittingServiceDefaults([VARIANT], { APP_VARIANT: "prod" })).toEqual({});
  });
});
