import { describe, expect, test } from "bun:test";

import {
  COMMAND_REGISTRY,
  commandAvailability,
  findCommand,
  type CommandContext,
} from "./command-registry.ts";
import { searchCommands } from "./command-search.ts";

const BASE_CONTEXT: CommandContext = {
  modelConfigured: true,
  currentModel: "deepseek-v4-pro",
  hasSession: true,
  transactionId: "tx_one",
  verificationStatus: "PASSED",
  evidenceId: "evidence_one",
};

describe("unified command registry", () => {
  test("contains every published operation with unique names and ids", () => {
    const required = [
      "new", "sessions", "resume", "clear", "history", "quit",
      "ask", "plan", "fix", "verify", "diff", "evidence", "apply", "abort",
      "read", "files", "context", "search", "model", "mode", "modules",
      "trace", "status", "cost", "init", "doctor", "benchmark", "config",
      "clean", "help", "keys", "theme", "export",
    ];
    const names = COMMAND_REGISTRY.map((command) => command.name);
    const ids = COMMAND_REGISTRY.map((command) => command.id);

    expect(names).toEqual(required);
    expect(new Set(names).size).toBe(names.length);
    expect(new Set(ids).size).toBe(ids.length);
  });

  test("supports exact, prefix, recent, fuzzy, Chinese and alias search", () => {
    expect(searchCommands(COMMAND_REGISTRY, "verify")[0]?.command.name).toBe(
      "verify",
    );
    expect(searchCommands(COMMAND_REGISTRY, "ver")[0]?.command.name).toBe(
      "verify",
    );
    expect(searchCommands(COMMAND_REGISTRY, "验证")[0]?.command.name).toBe(
      "verify",
    );
    expect(findCommand("退出")?.name).toBe("quit");
    expect(
      searchCommands(COMMAND_REGISTRY, "", ["command.fix"])[0]?.command.name,
    ).toBe("fix");
  });

  test("explains unavailable and dangerous operations without hiding them", () => {
    const unavailable = {
      ...BASE_CONTEXT,
      modelConfigured: false,
      transactionId: null,
      verificationStatus: "FAILED",
      evidenceId: null,
    };
    expect(commandAvailability(findCommand("ask")!, unavailable)).toEqual({
      available: false,
      reason: "尚未配置模型凭据",
    });
    expect(commandAvailability(findCommand("apply")!, unavailable).reason).toBe(
      "当前没有活动事务",
    );
    expect(
      commandAvailability(findCommand("apply")!, {
        ...unavailable,
        transactionId: "tx_one",
      }).reason,
    ).toBe("只有验证通过的事务可以应用");
    expect(findCommand("apply")?.dangerous).toBeTrue();
    expect(findCommand("clean")?.dangerous).toBeTrue();
  });
});
