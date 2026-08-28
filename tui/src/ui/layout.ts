export type LayoutMode = "single" | "split" | "three-column";
export type PanelId = "repository" | "timeline" | "inspector";

export interface LayoutDecision {
  mode: LayoutMode;
  visiblePanels: PanelId[];
  inspectorOverlay: boolean;
}

export function computeLayout(width: number, height: number): LayoutDecision {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    throw new Error("terminal dimensions must be positive");
  }
  if (width < 100) {
    return {
      mode: "single",
      visiblePanels: ["timeline"],
      inspectorOverlay: true,
    };
  }
  if (width < 160) {
    return {
      mode: "split",
      visiblePanels: ["repository", "timeline"],
      inspectorOverlay: true,
    };
  }
  return {
    mode: "three-column",
    visiblePanels: ["repository", "timeline", "inspector"],
    inspectorOverlay: false,
  };
}
