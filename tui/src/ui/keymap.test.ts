import { describe, expect, test } from "bun:test";

import { resolveKeyCommand } from "./keymap.ts";

describe("Rivet keymap", () => {
  test("maps the required global keys", () => {
    expect(resolveKeyCommand({ name: "tab", shift: false, ctrl: false })).toBe(
      "focus.next",
    );
    expect(resolveKeyCommand({ name: "tab", shift: true, ctrl: false })).toBe(
      "focus.previous",
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
      "worker.recover",
    );
    expect(resolveKeyCommand({ name: "return", shift: false, ctrl: false })).toBe(
      "input.submit",
    );
    expect(resolveKeyCommand({ name: "escape", shift: false, ctrl: false })).toBe(
      "overlay.close",
    );
  });
});
