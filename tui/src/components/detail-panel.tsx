import type { RivetState } from "../state/reducer.ts";
import type { PanelName } from "../ui/command-registry.ts";
import type { PanelPresentation } from "../ui/layout.ts";
import type { RivetTheme } from "./theme.ts";

export function DetailPanel({
  panel,
  state,
  selectedContextFiles,
  presentation,
  theme,
}: {
  panel: PanelName;
  state: RivetState;
  selectedContextFiles: string[];
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
        theme={theme}
      />
    </box>
  );
}

function PanelContent({
  panel,
  state,
  selectedContextFiles,
  theme,
}: {
  panel: PanelName;
  state: RivetState;
  selectedContextFiles: string[];
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
    return state.modules.length ? (
      <scrollbox flexGrow={1} focused={true}>
        {state.modules.map((module) => (
          <text key={module} fg={theme.textSecondary} content={`● ACTIVE  ${module}`} />
        ))}
      </scrollbox>
    ) : (
      <EmptyState text="没有激活可选模块" action="模块会在需要时自动激活" theme={theme} />
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
