import type { ModuleStatus, RivetState } from "../state/reducer.ts";
import type { PanelName } from "../ui/command-registry.ts";
import {
  compactIdentifier,
  evidencePanelModel,
  type EvidenceLineTone,
  verificationStatusText,
} from "../ui/evidence-presentation.ts";
import type { PanelPresentation } from "../ui/layout.ts";
import type { RivetTheme } from "./theme.ts";

export function DetailPanel({
  panel,
  state,
  selectedContextFiles,
  selectedModuleIndex,
  evidenceExpanded,
  presentation,
  theme,
}: {
  panel: PanelName;
  state: RivetState;
  selectedContextFiles: string[];
  selectedModuleIndex: number;
  evidenceExpanded: boolean;
  presentation: PanelPresentation;
  theme: RivetTheme;
}) {
  const placement = panelPlacement(presentation);
  return (
    <box
      {...placement}
      zIndex={presentation === "sidebar" ? 1 : 25}
      backgroundColor={theme.surface}
      border={["left"]}
      borderColor={theme.border}
      flexDirection="column"
      padding={1}
      gap={1}
    >
      <box height={1} flexDirection="row">
        <text fg={theme.accent} content={panelTitle(panel)} />
      </box>
      <PanelContent
        panel={panel}
        state={state}
        selectedContextFiles={selectedContextFiles}
        selectedModuleIndex={selectedModuleIndex}
        evidenceExpanded={evidenceExpanded}
        theme={theme}
      />
    </box>
  );
}

function PanelContent({
  panel,
  state,
  selectedContextFiles,
  selectedModuleIndex,
  evidenceExpanded,
  theme,
}: {
  panel: PanelName;
  state: RivetState;
  selectedContextFiles: string[];
  selectedModuleIndex: number;
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
      <EmptyState text="暂无修改" theme={theme} />
    );
  }
  if (panel === "Trace") {
    return (
      <scrollbox flexGrow={1} focused={true}>
        {state.timeline.map((item) => (
          <text
            key={`trace-${item.eventId}`}
            fg={theme.textSecondary}
            content={`${item.sequence.toString().padStart(4, "0")}  ${item.title}`}
          />
        ))}
      </scrollbox>
    );
  }
  if (panel === "Files") {
    return state.fileTree.length ? (
      <scrollbox flexGrow={1} focused={true}>
        {state.fileTree.map((path) => (
          <text key={path} fg={theme.textSecondary} content={path} />
        ))}
      </scrollbox>
    ) : (
      <EmptyState text="暂无文件" theme={theme} />
    );
  }
  if (panel === "Context") {
    const contextItems = [
      ...selectedContextFiles.map((path) => ({ path, reason: "" })),
      ...state.context.filter(
        (item) => !selectedContextFiles.includes(item.path),
      ),
    ];
    return contextItems.length ? (
      <scrollbox flexGrow={1} focused={true}>
        {contextItems.map((item) => (
          <box key={`${item.path}-${item.reason}`} flexDirection="column" marginBottom={1}>
            <text fg={theme.textPrimary} content={item.path} />
            {item.reason ? (
              <text fg={theme.textMuted} content={`  ${item.reason}`} />
            ) : null}
          </box>
        ))}
      </scrollbox>
    ) : (
      <EmptyState text="暂无上下文" theme={theme} />
    );
  }
  if (panel === "Modules") {
    if (state.moduleStatuses.length) {
      const safeModuleIndex = Math.max(
        0,
        Math.min(selectedModuleIndex, state.moduleStatuses.length - 1),
      );
      const selectedModule = state.moduleStatuses[safeModuleIndex]!;
      return (
        <box flexGrow={1} flexDirection="column">
          <scrollbox flexGrow={1} focused={true}>
            {state.moduleStatuses.map((module, index) => {
              const selected = index === safeModuleIndex;
              return (
                <box
                  key={module.moduleId}
                  height={1}
                  flexDirection="row"
                  backgroundColor={selected ? theme.selection : theme.surface}
                >
                  <text
                    fg={moduleColor(module, selected, theme)}
                    content={`${selected ? "›" : " "} ${modulePolicyIcon(module)} ${moduleDisplayName(module.moduleId)}`}
                  />
                </box>
              );
            })}
          </scrollbox>
          <box flexDirection="column" marginTop={1}>
            {selectedModule.availability === "AVAILABLE" ? null : (
              <box height={1} width="100%">
                <text
                  fg={theme.warning}
                  content={moduleAvailabilityLabel(selectedModule.availability)}
                />
              </box>
            )}
            {selectedModule.dependencies.length === 0 ? null : (
              <box height={1} width="100%">
                <text
                  fg={theme.textMuted}
                  content={`依赖 ${selectedModule.dependencies.join(", ")}`}
                />
              </box>
            )}
            {selectedModule.availabilityAction === null ||
            selectedModule.availabilityAction === undefined ? null : (
              <box height={1} width="100%">
                <text
                  fg={theme.warning}
                  content={`→ ${selectedModule.availabilityAction}`}
                />
              </box>
            )}
            {selectedModule.lastError === null ? null : (
              <box height={1} width="100%">
                <text fg={theme.danger} content={`! ${selectedModule.lastError}`} />
              </box>
            )}
            {moduleActionHints(selectedModule).length === 0 ? null : (
              <box height={1} width="100%">
                <text
                  fg={theme.accent}
                  content={`${state.moduleStatuses.length > 1 ? "↑↓ · " : ""}${moduleActionHints(selectedModule).join(" · ")}`}
                />
              </box>
            )}
          </box>
        </box>
      );
    }
    return (
      <EmptyState text="暂无能力" theme={theme} />
    );
  }
  if (panel === "Sessions") {
    return state.sessions.length ? (
      <scrollbox flexGrow={1} focused={true}>
        {state.sessions.map((session) => (
          <text
            key={session}
            fg={session === state.sessionId ? theme.accent : theme.textSecondary}
            content={`${session === state.sessionId ? "●" : "○"} ${compactIdentifier(session)}`}
          />
        ))}
      </scrollbox>
    ) : (
      <EmptyState text="暂无会话" theme={theme} />
    );
  }
  if (panel === "Evidence") {
    const safeTransactionIndex = Math.max(
      0,
      Math.min(selectedModuleIndex, Math.max(0, state.transactions.length - 1)),
    );
    const selectedTransaction = state.transactions[safeTransactionIndex];
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
          text={selectedTransactionId === null ? "暂无证据" : "加载中"}
          theme={theme}
        />
      );
    }
    const model = evidencePanelModel({
      evidence,
      evidenceLog: state.evidenceLog,
      transactions: state.transactions,
      selectedIndex: safeTransactionIndex,
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
  const lines =
    panel === "Plan"
      ? [planLabel(state)]
      : [verificationLabel(state.verifyStatus)];
  return (
    <box flexDirection="column" gap={1}>
      {lines.map((line, index) => (
        <text
          key={`${panel}-${index}`}
          fg={index === 0 ? theme.textPrimary : theme.textSecondary}
          content={line}
        />
      ))}
    </box>
  );
}

const PANEL_TITLES: Readonly<Record<PanelName, string>> = {
  Plan: "计划",
  Context: "上下文",
  Files: "文件",
  Diff: "修改",
  Verify: "验证",
  Evidence: "证据",
  Modules: "能力",
  Trace: "轨迹",
  Sessions: "会话",
};

function panelTitle(panel: PanelName): string {
  return PANEL_TITLES[panel];
}

function evidenceLineColor(tone: EvidenceLineTone, theme: RivetTheme): string {
  if (tone === "accent") return theme.accent;
  if (tone === "success") return theme.success;
  if (tone === "warning") return theme.warning;
  if (tone === "danger") return theme.danger;
  if (tone === "muted") return theme.textMuted;
  return theme.textPrimary;
}

function moduleActionHints(module: ModuleStatus): string[] {
  if (module.policy === "LOCKED" || !module.manualControl) return [];
  if (["required", "eager"].includes(module.activation)) return [];
  if (!module.configuredEnabled) {
    return ["E 启用"];
  }
  return ["D 禁用"];
}

const MODULE_NAMES: Readonly<Record<string, string>> = {
  "provider.deepseek": "DeepSeek 模型",
  "context.lexical": "词法检索",
  "context.syntax": "语法分析",
  "context.lsp": "LSP 语义",
  "reader.core": "基础读取",
  "reader.document": "文档读取",
  "reader.image": "图片读取",
  "reader.media": "媒体读取",
  "reader.archive": "7z 读取",
  "reader.transcription": "本地转录",
  "reader.pdf": "PDF 读取",
  "transaction.git": "隔离事务",
  "verify.matrix": "独立验证",
  "guard.sandbox": "安全沙箱",
};

function moduleDisplayName(moduleId: string): string {
  return MODULE_NAMES[moduleId] ?? moduleId;
}

function modulePolicyIcon(module: ModuleStatus): string {
  if (module.availability !== "AVAILABLE") return "!";
  if (module.policy === "LOCKED") return "◆";
  return module.policy === "ENABLED" ? "●" : "○";
}

function moduleAvailabilityLabel(availability: string): string {
  const labels: Readonly<Record<string, string>> = {
    AVAILABLE: "可用",
    SAFE_MODE_RESTRICTED: "Safe Mode 限制",
    MISSING_DEPENDENCY: "缺少依赖",
    MISSING_EXECUTABLE: "缺少程序",
    UNSUPPORTED: "当前平台不支持",
  };
  return labels[availability] ?? availability;
}

function moduleColor(
  module: ModuleStatus,
  selected: boolean,
  theme: RivetTheme,
): string {
  if (selected) return theme.accent;
  if (module.availability !== "AVAILABLE" || module.lastError !== null) {
    return theme.danger;
  }
  if (module.policy === "ENABLED") return theme.success;
  return module.policy === "LOCKED" ? theme.textSecondary : theme.textMuted;
}

function EmptyState({
  text,
  theme,
}: {
  text: string;
  theme: RivetTheme;
}) {
  return (
    <box flexGrow={1} alignItems="center" justifyContent="center" flexDirection="column">
      <text fg={theme.textSecondary} content={text} />
    </box>
  );
}

function panelPlacement(presentation: PanelPresentation): Partial<BoxProps> {
  if (presentation === "sidebar") return { width: "34%", height: "100%" } as const;
  if (presentation === "drawer") {
    return {
      position: "absolute" as const,
      top: 2,
      right: 0,
      width: "72%" as const,
      height: "88%" as const,
    };
  }
  return {
    position: "absolute" as const,
    top: 0,
    left: 0,
    width: "100%" as const,
    height: "100%" as const,
  };
}

function verificationLabel(status: string): string {
  const icons: Readonly<Record<string, string>> = {
    PASSED: "✓",
    FAILED: "×",
    BLOCKED: "!",
    INCONCLUSIVE: "!",
  };
  return `${icons[status.toUpperCase()] ?? "—"} ${verificationStatusText(status)}`;
}

function planLabel(state: RivetState): string {
  if (state.plan.phase.toUpperCase() === "IDLE") return "暂无计划";
  return state.plan.summary || state.plan.phase;
}
import type { BoxProps } from "@opentui/react";
