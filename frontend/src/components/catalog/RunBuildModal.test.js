import { describe, expect, it } from "vitest";

import { defaultParameterValues } from "./RunBuildModal.jsx";

describe("RunBuildModal parameter defaults", () => {
  it("starts every dialog from the pipeline defaults", () => {
    const parameters = [
      { name: "fastenvandroid", default: "android/app/areebapay.keystore" },
      { name: "BuildOnlyApk", default: true },
      { name: "optional", default: null },
    ];

    expect(defaultParameterValues(parameters)).toEqual({
      fastenvandroid: "android/app/areebapay.keystore",
      BuildOnlyApk: "true",
      optional: "",
    });
  });
});
