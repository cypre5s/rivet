import { describe, expect, test } from "bun:test";

import { initialRivetState } from "../state/reducer.ts";
import { COMPOSER_PLACEHOLDER, costLines, statusLines } from "./app-model.ts";

describe("concise application copy", () => {
  test("uses one short stable prompt instead of rotating explanatory copy", () => {
    expect(COMPOSER_PLACEHOLDER).toBe("输入任务…");
    expect(COMPOSER_PLACEHOLDER.length).toBeLessThanOrEqual(6);
  });

  test("keeps status and cost views factual without explanatory filler", () => {
    const state = initialRivetState();
    const status = statusLines(state, "ASK", false).join("\n");
    const cost = costLines(state).join("\n");

    expect(status).toContain("ASK · IDLE");
    expect(status).not.toContain("个可用");
    expect(cost).not.toContain("自动隐藏");
    expect(statusLines(state, "ASK", false)).toHaveLength(3);
    expect(costLines(state)).toHaveLength(1);
  });
});
