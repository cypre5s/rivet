import { describe, expect, test } from "bun:test";

import {
  COMMAND_REGISTRY,
  commandAvailability,
  findCommand,
  type CommandContext,
} from "./command-registry.ts";
import { searchCommands } from "./command-search.ts";

const READY: CommandContext = {
  modelConfigured: true,
  currentModel: "deepseek-v4-pro",
  transactionId: "tx_one",
  verificationStatus: "PASSED",
  evidenceId: "evidence_one",
  acceptanceReady: true,
};

describe("focused Slash registry", () => {
  test("exposes exactly the seven supported Slash commands", () => {
    expect(COMMAND_REGISTRY.map((command) => command.name)).toEqual([
      "help",
      "fix",
      "diff",
      "verify",
      "apply",
      "abort",
      "model",
    ]);
    expect(new Set(COMMAND_REGISTRY.map((command) => command.id)).size).toBe(7);
  });

  test("searches stable names and localized labels", () => {
    expect(searchCommands(COMMAND_REGISTRY, "ver")[0]?.command.name).toBe(
      "verify",
    );
    expect(searchCommands(COMMAND_REGISTRY, "验证")[0]?.command.name).toBe(
      "verify",
    );
    expect(findCommand("计划")).toBeNull();
  });

  test("gates FIX on acceptance and Apply on independent verification", () => {
    expect(commandAvailability(findCommand("fix")!, {
      ...READY,
      acceptanceReady: false,
    })).toEqual({
      available: false,
      reason: "缺少独立 AcceptanceSpec，不能开始修复",
    });
    expect(commandAvailability(findCommand("apply")!, {
      ...READY,
      verificationStatus: "NOT_RUN",
    }).available).toBeFalse();
    expect(commandAvailability(findCommand("apply")!, READY).available).toBeTrue();
  });

  test("keeps dangerous operations explicit", () => {
    expect(findCommand("apply")?.dangerous).toBeTrue();
    expect(findCommand("abort")?.dangerous).toBeTrue();
    expect(findCommand("fix")?.dangerous).not.toBeTrue();
  });
});
