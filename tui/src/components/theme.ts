export interface RivetTheme {
  background: string;
  surface: string;
  surfaceHover: string;
  textPrimary: string;
  textSecondary: string;
  textMuted: string;
  accent: string;
  border: string;
  success: string;
  warning: string;
  danger: string;
  selection: string;
}

export type ThemeName = "dark" | "light";

export const DARK_THEME: RivetTheme = {
  background: "#090909",
  surface: "#171717",
  surfaceHover: "#202020",
  textPrimary: "#eeeeee",
  textSecondary: "#a1a1a1",
  textMuted: "#666666",
  accent: "#67d4e8",
  border: "#303030",
  success: "#7fcf9b",
  warning: "#ddb66f",
  danger: "#e58b8b",
  selection: "#173c43",
};

export const LIGHT_THEME: RivetTheme = {
  background: "#f5f5f3",
  surface: "#ffffff",
  surfaceHover: "#ececea",
  textPrimary: "#202020",
  textSecondary: "#5f5f5f",
  textMuted: "#8b8b8b",
  accent: "#087f8c",
  border: "#d5d5d2",
  success: "#2f7d4a",
  warning: "#946b18",
  danger: "#b04444",
  selection: "#d5eef1",
};

export function createTheme(
  noColor: boolean,
  name: ThemeName = "dark",
): RivetTheme {
  if (noColor) {
    return {
      background: "black",
      surface: "black",
      surfaceHover: "black",
      textPrimary: "white",
      textSecondary: "white",
      textMuted: "gray",
      accent: "white",
      border: "gray",
      success: "white",
      warning: "white",
      danger: "white",
      selection: "gray",
    };
  }
  return name === "light" ? LIGHT_THEME : DARK_THEME;
}
