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
        <text fg={theme.accent} content={panel} />
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
    return state.moduleStatuses.length ? (
      <scrollbox flexGrow={1} focused={true}>
        {state.moduleStatuses.map((module, index) => (
          <box key={module.moduleId} flexDirection="column" marginBottom={1}>
            <text
              fg={
                index === selectedModuleIndex
                  ? theme.accent
                  : module.effectiveEnabled
                    ? theme.textPrimary
                    : theme.textMuted
              }
              content={`${index === selectedModuleIndex ? "›" : " "} ${module.runtimeState === "ACTIVE" ? "●" : "○"} ${module.moduleId}`}
            />
            <text
              fg={theme.textSecondary}
              content={`  ${module.configuredEnabled ? "enabled" : "disabled"} · ${module.runtimeState} · ${module.scope} · ${module.activation}`}
            />
            <text
              fg={theme.textMuted}
              content={`  依赖 ${module.dependencies.join(", ") || "无"} · Lease ${module.leaseCount} · Resource ${module.activeResourceCount}`}
            />
            <text
              fg={theme.accent}
              content={`  ${moduleActions(module).join("  ·  ")}`}
            />
            {module.lastError === null ? null : (
              <text fg={theme.danger} content={`  最近错误：${module.lastError}`} />
            )}
          </box>
        ))}
        <text
          fg={theme.textMuted}
          content="↑↓ 选择 · E 启用 · W 唤醒 · S 休眠 · D 禁用"
        />
      </scrollbox>
    ) : state.modules.length ? (
      <scrollbox flexGrow={1} focused={true}>
        {state.modules.map((module) => (
          <text key={module} fg={theme.textSecondary} content={`● ACTIVE  ${module}`} />
        ))}
      </scrollbox>
    ) : (
      <EmptyState text="模块状态尚未加载" action="执行 /modules list" theme={theme} />
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
  const content: Record<"Plan" | "Verify" | "Evidence", [string, string]> = {
    Plan: [state.plan.phase, state.plan.summary],
    Verify: [state.verifyStatus, verificationHint(state.verifyStatus)],
    Evidence: [state.evidenceId, state.evidenceId === "无" ? "当前没有验证证据" : "证据由后端哈希校验"],
  };
  const [title, detail] = content[panel];
  return (
    <box flexDirection="column" gap={1}>
      <text fg={theme.textPrimary} content={title} />
      <text fg={theme.textSecondary} content={detail} />
    </box>
  );
}

function moduleActions(module: ModuleStatus): string[] {
  if (!module.manualControl) return ["内部模块，仅由 Kernel 控制"];
  if (["required", "eager"].includes(module.activation)) {
    return ["系统常驻模块，只读"];
  }
  if (!module.configuredEnabled) {
    return [`/modules enable ${module.moduleId}`];
  }
  const actions: string[] = [];
  if (["INACTIVE", "SLEEPING"].includes(module.runtimeState)) {
    actions.push(`/modules wake ${module.moduleId}`);
  }
  if (["ACTIVE", "IDLE"].includes(module.runtimeState)) {
    actions.push(`/modules sleep ${module.moduleId}`);
  }
  actions.push(`/modules disable ${module.moduleId}`);
  if (module.leaseCount > 0) actions.push(`阻塞：${module.leaseCount} 个活动 Lease`);
  return actions;
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
