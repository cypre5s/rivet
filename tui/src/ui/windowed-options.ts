export interface WindowedOptions<T> {
  startIndex: number;
  items: T[];
}

export function windowedOptions<T>(
  items: T[],
  selectedIndex: number,
  limit: number,
): WindowedOptions<T> {
  if (items.length === 0 || limit <= 0) return { startIndex: 0, items: [] };
  const boundedIndex = Math.max(
    0,
    Math.min(items.length - 1, selectedIndex),
  );
  const startIndex = Math.max(
    0,
    Math.min(
      boundedIndex - Math.floor(limit / 2),
      Math.max(0, items.length - limit),
    ),
  );
  return {
    startIndex,
    items: items.slice(startIndex, startIndex + limit),
  };
}
