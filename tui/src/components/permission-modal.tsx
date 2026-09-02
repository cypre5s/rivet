import type { PermissionPrompt } from "../state/reducer.ts";
import type { RivetTheme } from "./theme.ts";

export function PermissionModal({
  permission,
  compact,
  viewportHeight,
  theme,
}: {
  permission: PermissionPrompt;
  compact: boolean;
  viewportHeight: number;
  theme: RivetTheme;
}) {
  return (
    <box
      position="absolute"
      zIndex={20}
      top={viewportHeight <= 14 ? 0 : "22%"}
      left={compact ? "2%" : "20%"}
      width={compact ? "96%" : "60%"}
      height={Math.min(compact ? 12 : 11, viewportHeight)}
      border={true}
      borderColor={theme.warning}
      backgroundColor={theme.surface}
      flexDirection="column"
      padding={1}
    >
      <text fg={theme.warning} content={`! ${permission.permission}`} />
      <text fg={theme.textPrimary} content={permission.reason} />
      <text fg={theme.textSecondary} content={`路径  ${permission.paths}`} />
      <text fg={theme.textSecondary} content={`命令  ${permission.argv}`} />
      <text fg={theme.textSecondary} content={`目录  ${permission.cwd}`} />
      <text
        fg={theme.textSecondary}
        content={`网络  ${permission.network} · ${permission.timeoutSeconds}s`}
      />
      <text fg={theme.textMuted} content="A 允许 · D 拒绝" />
    </box>
  );
}
