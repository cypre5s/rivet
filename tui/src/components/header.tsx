import type { RivetState } from "../state/reducer.ts";
import type { RivetTheme } from "./theme.ts";

export function Header({
  state,
  theme,
}: {
  state: RivetState;
  theme: RivetTheme;
}) {
  const usage = usageLabel(state);
  return (
    <box
      height={1}
      backgroundColor={theme.background}
      paddingX={2}
      flexDirection="row"
      justifyContent="space-between"
    >
      <text fg={theme.accent} content="RIVET" />
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
