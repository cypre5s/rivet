import type {
  CommandContext,
  CommandDescriptor,
} from "../ui/command-registry.ts";
import { commandAvailability } from "../ui/command-registry.ts";
import type { CommandSearchResult } from "../ui/command-search.ts";
import { windowedOptions } from "../ui/windowed-options.ts";
import type { RivetTheme } from "./theme.ts";

export function CommandPalette({
  variant,
  query,
  results,
  selectedIndex,
  context,
  compact,
  viewportHeight,
  theme,
  onQuery,
  onSelect,
  onHover,
}: {
  variant: "palette" | "slash";
  query: string;
  results: CommandSearchResult[];
  selectedIndex: number;
  context: CommandContext;
  compact: boolean;
  viewportHeight: number;
  theme: RivetTheme;
  onQuery(value: string): void;
  onSelect(command: CommandDescriptor): void;
  onHover(index: number): void;
}) {
  const selected = results[selectedIndex]?.command ?? null;
  const availability =
    selected === null ? null : commandAvailability(selected, context);
  const height = Math.max(
    8,
    Math.min(compact ? 14 : 19, viewportHeight),
  );
  const visible = windowedOptions(
    results,
    selectedIndex,
    Math.max(2, Math.min(compact ? 7 : 10, height - 7)),
  );
  const width = compact ? "96%" : "70%";

  return (
    <box
      position="absolute"
      zIndex={30}
      top={
        viewportHeight <= height + 2
          ? 0
          : variant === "slash"
            ? "22%"
            : "14%"
      }
      left={compact ? "2%" : "15%"}
      width={width}
      height={height}
      backgroundColor={theme.surface}
      border={true}
      borderColor={theme.border}
      flexDirection="column"
      padding={1}
      gap={1}
    >
      <box height={1} flexDirection="row">
        <text fg={theme.accent} content={variant === "slash" ? "/" : "⌘  "} />
        {variant === "palette" ? (
          <input
            value={query}
            placeholder="搜索命令、面板和资源"
            focused={true}
            flexGrow={1}
            maxLength={256}
            backgroundColor={theme.surface}
            focusedBackgroundColor={theme.surface}
            textColor={theme.textPrimary}
            placeholderColor={theme.textMuted}
            cursorColor={theme.accent}
            onInput={onQuery}
          />
        ) : (
          <text
            fg={query ? theme.textPrimary : theme.textMuted}
            content={query || "全部操作"}
          />
        )}
      </box>
      <box flexGrow={1} flexDirection="column">
        {visible.items.length === 0 ? (
          <text fg={theme.textMuted} content="没有匹配的操作" />
        ) : (
          visible.items.map(({ command }, index) => {
            const commandState = commandAvailability(command, context);
            const absoluteIndex = visible.startIndex + index;
            const selectedRow = absoluteIndex === selectedIndex;
            const marker = command.dangerous ? "!" : commandState.available ? "·" : "×";
            return (
              <box
                key={command.id}
                height={1}
                flexDirection="row"
                backgroundColor={selectedRow ? theme.selection : theme.surface}
                onMouseOver={() => onHover(absoluteIndex)}
                onMouseDown={() => onSelect(command)}
              >
                <text
                  fg={
                    commandState.available
                      ? selectedRow
                        ? theme.accent
                        : theme.textPrimary
                      : theme.textMuted
                  }
                  content={`${selectedRow ? "›" : " "} ${marker} /${command.name}`}
                  width={22}
                />
                <text
                  fg={commandState.available ? theme.textSecondary : theme.textMuted}
                  content={command.title}
                  flexGrow={1}
                />
                <text
                  fg={theme.textMuted}
                  content={command.shortcut ?? command.category}
                />
              </box>
            );
          })
        )}
      </box>
      <box height={compact ? 2 : 3} flexDirection="column">
        <text
          fg={availability?.available === false ? theme.warning : theme.textSecondary}
          content={
            selected === null
              ? "输入关键词搜索"
              : availability?.reason ?? selected.description
          }
        />
        {compact || selected === null ? null : (
          <text
            fg={theme.textMuted}
            content={`${selected.usage}  ·  ↑↓ 选择  Tab 补全  Enter 执行  Esc 关闭`}
          />
        )}
      </box>
    </box>
  );
}
