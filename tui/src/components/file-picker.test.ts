import { describe, expect, test } from "bun:test";

import { rankFilePaths } from "./file-picker.tsx";

describe("repository file search", () => {
  test("ranks exact names, path matches and fuzzy matches deterministically", () => {
    const files = [
      "tests/app.test.ts",
      "src/application.ts",
      "src/app.ts",
      "docs/architecture.txt",
    ];

    expect(rankFilePaths(files, "app").slice(0, 3)).toEqual([
      "src/app.ts",
      "tests/app.test.ts",
      "src/application.ts",
    ]);
    expect(rankFilePaths(files, "srat")).toContain("src/application.ts");
  });

  test("keeps selected context visible when scores are otherwise equal", () => {
    expect(rankFilePaths(["b.py", "a.py"], "", ["a.py"])).toEqual([
      "a.py",
      "b.py",
    ]);
  });
});
