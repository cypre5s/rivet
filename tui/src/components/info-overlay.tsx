import type { RivetTheme } from "./theme.ts";

export function InfoOverlay({
  title,
  lines,
  compact,
  viewportHeight,
  theme,
}: {
  title: string;
  lines: string[];
  compact: boolean;
  viewportHeight: number;
  theme: RivetTheme;
}) {
  const height = Math.min(
    viewportHeight,
    Math.max(7, Math.min(compact ? 16 : 22, lines.length + 6)),
  );
  return (
    <box
      position="absolute"
      zIndex={33}
      top={viewportHeight <= height + 2 ? 0 : compact ? "8%" : "12%"}
      left={compact ? 0 : "14%"}
      width={compact ? "100%" : "72%"}
      height={height}
      backgroundColor={theme.surface}
      border={true}
      borderColor={theme.border}
      flexDirection="column"
      padding={1}
      gap={1}
    >
      <box height={1} flexDirection="row">
        <text fg={theme.accent} content={title} />
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
