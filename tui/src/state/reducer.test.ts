import { describe, expect, test } from "bun:test";

import type { IpcEvent, JsonValue } from "../contracts/ipc.ts";
import {
  initialRivetState,
  reduceRivetState,
  reduceTraceEvent,
} from "./reducer.ts";

function event(
  sequence: number,
  eventType: string,
  payload: Record<string, JsonValue> = {},
): IpcEvent {
  return {
    schema_version: 1,
    message_type: "event",
    protocol_version: 1,
    event_id: `event_reducer_${sequence}`,
    event_type: eventType,
    sequence,
    payload,
  };
}

describe("focused Trace reducer", () => {
  test("projects only live model, transaction, patch and evidence state", () => {
    const events = [
      event(0, "worker.ready", {
        repository: "/repo",
        branch: "main",
        model: "reasoner-large",
        models: ["chat-fast", "reasoner-large"],
        credential_configured: true,
        acceptance_ready: true,
      }),
      event(1, "transaction.started", { transaction_id: "tx_one" }),
      event(2, "patch.updated", { diff: "@@ -1 +1 @@" }),
      event(3, "verification.completed", { status: "PASSED" }),
      event(4, "evidence.published", {
        transaction_id: "tx_one",
        evidence_id: "evidence_one",
        base_commit: "b".repeat(40),
        acceptance_sha256: "a".repeat(64),
        patch_sha256: "p".repeat(64),
        manifest_sha256: "m".repeat(64),
        changed_files: ["calculator.py"],
        verification_results: [{ kind: "Behavior", status: "PASSED" }],
      }),
    ];
    const state = events.reduce(reduceTraceEvent, initialRivetState());

    expect(state).toMatchObject({
      connection: "ready",
      repository: "/repo",
      branch: "main",
      model: "reasoner-large",
      models: ["chat-fast", "reasoner-large"],
      credentialConfigured: true,
      acceptanceReady: true,
      transaction: "tx_one",
      verifyStatus: "PASSED",
      evidenceId: "evidence_one",
    });
    expect(state.diff).toContain("@@");
    expect(state.evidence?.changedFiles).toEqual(["calculator.py"]);
    expect(state.evidence?.baseCommit).toBe("b".repeat(40));
    expect(state.evidence?.verificationResults).toMatchObject([
      { kind: "Behavior", status: "PASSED" },
    ]);
  });

  test("preserves Demand causality fields for grouped timeline audit", () => {
    const state = reduceTraceEvent(
      initialRivetState(),
      event(0, "demand.created", {
        demand_id: "demand_one",
        operation_id: "operation_one",
        parent_event_id: "event_parent",
        parent_demand_id: "demand_parent",
        capability_id: "context.search.lexical",
      }),
    );
    expect(state.timeline[0]).toMatchObject({
      demandId: "demand_one",
      operationId: "operation_one",
      parentEventId: "event_parent",
      parentDemandId: "demand_parent",
      title: "搜索代码",
    });
  });

  test("updates one assistant item from cumulative stream snapshots", () => {
    let state = reduceTraceEvent(
      initialRivetState(),
      event(0, "agent.output.delta", {
        response_id: "response_one",
        content: "正在分析",
      }),
    );
    state = reduceTraceEvent(
      state,
      event(1, "agent.output.delta", {
        response_id: "response_one",
        content: "正在分析仓库。",
      }),
    );
    expect(state.timeline).toHaveLength(1);
    expect(state.timeline[0]).toMatchObject({
      kind: "assistant",
      status: "running",
      detail: "正在分析仓库。",
    });

    state = reduceTraceEvent(
      state,
      event(2, "agent.answered", {
        response_id: "response_one",
        status: "ANSWERED",
        summary: "结论如下。",
      }),
    );
    expect(state.timeline).toHaveLength(1);
    expect(state.timeline[0]).toMatchObject({ status: "success", detail: "结论如下。" });
  });

  test("turns unfinished streaming rows into an explicit terminal failure", () => {
    let state = reduceTraceEvent(
      initialRivetState(),
      event(0, "agent.output.delta", {
        response_id: "response_failed",
        content: "正在生成候选补丁",
      }),
    );
    state = reduceTraceEvent(
      state,
      event(1, "command.failed", {
        status: "FAILED",
        summary: "候选补丁生成失败，事务已回滚",
        suggested_action: "重新运行 /fix",
      }),
    );

    expect(state.error).toBe("候选补丁生成失败，事务已回滚");
    expect(state.timeline[0]).toMatchObject({
      eventType: "agent.output.delta",
      status: "failed",
    });
    expect(state.timeline[1]).toMatchObject({
      eventType: "command.failed",
      title: "命令失败",
      status: "failed",
    });
  });

  test("opens and resolves permission prompts", () => {
    const requested = reduceTraceEvent(
      initialRivetState(),
      event(0, "permission.requested", {
        request_id: "request_permission",
        permission: "EXECUTE",
        reason: "确认真实提案",
        goal: "修复解析器",
        read_scope: ["src/parser.py", "src/context.py"],
        write_scope: ["src/parser.py"],
        allowed_new_paths: ["src/generated.py"],
        forbidden_paths: ["tests/test_parser.py"],
        expected_behaviors: ["拒绝负数端口"],
        preserved_behaviors: ["正常端口仍可解析"],
        acceptance_commands: [["pytest", "tests/test_parser.py", "-q"]],
        regression_commands: [["pytest", "-q"]],
        budgets: {
          max_wall_seconds: 900,
          max_tokens: 8192,
          max_tool_calls: 64,
          max_cost_usd: "1.25",
        },
        investigation: "负数分支缺失",
        proposal_run_id: "run_proposal_one",
        acceptance_sha256: `sha256:${"a".repeat(64)}`,
        base_commit: "b".repeat(40),
        argv: "rivet fix --allow-write src/parser.py --yes --acceptance-sha256 sha256:aaaaaaaa --base-commit bbbbbbbb",
        cwd: "批准后创建独立 Git Worktree",
        timeout_seconds: 900,
      }),
    );
    expect(requested.permission).toMatchObject({
      goal: "修复解析器",
      readScope: ["src/parser.py", "src/context.py"],
      writeScope: ["src/parser.py"],
      allowedNewPaths: ["src/generated.py"],
      expectedBehaviors: ["拒绝负数端口"],
      acceptanceCommands: [["pytest", "tests/test_parser.py", "-q"]],
      regressionCommands: [["pytest", "-q"]],
    });
    const resolved = reduceTraceEvent(
      requested,
      event(1, "permission.resolved", { request_id: "request_permission" }),
    );
    expect(resolved.permission).toBeNull();
  });

  test("tracks validated historical transaction snapshots", () => {
    const state = reduceTraceEvent(
      initialRivetState(),
      event(0, "transactions.snapshot", {
        transactions: [
          {
            transaction_id: "tx_verified",
            state: "VERIFIED",
            evidence_id: "evidence_verified",
          },
        ],
      }),
    );
    expect(state.transactions).toMatchObject([
      {
        transactionId: "tx_verified",
        state: "VERIFIED",
        evidenceId: "evidence_verified",
        applyEligible: true,
      },
    ]);
  });

  test("ignores stale sequence and keeps transport status separate", () => {
    let state = reduceTraceEvent(
      initialRivetState(),
      event(2, "worker.crashed", { summary: "worker stopped" }),
    );
    state = reduceTraceEvent(state, event(2, "worker.ready", {}));
    expect(state.connection).toBe("crashed");

    state = reduceRivetState(initialRivetState(), {
      kind: "worker-status",
      state: "crashed",
      summary: "worker stopped",
    });
    state = reduceRivetState(state, {
      kind: "trace",
      event: event(0, "worker.ready", { repository: "/repo" }),
    });
    expect(state.connection).toBe("ready");
    expect(state.lastSequence).toBe(0);
  });
});
