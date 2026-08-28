import type { RivetTheme } from "./theme.ts";

const COMMANDS = [
  "ask 只读问答",
  "plan 冻结计划",
  "fix 隔离修复",
  "verify 确定性验证",
  "diff 查看补丁",
  "apply 显式应用",
];

export function CommandPalette({ theme }: { theme: RivetTheme }) {
  return (
    <box
      title="命令面板"
      position="absolute"
      zIndex={10}
      top="18%"
      left="25%"
      width="50%"
      height={12}
      border={true}
      borderColor={theme.accent}
      backgroundColor={theme.panel}
      flexDirection="column"
      padding={1}
    >
      {COMMANDS.map((command) => (
        <text key={command} fg={theme.text} content={command} />
      ))}
      <text fg={theme.muted} content="Esc 关闭" />
    </box>
  );
}
