import { describe, expect, test } from "bun:test";

import { initialRivetState } from "../state/reducer.ts";
import { INSPECTOR_TABS, buildViewModel } from "./view-model.ts";

describe("observable workbench model", () => {
  test("always exposes all required inspector tabs and status fields", () => {
    const view = buildViewModel(initialRivetState(), {
      width: 200,
      height: 50,
      noColor: true,
    });

    expect(INSPECTOR_TABS).toEqual([
      "Plan",
      "Context",
      "Diff",
      "Verify",
      "Evidence",
      "Modules",
    ]);
    expect(view.header).toMatchObject({
      model: "未连接",
      phase: "IDLE",
      transaction: "无",
    });
    expect(view.noColor).toBeTrue();
    expect(view.layout.mode).toBe("three-column");
  });
});
