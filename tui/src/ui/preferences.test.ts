import { describe, expect, test } from "bun:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  DEFAULT_TUI_PREFERENCES,
  loadTuiPreferences,
  parseTuiPreferences,
  saveTuiPreferences,
} from "./preferences.ts";

describe("safe TUI preferences", () => {
  test("accepts only display fields from the closed vocabulary", () => {
    expect(
      parseTuiPreferences({
        mode: "FIX",
        theme: "light",
        panel: "Evidence",
        api_key: "discard",
      }),
    ).toEqual({ mode: "FIX", theme: "light", panel: "Evidence" });
  });

  test("falls back safely for malformed or unknown preferences", () => {
    expect(parseTuiPreferences(null)).toEqual(DEFAULT_TUI_PREFERENCES);
    expect(
      parseTuiPreferences({ mode: "ROOT", theme: "neon", panel: "Shell" }),
    ).toEqual(DEFAULT_TUI_PREFERENCES);
  });

  test("roundtrips only display preferences through an explicit temporary path", async () => {
    const directory = await mkdtemp(join(tmpdir(), "rivet-tui-preferences-"));
    const path = join(directory, "nested", "preferences.json");
    try {
      await saveTuiPreferences(
        { mode: "PLAN", theme: "light", panel: "Context" },
        path,
      );

      expect(await loadTuiPreferences(path)).toEqual({
        mode: "PLAN",
        theme: "light",
        panel: "Context",
      });
    } finally {
      await rm(directory, { recursive: true });
    }
  });
});
