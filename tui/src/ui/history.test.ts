import { describe, expect, test } from "bun:test";

import {
  MAX_HISTORY_ITEMS,
  appendHistory,
  redactRecentCommand,
  searchHistory,
} from "./history.ts";

describe("local interaction history", () => {
  test("deduplicates, bounds and searches newest input first", () => {
    let history: string[] = [];
    for (let index = 0; index <= MAX_HISTORY_ITEMS; index++) {
      history = appendHistory(history, `任务 ${index}`);
    }
    history = appendHistory(history, "任务 50");

    expect(history).toHaveLength(MAX_HISTORY_ITEMS);
    expect(history.at(-1)).toBe("任务 50");
    expect(searchHistory(history, "50")).toEqual(["任务 50"]);
  });

  test("stores only the command name for recency ranking", () => {
    expect(redactRecentCommand("/model secret-looking-argument")).toBe("model");
    expect(redactRecentCommand("普通任务文本")).toBeNull();
  });
});
