import type { PermissionPrompt } from "../state/reducer.ts";
import type { RivetTheme } from "./theme.ts";

export function PermissionModal({
  permission,
  theme,
}: {
  permission: PermissionPrompt;
  theme: RivetTheme;
}) {
  return (
    <box
      title="权限请求"
      position="absolute"
      zIndex={20}
      top="22%"
      left="20%"
      width="60%"
      height={14}
      border={true}
      borderStyle="double"
      borderColor={theme.accent}
      backgroundColor={theme.panel}
      flexDirection="column"
      padding={1}
    >
      <text fg={theme.accent} content={permission.permission} />
      <text fg={theme.text} content={`原因：${permission.reason}`} />
      <text fg={theme.text} content={`argv：${permission.argv}`} />
      <text fg={theme.text} content={`cwd：${permission.cwd}`} />
      <text fg={theme.text} content={`路径：${permission.paths}`} />
      <text fg={theme.text} content={`网络：${permission.network}`} />
      <text fg={theme.text} content={`超时：${permission.timeoutSeconds}s`} />
      <text fg={theme.muted} content="A 批准  D 拒绝  Esc 关闭" />
    </box>
  );
}
