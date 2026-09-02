export type LayoutMode = "minimal" | "standard";
export type LogoSize = "text" | "small";
export type PanelPresentation = "fullscreen" | "sidebar";

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
  const minimal = width < 72 || height < 18;
  return {
    mode: minimal ? "minimal" : "standard",
    logoSize: minimal ? "text" : "small",
    panelPresentation: width >= 110 ? "sidebar" : "fullscreen",
    contentWidth: minimal ? "100%" : width >= 110 ? 82 : "92%",
  };
}
