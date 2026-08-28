import type { IpcEvent, JsonValue } from "../contracts/ipc.ts";

export type ConnectionState = "connecting" | "ready" | "crashed";
export type InspectorTab =
  | "Plan"
  | "Context"
  | "Diff"
  | "Verify"
  | "Evidence"
  | "Modules";

export interface TimelineItem {
  eventId: string;
  eventType: string;
  sequence: number;
  summary: string;
}

export interface PermissionPrompt {
  requestId: string;
  permission: string;
  reason: string;
  argv: string;
  cwd: string;
  paths: string;
  network: string;
  timeoutSeconds: number;
}

export interface RivetState {
  connection: ConnectionState;
  repository: string;
  model: string;
  transaction: string;
  plan: { phase: string; summary: string };
  context: Array<{ path: string; reason: string }>;
  fileTree: string[];
  diff: string;
  verifyStatus: string;
  evidenceId: string;
  modules: string[];
  permission: PermissionPrompt | null;
  budget: { tokens: number; costUsd: number; elapsedMs: number };
  error: string | null;
  timeline: TimelineItem[];
  inspectorTab: InspectorTab;
  lastSequence: number;
}

export type RivetAction =
  | { kind: "trace"; event: IpcEvent }
  | {
      kind: "worker-status";
      state: "ready" | "crashed" | "closed";
      summary: string;
    }
  | { kind: "local-error"; summary: string };

const MAX_TIMELINE_ITEMS = 500;

export function initialRivetState(): RivetState {
  return {
    connection: "connecting",
    repository: "未连接",
    model: "未连接",
    transaction: "无",
    plan: { phase: "IDLE", summary: "等待任务" },
    context: [],
    fileTree: [],
    diff: "",
    verifyStatus: "未验证",
    evidenceId: "无",
    modules: [],
    permission: null,
    budget: { tokens: 0, costUsd: 0, elapsedMs: 0 },
    error: null,
    timeline: [],
    inspectorTab: "Plan",
    lastSequence: -1,
  };
}

export function reduceTraceEvent(state: RivetState, event: IpcEvent): RivetState {
  if (event.sequence <= state.lastSequence) return state;
  const next: RivetState = {
    ...state,
    lastSequence: event.sequence,
    timeline: appendTimeline(state.timeline, event),
  };
  switch (event.event_type) {
    case "worker.ready":
    case "worker.recovered":
      return {
        ...next,
        connection: "ready",
        repository: text(event.payload.repository, state.repository),
        model: text(event.payload.model, state.model),
        error: null,
      };
    case "worker.crashed":
      return {
        ...next,
        connection: "crashed",
        error: text(event.payload.summary, "Worker 已退出"),
      };
    case "plan.updated":
      return {
        ...next,
        plan: {
          phase: text(event.payload.phase, state.plan.phase),
          summary: text(event.payload.summary, state.plan.summary),
        },
      };
    case "context.selected":
      return {
        ...next,
        context: [
          ...state.context,
          {
            path: text(event.payload.path, "未知路径"),
            reason: text(event.payload.reason, "未说明"),
          },
        ],
      };
    case "workspace.tree_updated":
      return { ...next, fileTree: stringArray(event.payload.paths) };
    case "transaction.started":
      return {
        ...next,
        transaction: text(event.payload.transaction_id, state.transaction),
      };
    case "patch.updated":
      return { ...next, diff: text(event.payload.diff, "") };
    case "verification.completed":
      return {
        ...next,
        verifyStatus: text(event.payload.status, "UNKNOWN"),
      };
    case "evidence.published":
      return {
        ...next,
        evidenceId: text(event.payload.evidence_id, "无"),
      };
    case "module.activated": {
      const moduleId = text(event.payload.module_id, "");
      if (moduleId.length === 0 || state.modules.includes(moduleId)) return next;
      return { ...next, modules: [...state.modules, moduleId] };
    }
    case "module.slept": {
      const moduleId = text(event.payload.module_id, "");
      return { ...next, modules: state.modules.filter((item) => item !== moduleId) };
    }
    case "permission.requested":
      return { ...next, permission: permissionPrompt(event.payload) };
    case "permission.resolved":
      if (state.permission?.requestId !== text(event.payload.request_id, "")) {
        return next;
      }
      return { ...next, permission: null };
    case "budget.updated":
      return {
        ...next,
        budget: {
          tokens: number(event.payload.tokens, state.budget.tokens),
          costUsd: number(event.payload.cost_usd, state.budget.costUsd),
          elapsedMs: number(event.payload.elapsed_ms, state.budget.elapsedMs),
        },
      };
    case "tool.failed":
      return { ...next, error: text(event.payload.summary, "工具执行失败") };
    default:
      return next;
  }
}

export function reduceRivetState(state: RivetState, action: RivetAction): RivetState {
  if (action.kind === "trace") return reduceTraceEvent(state, action.event);
  if (action.kind === "local-error") {
    return { ...state, error: action.summary };
  }
  if (action.state === "ready") {
    return { ...state, connection: "ready", error: null };
  }
  return {
    ...state,
    connection: "crashed",
    error: action.summary,
  };
}

function appendTimeline(timeline: TimelineItem[], event: IpcEvent): TimelineItem[] {
  const appended = [
    ...timeline,
    {
      eventId: event.event_id,
      eventType: event.event_type,
      sequence: event.sequence,
      summary: text(event.payload.summary, event.event_type),
    },
  ];
  return appended.slice(-MAX_TIMELINE_ITEMS);
}

function permissionPrompt(payload: Record<string, JsonValue>): PermissionPrompt {
  return {
    requestId: text(payload.request_id, "request_unknown"),
    permission: text(payload.permission, "UNKNOWN"),
    reason: text(payload.reason, "未说明"),
    argv: text(payload.argv, "无"),
    cwd: text(payload.cwd, "."),
    paths: text(payload.paths, "无"),
    network: text(payload.network, "禁用"),
    timeoutSeconds: number(payload.timeout_seconds, 0),
  };
}

function text(value: JsonValue | undefined, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function number(value: JsonValue | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function stringArray(value: JsonValue | undefined): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}
