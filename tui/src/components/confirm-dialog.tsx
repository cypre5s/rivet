import type { RivetTheme } from "./theme.ts";

export function ConfirmDialog({
  title,
  description,
  impact,
  compact,
  viewportHeight,
  theme,
}: {
  title: string;
  description: string;
  impact: string;
  compact: boolean;
  viewportHeight: number;
  theme: RivetTheme;
}) {
  return (
    <box
      position="absolute"
      zIndex={45}
      top={viewportHeight < 12 ? 0 : "20%"}
      left={compact ? "2%" : "20%"}
      width={compact ? "96%" : "60%"}
      height={Math.min(9, viewportHeight)}
      backgroundColor={theme.surface}
      border={true}
      borderColor={theme.warning}
      flexDirection="column"
      padding={1}
      gap={1}
    >
      <text fg={theme.warning} content={`! ${title}`} />
      <text fg={theme.textPrimary} content={description} />
      <text fg={theme.textSecondary} content={`范围  ${impact}`} />
      <text fg={theme.textMuted} content="Y 确认 · N 取消" />
    </box>
  );
}
