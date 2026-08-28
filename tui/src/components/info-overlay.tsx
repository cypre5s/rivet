import type { RivetTheme } from "./theme.ts";

export function InfoOverlay({
  title,
  lines,
  compact,
  theme,
}: {
  title: string;
  lines: string[];
  compact: boolean;
  theme: RivetTheme;
}) {
  return (
    <box
      position="absolute"
      zIndex={33}
      top={compact ? 0 : "12%"}
      left={compact ? 0 : "14%"}
      width={compact ? "100%" : "72%"}
      height={compact ? "100%" : "76%"}
      backgroundColor={theme.surface}
      border={true}
      borderColor={theme.border}
      flexDirection="column"
      padding={1}
      gap={1}
    >
      <box height={1} flexDirection="row" justifyContent="space-between">
        <text fg={theme.accent} content={title} />
        <text fg={theme.textMuted} content="Esc 关闭" />
      </box>
      <scrollbox flexGrow={1} focused={true}>
        {lines.map((line, index) => (
          <text
            key={`${index}-${line}`}
            fg={line.startsWith("/") ? theme.textPrimary : theme.textSecondary}
            content={line}
          />
        ))}
      </scrollbox>
    </box>
  );
}
