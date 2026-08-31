import { describe, expect, test } from "bun:test";

import {
  CTRL_C_EXIT_WINDOW_MS,
  resolveCtrlCAction,
  resolveCtrlCIntent,
  resolveKeyCommand,
  resolveLeaderCommand,
} from "./keymap.ts";

describe("Rivet keymap", () => {
  test("maps the required global keys", () => {
    expect(resolveKeyCommand({ name: "tab", shift: false, ctrl: false })).toBe(
      "mode.next",
    );
    expect(resolveKeyCommand({ name: "tab", shift: true, ctrl: false })).toBe(
      "mode.previous",
    );
    expect(resolveKeyCommand({ name: "p", shift: false, ctrl: true })).toBe(
      "palette.open",
    );
    expect(resolveKeyCommand({ name: "l", shift: false, ctrl: true })).toBe(
      "timeline.clear",
    );
    expect(resolveKeyCommand({ name: "c", shift: false, ctrl: true })).toBe(
      "task.cancel",
    );
    expect(resolveKeyCommand({ name: "r", shift: false, ctrl: true })).toBe(
      "history.open",
    );
    expect(resolveKeyCommand({ name: "r", shift: true, ctrl: true })).toBe(
      "worker.recover",
    );
    expect(resolveKeyCommand({ name: "k", shift: false, ctrl: true })).toBe(
      "models.open",
    );
    expect(resolveKeyCommand({ name: "g", shift: false, ctrl: true })).toBe(
      "config.open",
    );
    expect(resolveKeyCommand({ name: "return", shift: false, ctrl: false })).toBe(
      "input.submit",
    );
    expect(resolveKeyCommand({ name: "escape", shift: false, ctrl: false })).toBe(
      "overlay.close",
    );
  });

  test("maps Leader keys without treating unknown input as a command", () => {
    expect(resolveLeaderCommand("V")).toBe("verify");
    expect(resolveLeaderCommand("?")).toBeNull();
  });
});

describe("Ctrl+C lifecycle", () => {
  test("cancels first and exits only on a prompt second press", () => {
    expect(resolveCtrlCAction(null, 1000)).toBe("cancel");
    expect(resolveCtrlCAction(1000, 1100)).toBe("exit");
    expect(resolveCtrlCAction(1000, 1000 + CTRL_C_EXIT_WINDOW_MS + 1)).toBe(
      "cancel",
    );
  });

  test("implements cancel, clear and double-press exit intents", () => {
    expect(
      resolveCtrlCIntent(null, 1_000, {
        running: true,
        inputEmpty: true,
        overlayOpen: false,
      }),
    ).toBe("cancel-task");
    expect(
      resolveCtrlCIntent(null, 1_000, {
        running: false,
        inputEmpty: false,
        overlayOpen: false,
      }),
    ).toBe("clear-input");
    expect(
      resolveCtrlCIntent(null, 1_000, {
        running: false,
        inputEmpty: true,
        overlayOpen: false,
      }),
    ).toBe("prompt-exit");
    expect(
      resolveCtrlCIntent(1_000, 1_100, {
        running: true,
        inputEmpty: true,
        overlayOpen: false,
      }),
    ).toBe("exit");
  });

  test("closes an overlay first and exits on the second press", () => {
    expect(
      resolveCtrlCIntent(null, 1_000, {
        running: false,
        inputEmpty: false,
        overlayOpen: true,
      }),
    ).toBe("close-overlay");
    expect(
      resolveCtrlCIntent(1_000, 1_100, {
        running: false,
        inputEmpty: false,
        overlayOpen: false,
      }),
    ).toBe("exit");
  });
});
