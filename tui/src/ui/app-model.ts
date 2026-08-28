import type { JsonValue } from "../contracts/ipc.ts";
import type { RivetState } from "../state/reducer.ts";
import {
  findCommand,
  type CommandDescriptor,
  type CommandOutcome,
  type PanelName,
  type WorkMode,
} from "./command-registry.ts";

export type Overlay =
  | { kind: "palette" }
  | { kind: "slash" }
  | { kind: "files" }
  | { kind: "history" }
  | { kind: "models" }
  | { kind: "arguments"; commandName: string }
  | { kind: "leader" }
  | { kind: "info"; title: string; lines: string[] }
  | {
      kind: "confirm";
      command: CommandDescriptor;
      outcome: CommandOutcome;
      displayInput: string;
    };

export const MODES: readonly WorkMode[] = ["ASK", "PLAN", "FIX"];
export const MODELS = ["deepseek-v4-pro", "deepseek-v4-flash"] as const;
export const MAX_CONTEXT_FILES = 20;
export const PLACEHOLDERS = [
  "修复失败的测试并给出验证证据",
  "解释这个仓库的技术架构",
  "为当前修改生成计划",
  "读取 @report.pdf 并落实其中的需求",
] as const;
export const TIPS = [
  "输入 / 可以查看 Rivet 的全部操作",
  "输入 @ 可以把仓库文件加入上下文",
  "修改只发生在隔离事务，验证通过后才能 Apply",
  "Ctrl+X 打开 Leader 快捷键提示",
] as const;

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
  if (command === "clean") return "仅带 Rivet ownership marker 的运行产物";
  if (command === "init") return "当前仓库的 .rivet 项目配置";
  return "当前命令声明的受控范围";
}

export function keyHelpLines(): string[] {
  return [
    "Ctrl+P      全局命令面板",
    "Ctrl+O      文件选择器",
    "Ctrl+R      输入历史",
    "Ctrl+X      Leader 快捷键",
    "Tab         ASK → PLAN → FIX",
    "Shift+Tab   反向切换模式",
    "Enter       提交",
    "Shift+Enter 换行",
    "Ctrl+J      换行兼容键",
    "Esc         关闭最上层视图",
    "Ctrl+C      取消 / 清空 / 二次安全退出",
  ];
}

export function statusLines(
  state: RivetState,
  mode: WorkMode,
  running: boolean,
): string[] {
  return [
    `连接：${state.connection}`,
    `模式：${mode}`,
    `阶段：${running ? "RUNNING" : state.plan.phase}`,
    `模型：${state.credentialConfigured ? state.model : "未配置"}`,
    `会话：${state.sessionId ?? "无"}`,
    `事务：${state.transaction}`,
    `验证：${state.verifyStatus}`,
  ];
}

export function costLines(state: RivetState): string[] {
  return [
    `Token：${state.budget.tokens}`,
    `费用：$${state.budget.costUsd.toFixed(4)}`,
    `耗时：${state.budget.elapsedMs} ms`,
    "零值在空闲状态栏中自动隐藏",
  ];
}
