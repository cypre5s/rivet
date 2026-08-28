import type { ReactNode } from "react";

import type { RivetState } from "../state/reducer.ts";
import type { LayoutDecision } from "../ui/layout.ts";
import type { RivetTheme } from "./theme.ts";

export function WelcomeScreen({
  state,
  layout,
  theme,
  tip,
  composer,
}: {
  state: RivetState;
  layout: LayoutDecision;
  theme: RivetTheme;
  tip: string;
  composer: ReactNode;
}) {
  const repository = displayRepository(state.repository);
  const branch = state.branch ? ` · ${state.branch}` : "";
  const connection = connectionLabel(state.connection);

  return (
    <box flexGrow={1} flexDirection="column" backgroundColor={theme.background}>
      <box
        flexGrow={1}
        alignItems="center"
        justifyContent="center"
        flexDirection="column"
        paddingX={layout.mode === "minimal" ? 1 : 2}
        gap={layout.logoSize === "large" ? 2 : 1}
      >
        <RivetLogo size={layout.logoSize} theme={theme} />
        <box width={layout.contentWidth} flexDirection="column" gap={1}>
          {composer}
          {layout.showShortcutHints ? (
            <text
              fg={theme.textMuted}
              content="tab 模式    ctrl+p 命令    / 操作    @ 文件"
            />
          ) : null}
          {layout.showTip ? (
            <text fg={theme.textSecondary} content={`● Tip  ${tip}`} />
          ) : null}
        </box>
      </box>
      <box height={1} paddingX={1} flexDirection="row" justifyContent="space-between">
        <text
          fg={theme.textMuted}
          content={`${repository}${branch}`}
        />
        <text fg={theme.textMuted} content={`${connection} · v0.1.0`} />
      </box>
    </box>
  );
}

function RivetLogo({
  size,
  theme,
}: {
  size: LayoutDecision["logoSize"];
  theme: RivetTheme;
}) {
  if (size === "text") {
    return <text fg={theme.accent} content="R I V E T" />;
  }
  return (
    <ascii-font
      text="RIVET"
      font={size === "large" ? "block" : "tiny"}
      color={theme.accent}
      backgroundColor={theme.background}
      selectable={false}
    />
  );
}

export function displayRepository(repository: string): string {
  if (!repository || repository === "未连接") return "当前仓库";
  const home = process.env.HOME;
  if (home && repository.startsWith(`${home}/`)) {
    return `~/${repository.slice(home.length + 1)}`;
  }
  const homeMatch = /^\/home\/[^/]+\/(.+)$/.exec(repository);
  if (homeMatch?.[1]) return `~/${homeMatch[1]}`;
  return repository;
}

function connectionLabel(connection: RivetState["connection"]): string {
  if (connection === "ready") return "已连接";
  if (connection === "crashed") return "连接中断";
  return "连接中";
}
