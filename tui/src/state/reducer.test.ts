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

describe("Trace-driven reducer", () => {
  test("projects plan, context, diff, evidence, modules and budget", () => {
    const events = [
      event(0, "worker.ready", {
        repository: "/repo",
        branch: "main",
        model: "deepseek",
        credential_configured: true,
      }),
      event(1, "plan.updated", { phase: "VERIFY", summary: "执行验证" }),
      event(2, "context.selected", { path: "src/app.py", reason: "错误栈" }),
      event(3, "patch.updated", { diff: "@@ -1 +1 @@" }),
      event(4, "evidence.published", { evidence_id: "evidence_one" }),
      event(5, "module.activated", { module_id: "reader.pdf" }),
      event(6, "budget.updated", { tokens: 120, cost_usd: 0.02, elapsed_ms: 80 }),
    ];

    const state = events.reduce(reduceTraceEvent, initialRivetState());

    expect(state.connection).toBe("ready");
    expect(state.repository).toBe("/repo");
    expect(state.branch).toBe("main");
    expect(state.credentialConfigured).toBeTrue();
    expect(state.plan.phase).toBe("VERIFY");
    expect(state.context).toEqual([{ path: "src/app.py", reason: "错误栈" }]);
    expect(state.diff).toContain("@@");
    expect(state.evidenceId).toBe("evidence_one");
    expect(state.modules).toEqual(["reader.pdf"]);
    expect(state.budget.tokens).toBe(120);
    expect(state.timeline).toHaveLength(7);
    expect(state.timeline[0]?.title).toBe("Rivet 已就绪");
    expect(state.timeline[0]?.title).not.toContain("worker.ready");
  });

  test("opens and resolves permission modal from events", () => {
    const requested = reduceTraceEvent(
      initialRivetState(),
      event(0, "permission.requested", {
        request_id: "request_permission",
        permission: "EXECUTE",
        reason: "运行测试",
        argv: "pytest -q",
        cwd: ".",
        timeout_seconds: 60,
      }),
    );

    expect(requested.permission?.permission).toBe("EXECUTE");
    expect(requested.permission?.argv).toBe("pytest -q");

    const resolved = reduceTraceEvent(
      requested,
      event(1, "permission.resolved", { request_id: "request_permission" }),
    );
    expect(resolved.permission).toBeNull();
  });

  test("ignores duplicate or stale sequence and exposes worker recovery", () => {
    let state = reduceTraceEvent(
      initialRivetState(),
      event(2, "worker.crashed", { summary: "worker stopped" }),
    );
    state = reduceTraceEvent(state, event(2, "worker.ready", {}));
    expect(state.connection).toBe("crashed");
    expect(state.timeline).toHaveLength(1);

    state = reduceTraceEvent(state, event(3, "worker.recovered", {}));
    expect(state.connection).toBe("ready");
    expect(state.error).toBeNull();
  });

  test("transport status does not consume the worker trace sequence", () => {
    let state = reduceRivetState(initialRivetState(), {
      kind: "worker-status",
      state: "crashed",
      summary: "worker stopped",
    });
    state = reduceRivetState(state, {
      kind: "trace",
      event: event(0, "worker.ready", { repository: "/repo" }),
    });

    expect(state.connection).toBe("ready");
    expect(state.repository).toBe("/repo");
    expect(state.lastSequence).toBe(0);
  });

  test("merges duplicate ready events and tracks session snapshots", () => {
    let state = reduceTraceEvent(
      initialRivetState(),
      event(0, "worker.ready", { repository: "/repo" }),
    );
    state = reduceTraceEvent(
      state,
      event(1, "worker.ready", { repository: "/repo" }),
    );
    state = reduceTraceEvent(
      state,
      event(2, "session.updated", { session_id: "session_one" }),
    );
    state = reduceTraceEvent(
      state,
      event(3, "sessions.snapshot", { sessions: ["session_two"] }),
    );

    expect(state.timeline.filter((item) => item.eventType === "worker.ready")).toHaveLength(1);
    expect(state.sessionId).toBe("session_one");
    expect(state.sessions).toEqual(["session_one", "session_two"]);
  });
});
