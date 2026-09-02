import type { IpcEvent, JsonValue } from "../contracts/ipc.ts";
import {
  presentTraceEvent,
  type TimelineKind,
  type TimelineStatus,
} from "../ui/event-presenter.ts";
export type ConnectionState = "connecting" | "ready" | "crashed";

export interface TimelineItem {
  eventId: string;
  eventType: string;
  sequence: number;
  title: string;
  detail: string;
  kind: TimelineKind;
  status: TimelineStatus;
  demandId: string | null;
  operationId: string | null;
  parentEventId: string | null;
  parentDemandId: string | null;
}

export interface PermissionPrompt {
  requestId: string;
  goal: string;
  readScope: string[];
  writeScope: string[];
  allowedNewPaths: string[];
  expectedBehaviors: string[];
  acceptanceCommands: string[][];
  regressionCommands: string[][];
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
  baseCommit: string;
  acceptanceSha256: string;
  patchSha256: string;
  manifestSha256: string;
  changedFiles: string[];
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

export interface RivetState {
  connection: ConnectionState;
  repository: string;
  branch: string;
  model: string;
  models: string[];
  credentialConfigured: boolean;
  acceptanceReady: boolean;
  transaction: string;
  transactions: TransactionSummary[];
  fileTree: string[];
  diff: string;
  verifyStatus: string;
  evidenceId: string;
  evidence: EvidenceSummary | null;
  evidenceLog: EvidenceLog | null;
  permission: PermissionPrompt | null;
  error: string | null;
  timeline: TimelineItem[];
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
  | { kind: "timeline-clear" };

const MAX_TIMELINE_ITEMS = 500;

export function initialRivetState(): RivetState {
  return {
    connection: "connecting",
    repository: "未连接",
    branch: "",
    model: "未连接",
    models: ["deepseek-v4-pro", "deepseek-v4-flash"],
    credentialConfigured: false,
    acceptanceReady: false,
    transaction: "无",
    transactions: [],
    fileTree: [],
    diff: "",
    verifyStatus: "未验证",
    evidenceId: "无",
    evidence: null,
    evidenceLog: null,
    permission: null,
    error: null,
    timeline: [],
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
      return projectState(
        {
          ...next,
          connection: "ready",
          repository: text(event.payload.repository, state.repository),
          branch: text(event.payload.branch, state.branch),
          error: null,
        },
        event.payload,
      );
    case "worker.crashed":
      return {
        ...next,
        connection: "crashed",
        error: text(event.payload.summary, "Worker 已退出"),
      };
    case "workspace.tree_updated":
      return { ...next, fileTree: stringArray(event.payload.paths) };
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
    case "permission.requested":
      return { ...next, permission: permissionPrompt(event.payload) };
    case "permission.resolved":
      if (state.permission?.requestId !== text(event.payload.request_id, "")) {
        return next;
      }
      return { ...next, permission: null };
    case "tool.failed":
      return { ...next, error: text(event.payload.summary, "工具执行失败") };
    case "command.failed":
      return { ...next, error: text(event.payload.summary, "命令执行失败") };
    case "command.cancelled":
      return { ...next, error: null };
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
      demandId: null,
      operationId: null,
      parentEventId: null,
      parentDemandId: null,
    };
    return {
      ...state,
      timeline: [...state.timeline, item].slice(-MAX_TIMELINE_ITEMS),
    };
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

function projectState(
  state: RivetState,
  payload: Record<string, JsonValue>,
): RivetState {
  const models = stringArray(payload.models);
  return {
    ...state,
    model: text(payload.model, state.model),
    models: models.length > 0 ? models : state.models,
    credentialConfigured: boolean(
      payload.credential_configured,
      state.credentialConfigured,
    ),
    acceptanceReady: boolean(payload.acceptance_ready, state.acceptanceReady),
  };
}

function appendTimeline(timeline: TimelineItem[], event: IpcEvent): TimelineItem[] {
  const presented = presentTraceEvent(event);
  const currentTimeline =
    event.event_type === "command.failed" ||
    event.event_type === "command.cancelled"
      ? timeline.map((item) =>
          item.status === "running"
            ? {
                ...item,
                status:
                  event.event_type === "command.cancelled"
                    ? ("cancelled" as const)
                    : ("failed" as const),
              }
            : item,
        )
      : timeline;
  if (
    event.event_type === "worker.ready" &&
    currentTimeline.some((item) => item.eventType === "worker.ready")
  ) {
    return currentTimeline;
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
    demandId: text(event.payload.demand_id, "") || null,
    operationId: text(event.payload.operation_id, "") || null,
    parentEventId: text(event.payload.parent_event_id, "") || null,
    parentDemandId: text(event.payload.parent_demand_id, "") || null,
    ...presented,
  };
  if (event.event_type === "agent.output.delta" && responseId) {
    const existingIndex = currentTimeline.findIndex(
      (existing) => existing.eventId === streamingEventId,
    );
    if (existingIndex >= 0) {
      return currentTimeline.map((existing, index) =>
        index === existingIndex ? item : existing,
      );
    }
  }
  const withoutPartial =
    responseId && item.kind === "assistant"
      ? currentTimeline.filter((existing) => existing.eventId !== streamingEventId)
      : currentTimeline;
  const appended: TimelineItem[] = [
    ...withoutPartial,
    item,
  ];
  return appended.slice(-MAX_TIMELINE_ITEMS);
}

function permissionPrompt(payload: Record<string, JsonValue>): PermissionPrompt {
  return {
    requestId: text(payload.request_id, "request_unknown"),
    goal: text(payload.goal, "未提供 Goal"),
    readScope: stringArray(payload.read_scope),
    writeScope: stringArray(payload.write_scope),
    allowedNewPaths: stringArray(payload.allowed_new_paths),
    expectedBehaviors: stringArray(payload.expected_behaviors),
    acceptanceCommands: stringMatrix(payload.acceptance_commands),
    regressionCommands: stringMatrix(payload.regression_commands),
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

function stringMatrix(value: JsonValue | undefined): string[][] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is JsonValue[] => Array.isArray(item))
    .map((item) => item.filter((argument): argument is string => typeof argument === "string"))
    .filter((command) => command.length > 0);
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
    baseCommit: text(payload.base_commit, "未提供"),
    acceptanceSha256: text(payload.acceptance_sha256, "未提供"),
    patchSha256: text(payload.patch_sha256, "未提供"),
    manifestSha256: text(payload.manifest_sha256, "未提供"),
    changedFiles: stringArray(payload.changed_files),
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
