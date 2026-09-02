import type { JsonValue } from "../contracts/ipc.ts";
import type { RivetState } from "../state/reducer.ts";
import {
  findCommand,
  type CommandDescriptor,
  type CommandOutcome,
  type PanelName,
} from "./command-registry.ts";

export type Overlay =
  | { kind: "slash" }
  | { kind: "files" }
  | { kind: "models" }
  | { kind: "arguments"; commandName: string }
  | { kind: "info"; title: string; lines: string[] }
  | {
      kind: "confirm";
      command: CommandDescriptor;
      outcome: CommandOutcome;
      displayInput: string;
    };

export const MAX_SELECTED_FILES = 20;
export const COMPOSER_PLACEHOLDER = "输入问题，或 /fix 开始修复…";

export function descriptorForInput(value: string): CommandDescriptor | null {
  if (!value.startsWith("/")) return null;
  const name = value.slice(1).split(/\s/, 1)[0] ?? "";
  return findCommand(name);
}

export function commandNeedsArgument(command: CommandDescriptor): boolean {
  return ["query", "transaction"].includes(command.argumentKind);
}

export function hasArgumentChoices(command: CommandDescriptor): boolean {
  return ["transaction", "optional-transaction", "model"].includes(
    command.argumentKind,
  );
}

export function panelForWorkerCommand(commandName: string): PanelName | null {
  if (commandName === "diff") return "Diff";
  if (commandName === "verify") return "Verify";
  return null;
}

export function overlayItemCount(
  overlay: Overlay | null,
  slashCount: number,
  fileCount: number,
  modelCount: number,
  argumentCount: number,
): number {
  if (overlay?.kind === "slash") return slashCount;
  if (overlay?.kind === "files") return fileCount;
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
  return "命令声明的受控范围";
}
