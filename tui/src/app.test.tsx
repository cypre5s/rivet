import { describe, expect, test } from "bun:test";
import { testRender } from "@opentui/react/test-utils";
import { act } from "react";

import { RivetApp } from "./app.tsx";
import type { IpcRequest } from "./contracts/ipc.ts";
import { WorkerClient, type WorkerTransport } from "./ipc/client.ts";
import {
  initialRivetState,
  type EvidenceSummary,
  type RivetState,
} from "./state/reducer.ts";

class CaptureTransport implements WorkerTransport {
  readonly writes: string[] = [];
  private readonly stdoutListeners = new Set<(chunk: string) => void>();
  private readonly stderrListeners = new Set<(chunk: string) => void>();
  private readonly exitListeners = new Set<(exitCode: number | null) => void>();

  write(line: string): void {
    this.writes.push(line);
    const request = JSON.parse(line) as IpcRequest;
    queueMicrotask(() => {
      const result =
        request.method === "workspace.files"
          ? { paths: ["src/service.py", "src/app.py"] }
          : request.method === "transactions.list"
            ? {
                transactions: [
                  { transaction_id: "tx_one", state: "VERIFIED" },
                ],
              }
            : { status: "ready" };
      this.emitRaw({
        schema_version: 1,
        message_type: "response",
        protocol_version: 1,
        request_id: request.request_id,
        ok: true,
        result,
        error: null,
      });
      if (request.method === "command.diff") {
        this.emitEvent("patch.updated", {
          transaction_id: request.params.transaction_id,
          diff: [
            "diff --git a/a.py b/a.py",
            "--- a/a.py",
            "+++ b/a.py",
            "@@ -1 +1 @@",
            "-print('old')",
            "+print('fixed')",
          ].join("\n"),
        });
      }
    });
  }

  onStdout(listener: (chunk: string) => void): () => void {
    this.stdoutListeners.add(listener);
    return () => this.stdoutListeners.delete(listener);
  }

  onStderr(listener: (chunk: string) => void): () => void {
    this.stderrListeners.add(listener);
    return () => this.stderrListeners.delete(listener);
  }

  onExit(listener: (exitCode: number | null) => void): () => void {
    this.exitListeners.add(listener);
    return () => this.exitListeners.delete(listener);
  }

  close(): void {}

  emitEvent(eventType: string, payload: Record<string, unknown>): void {
    this.emitRaw({
      schema_version: 1,
      message_type: "event",
      protocol_version: 1,
      event_id: `event_${this.writes.length}_${eventType.replaceAll(".", "_")}`,
      event_type: eventType,
      sequence: this.writes.length,
      payload,
    });
  }

  private emitRaw(value: object): void {
    const line = `${JSON.stringify(value)}\n`;
    for (const listener of this.stdoutListeners) listener(line);
  }
}

function requests(transport: CaptureTransport): IpcRequest[] {
  return transport.writes.map((line) => JSON.parse(line) as IpcRequest);
}

function readyState(overrides: Partial<RivetState> = {}): RivetState {
  return {
    ...initialRivetState(),
    connection: "ready",
    repository: "/home/tester/rivet",
    branch: "main",
    model: "deepseek-v4-pro",
    models: ["deepseek-v4-pro", "deepseek-v4-flash"],
    credentialConfigured: true,
    acceptanceReady: true,
    transaction: "tx_one",
    transactions: [
      {
        transactionId: "tx_one",
        state: "VERIFIED",
        evidenceId: "evidence_one",
        patchId: "patch_one",
        patchSha256: "p".repeat(64),
        updatedAt: "2026-09-02T00:00:00Z",
        applyEligible: true,
      },
    ],
    verifyStatus: "PASSED",
    evidenceId: "evidence_one",
    evidence: evidenceState(),
    ...overrides,
  };
}

function evidenceState(): EvidenceSummary {
  return {
    transactionId: "tx_one",
    state: "VERIFIED",
    verdictStatus: "PASSED",
    passed: true,
    applyEligible: true,
    evidenceVerified: true,
    evidenceId: "evidence_one",
    patchId: "patch_one",
    baseCommit: "b".repeat(40),
    acceptanceSha256: "a".repeat(64),
    patchSha256: "p".repeat(64),
    manifestSha256: "m".repeat(64),
    changedFiles: ["src/app.py"],
    verificationResults: [
      {
        stepId: "behavior",
        kind: "Behavior",
        name: "Behavior",
        status: "PASSED",
        required: true,
        argv: ["pytest", "-q"],
        durationMs: 10,
        exitCode: 0,
        logPath: "behavior.log",
        logSha256: "b".repeat(64),
        stdoutSummary: "1 passed",
        stderrSummary: "",
      },
    ],
    files: [],
    updatedAt: "2026-09-02T00:00:00Z",
    decidedAt: "2026-09-02T00:00:00Z",
    nextAction: "审查后显式 Apply",
  };
}

describe("focused Rivet TUI", () => {
  test("renders the minimal welcome and exact Slash menu", async () => {
    const setup = await testRender(
      <RivetApp
        initialState={readyState({ transaction: "无", transactions: [] })}
        noColor={true}
      />,
      { width: 80, height: 24 },
    );
    await act(async () => setup.renderOnce());
    expect(setup.captureCharFrame()).toContain("/fix");
    await act(async () => setup.mockInput.typeText("/"));
    await setup.flush();
    const frame = setup.captureCharFrame();
    for (const name of ["help", "fix", "diff", "verify", "apply", "abort", "model"]) {
      expect(frame).toContain(`/${name}`);
    }
    for (const removed of ["plan", "sessions", "modules", "trace", "config", "theme"]) {
      expect(frame).not.toContain(`/${removed}`);
    }
    await act(async () => setup.renderer.destroy());
  });

  test("sends ordinary input as ASK with the selected model", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} client={client} />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.mockInput.typeText("解释这个仓库"));
    await act(async () => setup.mockInput.pressEnter());
    await act(async () => Bun.sleep(20));
    const ask = requests(transport).find((request) => request.method === "command.ask");
    expect(ask?.params).toEqual({
      query: "解释这个仓库",
      model: "deepseek-v4-pro",
    });
    expect(requests(transport).some((request) => request.method === "command.plan")).toBeFalse();
    await act(async () => setup.renderer.destroy());
  });

  test("selects a Worker-advertised model without a config surface", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp
        initialState={readyState({
          model: "team-chat",
          models: ["team-chat", "team-reasoner"],
        })}
        noColor={true}
        client={client}
      />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.mockInput.pressKey("k", { ctrl: true }));
    await act(async () => setup.mockInput.pressArrow("down"));
    await act(async () => setup.mockInput.pressEnter());
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("team-reasoner ▾");
    expect(setup.captureCharFrame()).not.toContain("配置模型与预算");
    await act(async () => setup.renderer.destroy());
  });

  test("rejects FIX without AcceptanceSpec and never sends candidate_only", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp
        initialState={readyState({ acceptanceReady: false })}
        noColor={true}
        client={client}
      />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.mockInput.typeText("/fix 修复解析器"));
    await act(async () => setup.mockInput.pressEnter());
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("AcceptanceSpec");
    expect(requests(transport).some((request) => request.method === "command.fix")).toBeFalse();

    await act(async () => setup.renderer.destroy());

    const readyTransport = new CaptureTransport();
    const readyClient = new WorkerClient(readyTransport, { requireHandshake: false });
    const readySetup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} client={readyClient} />,
      { width: 100, height: 28 },
    );
    await act(async () => readySetup.renderOnce());
    await act(async () =>
      readySetup.mockInput.typeText(
        "/fix --write src/app.py --new src/generated.py -- 修复解析器",
      ),
    );
    await act(async () => readySetup.mockInput.pressEnter());
    await act(async () => Bun.sleep(20));
    const fix = requests(readyTransport).find((request) => request.method === "command.fix");
    expect(fix?.params.candidate_only).toBeUndefined();
    expect(fix?.params.write_scope).toEqual(["src/app.py"]);
    expect(fix?.params.allowed_new_paths).toEqual(["src/generated.py"]);
    await act(async () => readySetup.renderer.destroy());
  });

  test("uses workspace.files only for @ picker and forwards selected paths", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} client={client} />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());
    expect(requests(transport).some((request) => request.method === "workspace.files")).toBeFalse();
    await act(async () => setup.mockInput.typeText("解释 @service"));
    await act(async () => Bun.sleep(160));
    await setup.flush();
    expect(requests(transport).some((request) => request.method === "workspace.files")).toBeTrue();
    expect(setup.captureCharFrame()).toContain("src/service.py");
    await act(async () => setup.mockInput.pressEnter());
    await setup.flush();
    await act(async () => setup.mockInput.pressEnter());
    await act(async () => Bun.sleep(20));
    const ask = requests(transport).find((request) => request.method === "command.ask");
    expect(ask?.params.context_paths).toEqual(["src/service.py"]);
    await act(async () => setup.renderer.destroy());
  });

  test("never promotes selected read-only context into FIX write scope", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} client={client} />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());
    await act(async () =>
      setup.mockInput.typeText(
        "/fix --write src/app.py --new src/generated.py -- 修复 @service",
      ),
    );
    await act(async () => Bun.sleep(160));
    await setup.flush();
    await act(async () => setup.mockInput.pressEnter());
    await setup.flush();
    await act(async () => setup.mockInput.pressEnter());
    await act(async () => Bun.sleep(20));
    const fix = requests(transport).find((request) => request.method === "command.fix");
    expect(fix?.params.context_paths).toEqual(["src/service.py"]);
    expect(fix?.params.write_scope).toEqual(["src/app.py"]);
    expect(fix?.params.allowed_new_paths).toEqual(["src/generated.py"]);
    expect(fix?.params.write_scope).not.toContain("src/service.py");
    await act(async () => setup.renderer.destroy());
  });

  test("keeps Diff, Verify and Evidence reachable with on-demand IPC", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} client={client} />,
      { width: 120, height: 30 },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.mockInput.typeText("/diff"));
    await act(async () => setup.mockInput.pressEnter());
    await act(async () => Bun.sleep(40));
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("D Diff");
    expect(setup.captureCharFrame()).toContain("print('fixed')");
    expect(requests(transport).some((request) => request.method === "transactions.list")).toBeTrue();

    await act(async () => setup.mockInput.pressKey("v"));
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("结论 通过");
    await act(async () => setup.mockInput.pressKey("e"));
    await act(async () => Bun.sleep(20));
    await setup.flush();
    expect(requests(transport).some((request) => request.method === "evidence.get")).toBeTrue();
    expect(setup.captureCharFrame()).toContain("Evidence");
    await act(async () => setup.renderer.destroy());
  });

  test("shows every frozen permission field in a compact layout", async () => {
    const setup = await testRender(
      <RivetApp
        initialState={readyState({
          permission: {
            requestId: "permission_one",
            permission: "EXECUTE",
            reason: "确认真实提案",
            goal: "修复解析器边界",
            readScope: ["src/parser.py", "src/context.py"],
            writeScope: ["src/parser.py"],
            allowedNewPaths: ["src/generated.py"],
            forbiddenPaths: ["tests/test_parser.py"],
            expectedBehaviors: ["拒绝负数端口"],
            preservedBehaviors: ["正常端口仍可解析"],
            acceptanceCommands: [["pytest", "tests/test_parser.py", "-q"]],
            regressionCommands: [["pytest", "-q"]],
            budgets: {
              maxWallSeconds: 900,
              maxTokens: 8192,
              maxToolCalls: 64,
              maxCostUsd: "1.25",
            },
            investigation: "负数端口缺少拒绝分支",
            proposalRunId: "run_proposal_one",
            acceptanceSha256: `sha256:${"a".repeat(64)}`,
            baseCommit: "b".repeat(40),
            argv: "rivet fix --allow-write src/parser.py --yes --acceptance-sha256 sha256:aaaaaaaa --base-commit bbbbbbbb",
            cwd: "批准后创建独立 Git Worktree",
            paths: "src/parser.py",
            network: "按 Guard 策略执行",
            timeoutSeconds: 900,
          },
        })}
        noColor={true}
      />,
      { width: 80, height: 22 },
    );
    await act(async () => setup.renderOnce());
    const frame = setup.captureCharFrame();
    expect(frame).toContain("修复解析器边界");
    expect(frame).toContain("src/parser.py");
    expect(frame).toContain("src/context.py");
    expect(frame).toContain("src/generated.py");
    expect(frame).toContain("拒绝负数端口");
    expect(frame).toContain("负数端口缺少拒绝分支");
    expect(frame).toContain("tests/test_parser.py");
    expect(frame).toContain("64 tools");
    expect(frame).toContain("aaaaaaaa");
    expect(frame).toContain("bbbbbbbb");
    expect(frame).toContain("A 允许 · D 拒绝");
    await act(async () => setup.renderer.destroy());
  });

  test("requires explicit confirmation before Apply", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} client={client} />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.mockInput.typeText("/apply tx_one"));
    await act(async () => setup.mockInput.pressEnter());
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("Y 确认 · N 取消");
    expect(requests(transport).some((request) => request.method === "command.apply")).toBeFalse();
    await act(async () => setup.mockInput.pressKey("y"));
    await act(async () => Bun.sleep(20));
    expect(requests(transport).some((request) => request.method === "command.apply")).toBeTrue();
    await act(async () => setup.renderer.destroy());
  });
});
