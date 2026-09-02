import type { ReactNode } from "react";

import type { RivetState } from "../state/reducer.ts";
import type { LayoutDecision } from "../ui/layout.ts";
import type { RivetTheme } from "./theme.ts";

export function WelcomeScreen({
  state,
  layout,
  theme,
  composer,
}: {
  state: RivetState;
  layout: LayoutDecision;
  theme: RivetTheme;
  composer: ReactNode;
}) {
  const repository = displayRepository(state.repository);
  const branch = state.branch ? ` · ${state.branch}` : "";
  const connection = connectionMark(state.connection);

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
          {layout.mode === "minimal" ? null : (
            <text
              fg={theme.textMuted}
              content="Tab · / · @"
            />
          )}
        </box>
      </box>
      <box height={1} paddingX={1} flexDirection="row" justifyContent="space-between">
        <text
          fg={theme.textMuted}
          content={`${repository}${branch}`}
        />
        <text
          fg={
            state.connection === "ready"
              ? theme.success
              : state.connection === "crashed"
                ? theme.danger
                : theme.textMuted
          }
          content={connection}
        />
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

function connectionMark(connection: RivetState["connection"]): string {
  if (connection === "ready") return "●";
  if (connection === "crashed") return "×";
  return "◌";
}
