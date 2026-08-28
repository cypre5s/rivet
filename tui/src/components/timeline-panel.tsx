import { SyntaxStyle } from "@opentui/core";
import { useEffect, useMemo } from "react";

import type { RivetState } from "../state/reducer.ts";
import type { RivetTheme } from "./theme.ts";

export function TimelinePanel({
  state,
  theme,
  hiddenBefore,
}: {
  state: RivetState;
  theme: RivetTheme;
  hiddenBefore: number;
}) {
  const visible = state.timeline.filter((item) => item.sequence > hiddenBefore);
  const latestSummary = visible.at(-1)?.summary ?? "等待任务。输入 /help 查看命令。";
  const syntaxStyle = useMemo(
    () =>
      SyntaxStyle.fromStyles({
        default: { fg: theme.text },
        heading: { fg: theme.accent, bold: true },
        code: { fg: theme.accent },
        link: { fg: theme.accent, underline: true },
      }),
    [theme.accent, theme.text],
  );
  useEffect(() => () => syntaxStyle.destroy(), [syntaxStyle]);

  return (
    <box
      title="Chat / Trace"
      border={true}
      borderColor={theme.border}
      backgroundColor={theme.background}
      flexGrow={1}
      flexDirection="column"
      padding={1}
    >
      <scrollbox flexGrow={1} focused={false}>
        {visible.length === 0 ? (
          <text fg={theme.muted} content="暂无 Trace 事件" />
        ) : (
          visible.map((item) => (
            <text
              key={item.eventId}
              fg={theme.text}
              content={`${item.sequence.toString().padStart(4, "0")}  ${item.eventType}  ${item.summary}`}
            />
          ))
        )}
        <markdown
          content={latestSummary}
          syntaxStyle={syntaxStyle}
          conceal={true}
          streaming={false}
        />
      </scrollbox>
      {state.error === null ? null : (
        <text
          fg={theme.error}
          content={`${state.error}${state.connection === "crashed" ? "  Ctrl+R 恢复 Worker" : ""}`}
        />
      )}
    </box>
  );
}
