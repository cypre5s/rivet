export type KeyCommand =
  | "models.open"
  | "task.cancel"
  | "worker.recover"
  | "overlay.close";

export interface KeyDescriptor {
  name: string;
  shift: boolean;
  ctrl: boolean;
}

export type CtrlCAction = "cancel" | "exit";
export type CtrlCIntent =
  | "cancel-task"
  | "close-overlay"
  | "clear-input"
  | "prompt-exit"
  | "exit";

export interface CtrlCContext {
  running: boolean;
  inputEmpty: boolean;
  overlayOpen: boolean;
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
  if (context.overlayOpen) return "close-overlay";
  if (!context.inputEmpty) return "clear-input";
  return "prompt-exit";
}

export function resolveKeyCommand(key: KeyDescriptor): KeyCommand | null {
  if (key.ctrl && key.shift && key.name === "r") return "worker.recover";
  if (key.ctrl && key.name === "k") return "models.open";
  if (key.ctrl && key.name === "c") return "task.cancel";
  if (!key.ctrl && key.name === "escape") return "overlay.close";
  return null;
}
