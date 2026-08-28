import { describe, expect, test } from "bun:test";

import { windowedOptions } from "./windowed-options.ts";

describe("windowed option lists", () => {
  test("keeps keyboard selection visible at the start, middle and end", () => {
    const values = Array.from({ length: 20 }, (_, index) => `item-${index}`);

    expect(windowedOptions(values, 0, 5)).toEqual({
      startIndex: 0,
      items: values.slice(0, 5),
    });
    expect(windowedOptions(values, 10, 5)).toEqual({
      startIndex: 8,
      items: values.slice(8, 13),
    });
    expect(windowedOptions(values, 19, 5)).toEqual({
      startIndex: 15,
      items: values.slice(15, 20),
    });
  });
});
