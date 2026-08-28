import type { RivetTheme } from "./theme.ts";

export function LeaderHelp({ theme }: { theme: RivetTheme }) {
  return (
    <box
      position="absolute"
      zIndex={35}
      bottom={2}
      left="8%"
      width="84%"
      height={5}
      backgroundColor={theme.surfaceHover}
      paddingX={1}
      flexDirection="column"
    >
      <text fg={theme.accent} content="Leader" />
      <text fg={theme.textSecondary} content="p Plan  ·  f Fix  ·  v Verify  ·  d Diff" />
      <text fg={theme.textSecondary} content="e Evidence  ·  t Trace  ·  m Modules" />
      <text fg={theme.textSecondary} content="c Context  ·  s Sessions  ·  h Help  ·  q Quit" />
    </box>
  );
}
