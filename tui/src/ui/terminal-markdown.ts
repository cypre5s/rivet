export type TerminalMarkdownKind =
  | "blank"
  | "code"
  | "heading"
  | "quote"
  | "text";

export interface TerminalMarkdownLine {
  kind: TerminalMarkdownKind;
  content: string;
}

const FENCE = /^\s{0,3}(`{3,}|~{3,})/;
const HEADING = /^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$/;
const BULLET = /^(\s*)[-+*]\s+(.+)$/;
const ORDERED = /^(\s*)(\d+)[.)]\s+(.+)$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const RULE = /^\s{0,3}(?:[-*_]\s*){3,}$/;
const TABLE_SEPARATOR = /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/;

/** 把常见 Markdown 降级为稳定、无原始控制符的终端行。 */
export function terminalMarkdown(source: string): TerminalMarkdownLine[] {
  const output: TerminalMarkdownLine[] = [];
  let fence: string | null = null;
  for (const rawLine of source.replaceAll("\r\n", "\n").split("\n")) {
    const fenceMatch = FENCE.exec(rawLine);
    if (fenceMatch?.[1] !== undefined) {
      const marker = fenceMatch[1].at(0);
      if (marker === undefined) continue;
      if (fence === null) fence = marker;
      else if (fence === marker) fence = null;
      continue;
    }
    if (fence !== null) {
      output.push({ kind: "code", content: rawLine || " " });
      continue;
    }
    if (!rawLine.trim()) {
      appendBlank(output);
      continue;
    }
    const heading = HEADING.exec(rawLine);
    if (heading?.[1] !== undefined) {
      output.push({ kind: "heading", content: inlineMarkdown(heading[1]) });
      continue;
    }
    if (RULE.test(rawLine)) {
      output.push({ kind: "text", content: "────────────────" });
      continue;
    }
    if (TABLE_SEPARATOR.test(rawLine)) continue;
    if (isTableRow(rawLine)) {
      output.push({ kind: "text", content: tableRow(rawLine) });
      continue;
    }
    const quote = QUOTE.exec(rawLine);
    if (quote?.[1] !== undefined) {
      output.push({ kind: "quote", content: `│ ${inlineMarkdown(quote[1])}` });
      continue;
    }
    const bullet = BULLET.exec(rawLine);
    if (bullet?.[2] !== undefined) {
      output.push({
        kind: "text",
        content: `${bullet[1]}• ${taskMarker(inlineMarkdown(bullet[2]))}`,
      });
      continue;
    }
    const ordered = ORDERED.exec(rawLine);
    if (ordered?.[2] !== undefined && ordered[3] !== undefined) {
      output.push({
        kind: "text",
        content: `${ordered[1]}${ordered[2]}. ${inlineMarkdown(ordered[3])}`,
      });
      continue;
    }
    output.push({ kind: "text", content: inlineMarkdown(rawLine) });
  }
  while (output.at(-1)?.kind === "blank") output.pop();
  return output;
}

function appendBlank(output: TerminalMarkdownLine[]): void {
  if (output.length > 0 && output.at(-1)?.kind !== "blank") {
    output.push({ kind: "blank", content: "" });
  }
}

function inlineMarkdown(value: string): string {
  return value
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, (_match, label: string) =>
      label ? `[图片：${label}]` : "[图片]",
    )
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1 ($2)")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/~~([^~]+)~~/g, "$1")
    .replace(/\*([^*\n]+)\*/g, "$1")
    .replace(/(^|\s)_([^_\n]+)_(?=\s|[.,!?;:]|$)/g, "$1$2")
    .replace(/\\([\\`*_[\]#>+\-.])/g, "$1");
}

function taskMarker(value: string): string {
  if (/^\[[xX]\]\s+/.test(value)) return value.replace(/^\[[xX]\]\s+/, "☑ ");
  if (/^\[ \]\s+/.test(value)) return value.replace(/^\[ \]\s+/, "☐ ");
  return value;
}

function isTableRow(value: string): boolean {
  const trimmed = value.trim();
  return trimmed.startsWith("|") && trimmed.endsWith("|") && trimmed.length > 2;
}

function tableRow(value: string): string {
  return value
    .trim()
    .slice(1, -1)
    .split("|")
    .map((cell) => inlineMarkdown(cell.trim()))
    .join("  │  ");
}
