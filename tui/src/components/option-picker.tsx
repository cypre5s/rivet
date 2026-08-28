import { windowedOptions } from "../ui/windowed-options.ts";
import type { RivetTheme } from "./theme.ts";

export interface PickerOption {
  id: string;
  title: string;
  description?: string;
  marker?: string;
  available?: boolean;
}

export function OptionPicker({
  title,
  placeholder,
  query,
  options,
  selectedIndex,
  compact,
  viewportHeight,
  theme,
  onQuery,
  onSelect,
  onHover,
}: {
  title: string;
  placeholder: string;
  query: string;
  options: PickerOption[];
  selectedIndex: number;
  compact: boolean;
  viewportHeight: number;
  theme: RivetTheme;
  onQuery(value: string): void;
  onSelect(option: PickerOption): void;
  onHover(index: number): void;
}) {
  const height = Math.max(8, Math.min(compact ? 14 : 18, viewportHeight));
  const visibleCount = Math.max(2, Math.min(compact ? 7 : 10, height - 6));
  const visible = windowedOptions(options, selectedIndex, visibleCount);
  return (
    <box
      position="absolute"
      zIndex={34}
      top={viewportHeight <= height + 2 ? 0 : compact ? "10%" : "18%"}
      left={compact ? "2%" : "20%"}
      width={compact ? "96%" : "60%"}
      height={height}
      backgroundColor={theme.surface}
      border={true}
      borderColor={theme.border}
      flexDirection="column"
      padding={1}
      gap={1}
    >
      <text fg={theme.accent} content={title} />
      <input
        value={query}
        placeholder={placeholder}
        focused={true}
        maxLength={256}
        backgroundColor={theme.surfaceHover}
        focusedBackgroundColor={theme.surfaceHover}
        textColor={theme.textPrimary}
        placeholderColor={theme.textMuted}
        cursorColor={theme.accent}
        onInput={onQuery}
      />
      <box flexGrow={1} flexDirection="column">
        {visible.items.length === 0 ? (
          <text fg={theme.textMuted} content="没有匹配结果" />
        ) : (
          visible.items.map((option, index) => {
            const absoluteIndex = visible.startIndex + index;
            const selected = absoluteIndex === selectedIndex;
            return (
              <box
                key={option.id}
                minHeight={1}
                backgroundColor={selected ? theme.selection : theme.surface}
                flexDirection="row"
                onMouseOver={() => onHover(absoluteIndex)}
                onMouseDown={() => {
                  if (option.available !== false) onSelect(option);
                }}
              >
                <text
                  width={3}
                  fg={
                    option.available === false
                      ? theme.textMuted
                      : selected
                        ? theme.accent
                        : theme.textMuted
                  }
                  content={
                    option.available === false
                      ? "×"
                      : selected
                        ? "›"
                        : option.marker ?? " "
                  }
                />
                <text
                  fg={
                    option.available === false
                      ? theme.textMuted
                      : theme.textPrimary
                  }
                  content={option.title}
                  flexGrow={1}
                />
                {option.description ? (
                  <text fg={theme.textMuted} content={option.description} />
                ) : null}
              </box>
            );
          })
        )}
      </box>
      <text fg={theme.textMuted} content="↑↓ 选择 · Enter 确认 · Esc 关闭" />
    </box>
  );
}
