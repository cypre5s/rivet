import { describe, expect, test } from "bun:test";

import { computeLayout } from "./layout.ts";

describe("responsive terminal layout", () => {
  test("uses one column at 80x24", () => {
    expect(computeLayout(80, 24)).toEqual({
      mode: "single",
      visiblePanels: ["timeline"],
      inspectorOverlay: true,
    });
  });

  test("uses two columns at 120x30", () => {
    expect(computeLayout(120, 30)).toEqual({
      mode: "split",
      visiblePanels: ["repository", "timeline"],
      inspectorOverlay: true,
    });
  });

  test("uses three columns at 200x50", () => {
    expect(computeLayout(200, 50)).toEqual({
      mode: "three-column",
      visiblePanels: ["repository", "timeline", "inspector"],
      inspectorOverlay: false,
    });
  });
});
