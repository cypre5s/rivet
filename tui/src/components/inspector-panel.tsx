import type { InspectorTab, RivetState } from "../state/reducer.ts";
import { INSPECTOR_TABS } from "../ui/view-model.ts";
import type { RivetTheme } from "./theme.ts";

export function InspectorPanel({
  state,
  theme,
  activeTab,
}: {
  state: RivetState;
  theme: RivetTheme;
  activeTab: InspectorTab;
}) {
  return (
    <box
      title="Inspector"
      border={true}
      borderColor={theme.border}
      backgroundColor={theme.panel}
      width="30%"
      flexDirection="column"
      padding={1}
    >
      <text
        fg={theme.accent}
        content={INSPECTOR_TABS.map((tab) => (tab === activeTab ? `[${tab}]` : tab)).join(" ")}
      />
      <InspectorContent state={state} theme={theme} activeTab={activeTab} />
    </box>
  );
}

function InspectorContent({
  state,
  theme,
  activeTab,
}: {
  state: RivetState;
  theme: RivetTheme;
  activeTab: InspectorTab;
}) {
  if (activeTab === "Diff") {
    return state.diff.length > 0 ? (
      <diff
        diff={state.diff}
        view="unified"
        wrapMode="char"
        showLineNumbers={true}
        flexGrow={1}
      />
    ) : (
      <text fg={theme.muted} content="暂无 Diff" />
    );
  }
  const content: Record<Exclude<InspectorTab, "Diff">, string> = {
    Plan: `${state.plan.phase}\n${state.plan.summary}`,
    Context:
      state.context.map((item) => `${item.path}\n  ${item.reason}`).join("\n") ||
      "暂无上下文",
    Verify: `状态：${state.verifyStatus}`,
    Evidence: `Evidence：${state.evidenceId}`,
    Modules: state.modules.join("\n") || "暂无激活模块",
  };
  return <text fg={theme.text} content={content[activeTab]} />;
}
