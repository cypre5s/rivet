import type {
  CommandContext,
  CommandDescriptor,
} from "../ui/command-registry.ts";
import { commandAvailability } from "../ui/command-registry.ts";
import type { CommandSearchResult } from "../ui/command-search.ts";
import { windowedOptions } from "../ui/windowed-options.ts";
import type { RivetTheme } from "./theme.ts";

export function SlashMenu({
  query,
  results,
  selectedIndex,
  context,
  compact,
  viewportHeight,
  theme,
  onSelect,
  onHover,
}: {
  query: string;
  results: CommandSearchResult[];
  selectedIndex: number;
  context: CommandContext;
  compact: boolean;
  viewportHeight: number;
  theme: RivetTheme;
  onSelect(command: CommandDescriptor): void;
  onHover(index: number): void;
}) {
  const selected = results[selectedIndex]?.command ?? null;
  const availability =
    selected === null ? null : commandAvailability(selected, context);
  const height = Math.max(8, Math.min(14, viewportHeight, results.length + 7));
  const visible = windowedOptions(
    results,
    selectedIndex,
    Math.max(2, Math.min(8, height - 6)),
  );

  return (
    <box
      position="absolute"
      zIndex={30}
      top={viewportHeight <= height + 2 ? 0 : "22%"}
      left={compact ? "2%" : "18%"}
      width={compact ? "96%" : "64%"}
      height={height}
      backgroundColor={theme.surface}
      border={true}
      borderColor={theme.border}
      flexDirection="column"
      padding={1}
      gap={1}
    >
      <text fg={theme.accent} content={`/${query}`} />
      <box flexGrow={1} flexDirection="column">
        {visible.items.length === 0 ? (
          <text fg={theme.textMuted} content="无匹配" />
        ) : (
          visible.items.map(({ command }, index) => {
            const commandState = commandAvailability(command, context);
            const absoluteIndex = visible.startIndex + index;
            const selectedRow = absoluteIndex === selectedIndex;
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
                  fg={selectedRow ? theme.accent : theme.textMuted}
                  content={`${selectedRow ? "›" : " "} `}
                  width={2}
                />
                <text
                  fg={commandState.available ? theme.textPrimary : theme.textMuted}
                  content={`/${command.name}`}
                  width={13}
                />
                <text
                  fg={commandState.available ? theme.textSecondary : theme.textMuted}
                  content={command.title}
                  flexGrow={1}
                />
              </box>
            );
          })
        )}
      </box>
      <text
        fg={availability?.available === false ? theme.warning : theme.textSecondary}
        content={
          selected === null
            ? ""
            : availability?.reason ?? `${selected.description} · ${selected.usage}`
        }
      />
    </box>
  );
}
