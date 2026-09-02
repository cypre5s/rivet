import type { BoxProps } from "@opentui/react";

import type { RivetState, VerificationSummary } from "../state/reducer.ts";
import type { PanelName } from "../ui/command-registry.ts";
import {
  evidencePanelModel,
  type EvidenceLineTone,
  verificationStatusText,
} from "../ui/evidence-presentation.ts";
import type { PanelPresentation } from "../ui/layout.ts";
import type { RivetTheme } from "./theme.ts";

const PANELS: readonly PanelName[] = ["Diff", "Verify", "Evidence"];

export function DetailPanel({
  panel,
  state,
  selectedTransactionIndex,
  evidenceExpanded,
  presentation,
  theme,
  onSelectPanel,
}: {
  panel: PanelName;
  state: RivetState;
  selectedTransactionIndex: number;
  evidenceExpanded: boolean;
  presentation: PanelPresentation;
  theme: RivetTheme;
  onSelectPanel(panel: PanelName): void;
}) {
  return (
    <box
      {...panelPlacement(presentation)}
      zIndex={presentation === "sidebar" ? 1 : 25}
      backgroundColor={theme.surface}
      border={presentation === "sidebar" ? ["left"] : true}
      borderColor={theme.border}
      flexDirection="column"
      padding={1}
      gap={1}
    >
      <box height={1} flexDirection="row" gap={2}>
        {PANELS.map((candidate) => (
          <box key={candidate} onMouseDown={() => onSelectPanel(candidate)}>
            <text
              fg={candidate === panel ? theme.accent : theme.textMuted}
              content={`${panelKey(candidate)} ${candidate}`}
            />
          </box>
        ))}
      </box>
      <PanelContent
        panel={panel}
        state={state}
        selectedTransactionIndex={selectedTransactionIndex}
        evidenceExpanded={evidenceExpanded}
        theme={theme}
      />
    </box>
  );
}

function PanelContent({
  panel,
  state,
  selectedTransactionIndex,
  evidenceExpanded,
  theme,
}: {
  panel: PanelName;
  state: RivetState;
  selectedTransactionIndex: number;
  evidenceExpanded: boolean;
  theme: RivetTheme;
}) {
  if (panel === "Diff") {
    return state.diff ? (
      <diff
        diff={state.diff}
        view="unified"
        wrapMode="char"
        showLineNumbers={true}
        flexGrow={1}
      />
    ) : (
      <EmptyState text="暂无候选补丁" theme={theme} />
    );
  }
  if (panel === "Verify") {
    const results = state.evidence?.verificationResults ?? [];
    return (
      <box flexGrow={1} flexDirection="column" gap={1}>
        <text
          fg={verificationColor(state.verifyStatus, theme)}
          content={`结论 ${verificationStatusText(state.verifyStatus)}`}
        />
        {results.length === 0 ? (
          <EmptyState text="等待独立验证" theme={theme} />
        ) : (
          <scrollbox flexGrow={1} focused={true}>
            {results.map((result) => (
              <text
                key={result.stepId}
                fg={verificationColor(result.status, theme)}
                content={`${verificationIcon(result)} ${result.name}`}
              />
            ))}
          </scrollbox>
        )}
      </box>
    );
  }

  const safeIndex = Math.max(
    0,
    Math.min(selectedTransactionIndex, Math.max(0, state.transactions.length - 1)),
  );
  const selectedTransaction = state.transactions[safeIndex];
  const selectedTransactionId =
    selectedTransaction?.transactionId ??
    (state.transaction === "无" ? null : state.transaction);
  const evidence =
    state.evidence !== null &&
    (!state.evidence.transactionId ||
      state.evidence.transactionId === selectedTransactionId)
      ? state.evidence
      : null;
  if (evidence === null) {
    return (
      <EmptyState
        text={selectedTransactionId === null ? "暂无事务" : "Evidence 加载中"}
        theme={theme}
      />
    );
  }
  const model = evidencePanelModel({
    evidence,
    evidenceLog: state.evidenceLog,
    transactions: state.transactions,
    selectedIndex: safeIndex,
    expanded: evidenceExpanded,
  });
  return (
    <scrollbox flexGrow={1} focused={true}>
      {model.lines.map((line) => (
        <text
          key={line.key}
          fg={evidenceLineColor(line.tone, theme)}
          content={line.text}
        />
      ))}
    </scrollbox>
  );
}

function panelKey(panel: PanelName): string {
  if (panel === "Diff") return "D";
  if (panel === "Verify") return "V";
  return "E";
}

function verificationIcon(result: VerificationSummary): string {
  const status = result.status.toUpperCase();
  if (["PASSED", "VERIFIED"].includes(status)) return "✓";
  if (["FAILED", "ERROR"].includes(status)) return "×";
  return "—";
}

function verificationColor(status: string, theme: RivetTheme): string {
  const normalized = status.toUpperCase();
  if (["PASSED", "VERIFIED"].includes(normalized)) return theme.success;
  if (["FAILED", "ERROR"].includes(normalized)) return theme.danger;
  return theme.textSecondary;
}

function evidenceLineColor(tone: EvidenceLineTone, theme: RivetTheme): string {
  if (tone === "accent") return theme.accent;
  if (tone === "success") return theme.success;
  if (tone === "warning") return theme.warning;
  if (tone === "danger") return theme.danger;
  if (tone === "muted") return theme.textMuted;
  return theme.textPrimary;
}

function EmptyState({ text, theme }: { text: string; theme: RivetTheme }) {
  return (
    <box flexGrow={1} alignItems="center" justifyContent="center">
      <text fg={theme.textSecondary} content={text} />
    </box>
  );
}

function panelPlacement(presentation: PanelPresentation): Partial<BoxProps> {
  if (presentation === "sidebar") return { width: "38%", height: "100%" };
  return {
    position: "absolute",
    top: 0,
    left: 0,
    width: "100%",
    height: "100%",
  };
}
