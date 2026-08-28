import type { CommandDescriptor } from "./command-registry.ts";

export interface CommandSearchResult {
  command: CommandDescriptor;
  score: number;
}

export function searchCommands(
  commands: readonly CommandDescriptor[],
  query: string,
  recentCommandIds: readonly string[] = [],
): CommandSearchResult[] {
  const normalized = normalize(query);
  const recentRanks = new Map(
    [...recentCommandIds].reverse().map((id, index) => [id, index]),
  );
  return commands
    .map((command) => ({
      command,
      score: commandScore(command, normalized, recentRanks.get(command.id)),
    }))
    .filter((result) => result.score >= 0)
    .sort(
      (left, right) =>
        right.score - left.score ||
        left.command.category.localeCompare(right.command.category, "zh-CN") ||
        left.command.name.localeCompare(right.command.name),
    );
}

function commandScore(
  command: CommandDescriptor,
  query: string,
  recentRank: number | undefined,
): number {
  if (!query) return recentRank === undefined ? 100 : 400 - recentRank;
  const fields = [
    command.name,
    command.title,
    command.description,
    ...command.aliases,
  ].map(normalize);
  let best = -1;
  for (const field of fields) {
    if (field === query) best = Math.max(best, 1_000);
    else if (field.startsWith(query)) best = Math.max(best, 800 - field.length);
    else if (field.includes(query)) {
      best = Math.max(best, 600 - field.indexOf(query));
    } else {
      const fuzzy = subsequenceScore(field, query);
      if (fuzzy >= 0) best = Math.max(best, 300 + fuzzy);
    }
  }
  if (best >= 0 && recentRank !== undefined) {
    best += Math.max(0, 40 - recentRank);
  }
  return best;
}

function subsequenceScore(value: string, query: string): number {
  let queryIndex = 0;
  let gaps = 0;
  let previousMatch = -1;
  for (let index = 0; index < value.length && queryIndex < query.length; index++) {
    if (value[index] !== query[queryIndex]) continue;
    if (previousMatch >= 0) gaps += index - previousMatch - 1;
    previousMatch = index;
    queryIndex++;
  }
  return queryIndex === query.length ? Math.max(0, 100 - gaps) : -1;
}

function normalize(value: string): string {
  return value.trim().replace(/^\//, "").toLocaleLowerCase();
}
