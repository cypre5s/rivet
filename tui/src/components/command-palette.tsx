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
    Math.min(compact ? 14 : 19, viewportHeight, results.length + 7),
  );
  const visible = windowedOptions(
    results,
    selectedIndex,
    Math.max(2, Math.min(compact ? 7 : 12, height - 7)),
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
            placeholder="搜索"
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
            content={query}
          />
        )}
      </box>
      <box flexGrow={1} flexDirection="column">
        {visible.items.length === 0 ? (
          <text fg={theme.textMuted} content="无匹配" />
        ) : (
          visible.items.map(({ command }, index) => {
            const commandState = commandAvailability(command, context);
            const absoluteIndex = visible.startIndex + index;
            const selectedRow = absoluteIndex === selectedIndex;
            const resource = command.id.startsWith("resource.");
            const marker = !commandState.available
              ? "×"
              : command.dangerous
                ? "!"
                : "✓";
            const markerColor = !commandState.available
              ? theme.danger
              : command.dangerous
                ? theme.warning
                : theme.success;
            const commandColor = commandState.available
              ? selectedRow
                ? theme.accent
                : theme.textPrimary
              : theme.textMuted;
            return (
              <box
                key={command.id}
                height={1}
                flexDirection="row"
                backgroundColor={selectedRow ? theme.selection : theme.surface}
                onMouseOver={() => onHover(absoluteIndex)}
                onMouseDown={() => onSelect(command)}
              >
                {resource ? (
                  <>
                    <text
                      fg={selectedRow ? theme.accent : theme.textMuted}
                      content={`${selectedRow ? "›" : " "} `}
                      width={2}
                    />
                    <text fg={markerColor} content={`${marker} `} width={2} />
                    <text
                      fg={commandColor}
                      content={`/${command.name}`}
                      flexGrow={1}
                    />
                  </>
                ) : (
                  <>
                    <text
                      fg={selectedRow ? theme.accent : theme.textMuted}
                      content={`${selectedRow ? "›" : " "} `}
                      width={2}
                    />
                    <text fg={markerColor} content={`${marker} `} width={2} />
                    <text
                      fg={commandColor}
                      content={`/${command.name}`}
                      width={16}
                    />
                    <text
                      fg={commandState.available ? theme.textSecondary : theme.textMuted}
                      content={command.title}
                      flexGrow={1}
                    />
                  </>
                )}
                <text
                  fg={theme.textMuted}
                  content={resource || compact ? "" : command.shortcut ?? ""}
                />
              </box>
            );
          })
        )}
      </box>
      <box height={1} flexDirection="column">
        <text
          fg={availability?.available === false ? theme.warning : theme.textSecondary}
          content={
            selected === null
              ? ""
              : availability?.reason ??
                (compact
                  ? selected.description
                  : `${selected.description}${selected.argumentKind === "none" ? "" : ` · ${selected.usage}`}`)
          }
        />
      </box>
    </box>
  );
}
