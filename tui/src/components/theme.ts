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

export function createTheme(noColor: boolean): RivetTheme {
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
  return DARK_THEME;
}
