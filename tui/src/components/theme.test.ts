import { describe, expect, test } from "bun:test";

import { DARK_THEME, createTheme } from "./theme.ts";

describe("fixed theme", () => {
  test("uses one dark token set", () => {
    expect(createTheme(false)).toBe(DARK_THEME);
    expect(DARK_THEME.background).toBe("#090909");
    expect(DARK_THEME.accent).not.toBe(DARK_THEME.danger);
  });

  test("provides a deterministic no-color fallback", () => {
    const theme = createTheme(true);
    expect(theme.background).toBe("black");
    expect(theme.textPrimary).toBe("white");
    expect(theme.danger).toBe("white");
  });
});
