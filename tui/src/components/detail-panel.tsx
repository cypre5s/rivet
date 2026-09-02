import type { ModuleStatus, RivetState } from "../state/reducer.ts";
import type { PanelName } from "../ui/command-registry.ts";
import type { PanelPresentation } from "../ui/layout.ts";
import type { RivetTheme } from "./theme.ts";

export function DetailPanel({
  panel,
  state,
  selectedContextFiles,
  selectedModuleIndex,
  presentation,
  theme,
}: {
  panel: PanelName;
  state: RivetState;
  selectedContextFiles: string[];
  selectedModuleIndex: number;
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
      <box height={1} flexDirection="row" justifyContent="space-between">
        <text fg={theme.accent} content={panel === "Modules" ? "能力策略" : panel} />
        <text fg={theme.textMuted} content="Esc 关闭" />
      </box>
      <PanelContent
        panel={panel}
        state={state}
        selectedContextFiles={selectedContextFiles}
        selectedModuleIndex={selectedModuleIndex}
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
  theme,
}: {
  panel: PanelName;
  state: RivetState;
  selectedContextFiles: string[];
  selectedModuleIndex: number;
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
      <EmptyState text="当前事务还没有 Diff" action="完成 /fix 后重试" theme={theme} />
    );
  }
  if (panel === "Trace") {
    return (
      <scrollbox flexGrow={1} focused={true}>
        {state.timeline.map((item) => (
          <text
            key={`trace-${item.eventId}`}
            fg={theme.textSecondary}
            content={`${item.sequence.toString().padStart(4, "0")}  ${item.eventType}\n  ${item.title}`}
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
      <EmptyState text="文件清单尚未加载" action="按 Ctrl+O 按需加载" theme={theme} />
    );
  }
  if (panel === "Context") {
    const contextItems = [
      ...selectedContextFiles.map((path) => ({ path, reason: "用户显式选择" })),
      ...state.context.filter(
        (item) => !selectedContextFiles.includes(item.path),
      ),
    ];
    return contextItems.length ? (
      <scrollbox flexGrow={1} focused={true}>
        {contextItems.map((item) => (
          <box key={`${item.path}-${item.reason}`} flexDirection="column" marginBottom={1}>
            <text fg={theme.textPrimary} content={item.path} />
            <text fg={theme.textMuted} content={`  ${item.reason}`} />
          </box>
        ))}
      </scrollbox>
    ) : (
      <EmptyState text="当前还没有上下文来源" action="输入 @ 选择文件" theme={theme} />
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
                    content={`${selected ? "›" : " "} ${modulePolicyIcon(module)} ${moduleDisplayName(module.moduleId)} · ${modulePolicyLabel(module)}`}
                  />
                </box>
              );
            })}
          </scrollbox>
          <box flexDirection="column" marginTop={1}>
            <box height={1} width="100%">
              <text
                fg={theme.textMuted}
                content={`可用性 ${moduleAvailabilityLabel(selectedModule.availability)} · 依赖 ${selectedModule.dependencies.join(", ") || "无"}`}
              />
            </box>
            {selectedModule.availabilityAction === null ||
            selectedModule.availabilityAction === undefined ? null : (
              <box height={1} width="100%">
                <text
                  fg={theme.warning}
                  content={`下一步：${selectedModule.availabilityAction}`}
                />
              </box>
            )}
            {selectedModule.lastError === null ? null : (
              <box height={1} width="100%">
                <text fg={theme.danger} content={`异常：${selectedModule.lastError}`} />
              </box>
            )}
            <box height={1} width="100%">
              <text
                fg={theme.accent}
                content={`↑↓ 选择 · ${moduleActionHints(selectedModule).join(" · ")}`}
              />
            </box>
          </box>
        </box>
      );
    }
    return (
      <EmptyState text="能力策略尚未加载" action="执行 /modules list" theme={theme} />
    );
  }
  if (panel === "Sessions") {
    return state.sessions.length ? (
      <scrollbox flexGrow={1} focused={true}>
        {state.sessions.map((session) => (
          <text
            key={session}
            fg={session === state.sessionId ? theme.accent : theme.textSecondary}
            content={`${session === state.sessionId ? "●" : "○"} ${session}`}
          />
        ))}
      </scrollbox>
    ) : (
      <EmptyState text="当前没有可恢复会话" action="提交任务后会自动保存" theme={theme} />
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
          text={selectedTransactionId === null ? "当前没有历史事务" : "正在复核事务证据"}
          action={selectedTransactionId === null ? "完成 /fix 后重试" : selectedTransactionId}
          theme={theme}
        />
      );
    }
    const lines = [
      "近期事务（↑↓ 选择）",
      ...state.transactions.slice(0, 12).map(
        (transaction, index) =>
          `${index === safeTransactionIndex ? "›" : " "} ${transaction.transactionId} · ${transaction.state}${transaction.applyEligible ? " · 可 Apply" : ""}`,
      ),
      "",
      `Transaction：${evidence.transactionId || selectedTransactionId || "无"}`,
      `状态：${evidence.state} · Verdict：${evidence.verdictStatus}`,
      `Apply：${evidence.applyEligible ? "后端允许（仍需显式确认）" : "不允许"}`,
      `Evidence 完整性：${evidence.evidenceVerified ? "已复核" : "尚未发布"}`,
      `Evidence：${evidence.evidenceId ?? "无"}`,
      `Patch：${evidence.patchId ?? "无"}`,
      `AcceptanceSpec SHA-256：${evidence.acceptanceSha256}`,
      `Patch SHA-256：${evidence.patchSha256}`,
      `Manifest SHA-256：${evidence.manifestSha256}`,
      `Changed Files：\n${evidence.changedFiles.join(", ") || "无"}`,
      `Changed Symbols：\n${evidence.changedSymbols.join(", ") || "无"}`,
      "V0–V10 验证矩阵",
      ...evidence.verificationResults.map(
        (result) =>
          `${result.kind} · ${result.status} · ${result.required ? "required" : "optional"} · ${result.durationMs}ms · exit=${result.exitCode ?? "-"}\n  ${result.stepId}\n  argv: ${JSON.stringify(result.argv)}${result.logPath === null ? "" : `\n  log: ${result.logPath} · ${result.logSha256 ?? "无哈希"}`}`,
      ),
      "Evidence 文件索引",
      ...evidence.files.map(
        (file) => `${file.path} · ${file.sizeBytes} B\n  ${file.sha256}`,
      ),
      `下一步：${evidence.nextAction}`,
      "L / Enter：惰性加载首个失败步骤日志",
      ...(state.evidenceLog !== null &&
      state.evidenceLog.transactionId === evidence.transactionId
        ? [
            "",
            `日志：${state.evidenceLog.stepId} · ${state.evidenceLog.status}`,
            `${state.evidenceLog.logPath} · ${state.evidenceLog.logSha256}`,
            state.evidenceLog.content || "（空日志）",
            state.evidenceLog.truncated ? "…日志展示已截断" : "",
          ]
        : []),
    ];
    return (
      <scrollbox flexGrow={1} focused={true}>
        {lines.map((line, index) => (
          <text key={`${index}:${line}`} fg={theme.textSecondary} content={line} />
        ))}
      </scrollbox>
    );
  }
  const content: Record<"Plan" | "Verify" | "Evidence", [string, string]> = {
    Plan: [state.plan.phase, state.plan.summary],
    Verify: [state.verifyStatus, verificationHint(state.verifyStatus)],
    Evidence: [state.evidenceId, "证据由后端哈希校验"],
  };
  const [title, detail] = content[panel];
  return (
    <box flexDirection="column" gap={1}>
      <text fg={theme.textPrimary} content={title} />
      <text fg={theme.textSecondary} content={detail} />
    </box>
  );
}

function moduleActionHints(module: ModuleStatus): string[] {
  if (module.policy === "LOCKED" || !module.manualControl) return ["Kernel 管理"];
  if (["required", "eager"].includes(module.activation)) {
    return ["系统常驻"];
  }
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

function modulePolicyLabel(module: ModuleStatus): string {
  const labels: Readonly<Record<string, string>> = {
    LOCKED: "Kernel 管理",
    ENABLED: "已启用",
    DISABLED: "已禁用",
  };
  return labels[module.policy] ?? module.policy;
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
  action,
  theme,
}: {
  text: string;
  action: string;
  theme: RivetTheme;
}) {
  return (
    <box flexGrow={1} alignItems="center" justifyContent="center" flexDirection="column">
      <text fg={theme.textSecondary} content={text} />
      <text fg={theme.textMuted} content={action} />
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

function verificationHint(status: string): string {
  if (status.toUpperCase() === "PASSED") return "验证通过，可以显式 Apply";
  if (status.toUpperCase() === "FAILED") return "验证未通过，请查看 Evidence";
  if (status.toUpperCase() === "INCONCLUSIVE") return "验证不确定，补丁不会被放行";
  return "尚未执行验证";
}
import type { BoxProps } from "@opentui/react";
