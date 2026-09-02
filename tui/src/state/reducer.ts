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
  policy: string;
  availability: string;
  missingComponents?: string[];
  availabilityAction?: string | null;
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
  patchId: string | null;
  patchSha256: string | null;
  updatedAt: string;
  applyEligible: boolean;
}

export interface VerificationSummary {
  stepId: string;
  kind: string;
  name: string;
  status: string;
  required: boolean;
  argv: string[];
  durationMs: number;
  exitCode: number | null;
  logPath: string | null;
  logSha256: string | null;
  stdoutSummary: string;
  stderrSummary: string;
}

export interface EvidenceFileSummary {
  path: string;
  sha256: string;
  sizeBytes: number;
}

export interface EvidenceSummary {
  transactionId: string;
  state: string;
  verdictStatus: string;
  passed: boolean;
  applyEligible: boolean;
  evidenceVerified: boolean;
  evidenceId: string | null;
  patchId: string | null;
  acceptanceSha256: string;
  patchSha256: string;
  manifestSha256: string;
  changedFiles: string[];
  changedSymbols: string[];
  verificationResults: VerificationSummary[];
  files: EvidenceFileSummary[];
  updatedAt: string;
  decidedAt: string;
  nextAction: string;
}

export interface EvidenceLog {
  transactionId: string;
  evidenceId: string;
  stepId: string;
  status: string;
  logPath: string;
  logSha256: string;
  content: string;
  truncated: boolean;
}

export interface VerificationSuggestion {
  kind: string;
  category: string;
  argv: string[];
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
  acceptanceReady: boolean;
  acceptanceReason: string;
  acceptanceAction: string;
  projectKinds: string[];
  verificationSuggestions: VerificationSuggestion[];
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
  evidence: EvidenceSummary | null;
  evidenceLog: EvidenceLog | null;
  taskModules: string[];
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
    acceptanceReady: false,
    acceptanceReason: "尚未完成 Evidence 预检",
    acceptanceAction: "运行 rivet init 查看项目检测建议",
    projectKinds: [],
    verificationSuggestions: [],
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
    evidence: null,
    evidenceLog: null,
    taskModules: [],
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
    case "run.started":
    case "run.resumed":
      return { ...next, taskModules: [] };
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
    case "candidate.ready":
      return {
        ...next,
        verifyStatus: "CANDIDATE_ONLY",
      };
    case "evidence.published":
      return {
        ...next,
        evidenceId: text(event.payload.evidence_id, "无"),
        evidence: evidenceSummary(event.payload),
        evidenceLog: null,
      };
    case "evidence.snapshot": {
      const evidence = evidenceSummary(event.payload);
      return {
        ...next,
        transaction: text(event.payload.transaction_id, state.transaction),
        verifyStatus: text(
          event.payload.verdict_status,
          text(event.payload.state, state.verifyStatus),
        ),
        evidenceId: text(event.payload.evidence_id, "无"),
        evidence,
        evidenceLog: null,
      };
    }
    case "evidence.log":
      return {
        ...next,
        evidenceLog: evidenceLog(event.payload),
      };
    case "module.activated": {
      const moduleId = text(event.payload.module_id, "");
      if (moduleId.length === 0 || state.taskModules.includes(moduleId)) return next;
      return { ...next, taskModules: [...state.taskModules, moduleId] };
    }
    case "module.released": {
      const moduleId = text(event.payload.module_id, "");
      if (number(event.payload.lease_count, 0) > 0) return next;
      return {
        ...next,
        taskModules: state.taskModules.filter((item) => item !== moduleId),
      };
    }
    case "module.slept": {
      const moduleId = text(event.payload.module_id, "");
      return {
        ...next,
        taskModules: state.taskModules.filter((item) => item !== moduleId),
      };
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
              configuredEnabled:
                event.event_type === "module.operation.completed"
                  ? text(event.payload.operation, "") === "enable"
                    ? true
                    : text(event.payload.operation, "") === "disable"
                      ? false
                      : status.configuredEnabled
                  : status.configuredEnabled,
              runtimeState: text(event.payload.current_state, status.runtimeState),
              effectiveEnabled: boolean(
                event.payload.effective_enabled,
                status.effectiveEnabled,
              ),
              policy:
                status.policy === "LOCKED"
                  ? "LOCKED"
                  : text(event.payload.operation, "") === "enable"
                    ? "ENABLED"
                    : text(event.payload.operation, "") === "disable"
                      ? "DISABLED"
                      : status.policy,
            }
          : status,
      );
      return { ...next, moduleStatuses: statuses };
    }
    case "modules.snapshot": {
      const statuses = moduleStatusArray(event.payload.modules);
      return statuses.length === 0 ? next : { ...next, moduleStatuses: statuses };
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
    case "command.completed":
      return { ...next, error: null };
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
    acceptanceReady: boolean(payload.acceptance_ready, state.acceptanceReady),
    acceptanceReason: text(payload.acceptance_reason, state.acceptanceReason),
    acceptanceAction: text(payload.acceptance_action, state.acceptanceAction),
    projectKinds:
      payload.project_kinds === undefined
        ? state.projectKinds
        : stringArray(payload.project_kinds),
    verificationSuggestions:
      payload.verification_suggestions === undefined
        ? state.verificationSuggestions
        : verificationSuggestionArray(payload.verification_suggestions),
    configSources: stringRecord(payload.sources, state.configSources),
  };
}

function verificationSuggestionArray(
  value: JsonValue | undefined,
): VerificationSuggestion[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (item === null || Array.isArray(item) || typeof item !== "object") return [];
    const suggestion = item as Record<string, JsonValue>;
    const argv = stringArray(suggestion.argv);
    const kind = text(suggestion.kind, "");
    const category = text(suggestion.category, "");
    return argv.length > 0 && kind && category ? [{ kind, category, argv }] : [];
  });
}

function appendTimeline(timeline: TimelineItem[], event: IpcEvent): TimelineItem[] {
  const presented = presentTraceEvent(event);
  if (
    event.event_type === "worker.ready" &&
    timeline.some((item) => item.eventType === "worker.ready")
  ) {
    return timeline;
  }
  const responseId = text(event.payload.response_id, "");
  const streamingEventId = responseId ? `agent_stream_${responseId}` : event.event_id;
  const item: TimelineItem = {
    eventId:
      event.event_type === "agent.output.delta"
        ? streamingEventId
        : event.event_id,
    eventType: event.event_type,
    sequence: event.sequence,
    ...presented,
  };
  if (event.event_type === "agent.output.delta" && responseId) {
    const existingIndex = timeline.findIndex(
      (existing) => existing.eventId === streamingEventId,
    );
    if (existingIndex >= 0) {
      return timeline.map((existing, index) =>
        index === existingIndex ? item : existing,
      );
    }
  }
  const withoutPartial =
    responseId && item.kind === "assistant"
      ? timeline.filter((existing) => existing.eventId !== streamingEventId)
      : timeline;
  const appended: TimelineItem[] = [
    ...withoutPartial,
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
      patchId: typeof item.patch_id === "string" ? item.patch_id : null,
      patchSha256:
        typeof item.patch_sha256 === "string" ? item.patch_sha256 : null,
      updatedAt: text(item.updated_at, ""),
      applyEligible: boolean(item.apply_eligible, state === "VERIFIED"),
    });
  }
  return transactions;
}

function evidenceSummary(payload: Record<string, JsonValue>): EvidenceSummary | null {
  const evidenceId = text(payload.evidence_id, "") || null;
  const transactionId = text(payload.transaction_id, "");
  if (evidenceId === null && !transactionId) return null;
  const verificationResults: VerificationSummary[] = [];
  if (Array.isArray(payload.verification_results)) {
    for (const value of payload.verification_results) {
      if (typeof value !== "object" || value === null || Array.isArray(value)) continue;
      const kind = text(value.kind, "");
      const status = text(value.status, "");
      if (!kind || !status) continue;
      verificationResults.push({
        stepId: text(value.step_id, kind),
        kind,
        name: text(value.name, kind),
        status,
        required: boolean(value.required, true),
        argv: stringArray(value.argv),
        durationMs: number(value.duration_ms, 0),
        exitCode:
          typeof value.exit_code === "number" ? value.exit_code : null,
        logPath: typeof value.log_path === "string" ? value.log_path : null,
        logSha256:
          typeof value.log_sha256 === "string" ? value.log_sha256 : null,
        stdoutSummary: text(value.stdout_summary, ""),
        stderrSummary: text(value.stderr_summary, ""),
      });
    }
  }
  const files: EvidenceFileSummary[] = [];
  if (Array.isArray(payload.files)) {
    for (const value of payload.files) {
      if (typeof value !== "object" || value === null || Array.isArray(value)) continue;
      const path = text(value.path, "");
      const sha256 = text(value.sha256, "");
      if (!path || !sha256) continue;
      files.push({ path, sha256, sizeBytes: number(value.size_bytes, 0) });
    }
  }
  return {
    transactionId,
    state: text(payload.state, "UNKNOWN"),
    verdictStatus: text(payload.verdict_status, text(payload.status, "NOT_RUN")),
    passed: boolean(payload.passed, false),
    applyEligible: boolean(payload.apply_eligible, false),
    evidenceVerified: boolean(payload.evidence_verified, evidenceId !== null),
    evidenceId,
    patchId: typeof payload.patch_id === "string" ? payload.patch_id : null,
    acceptanceSha256: text(payload.acceptance_sha256, "未提供"),
    patchSha256: text(payload.patch_sha256, "未提供"),
    manifestSha256: text(payload.manifest_sha256, "未提供"),
    changedFiles: stringArray(payload.changed_files),
    changedSymbols: stringArray(payload.changed_symbols),
    verificationResults,
    files,
    updatedAt: text(payload.updated_at, ""),
    decidedAt: text(payload.decided_at, ""),
    nextAction: text(payload.next_action, "审查后端事务状态"),
  };
}

function evidenceLog(payload: Record<string, JsonValue>): EvidenceLog | null {
  const transactionId = text(payload.transaction_id, "");
  const evidenceId = text(payload.evidence_id, "");
  const stepId = text(payload.step_id, "");
  const logPath = text(payload.log_path, "");
  const logSha256 = text(payload.log_sha256, "");
  if (!transactionId || !evidenceId || !stepId || !logPath || !logSha256) {
    return null;
  }
  return {
    transactionId,
    evidenceId,
    stepId,
    status: text(payload.status, "UNKNOWN"),
    logPath,
    logSha256,
    content: text(payload.content, ""),
    truncated: boolean(payload.truncated, false),
  };
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
      policy: text(
        item.policy,
        boolean(item.manual_control, false)
          ? boolean(item.configured_enabled, false)
            ? "ENABLED"
            : "DISABLED"
          : "LOCKED",
      ),
      availability: text(item.availability, "AVAILABLE"),
      missingComponents: stringArray(item.missing_components),
      availabilityAction:
        typeof item.availability_action === "string"
          ? item.availability_action
          : null,
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
