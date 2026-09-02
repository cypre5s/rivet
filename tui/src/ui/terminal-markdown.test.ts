import { describe, expect, test } from "bun:test";

import { terminalMarkdown } from "./terminal-markdown.ts";

describe("terminal Markdown", () => {
  test("renders common blocks without leaking control markers", () => {
    expect(
      terminalMarkdown(
        [
          "# 修复结果",
          "",
          "- **已修复** `calculator.py`",
          "- 保留 total_with_tax 标识符",
          "> 独立验证通过",
          "",
          "```python",
          "print('ok')",
          "```",
          "",
          "| 检查 | 结果 |",
          "| --- | --- |",
          "| Behavior | PASSED |",
        ].join("\n"),
      ),
    ).toEqual([
      { kind: "heading", content: "修复结果" },
      { kind: "blank", content: "" },
      { kind: "text", content: "• 已修复 calculator.py" },
      { kind: "text", content: "• 保留 total_with_tax 标识符" },
      { kind: "quote", content: "│ 独立验证通过" },
      { kind: "blank", content: "" },
      { kind: "code", content: "print('ok')" },
      { kind: "blank", content: "" },
      { kind: "text", content: "检查  │  结果" },
      { kind: "text", content: "Behavior  │  PASSED" },
    ]);
  });
});
