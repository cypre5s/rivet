import { describe, expect, test } from "bun:test";
import { testRender } from "@opentui/react/test-utils";
import { act } from "react";

import { COMMAND_REGISTRY } from "../ui/command-registry.ts";
import { searchCommands } from "../ui/command-search.ts";
import { createTheme } from "./theme.ts";
import { SlashMenu } from "./slash-menu.tsx";

describe("Slash menu", () => {
  test("renders only the focused command surface", async () => {
    const setup = await testRender(
      <SlashMenu
        query=""
        results={searchCommands(COMMAND_REGISTRY, "")}
        selectedIndex={0}
        context={{
          modelConfigured: true,
          currentModel: "deepseek-v4-pro",
          transactionId: "tx_one",
          verificationStatus: "PASSED",
          evidenceId: "evidence_one",
          acceptanceReady: true,
        }}
        compact={false}
        viewportHeight={30}
        theme={createTheme(true)}
        onSelect={() => {}}
        onHover={() => {}}
      />,
      { width: 100, height: 30 },
    );
    await act(async () => setup.renderOnce());
    const frame = setup.captureCharFrame();
    for (const name of ["help", "fix", "diff", "verify", "apply", "abort", "model"]) {
      expect(frame).toContain(`/${name}`);
    }
    for (const removed of ["plan", "sessions", "modules", "config", "theme"]) {
      expect(frame).not.toContain(`/${removed}`);
    }
    await act(async () => setup.renderer.destroy());
  });
});
