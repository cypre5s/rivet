import { describe, expect, test } from "bun:test";

import {
  commandArgumentCompletions,
  commandArgumentRequest,
  fileMentionQuery,
  moduleCommandUnavailableReason,
  parseCommandInput,
  replaceFileMention,
  slashQuery,
} from "./commands.ts";

describe("TUI command input", () => {
  test("routes plain text to the read-only ask command", () => {
    expect(parseCommandInput("解释这个仓库")).toEqual({
      kind: "worker",
      method: "command.ask",
      params: {
        model: "deepseek-v4-pro",
        query: "解释这个仓库",
      },
    });
  });

  test("routes the six workbench commands without shell parsing", () => {
    expect(parseCommandInput("/ask 为什么失败")).toMatchObject({
      kind: "worker",
      method: "command.ask",
    });
    expect(parseCommandInput("/plan 修复边界")).toMatchObject({
      kind: "worker",
      method: "command.plan",
    });
    expect(parseCommandInput("/fix 修复边界")).toMatchObject({
      kind: "worker",
      method: "command.fix",
    });
    expect(parseCommandInput("/verify tx_one")).toEqual({
      kind: "worker",
      method: "command.verify",
      params: { transaction_id: "tx_one" },
    });
    expect(parseCommandInput("/diff tx_one")).toEqual({
      kind: "worker",
      method: "command.diff",
      params: { transaction_id: "tx_one" },
    });
    expect(parseCommandInput("/export evidence .rivet/exports/evidence.json")).toEqual({
      kind: "worker",
      method: "command.export",
      params: {
        export_kind: "evidence",
        output_path: ".rivet/exports/evidence.json",
      },
    });
    expect(parseCommandInput("/apply tx_one", {
      modelConfigured: true,
      currentModel: "deepseek-v4-pro",
      hasSession: true,
      transactionId: "tx_one",
      verificationStatus: "PASSED",
      evidenceId: "evidence_one",
      acceptanceReady: true,
    })).toMatchObject({ kind: "worker", method: "command.apply" });
  });

  test("routes bounded multi-format reader options to the worker", () => {
    expect(
      parseCommandInput(
        "/read samples/scan image.png --ocr --transcribe --frames 8 " +
          "--max-ocr-pages 12 --max-image-pixels 2000000 " +
          "--max-audio-duration 600 --max-output-chars 5000 --timeout 15",
      ),
    ).toEqual({
      kind: "worker",
      method: "command.read",
      params: {
        file: "samples/scan image.png",
        ocr: true,
        transcribe: true,
        frames: 8,
        max_ocr_pages: 12,
        max_image_pixels: 2_000_000,
        max_audio_duration: 600,
        max_output_chars: 5_000,
        timeout: 15,
      },
    });
    expect(
      commandArgumentRequest("/read samples/scan.png --ocr"),
    ).toBeNull();
  });

  test("rejects invalid reader enhancement options", () => {
    expect(() => parseCommandInput("/read image.png --frames 21")).toThrow(
      "--frames",
    );
    expect(() => parseCommandInput("/read image.png --unknown")).toThrow(
      "不支持参数",
    );
  });

  test("rejects incomplete or unknown commands", () => {
    expect(() => parseCommandInput("/fix")).toThrow("需要任务文本");
    expect(() => parseCommandInput("/apply")).toThrow("需要事务 ID");
    expect(() => parseCommandInput("/unknown value")).toThrow("未知命令");
    expect(() =>
      parseCommandInput("/modules enable context.syntax --cascade"),
    ).toThrow("不支持参数");
    expect(() =>
      parseCommandInput("/apply tx_other", {
        modelConfigured: true,
        currentModel: "deepseek-v4-pro",
        hasSession: true,
        transactionId: "tx_verified",
        verificationStatus: "PASSED",
        evidenceId: "evidence_one",
        acceptanceReady: true,
      }),
    ).toThrow("只有验证通过的事务可以应用");
    expect(
      parseCommandInput("/apply tx_historical", {
        modelConfigured: true,
        currentModel: "deepseek-v4-pro",
        hasSession: true,
        transactionId: "tx_current",
        verificationStatus: "FAILED",
        evidenceId: null,
        acceptanceReady: true,
        transactionStates: { tx_historical: "VERIFIED" },
      }),
    ).toEqual({
      kind: "worker",
      method: "command.apply",
      params: { transaction_id: "tx_historical" },
    });
  });

  test("detects slash and file mention triggers at the composer boundary", () => {
    expect(slashQuery("/ver")).toBe("ver");
    expect(slashQuery("text /ver")).toBeNull();
    expect(slashQuery("/verify tx_one")).toBeNull();
    expect(fileMentionQuery("请读取 @src/app")).toBe("src/app");
    expect(fileMentionQuery("邮件 a@example.com")).toBeNull();
    expect(replaceFileMention("请读取 @src/app", "src/app.ts")).toBe(
      "请读取 @src/app.ts ",
    );
  });

  test("completes command arguments from live sources", () => {
    const sources = {
      models: ["deepseek-v4-pro"],
      sessions: ["session_one"],
      transactions: ["tx_one"],
      modules: ["reader.pdf"],
      files: ["src/app.ts", "tests/app.test.ts"],
      contextFiles: ["src/app.ts"],
    };
    expect(commandArgumentCompletions("model", "v4", sources)).toEqual([
      "deepseek-v4-pro",
    ]);
    expect(commandArgumentCompletions("apply", "tx", sources)).toEqual([
      "tx_one",
    ]);
    expect(commandArgumentCompletions("read", "app", sources)).toEqual([
      "src/app.ts",
      "tests/app.test.ts",
    ]);
    expect(commandArgumentCompletions("modules", "reader", sources)).toEqual([
      "show reader.pdf",
      "enable reader.pdf",
      "disable reader.pdf",
    ]);
    expect(commandArgumentCompletions("context", "app", sources)).toEqual([
      "add src/app.ts",
      "add tests/app.test.ts",
      "remove src/app.ts",
    ]);
    expect(commandArgumentRequest("/mode f")).toEqual({
      commandName: "mode",
      query: "f",
    });
    expect(commandArgumentRequest("/ask explain this")).toBeNull();
  });

  test("keeps Kernel-managed capabilities out of policy mutations", () => {
    const status = {
      moduleId: "kernel.required",
      policy: "LOCKED",
      availability: "AVAILABLE",
      manifestDefaultEnabled: true,
      persistedOverride: null,
      configuredEnabled: true,
      effectiveEnabled: true,
      runtimeState: "ACTIVE",
      activation: "required",
      scope: "application",
      manualControl: true,
      sleepPolicy: "never",
      dependencies: [],
      dependents: [],
      providedCapabilities: ["kernel.required"],
      leaseCount: 0,
      activeResourceCount: 0,
      lastError: null,
    };
    const completions = commandArgumentCompletions("modules", "required", {
      models: [],
      sessions: [],
      transactions: [],
      modules: [],
      moduleStatuses: [status],
      files: [],
      contextFiles: [],
    });

    expect(completions).toEqual(["show kernel.required"]);
    expect(
      moduleCommandUnavailableReason("disable kernel.required", [status]),
    ).toContain("系统必需");
  });
});
