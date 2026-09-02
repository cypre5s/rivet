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
  "demand.created": "能力需求",
  "capability.requested": "能力请求",
  "module.requested": "能力请求",
  "module.released": "能力释放",
  "module.activation_failed": "能力激活失败",
  "module.release_failed": "能力释放失败",
  "context.selected": "上下文选择",
  "context.degraded": "上下文降级",
  "operation.executed": "能力操作完成",
  "workspace.tree_updated": "文件刷新",
  "tool.started": "工具运行中",
  "tool.completed": "工具完成",
  "tool.failed": "工具失败",
  "transaction.started": "事务创建",
  "patch.updated": "补丁更新",
  "verification.started": "验证中",
  "verification.completed": "验证完成",
  "acceptance.proposed": "验收提案待确认",
  "evidence.published": "证据生成",
  "evidence.snapshot": "证据复核",
  "evidence.log": "日志加载",
  "permission.requested": "等待确认",
  "permission.resolved": "权限处理",
  "agent.output.delta": "回复中",
  "agent.answered": "已回答",
  "agent.patch_ready": "待验证",
  "agent.completed": "完成",
  "agent.cancelled": "已取消",
  "budget.updated": "用量更新",
  "command.completed": "命令完成",
  "transactions.snapshot": "事务刷新",
};

export function presentTraceEvent(event: IpcEvent): PresentedEvent {
  const payload = event.payload;
  const explicitSummary =
    (event.event_type === "agent.output.delta" ? text(payload.content) : "") ||
    text(payload.summary) ||
    text(payload.human_message) ||
    text(payload.reason);
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
  if (eventType === "demand.created") {
    return demandTitle(text(payload.capability_id));
  }
  if (eventType === "capability.requested") {
    const capabilityId = text(payload.capability_id);
    return capabilityId ? `${capabilityId} · 请求` : null;
  }
  if (eventType === "module.activated") {
    const moduleId = text(payload.module_id);
    return moduleId ? `${moduleId} · 激活` : null;
  }
  if (eventType === "module.requested") {
    const moduleId = text(payload.module_id);
    return moduleId ? `${moduleId} · 请求` : null;
  }
  if (eventType === "operation.executed") {
    const tool = text(payload.tool_name);
    return tool ? `${toolLabel(tool)} · 完成` : null;
  }
  if (eventType === "module.released") {
    const moduleId = text(payload.module_id);
    return moduleId ? `${moduleId} · 释放` : null;
  }
  if (eventType === "module.slept") {
    const moduleId = text(payload.module_id);
    return moduleId ? `${moduleId} · 休眠` : null;
  }
  if (eventType === "tool.started") {
    const tool = text(payload.tool) || text(payload.tool_name);
    return tool ? `${toolLabel(tool)} · 运行` : null;
  }
  if (eventType === "tool.completed") {
    const tool = text(payload.tool) || text(payload.tool_name);
    return tool ? `${toolLabel(tool)} · 完成` : null;
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
  return null;
}

function demandTitle(capabilityId: string): string | null {
  if (capabilityId === "provider.chat.completions") return "模型推理";
  if (capabilityId === "context.search.lexical") return "搜索代码";
  if (capabilityId === "guard.local_execution") return "执行本地工具";
  if (capabilityId === "transaction.worktree") return "建立隔离事务";
  if (capabilityId === "verify.deterministic") return "独立验证";
  return capabilityId ? `${capabilityId} · 需求` : null;
}

function toolLabel(tool: string): string {
  const labels: Record<string, string> = {
    context_search: "搜索代码",
    workspace_info: "查看工作区",
    file_read: "读取文件",
    file_write: "写入文件",
    file_replace: "修改代码",
    file_create: "创建文件",
    file_delete: "删除文件",
    git_diff: "查看变更",
    process_run: "运行命令",
  };
  return labels[tool] ?? tool;
}

function eventKind(eventType: string): TimelineKind {
  if (
    eventType === "agent.output.delta" ||
    eventType === "agent.completed" ||
    eventType === "agent.answered"
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

function text(value: JsonValue | undefined): string {
  return typeof value === "string" ? value : "";
}
