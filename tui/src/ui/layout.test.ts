import { describe, expect, test } from "bun:test";

import { computeLayout } from "./layout.ts";

describe("responsive terminal layout", () => {
  test("selects a stable layout for every required terminal size", () => {
    expect(computeLayout(40, 12).mode).toBe("minimal");
    expect(computeLayout(60, 18).mode).toBe("compact");
    expect(computeLayout(80, 24).mode).toBe("drawer");
    expect(computeLayout(100, 28).mode).toBe("drawer");
    expect(computeLayout(120, 30).mode).toBe("wide");
    expect(computeLayout(160, 40).mode).toBe("wide");
  });

  test("degrades logo and optional copy before hiding the composer", () => {
    expect(computeLayout(40, 12)).toMatchObject({
      logoSize: "text",
      panelPresentation: "fullscreen",
      contentWidth: "100%",
      showShortcutHints: false,
      showTip: false,
    });
    expect(computeLayout(80, 24)).toMatchObject({
      panelPresentation: "drawer",
      showShortcutHints: true,
      showTip: true,
    });
    expect(computeLayout(120, 30)).toMatchObject({
      logoSize: "large",
      panelPresentation: "sidebar",
    });
  });
});
