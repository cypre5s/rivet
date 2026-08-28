import type { RivetTheme } from "./theme.ts";

export function InputBar({
  value,
  theme,
  focused,
  onInput,
  onSubmit,
}: {
  value: string;
  theme: RivetTheme;
  focused: boolean;
  onInput(value: string): void;
  onSubmit(value: string): void;
}) {
  return (
    <box
      height={3}
      border={true}
      borderColor={theme.border}
      backgroundColor={theme.panel}
      paddingX={1}
      flexDirection="row"
    >
      <text fg={theme.accent} content="> " />
      <input
        value={value}
        placeholder="输入任务或 /command"
        focused={focused}
        flexGrow={1}
        textColor={theme.text}
        onInput={onInput}
        onKeyDown={(event) => {
          if (event.name === "return") {
            event.preventDefault();
            onSubmit(value);
          }
        }}
      />
    </box>
  );
}
