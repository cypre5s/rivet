import { describe, expect, test } from "bun:test";

import type { CommandContext } from "./command-registry.ts";
import {
  commandArgumentCompletions,
  commandArgumentRequest,
  commandNames,
  fileMentionQuery,
  parseCommandInput,
  parseFixArgument,
  replaceFileMention,
  slashQuery,
} from "./commands.ts";

const READY: CommandContext = {
  modelConfigured: true,
  currentModel: "deepseek-v4-pro",
  transactionId: "tx_one",
  verificationStatus: "PASSED",
  evidenceId: "evidence_one",
  acceptanceReady: true,
};

describe("focused command parsing", () => {
  test("maps plain input directly to ASK", () => {
    expect(parseCommandInput("解释这个项目", READY)).toEqual({
      kind: "worker",
      method: "command.ask",
      params: { query: "解释这个项目", model: "deepseek-v4-pro" },
    });
  });

  test("maps FIX without candidate-only escape hatches", () => {
    const outcome = parseCommandInput("/fix 修复解析器", READY);
    expect(outcome).toEqual({
      kind: "worker",
      method: "command.fix",
      params: { query: "修复解析器", model: "deepseek-v4-pro" },
    });
    expect(outcome.kind === "worker" && outcome.params.candidate_only).toBeUndefined();
    expect(() => parseCommandInput("/fix 修复解析器", {
      ...READY,
      acceptanceReady: false,
    })).toThrow("AcceptanceSpec");
  });

  test("keeps existing writes and authorized new paths distinct", () => {
    expect(
      parseCommandInput(
        "/fix --write src/parser.py --new src/generated.py -- 修复解析器",
        READY,
      ),
    ).toEqual({
      kind: "worker",
      method: "command.fix",
      params: {
        query: "修复解析器",
        model: "deepseek-v4-pro",
        write_scope: ["src/parser.py"],
        allowed_new_paths: ["src/generated.py"],
      },
    });
    expect(() =>
      parseFixArgument("--write src/parser.py --write src/parser.py -- 修复"),
    ).toThrow("范围路径重复");
    expect(() => parseFixArgument("--new src/generated.py 修复")).toThrow(
      "以 -- 结束",
    );
  });

  test("maps transaction commands and enforces VERIFIED Apply", () => {
    expect(parseCommandInput("/diff", READY)).toMatchObject({
      method: "command.diff",
      params: { transaction_id: "tx_one" },
    });
    expect(parseCommandInput("/verify tx_two", READY)).toMatchObject({
      method: "command.verify",
      params: { transaction_id: "tx_two" },
    });
    expect(parseCommandInput("/apply tx_one", READY)).toMatchObject({
      method: "command.apply",
      params: { transaction_id: "tx_one" },
    });
    expect(() => parseCommandInput("/apply tx_one", {
      ...READY,
      verificationStatus: "NOT_RUN",
    })).toThrow("VERIFIED");
    expect(() => parseCommandInput("/abort", READY)).toThrow("事务 ID");
  });

  test("rejects every removed Slash surface", () => {
    for (const removed of [
      "ask", "plan", "resume", "read", "files", "context", "modules",
      "trace", "sessions", "config", "theme", "history", "export",
    ]) {
      expect(() => parseCommandInput(`/${removed}`, READY)).toThrow("未知命令");
    }
    expect(commandNames()).toEqual([
      "help", "fix", "diff", "verify", "apply", "abort", "model",
    ]);
  });

  test("completes only models and transactions", () => {
    const sources = {
      models: ["deepseek-chat", "deepseek-reasoner"],
      transactions: ["tx_one", "tx_two"],
    };
    expect(commandArgumentCompletions("model", "reason", sources)).toEqual([
      "deepseek-reasoner",
    ]);
    expect(commandArgumentCompletions("apply", "two", sources)).toEqual([
      "tx_two",
    ]);
    expect(commandArgumentRequest("/model deep")).toEqual({
      commandName: "model",
      query: "deep",
    });
    expect(commandArgumentRequest("/fix repair")).toBeNull();
  });

  test("recognizes Slash and @file completion boundaries", () => {
    expect(slashQuery("/ver")).toBe("ver");
    expect(slashQuery("/verify tx")).toBeNull();
    expect(fileMentionQuery("explain @src/app")).toBe("src/app");
    expect(replaceFileMention("explain @app", "src/app.ts")).toBe(
      "explain @src/app.ts ",
    );
  });
});
