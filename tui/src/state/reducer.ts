import type { IpcEvent, JsonValue } from "../contracts/ipc.ts";
import {
  presentTraceEvent,
  type TimelineKind,
  type TimelineStatus,
} from "../ui/event-presenter.ts";
import type { PanelName } from "../ui/command-registry.ts";

export type ConnectionState = "connecting" | "ready" | "crashed";
export type InspectorTab = PanelName;

export interface TimelineItem {
  eventId: string;
  eventType: string;
  sequence: number;
  title: string;
  detail: string;
  kind: TimelineKind;
  status: TimelineStatus;
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

export interface ModuleStatus {
  moduleId: string;
  manifestDefaultEnabled: boolean;
  persistedOverride: boolean | null;
  configuredEnabled: boolean;
  effectiveEnabled: boolean;
  runtimeState: string;
  activation: string;
  scope: string;
  manualControl: boolean;
  sleepPolicy: string;
  dependencies: string[];
  dependents: string[];
  providedCapabilities: string[];
  leaseCount: number;
  activeResourceCount: number;
  lastError: string | null;
}

export interface TransactionSummary {
  transactionId: string;
  state: string;
  evidenceId: string | null;
}

export interface RivetState {
  connection: ConnectionState;
  repository: string;
  branch: string;
  model: string;
  models: string[];
  baseUrl: string;
  maxRounds: number;
  maxTotalTokens: number;
  maxCostUsd: string | null;
  safeMode: boolean;
  configSources: Record<string, string>;
  credentialConfigured: boolean;
  sessionId: string | null;
  sessions: string[];
  transaction: string;
  transactions: TransactionSummary[];
  plan: { phase: string; summary: string };
  context: Array<{ path: string; reason: string }>;
  fileTree: string[];
  diff: string;
  verifyStatus: string;
  evidenceId: string;
  modules: string[];
  moduleStatuses: ModuleStatus[];
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
  | { kind: "local-error"; summary: string }
  | { kind: "local-message"; summary: string }
  | { kind: "configuration-loaded"; payload: Record<string, JsonValue> }
  | { kind: "timeline-clear" };

const MAX_TIMELINE_ITEMS = 500;

export function initialRivetState(): RivetState {
  return {
    connection: "connecting",
    repository: "未连接",
    branch: "",
    model: "未连接",
    models: ["deepseek-v4-pro", "deepseek-v4-flash"],
    baseUrl: "https://api.deepseek.com",
    maxRounds: 24,
    maxTotalTokens: 128_000,
    maxCostUsd: null,
    safeMode: false,
    configSources: {},
    credentialConfigured: false,
    sessionId: null,
    sessions: [],
    transaction: "无",
    transactions: [],
    plan: { phase: "IDLE", summary: "等待任务" },
    context: [],
    fileTree: [],
    diff: "",
    verifyStatus: "未验证",
    evidenceId: "无",
    modules: [],
    moduleStatuses: [],
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
      return projectConfiguration(
        {
          ...next,
          connection: "ready",
          repository: text(event.payload.repository, state.repository),
          branch: text(event.payload.branch, state.branch),
          error: null,
        },
        event.payload,
      );
    case "config.updated":
      return projectConfiguration(next, event.payload);
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
    case "session.updated": {
      const sessionId = text(event.payload.session_id, "");
      if (!sessionId) return next;
      return {
        ...next,
        sessionId,
        sessions: [sessionId, ...state.sessions.filter((item) => item !== sessionId)],
      };
    }
    case "sessions.snapshot": {
      const sessions = stringArray(event.payload.sessions);
      if (state.sessionId !== null && !sessions.includes(state.sessionId)) {
        sessions.unshift(state.sessionId);
      }
      return { ...next, sessions };
    }
    case "transaction.started":
      return {
        ...next,
        transaction: text(event.payload.transaction_id, state.transaction),
      };
    case "transactions.snapshot":
      return {
        ...next,
        transactions: transactionSummaryArray(event.payload.transactions),
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
    case "module.state.changed":
    case "module.enablement.changed":
    case "module.operation.completed": {
      const moduleId = text(event.payload.module_id, "");
      if (!moduleId) return next;
      const statuses = state.moduleStatuses.map((status) =>
        status.moduleId === moduleId
          ? {
              ...status,
              runtimeState: text(event.payload.current_state, status.runtimeState),
              effectiveEnabled: boolean(
                event.payload.effective_enabled,
                status.effectiveEnabled,
              ),
            }
          : status,
      );
      return {
        ...next,
        moduleStatuses: statuses,
        modules: activeModuleIds(statuses),
      };
    }
    case "modules.snapshot": {
      const statuses = moduleStatusArray(event.payload.modules);
      if (statuses.length === 0) {
        return { ...next, modules: stringArray(event.payload.modules) };
      }
      return {
        ...next,
        moduleStatuses: statuses,
        modules: activeModuleIds(statuses),
      };
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
  if (action.kind === "timeline-clear") return { ...state, timeline: [] };
  if (action.kind === "local-message") {
    const item: TimelineItem = {
      eventId: `local_user_${state.lastSequence}_${state.timeline.length}`,
      eventType: "user.message",
      sequence: state.lastSequence,
      title: action.summary,
      detail: "",
      kind: "user",
      status: "success",
    };
    return {
      ...state,
      timeline: [...state.timeline, item].slice(-MAX_TIMELINE_ITEMS),
    };
  }
  if (action.kind === "configuration-loaded") {
    return projectConfiguration(state, action.payload);
  }
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

function projectConfiguration(
  state: RivetState,
  payload: Record<string, JsonValue>,
): RivetState {
  const models = stringArray(payload.models);
  return {
    ...state,
    model: text(payload.model, state.model),
    models: models.length > 0 ? models : state.models,
    baseUrl: text(payload.base_url, state.baseUrl),
    maxRounds: number(payload.max_rounds, state.maxRounds),
    maxTotalTokens: number(payload.max_total_tokens, state.maxTotalTokens),
    maxCostUsd:
      payload.max_cost_usd === null
        ? null
        : text(payload.max_cost_usd, state.maxCostUsd ?? "") || null,
    safeMode: boolean(payload.safe_mode, state.safeMode),
    credentialConfigured: boolean(
      payload.credential_configured,
      state.credentialConfigured,
    ),
    configSources: stringRecord(payload.sources, state.configSources),
  };
}

function appendTimeline(timeline: TimelineItem[], event: IpcEvent): TimelineItem[] {
  const presented = presentTraceEvent(event);
  if (
    event.event_type === "worker.ready" &&
    timeline.some((item) => item.eventType === "worker.ready")
  ) {
    return timeline;
  }
  const item: TimelineItem = {
    eventId: event.event_id,
    eventType: event.event_type,
    sequence: event.sequence,
    ...presented,
  };
  const appended: TimelineItem[] = [
    ...timeline,
    item,
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

function boolean(value: JsonValue | undefined, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function stringArray(value: JsonValue | undefined): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function transactionSummaryArray(value: JsonValue | undefined): TransactionSummary[] {
  if (!Array.isArray(value)) return [];
  const transactions: TransactionSummary[] = [];
  for (const item of value) {
    if (typeof item !== "object" || item === null || Array.isArray(item)) continue;
    const transactionId = text(item.transaction_id, "");
    const state = text(item.state, "");
    if (!transactionId || !state) continue;
    transactions.push({
      transactionId,
      state,
      evidenceId: typeof item.evidence_id === "string" ? item.evidence_id : null,
    });
  }
  return transactions;
}

function stringRecord(
  value: JsonValue | undefined,
  fallback: Record<string, string>,
): Record<string, string> {
  if (value === null || Array.isArray(value) || typeof value !== "object") {
    return fallback;
  }
  const result: Record<string, string> = {};
  for (const [key, item] of Object.entries(value)) {
    if (typeof item === "string") result[key] = item;
  }
  return result;
}

function moduleStatusArray(value: JsonValue | undefined): ModuleStatus[] {
  if (!Array.isArray(value)) return [];
  const statuses: ModuleStatus[] = [];
  for (const item of value) {
    if (typeof item !== "object" || item === null || Array.isArray(item)) continue;
    const moduleId = text(item.module_id, "");
    if (!moduleId) continue;
    statuses.push({
      moduleId,
      manifestDefaultEnabled: boolean(item.manifest_default_enabled, false),
      persistedOverride:
        typeof item.persisted_override === "boolean" ? item.persisted_override : null,
      configuredEnabled: boolean(item.configured_enabled, false),
      effectiveEnabled: boolean(item.effective_enabled, false),
      runtimeState: text(item.runtime_state, "UNKNOWN"),
      activation: text(item.activation, "unknown"),
      scope: text(item.scope, "workspace"),
      manualControl: boolean(item.manual_control, false),
      sleepPolicy: text(item.sleep_policy, "unknown"),
      dependencies: stringArray(item.dependencies),
      dependents: stringArray(item.dependents),
      providedCapabilities: stringArray(item.provided_capabilities),
      leaseCount: number(item.lease_count, 0),
      activeResourceCount: number(item.active_resource_count, 0),
      lastError: typeof item.last_error === "string" ? item.last_error : null,
    });
  }
  return statuses;
}

function activeModuleIds(statuses: ModuleStatus[]): string[] {
  return statuses
    .filter((status) => ["ACTIVE", "IDLE", "ACTIVATING"].includes(status.runtimeState))
    .map((status) => status.moduleId);
}
