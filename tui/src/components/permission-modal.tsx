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
  const readScope = renderList(permission.readScope);
  const newPathSet = new Set(permission.allowedNewPaths);
  const writeScope = renderList(
    permission.writeScope.filter((path) => !newPathSet.has(path)),
  );
  const newPaths = renderList(permission.allowedNewPaths);
  const expected = renderList(permission.expectedBehaviors);
  const acceptance = renderCommands(permission.acceptanceCommands);
  const regression = renderCommands(permission.regressionCommands);
  return (
    <box
      position="absolute"
      zIndex={20}
      top={viewportHeight <= 13 ? 0 : "24%"}
      left={compact ? "2%" : "12%"}
      width={compact ? "96%" : "76%"}
      height={Math.min(13, viewportHeight)}
      border={true}
      borderColor={theme.warning}
      backgroundColor={theme.surface}
      flexDirection="column"
      padding={1}
    >
      <text fg={theme.warning} content="! 确认 FIX" />
      <text fg={theme.textPrimary} content={`目标  ${permission.goal}`} />
      <text fg={theme.textSecondary} content={`只读  ${readScope}`} />
      <text fg={theme.textSecondary} content={`修改  ${writeScope}`} />
      <text fg={theme.textSecondary} content={`新建  ${newPaths}`} />
      <text fg={theme.textSecondary} content={`预期  ${expected}`} />
      <text fg={theme.textSecondary} content={`验收  ${acceptance}`} />
      <text fg={theme.textSecondary} content={`回归  ${regression}`} />
      <text
        fg={theme.textMuted}
        content={
          compact
            ? "隔离修改 · 验证后仍需 Apply"
            : "影响  仅修改隔离 Worktree；验证通过后仍需显式 Apply"
        }
      />
      <text fg={theme.textMuted} content="A 允许 · D 拒绝" />
    </box>
  );
}

function renderCommands(commands: string[][]): string {
  if (commands.length === 0) return "无";
  return commands.map((command) => command.join(" ")).join(" · ");
}

function renderList(values: string[]): string {
  return values.length === 0 ? "无" : values.join(", ");
}
