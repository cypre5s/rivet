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
  const writeScope = renderList(permission.writeScope);
  const newPaths = renderList(permission.allowedNewPaths);
  const forbiddenPaths = renderList(permission.forbiddenPaths);
  const expected = renderList(permission.expectedBehaviors);
  const acceptance = renderCommands(permission.acceptanceCommands);
  const regression = renderCommands(permission.regressionCommands);
  const budgets = renderBudgets(permission);
  const acceptanceBinding = permission.acceptanceSha256
    .replace(/^sha256:/, "")
    .slice(0, 8);
  const baseBinding = permission.baseCommit.slice(0, 8);
  return (
    <box
      position="absolute"
      zIndex={20}
      top={viewportHeight <= 14 ? 0 : "22%"}
      left={compact ? "2%" : "20%"}
      width={compact ? "96%" : "60%"}
      height={Math.min(compact ? 16 : 18, viewportHeight)}
      border={true}
      borderColor={theme.warning}
      backgroundColor={theme.surface}
      flexDirection="column"
      padding={1}
    >
      <text
        fg={theme.warning}
        content={
          compact
            ? `! ${permission.permission}`
            : `! ${permission.permission} · ${permission.reason}`
        }
      />
      <text fg={theme.textPrimary} content={`Goal  ${permission.goal}`} />
      <text fg={theme.textSecondary} content={`Read  ${readScope}`} />
      <text fg={theme.textSecondary} content={`Write  ${writeScope}`} />
      <text fg={theme.textSecondary} content={`New  ${newPaths}`} />
      <text fg={theme.textSecondary} content={`Forbidden  ${forbiddenPaths}`} />
      <text fg={theme.textSecondary} content={`Expected  ${expected}`} />
      <text
        fg={theme.textSecondary}
        content={`调查  ${permission.investigation}`}
      />
      <text
        fg={theme.textSecondary}
        content={`${compact ? "A" : "Acceptance"}  ${acceptance}`}
      />
      <text
        fg={theme.textSecondary}
        content={`${compact ? "R" : "Regression"}  ${regression}`}
      />
      <text fg={theme.textSecondary} content={`Budgets  ${budgets}`} />
      <text
        fg={theme.textSecondary}
        content={
          compact
            ? `确认  +--yes · ${acceptanceBinding}/${baseBinding}`
            : `确认命令  ${permission.argv}`
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

function renderBudgets(permission: PermissionPrompt): string {
  const budget = permission.budgets;
  const cost = budget.maxCostUsd === null ? "无" : String(budget.maxCostUsd);
  return `${budget.maxWallSeconds}s · ${budget.maxToolCalls} tools · ${budget.maxTokens} tokens · $${cost}`;
}
