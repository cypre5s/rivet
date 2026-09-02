import { describe, expect, test } from "bun:test";

import {
  CTRL_C_EXIT_WINDOW_MS,
  resolveCtrlCAction,
  resolveCtrlCIntent,
  resolveKeyCommand,
} from "./keymap.ts";

describe("minimal keymap", () => {
  test("keeps only model, recovery, cancel and close shortcuts", () => {
    expect(resolveKeyCommand({ name: "k", shift: false, ctrl: true })).toBe(
      "models.open",
    );
    expect(resolveKeyCommand({ name: "r", shift: true, ctrl: true })).toBe(
      "worker.recover",
    );
    expect(resolveKeyCommand({ name: "c", shift: false, ctrl: true })).toBe(
      "task.cancel",
    );
    expect(resolveKeyCommand({ name: "escape", shift: false, ctrl: false })).toBe(
      "overlay.close",
    );
    for (const name of ["p", "o", "g", "x", "l"]) {
      expect(resolveKeyCommand({ name, shift: false, ctrl: true })).toBeNull();
    }
    expect(resolveKeyCommand({ name: "tab", shift: false, ctrl: false })).toBeNull();
  });
});

describe("Ctrl+C lifecycle", () => {
  test("cancels first and exits only on a prompt second press", () => {
    expect(resolveCtrlCAction(null, 1_000)).toBe("cancel");
    expect(resolveCtrlCAction(1_000, 1_100)).toBe("exit");
    expect(resolveCtrlCAction(1_000, 1_000 + CTRL_C_EXIT_WINDOW_MS + 1)).toBe(
      "cancel",
    );
  });

  test("prioritizes running work, overlays and input", () => {
    expect(resolveCtrlCIntent(null, 1_000, {
      running: true,
      inputEmpty: true,
      overlayOpen: false,
    })).toBe("cancel-task");
    expect(resolveCtrlCIntent(null, 1_000, {
      running: false,
      inputEmpty: false,
      overlayOpen: true,
    })).toBe("close-overlay");
    expect(resolveCtrlCIntent(null, 1_000, {
      running: false,
      inputEmpty: false,
      overlayOpen: false,
    })).toBe("clear-input");
  });
});
