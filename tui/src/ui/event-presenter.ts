import type { IpcEvent, JsonValue } from "../contracts/ipc.ts";

export type TimelineKind = "assistant" | "tool" | "status" | "error" | "user";
export type TimelineStatus = "running" | "success" | "failed" | "blocked" | "cancelled";

export interface PresentedEvent {
  title: string;
  detail: string;
  kind: TimelineKind;
  status: TimelineStatus;
}

const EVENT_TITLES: Record<string, string> = {
  "worker.ready": "Rivet 已就绪",
  "worker.recovered": "Rivet 已恢复连接",
  "worker.stopping": "正在安全退出",
  "module.activated": "已启用按需模块",
  "module.slept": "按需模块已休眠",
  "module.operation.requested": "模块操作已请求",
  "module.operation.started": "正在执行模块操作",
  "module.state.changed": "模块运行状态已更新",
  "module.enablement.changed": "模块启用策略已更新",
  "module.operation.completed": "模块操作已完成",
  "module.operation.blocked": "模块操作被安全边界阻止",
  "module.operation.failed": "模块操作失败",
  "modules.snapshot": "模块状态已刷新",
  "context.selected": "已选择相关上下文",
  "workspace.tree_updated": "仓库文件清单已更新",
  "tool.started": "正在执行工具",
  "tool.completed": "工具执行完成",
  "tool.failed": "工具执行失败",
  "transaction.started": "已创建隔离修改事务",
  "patch.updated": "隔离补丁已更新",
  "verification.started": "正在执行验证矩阵",
  "verification.completed": "验证已完成",
  "evidence.published": "验证证据已发布",
  "permission.requested": "需要确认受限操作",
  "permission.resolved": "权限请求已处理",
  "agent.answered": "回复已生成",
  "agent.planned": "计划已生成",
  "agent.patch_ready": "补丁生成完成，等待独立验证",
  "agent.completed": "Rivet 已完成回复",
  "agent.cancelled": "当前任务已取消",
  "plan.updated": "任务阶段已更新",
  "budget.updated": "本次用量已更新",
  "reader.completed": "文件读取完成",
  "command.completed": "命令执行完成",
  "sessions.snapshot": "近期会话已更新",
};

export function presentTraceEvent(event: IpcEvent): PresentedEvent {
  const payload = event.payload;
  const explicitSummary = text(payload.summary) || text(payload.human_message);
  const suggestedAction = text(payload.suggested_action);
  const title = specializedTitle(event.event_type, payload) ?? EVENT_TITLES[event.event_type] ?? "运行状态已更新";
  const detailParts = [
    explicitSummary === event.event_type ? "" : explicitSummary,
    suggestedAction ? `建议：${suggestedAction}` : "",
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
    return moduleId ? `已启用 ${moduleId}` : null;
  }
  if (eventType === "module.slept") {
    const moduleId = text(payload.module_id);
    return moduleId ? `${moduleId} 已休眠` : null;
  }
  if (eventType.startsWith("module.operation.")) {
    const moduleId = text(payload.module_id);
    const operation = moduleOperation(text(payload.operation));
    if (!moduleId || !operation) return null;
    if (eventType.endsWith("requested")) return `已请求${operation} ${moduleId}`;
    if (eventType.endsWith("started")) return `正在${operation} ${moduleId}`;
    if (eventType.endsWith("completed")) return `${moduleId} ${operation}完成`;
    if (eventType.endsWith("blocked")) return `${moduleId} ${operation}被阻止`;
    if (eventType.endsWith("failed")) return `${moduleId} ${operation}失败`;
  }
  if (eventType === "tool.started") {
    const tool = text(payload.tool) || text(payload.tool_name);
    return tool ? `正在执行 ${tool}` : null;
  }
  if (eventType === "tool.completed") {
    const tool = text(payload.tool) || text(payload.tool_name);
    return tool ? `${tool} 执行完成` : null;
  }
  if (eventType === "context.selected") {
    const path = text(payload.path);
    return path ? `已选择 ${path}` : null;
  }
  if (eventType === "transaction.started") {
    const transactionId = text(payload.transaction_id);
    return transactionId ? `已创建隔离事务 ${transactionId}` : null;
  }
  if (eventType === "verification.completed") {
    const status = text(payload.status).toUpperCase();
    if (status === "PASSED") return "验证通过";
    if (status === "FAILED") return "验证未通过";
    if (status === "BLOCKED") return "验证被阻塞";
    if (status === "INCONCLUSIVE") return "验证结果不确定";
  }
  if (eventType === "agent.patch_ready") {
    return "补丁生成完成，等待独立验证";
  }
  return null;
}

function eventKind(eventType: string): TimelineKind {
  if (
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
  if (status === "READY_FOR_VERIFICATION") return "running";
  if (eventType.endsWith("started") || status === "RUNNING") return "running";
  if (eventType.includes("cancel") || status === "CANCELLED") return "cancelled";
  if (status === "BLOCKED" || status === "INCONCLUSIVE") return "blocked";
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
