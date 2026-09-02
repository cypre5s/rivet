import { describe, expect, test } from "bun:test";

import { computeLayout } from "./layout.ts";

describe("minimal terminal layout", () => {
  test("uses only minimal and standard modes", () => {
    expect(computeLayout(40, 12).mode).toBe("minimal");
    expect(computeLayout(80, 24).mode).toBe("standard");
    expect(computeLayout(160, 40).mode).toBe("standard");
  });

  test("keeps detail readable in narrow and wide terminals", () => {
    expect(computeLayout(80, 24).panelPresentation).toBe("fullscreen");
    expect(computeLayout(120, 30).panelPresentation).toBe("sidebar");
  });

  test("rejects invalid terminal dimensions", () => {
    expect(() => computeLayout(0, 20)).toThrow();
  });
});
