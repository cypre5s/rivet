export interface RivetTheme {
  background: string;
  panel: string;
  text: string;
  muted: string;
  accent: string;
  border: string;
  error: string;
}

export function createTheme(noColor: boolean): RivetTheme {
  if (noColor) {
    return {
      background: "black",
      panel: "black",
      text: "white",
      muted: "gray",
      accent: "white",
      border: "gray",
      error: "white",
    };
  }
  return {
    background: "#0b0f14",
    panel: "#111821",
    text: "#d8dee9",
    muted: "#7f8ea3",
    accent: "#71c4ff",
    border: "#334155",
    error: "#ff7b72",
  };
}
