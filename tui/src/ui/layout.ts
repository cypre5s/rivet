export type LayoutMode = "minimal" | "compact" | "drawer" | "wide";
export type LogoSize = "text" | "small" | "large";
export type PanelPresentation = "fullscreen" | "drawer" | "sidebar";

export interface LayoutDecision {
  mode: LayoutMode;
  logoSize: LogoSize;
  panelPresentation: PanelPresentation;
  contentWidth: number | `${number}%`;
}

export function computeLayout(width: number, height: number): LayoutDecision {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    throw new Error("terminal dimensions must be positive");
  }
  if (width < 60) {
    return {
      mode: "minimal",
      logoSize: "text",
      panelPresentation: "fullscreen",
      contentWidth: "100%",
    };
  }
  if (width < 80) {
    return {
      mode: "compact",
      logoSize: "small",
      panelPresentation: "fullscreen",
      contentWidth: "94%",
    };
  }
  if (width < 120) {
    return {
      mode: "drawer",
      logoSize: width >= 96 && height >= 24 ? "large" : "small",
      panelPresentation: "drawer",
      contentWidth: width >= 100 ? 76 : "88%",
    };
  }
  return {
    mode: "wide",
    logoSize: "large",
    panelPresentation: "sidebar",
    contentWidth: 82,
  };
}
