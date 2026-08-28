import type { RivetState } from "../state/reducer.ts";
import type { RivetTheme } from "./theme.ts";

export function RepositoryPanel({
  state,
  theme,
}: {
  state: RivetState;
  theme: RivetTheme;
}) {
  const files = state.fileTree.length > 0 ? state.fileTree : ["（等待仓库清单）"];
  const context =
    state.context.length > 0
      ? state.context.map((item) => `${item.path}  ← ${item.reason}`)
      : ["（尚未选择上下文）"];
  return (
    <box
      title="Repository / Context"
      border={true}
      borderColor={theme.border}
      backgroundColor={theme.panel}
      width="24%"
      flexDirection="column"
      padding={1}
    >
      <text fg={theme.muted} content={state.repository} />
      <text fg={theme.accent} content="文件" />
      {files.slice(0, 12).map((path) => (
        <text key={`file-${path}`} fg={theme.text} content={`  ${path}`} />
      ))}
      <text fg={theme.accent} content="上下文来源" />
      {context.slice(0, 8).map((item, index) => (
        <text key={`context-${index}`} fg={theme.text} content={`  ${item}`} />
      ))}
    </box>
  );
}
