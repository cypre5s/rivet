import type { RivetTheme } from "./theme.ts";

export function LeaderHelp({ theme }: { theme: RivetTheme }) {
  return (
    <box
      position="absolute"
      zIndex={35}
      bottom={2}
      left="8%"
      width="84%"
      height={4}
      backgroundColor={theme.surfaceHover}
      paddingX={1}
      flexDirection="column"
    >
      <text fg={theme.textSecondary} content="p 计划 · f 修复 · v 验证 · d 修改" />
      <text fg={theme.textSecondary} content="e 证据 · t 轨迹 · m 能力" />
      <text fg={theme.textSecondary} content="c 上下文 · s 会话 · h 帮助 · q 退出" />
    </box>
  );
}
