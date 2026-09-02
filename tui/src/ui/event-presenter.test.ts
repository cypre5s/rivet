import { describe, expect, test } from "bun:test";

import type { IpcEvent } from "../contracts/ipc.ts";
import { presentTraceEvent } from "./event-presenter.ts";

function event(eventType: string, payload: IpcEvent["payload"] = {}): IpcEvent {
  return {
    schema_version: 1,
    message_type: "event",
    protocol_version: 1,
    event_id: "event_presenter",
    event_type: eventType,
    sequence: 0,
    payload,
  };
}

describe("trace event presentation", () => {
  test("turns internal event names into restrained user language", () => {
    expect(presentTraceEvent(event("worker.ready")).title).toBe("就绪");
    expect(
      presentTraceEvent(event("tool.started", { tool: "search_text" })),
    ).toMatchObject({ title: "search_text · 运行", status: "running" });
    expect(
      presentTraceEvent(
        event("module.activated", { module_id: "context.syntax" }),
      ).title,
    ).toBe("context.syntax · 激活");
    expect(
      presentTraceEvent(
        event("module.released", { module_id: "context.syntax" }),
      ).title,
    ).toBe("context.syntax · 释放");
    expect(
      presentTraceEvent(
        event("verification.completed", { status: "PASSED" }),
      ),
    ).toMatchObject({ title: "通过", status: "success" });
    expect(
      presentTraceEvent(
        event("agent.patch_ready", { status: "READY_FOR_VERIFICATION" }),
      ),
    ).toMatchObject({
      title: "待验证",
      status: "running",
    });
    expect(
      presentTraceEvent(event("agent.answered", { status: "ANSWERED" })),
    ).toMatchObject({ title: "已回答", kind: "assistant" });
    expect(presentTraceEvent(event("config.updated"))).toMatchObject({
      title: "配置更新",
      kind: "status",
      status: "success",
    });
  });

  test("keeps unknown events generic instead of exposing raw protocol names", () => {
    expect(presentTraceEvent(event("future.internal.event"))).toMatchObject({
      title: "状态已更新",
      kind: "status",
    });
  });

  test("shows module failure reason and next action", () => {
    const presented = presentTraceEvent(
      event("module.operation.blocked", {
        module_id: "context.syntax",
        operation: "sleep",
        human_message: "模块存在活动 Lease",
        suggested_action: "等待任务结束后重试",
      }),
    );

    expect(presented.title).toContain("context.syntax");
    expect(presented.detail).toContain("模块存在活动 Lease");
    expect(presented.detail).toContain("等待任务结束后重试");
    expect(presented.status).toBe("blocked");
  });
});
