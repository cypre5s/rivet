import { describe, expect, test } from "bun:test";

import { parseCommandInput } from "./commands.ts";

describe("TUI command input", () => {
  test("routes plain text to the read-only ask command", () => {
    expect(parseCommandInput("解释这个仓库")).toEqual({
      method: "command.ask",
      params: { query: "解释这个仓库" },
    });
  });

  test("routes the six workbench commands without shell parsing", () => {
    expect(parseCommandInput("/ask 为什么失败").method).toBe("command.ask");
    expect(parseCommandInput("/plan 修复边界").method).toBe("command.plan");
    expect(parseCommandInput("/fix 修复边界").method).toBe("command.fix");
    expect(parseCommandInput("/verify tx_one")).toEqual({
      method: "command.verify",
      params: { transaction_id: "tx_one" },
    });
    expect(parseCommandInput("/diff")).toEqual({
      method: "command.diff",
      params: {},
    });
    expect(parseCommandInput("/apply tx_one").method).toBe("command.apply");
  });

  test("rejects incomplete or unknown commands", () => {
    expect(() => parseCommandInput("/fix")).toThrow("需要任务文本");
    expect(() => parseCommandInput("/apply")).toThrow("需要事务 ID");
    expect(() => parseCommandInput("/unknown value")).toThrow("未知命令");
  });
});
