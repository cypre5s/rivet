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
  test("recognizes only the focused nine-tool surface", () => {
    const supportedTools = [
      "workspace_info",
      "context_search",
      "file_read",
      "file_write",
      "file_replace",
      "file_create",
      "file_delete",
      "process_run",
      "git_diff",
    ];

    for (const tool of supportedTools) {
      expect(
        presentTraceEvent(event("tool.completed", { tool_name: tool })).title,
      ).not.toBe(tool);
    }
    expect(
      presentTraceEvent(
        event("tool.completed", { tool_name: "search_text" }),
      ).title,
    ).toBe("search_text · 完成");
  });

  test("turns internal event names into restrained user language", () => {
    expect(presentTraceEvent(event("worker.ready")).title).toBe("就绪");
    expect(
      presentTraceEvent(event("tool.started", { tool: "context_search" })),
    ).toMatchObject({ title: "搜索代码 · 运行", status: "running" });
    expect(
      presentTraceEvent(
        event("module.activated", { module_id: "context.lexical" }),
      ).title,
    ).toBe("context.lexical · 激活");
    expect(
      presentTraceEvent(
        event("module.released", { module_id: "context.lexical" }),
      ).title,
    ).toBe("context.lexical · 释放");
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
      status: "success",
    });
    expect(
      presentTraceEvent(event("agent.answered", { status: "ANSWERED" })),
    ).toMatchObject({ title: "已回答", kind: "assistant" });
    expect(presentTraceEvent(event("acceptance.proposed"))).toMatchObject({
      title: "验收提案待确认",
      status: "success",
    });
    expect(
      presentTraceEvent(
        event("command.failed", {
          status: "FAILED",
          summary: "候选补丁为空，事务已回滚",
          suggested_action: "重新运行 /fix",
        }),
      ),
    ).toMatchObject({
      title: "命令失败",
      status: "failed",
      detail: "候选补丁为空，事务已回滚 · → 重新运行 /fix",
    });
    expect(
      presentTraceEvent(
        event("demand.created", { capability_id: "context.search.lexical" }),
      ).title,
    ).toBe("搜索代码");
  });

  test("keeps unknown events generic instead of exposing raw protocol names", () => {
    expect(presentTraceEvent(event("future.internal.event"))).toMatchObject({
      title: "状态已更新",
      kind: "status",
    });
  });

  test("shows activation failure reason and next action", () => {
    const presented = presentTraceEvent(
      event("module.activation_failed", {
        module_id: "context.lexical",
        human_message: "模块存在活动 Lease",
        suggested_action: "等待任务结束后重试",
      }),
    );

    expect(presented.title).toBe("能力激活失败");
    expect(presented.detail).toContain("模块存在活动 Lease");
    expect(presented.detail).toContain("等待任务结束后重试");
    expect(presented.status).toBe("failed");
  });
});
