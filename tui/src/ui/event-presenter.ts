import type { IpcEvent, JsonValue } from "../contracts/ipc.ts";
import { compactIdentifier } from "./evidence-presentation.ts";

export type TimelineKind = "assistant" | "tool" | "status" | "error" | "user";
export type TimelineStatus = "running" | "success" | "failed" | "blocked" | "cancelled";

export interface PresentedEvent {
  title: string;
  detail: string;
  kind: TimelineKind;
  status: TimelineStatus;
}

const EVENT_TITLES: Record<string, string> = {
  "worker.ready": "就绪",
  "worker.recovered": "已恢复",
  "worker.stopping": "退出中",
  "module.activated": "能力激活",
  "module.slept": "能力休眠",
  "module.operation.requested": "能力变更请求",
  "module.operation.started": "能力变更中",
  "module.state.changed": "能力状态变更",
  "module.enablement.changed": "能力策略变更",
  "module.operation.completed": "能力变更完成",
  "module.operation.blocked": "能力变更阻塞",
  "module.operation.failed": "能力变更失败",
  "modules.snapshot": "能力刷新",
  "module.requested": "能力请求",
  "module.released": "能力释放",
  "module.activation_failed": "能力激活失败",
  "module.release_failed": "能力释放失败",
  "context.selected": "上下文选择",
  "workspace.tree_updated": "文件刷新",
  "tool.started": "工具运行中",
  "tool.completed": "工具完成",
  "tool.failed": "工具失败",
  "transaction.started": "事务创建",
  "patch.updated": "补丁更新",
  "verification.started": "验证中",
  "verification.completed": "验证完成",
  "evidence.published": "证据生成",
  "evidence.snapshot": "证据复核",
  "evidence.log": "日志加载",
  "candidate.ready": "候选补丁保存",
  "permission.requested": "等待确认",
  "permission.resolved": "权限处理",
  "agent.output.delta": "回复中",
  "agent.answered": "已回答",
  "agent.planned": "计划完成",
  "agent.patch_ready": "待验证",
  "agent.completed": "完成",
  "agent.cancelled": "已取消",
  "plan.updated": "阶段变更",
  "budget.updated": "用量更新",
  "reader.completed": "读取完成",
  "command.completed": "命令完成",
  "sessions.snapshot": "会话刷新",
  "transactions.snapshot": "事务刷新",
  "config.updated": "配置更新",
};

export function presentTraceEvent(event: IpcEvent): PresentedEvent {
  const payload = event.payload;
  const explicitSummary =
    (event.event_type === "agent.output.delta" ? text(payload.content) : "") ||
    text(payload.summary) ||
    text(payload.human_message);
  const suggestedAction = text(payload.suggested_action);
  const title = specializedTitle(event.event_type, payload) ?? EVENT_TITLES[event.event_type] ?? "状态已更新";
  const detailParts = [
    explicitSummary === event.event_type ? "" : explicitSummary,
    suggestedAction ? `→ ${suggestedAction}` : "",
  ].filter(Boolean);
  return {
    title,
    detail: detailParts.join(" · "),
    kind: eventKind(event.event_type),
    status: eventStatus(event.event_type, payload),
  };
}

function specializedTitle(
  eventType: string,
  payload: Record<string, JsonValue>,
): string | null {
  if (eventType === "module.activated") {
    const moduleId = text(payload.module_id);
    return moduleId ? `${moduleId} · 激活` : null;
  }
  if (eventType === "module.requested") {
    const moduleId = text(payload.module_id);
    return moduleId ? `${moduleId} · 请求` : null;
  }
  if (eventType === "module.released") {
    const moduleId = text(payload.module_id);
    return moduleId ? `${moduleId} · 释放` : null;
  }
  if (eventType === "module.slept") {
    const moduleId = text(payload.module_id);
    return moduleId ? `${moduleId} · 休眠` : null;
  }
  if (eventType.startsWith("module.operation.")) {
    const moduleId = text(payload.module_id);
    const operation = moduleOperation(text(payload.operation));
    if (!moduleId || !operation) return null;
    if (eventType.endsWith("requested")) return `${moduleId} · ${operation}请求`;
    if (eventType.endsWith("started")) return `${moduleId} · ${operation}中`;
    if (eventType.endsWith("completed")) return `${moduleId} · ${operation}完成`;
    if (eventType.endsWith("blocked")) return `${moduleId} · ${operation}阻塞`;
    if (eventType.endsWith("failed")) return `${moduleId} · ${operation}失败`;
  }
  if (eventType === "tool.started") {
    const tool = text(payload.tool) || text(payload.tool_name);
    return tool ? `${tool} · 运行` : null;
  }
  if (eventType === "tool.completed") {
    const tool = text(payload.tool) || text(payload.tool_name);
    return tool ? `${tool} · 完成` : null;
  }
  if (eventType === "context.selected") {
    const path = text(payload.path);
    return path ? `+ ${path}` : null;
  }
  if (eventType === "transaction.started") {
    const transactionId = text(payload.transaction_id);
    return transactionId
      ? `${compactIdentifier(transactionId, 22)} · 创建`
      : null;
  }
  if (eventType === "verification.completed") {
    const status = text(payload.status).toUpperCase();
    if (status === "PASSED") return "通过";
    if (status === "FAILED") return "失败";
    if (status === "BLOCKED") return "阻塞";
    if (status === "INCONCLUSIVE") return "不确定";
  }
  if (eventType === "agent.patch_ready") {
    return "待验证";
  }
  if (eventType === "reader.completed") {
    const status = text(payload.status).toUpperCase();
    if (status === "DEGRADED") return "读取降级";
    if (status === "TRUNCATED") return "读取截断";
    if (status === "FAILED") return "读取失败";
  }
  return null;
}

function eventKind(eventType: string): TimelineKind {
  if (
    eventType === "agent.output.delta" ||
    eventType === "agent.completed" ||
    eventType === "agent.answered" ||
    eventType === "agent.planned"
  )
    return "assistant";
  if (eventType.startsWith("tool.")) return eventType.endsWith("failed") ? "error" : "tool";
  if (eventType.includes("failed") || eventType.includes("error")) return "error";
  return "status";
}

function eventStatus(
  eventType: string,
  payload: Record<string, JsonValue>,
): TimelineStatus {
  const status = text(payload.status).toUpperCase();
  if (eventType === "agent.output.delta") return "running";
  if (status === "READY_FOR_VERIFICATION") return "running";
  if (eventType.endsWith("started") || status === "RUNNING") return "running";
  if (eventType.includes("cancel") || status === "CANCELLED") return "cancelled";
  if (status === "BLOCKED" || status === "INCONCLUSIVE") return "blocked";
  if (status === "DEGRADED") return "blocked";
  if (eventType.endsWith("blocked")) return "blocked";
  if (eventType.includes("failed") || status === "FAILED") return "failed";
  return "success";
}

function moduleOperation(operation: string): string {
  const labels: Record<string, string> = {
    enable: "启用",
    disable: "禁用",
    wake: "唤醒",
    sleep: "休眠",
  };
  return labels[operation] ?? "";
}

function text(value: JsonValue | undefined): string {
  return typeof value === "string" ? value : "";
}
