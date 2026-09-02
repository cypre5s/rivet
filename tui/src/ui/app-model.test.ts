import { describe, expect, test } from "bun:test";

import {
  COMPOSER_PLACEHOLDER,
  descriptorForInput,
  panelForWorkerCommand,
} from "./app-model.ts";

describe("minimal application model", () => {
  test("treats plain input as ASK without exposing an ask mode command", () => {
    expect(COMPOSER_PLACEHOLDER).toContain("/fix");
    expect(descriptorForInput("解释项目")).toBeNull();
  });

  test("routes only transaction inspection commands to panels", () => {
    expect(panelForWorkerCommand("diff")).toBe("Diff");
    expect(panelForWorkerCommand("verify")).toBe("Verify");
    expect(panelForWorkerCommand("fix")).toBeNull();
  });
});
