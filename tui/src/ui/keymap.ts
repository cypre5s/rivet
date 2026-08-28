export type KeyCommand =
  | "focus.next"
  | "focus.previous"
  | "palette.open"
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

export function resolveKeyCommand(key: KeyDescriptor): KeyCommand | null {
  if (key.name === "tab") return key.shift ? "focus.previous" : "focus.next";
  if (key.ctrl && key.name === "p") return "palette.open";
  if (key.ctrl && key.name === "l") return "timeline.clear";
  if (key.ctrl && key.name === "c") return "task.cancel";
  if (key.ctrl && key.name === "r") return "worker.recover";
  if (!key.ctrl && key.name === "return") return "input.submit";
  if (!key.ctrl && key.name === "escape") return "overlay.close";
  return null;
}
