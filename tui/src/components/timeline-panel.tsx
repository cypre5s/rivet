import { SyntaxStyle } from "@opentui/core";
import { useEffect, useMemo } from "react";

import type { RivetState } from "../state/reducer.ts";
import type { RivetTheme } from "./theme.ts";

export function TimelinePanel({
  state,
  theme,
  running,
}: {
  state: RivetState;
  theme: RivetTheme;
  running: boolean;
}) {
  const visible = state.timeline.slice(-120);
  const syntaxStyle = useMemo(
    () =>
      SyntaxStyle.fromStyles({
        default: { fg: theme.textPrimary },
        heading: { fg: theme.accent, bold: true },
        code: { fg: theme.accent },
        link: { fg: theme.accent, underline: true },
      }),
    [theme.accent, theme.textPrimary],
  );
  useEffect(() => () => syntaxStyle.destroy(), [syntaxStyle]);

  return (
    <box
      backgroundColor={theme.background}
      flexGrow={1}
      flexDirection="column"
      paddingX={2}
      paddingY={1}
    >
      <scrollbox flexGrow={1} focused={false} stickyScroll={true} stickyStart="bottom">
        {visible.length === 0 ? (
          <text fg={theme.textMuted} content="提交任务后，执行步骤会显示在这里。" />
        ) : (
          visible.map((item) => {
            if (item.kind === "user") {
              return (
                <box key={item.eventId} flexDirection="column" marginBottom={1}>
                  <text fg={theme.textMuted} content="YOU" />
                  <text fg={theme.textPrimary} content={item.title} />
                </box>
              );
            }
            if (item.kind === "assistant") {
              return (
                <box key={item.eventId} flexDirection="column" marginBottom={1}>
                  <text fg={theme.accent} content="RIVET" />
                  <markdown
                    content={item.detail || item.title}
                    syntaxStyle={syntaxStyle}
                    conceal={true}
                    streaming={running && item === visible.at(-1)}
                  />
                </box>
              );
            }
            return (
              <box key={item.eventId} flexDirection="row" minHeight={1}>
                <text
                  width={2}
                  fg={statusColor(item.status, theme)}
                  content={statusIcon(item.status)}
                />
                <text fg={theme.textSecondary} content={item.title} />
                {item.detail ? (
                  <text fg={theme.textMuted} content={`  ${item.detail}`} />
                ) : null}
              </box>
            );
          })
        )}
      </scrollbox>
      {state.error === null ? null : (
        <text
          fg={theme.danger}
          content={`${state.error}${state.connection === "crashed" ? "  Ctrl+Shift+R 恢复 Worker" : ""}`}
        />
      )}
    </box>
  );
}

function statusIcon(status: RivetState["timeline"][number]["status"]): string {
  if (status === "running") return "◌";
  if (status === "success") return "✓";
  if (status === "failed") return "×";
  if (status === "blocked") return "!";
  return "−";
}

function statusColor(
  status: RivetState["timeline"][number]["status"],
  theme: RivetTheme,
): string {
  if (status === "running") return theme.accent;
  if (status === "success") return theme.success;
  if (status === "failed") return theme.danger;
  if (status === "blocked") return theme.warning;
  return theme.textMuted;
}
