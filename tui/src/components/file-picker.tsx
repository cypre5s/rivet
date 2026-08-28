import { windowedOptions } from "../ui/windowed-options.ts";
import type { RivetTheme } from "./theme.ts";

export function FilePicker({
  query,
  files,
  selectedIndex,
  loading,
  selectedPaths,
  compact,
  viewportHeight,
  theme,
  onQuery,
  onSelect,
  onHover,
}: {
  query: string;
  files: string[];
  selectedIndex: number;
  loading: boolean;
  selectedPaths: string[];
  compact: boolean;
  viewportHeight: number;
  theme: RivetTheme;
  onQuery(value: string): void;
  onSelect(path: string): void;
  onHover(index: number): void;
}) {
  const height = Math.max(8, Math.min(compact ? 15 : 20, viewportHeight));
  const visible = windowedOptions(
    rankFilePaths(files, query, selectedPaths),
    selectedIndex,
    Math.max(2, Math.min(compact ? 8 : 13, height - 5)),
  );
  return (
    <box
      position="absolute"
      zIndex={32}
      top={viewportHeight <= height + 2 ? 0 : compact ? "8%" : "14%"}
      left={compact ? "2%" : "15%"}
      width={compact ? "96%" : "70%"}
      height={height}
      backgroundColor={theme.surface}
      border={true}
      borderColor={theme.border}
      flexDirection="column"
      padding={1}
      gap={1}
    >
      <box height={1} flexDirection="row">
        <text fg={theme.accent} content="@  " />
        <input
          value={query}
          placeholder="搜索仓库内文件"
          focused={true}
          flexGrow={1}
          maxLength={512}
          backgroundColor={theme.surface}
          focusedBackgroundColor={theme.surface}
          textColor={theme.textPrimary}
          placeholderColor={theme.textMuted}
          cursorColor={theme.accent}
          onInput={onQuery}
        />
      </box>
      <box flexGrow={1} flexDirection="column">
        {loading ? <text fg={theme.textMuted} content="◌ 正在加载文件清单…" /> : null}
        {!loading && visible.items.length === 0 ? (
          <text fg={theme.textMuted} content="没有匹配的仓库内文件" />
        ) : null}
        {visible.items.map((path, index) => {
          const absoluteIndex = visible.startIndex + index;
          const selected = absoluteIndex === selectedIndex;
          const marker = selectedPaths.includes(path) ? "✓" : fileMarker(path);
          return (
            <box
              key={path}
              height={1}
              flexDirection="row"
              backgroundColor={selected ? theme.selection : theme.surface}
              onMouseOver={() => onHover(absoluteIndex)}
              onMouseDown={() => onSelect(path)}
            >
              <text
                fg={selected ? theme.accent : theme.textMuted}
                content={`${selected ? "›" : " "} ${marker} `}
              />
              <text fg={theme.textPrimary} content={path} />
            </box>
          );
        })}
      </box>
      <text
        fg={theme.textMuted}
        content="仅显示 Git 可见路径 · Enter 加入 · Shift+Enter 连续选择 · Esc 关闭"
      />
    </box>
  );
}

export function rankFilePaths(
  files: string[],
  query: string,
  selectedPaths: string[] = [],
): string[] {
  const normalized = query.trim().toLocaleLowerCase();
  const selected = new Set(selectedPaths);
  return files
    .map((path, index) => ({
      path,
      index,
      score: fileScore(path, normalized) + (selected.has(path) ? 50 : 0),
    }))
    .filter((item) => item.score >= 0)
    .sort((left, right) => right.score - left.score || left.index - right.index)
    .map((item) => item.path);
}

function fileScore(path: string, query: string): number {
  if (!query) return 0;
  const normalized = path.toLocaleLowerCase();
  const name = normalized.split("/").at(-1) ?? normalized;
  if (normalized === query || name === query) return 1_000;
  if (name.startsWith(`${query}.`)) return 900 - name.length;
  if (name.startsWith(query)) return 800;
  if (normalized.includes(query)) return 600 - normalized.indexOf(query);
  let queryIndex = 0;
  for (const character of normalized) {
    if (character === query[queryIndex]) queryIndex++;
  }
  return queryIndex === query.length ? 300 : -1;
}

function fileMarker(path: string): string {
  const suffix = path.split(".").at(-1)?.toLocaleLowerCase();
  if (["py", "pyi"].includes(suffix ?? "")) return "PY";
  if (["ts", "tsx", "js", "jsx"].includes(suffix ?? "")) return "TS";
  if (["md", "txt", "rst"].includes(suffix ?? "")) return "TX";
  if (["json", "toml", "yaml", "yml"].includes(suffix ?? "")) return "CF";
  return "· ";
}
