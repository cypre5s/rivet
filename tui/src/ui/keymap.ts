export type KeyCommand =
  | "mode.next"
  | "mode.previous"
  | "palette.open"
  | "files.open"
  | "history.open"
  | "models.open"
  | "config.open"
  | "leader.open"
  | "timeline.clear"
  | "task.cancel"
  | "worker.recover"
  | "input.submit"
  | "overlay.close";

export interface KeyDescriptor {
  name: string;
  shift: boolean;
  ctrl: boolean;
}

export type CtrlCAction = "cancel" | "exit";
export type CtrlCIntent = "cancel-task" | "clear-input" | "prompt-exit" | "exit";

export interface CtrlCContext {
  running: boolean;
  inputEmpty: boolean;
}

export const CTRL_C_EXIT_WINDOW_MS = 1500;

export function resolveCtrlCAction(
  previousAt: number | null,
  now: number,
): CtrlCAction {
  if (
    previousAt !== null &&
    now >= previousAt &&
    now - previousAt <= CTRL_C_EXIT_WINDOW_MS
  ) {
    return "exit";
  }
  return "cancel";
}

export function resolveCtrlCIntent(
  previousAt: number | null,
  now: number,
  context: CtrlCContext,
): CtrlCIntent {
  if (resolveCtrlCAction(previousAt, now) === "exit") return "exit";
  if (context.running) return "cancel-task";
  if (!context.inputEmpty) return "clear-input";
  return "prompt-exit";
}

export const LEADER_COMMANDS = {
  p: "plan",
  f: "fix",
  v: "verify",
  d: "diff",
  e: "evidence",
  t: "trace",
  m: "modules",
  c: "context",
  s: "sessions",
  h: "help",
  q: "quit",
} as const;

export function resolveLeaderCommand(keyName: string): string | null {
  return LEADER_COMMANDS[keyName.toLocaleLowerCase() as keyof typeof LEADER_COMMANDS] ?? null;
}

export function resolveKeyCommand(key: KeyDescriptor): KeyCommand | null {
  if (key.name === "tab") return key.shift ? "mode.previous" : "mode.next";
  if (key.ctrl && key.name === "p") return "palette.open";
  if (key.ctrl && key.name === "o") return "files.open";
  if (key.ctrl && key.shift && key.name === "r") return "worker.recover";
  if (key.ctrl && key.name === "r") return "history.open";
  if (key.ctrl && key.name === "k") return "models.open";
  if (key.ctrl && key.name === "g") return "config.open";
  if (key.ctrl && key.name === "x") return "leader.open";
  if (key.ctrl && key.name === "l") return "timeline.clear";
  if (key.ctrl && key.name === "c") return "task.cancel";
  if (!key.ctrl && key.name === "return") return "input.submit";
  if (!key.ctrl && key.name === "escape") return "overlay.close";
  return null;
}
