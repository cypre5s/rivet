import type { RivetState } from "../state/reducer.ts";
import type { WorkMode } from "../ui/command-registry.ts";
import type { RivetTheme } from "./theme.ts";

export function Header({
  state,
  mode,
  running,
  theme,
}: {
  state: RivetState;
  mode: WorkMode;
  running: boolean;
  theme: RivetTheme;
}) {
  const transaction = state.transaction === "无" ? "" : `transaction ${state.transaction}`;
  const usage = usageLabel(state);
  return (
    <box
      height={2}
      backgroundColor={theme.background}
      paddingX={2}
      paddingTop={1}
      flexDirection="row"
      justifyContent="space-between"
    >
      <text
        fg={running ? theme.warning : theme.accent}
        content={`Rivet · ${mode} · ${running ? "RUNNING" : state.plan.phase}`}
      />
      <text fg={theme.textSecondary} content={transaction} />
      <text fg={theme.textMuted} content={usage} />
    </box>
  );
}

function usageLabel(state: RivetState): string {
  const parts: string[] = [];
  if (state.budget.tokens > 0) {
    parts.push(`${formatTokens(state.budget.tokens)} tok`);
  }
  if (state.budget.costUsd > 0) parts.push(`$${state.budget.costUsd.toFixed(3)}`);
  if (state.budget.elapsedMs > 0) {
    parts.push(`${(state.budget.elapsedMs / 1_000).toFixed(1)}s`);
  }
  return parts.join(" · ");
}

function formatTokens(value: number): string {
  return value >= 1_000 ? `${(value / 1_000).toFixed(1)}k` : value.toString();
}
