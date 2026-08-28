export const MAX_HISTORY_ITEMS = 100;

export function appendHistory(history: string[], value: string): string[] {
  const normalized = value.trim();
  if (!normalized) return history;
  const withoutDuplicate = history.filter((item) => item !== normalized);
  return [...withoutDuplicate, normalized].slice(-MAX_HISTORY_ITEMS);
}

export function searchHistory(history: string[], query: string): string[] {
  const normalized = query.trim().toLocaleLowerCase();
  const newestFirst = [...history].reverse();
  if (!normalized) return newestFirst;
  return newestFirst.filter((item) => item.toLocaleLowerCase().includes(normalized));
}

export function redactRecentCommand(value: string): string | null {
  const match = /^\/([a-z][a-z-]*)/i.exec(value.trim());
  return match?.[1]?.toLocaleLowerCase() ?? null;
}
