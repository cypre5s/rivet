import { describe, expect, test } from "bun:test";

import { DARK_THEME, LIGHT_THEME, createTheme } from "./theme.ts";

describe("central theme tokens", () => {
  test("defines the same complete token set for light and dark modes", () => {
    expect(Object.keys(DARK_THEME)).toEqual(Object.keys(LIGHT_THEME));
    expect(DARK_THEME.background).toBe("#090909");
    expect(DARK_THEME.accent).not.toBe(DARK_THEME.danger);
  });

  test("provides a deterministic no-color fallback", () => {
    const theme = createTheme(true, "light");
    expect(theme.background).toBe("black");
    expect(theme.textPrimary).toBe("white");
    expect(theme.danger).toBe("white");
  });
});
