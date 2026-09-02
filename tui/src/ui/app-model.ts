import type { JsonValue } from "../contracts/ipc.ts";
import type { RivetState } from "../state/reducer.ts";
import {
  findCommand,
  type CommandDescriptor,
  type CommandOutcome,
  type PanelName,
  type WorkMode,
} from "./command-registry.ts";
import {
  compactIdentifier,
  verificationStatusText,
} from "./evidence-presentation.ts";

export type Overlay =
  | { kind: "palette" }
  | { kind: "slash" }
  | { kind: "files" }
  | { kind: "history" }
  | { kind: "models" }
  | { kind: "config" }
  | { kind: "arguments"; commandName: string }
  | { kind: "leader" }
  | { kind: "info"; title: string; lines: string[] }
  | {
      kind: "confirm";
      command: CommandDescriptor;
      outcome: CommandOutcome;
      displayInput: string;
      title?: string;
      description?: string;
      impact?: string;
    };

export const MODES: readonly WorkMode[] = ["ASK", "PLAN", "FIX"];
export const MAX_CONTEXT_FILES = 20;
export const COMPOSER_PLACEHOLDER = "输入任务…";

export function descriptorForInput(
  value: string,
  mode: WorkMode,
): CommandDescriptor | null {
  if (!value.startsWith("/")) return findCommand(mode.toLocaleLowerCase());
  const name = value.slice(1).split(/\s/, 1)[0] ?? "";
  return findCommand(name);
}

export function commandNeedsArgument(command: CommandDescriptor): boolean {
  return ["query", "path", "transaction", "session"].includes(
    command.argumentKind,
  );
}

export function hasArgumentChoices(command: CommandDescriptor): boolean {
  return [
    "path",
    "optional-path",
    "transaction",
    "optional-transaction",
    "session",
    "mode",
    "model",
    "theme",
    "module",
    "context",
    "export",
  ].includes(command.argumentKind);
}

export function parseMode(value: string): WorkMode | null {
  const normalized = value.trim().toUpperCase();
  return MODES.includes(normalized as WorkMode) ? (normalized as WorkMode) : null;
}

export function explicitTaskMode(value: string): WorkMode | null {
  const match = /^\/(ask|plan|fix)(?=\s|$)/i.exec(value.trimStart());
  return match?.[1] === undefined ? null : parseMode(match[1]);
}

export function replaceExplicitTaskMode(value: string, mode: WorkMode): string {
  return value.replace(
    /^(\s*)\/(?:ask|plan|fix)(?=\s|$)/i,
    `$1/${mode.toLocaleLowerCase()}`,
  );
}

export function panelForAction(
  action: Extract<CommandOutcome, { kind: "ui" }>["action"],
  argument: string,
): PanelName | null {
  if (action === "open-sessions") return "Sessions";
  if (action === "open-context") return "Context";
  if (action === "open-modules") return "Modules";
  if (action === "open-trace") return "Trace";
  if (action === "open-evidence") return "Evidence";
  if (action === "export-view") {
    if (argument === "evidence") return "Evidence";
    if (argument === "session") return "Sessions";
    return "Trace";
  }
  return null;
}

export function overlayItemCount(
  overlay: Overlay | null,
  slashCount: number,
  paletteCount: number,
  fileCount: number,
  historyCount: number,
  modelCount: number,
  argumentCount: number,
): number {
  if (overlay?.kind === "slash") return slashCount;
  if (overlay?.kind === "palette") return paletteCount;
  if (overlay?.kind === "files") return fileCount;
  if (overlay?.kind === "history") return historyCount;
  if (overlay?.kind === "models") return modelCount;
  if (overlay?.kind === "arguments") return argumentCount;
  return 0;
}

export function clampIndex(index: number, count: number): number {
  if (count <= 0) return 0;
  return Math.max(0, Math.min(count - 1, index));
}

export function resultPaths(result: JsonValue): string[] {
  if (result === null || Array.isArray(result) || typeof result !== "object") {
    return [];
  }
  const paths = result.paths;
  return Array.isArray(paths)
    ? paths.filter((path): path is string => typeof path === "string")
    : [];
}

export function dangerousImpact(command: string, state: RivetState): string {
  if (command === "apply") return `主工作区 · ${state.transaction}`;
  if (command === "abort") return `隔离事务 · ${state.transaction}`;
  if (command === "clean") return "Rivet 标记的运行产物";
  if (command === "init") return "当前仓库的 .rivet 配置";
  if (command === "modules") return "能力策略及依赖";
  return "命令声明的受控范围";
}

export function keyHelpLines(): string[] {
  return [
    "Ctrl+P      命令",
    "Ctrl+O      文件",
    "Ctrl+R      历史",
    "↑ / ↓       历史",
    "Ctrl+K      模型",
    "Ctrl+G      配置",
    "Ctrl+X      快捷操作",
    "Tab         模式",
    "Shift+Tab   反向模式",
    "Enter       提交",
    "Shift+Enter 换行",
    "Ctrl+J      换行",
    "Esc         关闭",
    "Ctrl+C      取消 / 清空 / 退出",
  ];
}

export function statusLines(
  state: RivetState,
  mode: WorkMode,
  running: boolean,
): string[] {
  const connection =
    state.connection === "ready" ? "●" : state.connection === "crashed" ? "×" : "◌";
  const lines = [
    `${connection} ${mode} · ${running ? "RUNNING" : state.plan.phase}`,
    `${state.model} · Key ${state.credentialConfigured ? "●" : "○"}`,
    `验收 ${state.acceptanceReady ? "✓" : "×"} · 验证 ${verificationStatusText(state.verifyStatus)}`,
  ];
  if (state.sessionId !== null) {
    lines.push(`会话 ${compactIdentifier(state.sessionId)}`);
  }
  if (state.transaction !== "无") {
    lines.push(`事务 ${compactIdentifier(state.transaction)}`);
  }
  return lines;
}

export function costLines(state: RivetState): string[] {
  return [
    `${state.budget.tokens} tok · $${state.budget.costUsd.toFixed(4)} · ${formatElapsed(state.budget.elapsedMs)}`,
  ];
}

function formatElapsed(elapsedMs: number): string {
  return elapsedMs < 1_000
    ? `${elapsedMs}ms`
    : `${(elapsedMs / 1_000).toFixed(1)}s`;
}
