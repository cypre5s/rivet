import type { RivetState } from "../state/reducer.ts";
import type { RivetTheme } from "./theme.ts";

export function Header({ state, theme }: { state: RivetState; theme: RivetTheme }) {
  return (
    <box
      height={3}
      border={true}
      borderColor={theme.border}
      backgroundColor={theme.panel}
      paddingX={1}
      flexDirection="row"
      justifyContent="space-between"
    >
      <text fg={theme.accent} content="Rivet" />
      <text
        fg={theme.text}
        content={`模型 ${state.model}  阶段 ${state.plan.phase}  事务 ${state.transaction}`}
      />
      <text
        fg={theme.muted}
        content={`token ${state.budget.tokens}  $${state.budget.costUsd.toFixed(4)}  ${state.budget.elapsedMs}ms`}
      />
    </box>
  );
}
