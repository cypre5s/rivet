import { describe, expect, test } from "bun:test";

import { initialRivetState } from "../state/reducer.ts";
import { createPaletteResources } from "./palette-resources.ts";

describe("global palette resources", () => {
  test("adapts live sessions, files, model, transaction, evidence and modules", () => {
    const state = {
      ...initialRivetState(),
      sessions: ["session_one"],
      taskModules: ["reader.pdf"],
      transaction: "tx_one",
      evidenceId: "evidence_one",
    };
    const resources = createPaletteResources(
      state,
      ["src/app.ts"],
      ["deepseek-v4-pro"],
    );
    const names = resources.map((resource) => resource.name);

    expect(names).toContain("resume session_one");
    expect(names).toContain("read src/app.ts");
    expect(names).toContain("model deepseek-v4-pro");
    expect(names).toContain("modules reader.pdf");
    expect(names).toContain("diff tx_one");
    expect(resources.map((resource) => resource.title)).toContain(
      "查看 evidence_one",
    );
  });
});
