import type { ReactNode } from "react";

import type { RivetState } from "../state/reducer.ts";
import type { PanelName } from "../ui/command-registry.ts";
import type { LayoutDecision } from "../ui/layout.ts";
import { DetailPanel } from "./detail-panel.tsx";
import { Header } from "./header.tsx";
import type { RivetTheme } from "./theme.ts";
import { TimelinePanel } from "./timeline-panel.tsx";

export function SessionScreen({
  state,
  running,
  openPanel,
  layout,
  theme,
  composer,
  selectedContextFiles,
  selectedModuleIndex,
  evidenceExpanded,
}: {
  state: RivetState;
  running: boolean;
  openPanel: PanelName | null;
  layout: LayoutDecision;
  theme: RivetTheme;
  composer: ReactNode;
  selectedContextFiles: string[];
  selectedModuleIndex: number;
  evidenceExpanded: boolean;
}) {
  return (
    <box flexGrow={1} flexDirection="column" backgroundColor={theme.background}>
      <Header state={state} theme={theme} />
      <box flexGrow={1} flexDirection="row">
        <TimelinePanel state={state} running={running} theme={theme} />
        {openPanel !== null && layout.panelPresentation === "sidebar" ? (
          <DetailPanel
            panel={openPanel}
            state={state}
            selectedContextFiles={selectedContextFiles}
            selectedModuleIndex={selectedModuleIndex}
            evidenceExpanded={evidenceExpanded}
            presentation={layout.panelPresentation}
            theme={theme}
          />
        ) : null}
      </box>
      <box paddingX={layout.mode === "minimal" ? 0 : 2} paddingBottom={1}>
        {composer}
      </box>
      {openPanel !== null && layout.panelPresentation !== "sidebar" ? (
        <DetailPanel
          panel={openPanel}
          state={state}
          selectedContextFiles={selectedContextFiles}
          selectedModuleIndex={selectedModuleIndex}
          evidenceExpanded={evidenceExpanded}
          presentation={layout.panelPresentation}
          theme={theme}
        />
      ) : null}
    </box>
  );
}
