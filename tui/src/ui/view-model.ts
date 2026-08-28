import type { RivetState } from "../state/reducer.ts";
import { computeLayout } from "./layout.ts";

export const INSPECTOR_TABS = [
  "Plan",
  "Context",
  "Files",
  "Diff",
  "Verify",
  "Evidence",
  "Modules",
  "Trace",
  "Sessions",
] as const;

export interface ViewOptions {
  width: number;
  height: number;
  noColor: boolean;
}

export function buildViewModel(state: RivetState, options: ViewOptions) {
  return {
    layout: computeLayout(options.width, options.height),
    noColor: options.noColor,
    header: {
      repository: state.repository,
      connection: state.connection,
      model: state.model,
      phase: state.plan.phase,
      transaction: state.transaction,
      tokens: state.budget.tokens,
      costUsd: state.budget.costUsd,
      elapsedMs: state.budget.elapsedMs,
    },
    inspectorTabs: INSPECTOR_TABS,
    permission: state.permission,
    timeline: state.timeline,
    error: state.error,
  };
}
