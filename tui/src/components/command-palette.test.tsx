import { describe, expect, test } from "bun:test";
import { RGBA } from "@opentui/core";
import { testRender } from "@opentui/react/test-utils";
import { act } from "react";

import {
  findCommand,
  type CommandContext,
} from "../ui/command-registry.ts";
import type { CommandSearchResult } from "../ui/command-search.ts";
import { CommandPalette } from "./command-palette.tsx";
import { DARK_THEME } from "./theme.ts";

const CONTEXT: CommandContext = {
  modelConfigured: true,
  currentModel: "deepseek-v4-pro",
  hasSession: true,
  transactionId: null,
  verificationStatus: "NOT_RUN",
  evidenceId: null,
  acceptanceReady: false,
};

function result(name: string): CommandSearchResult {
  const command = findCommand(name);
  if (command === null) throw new Error(`测试命令不存在：${name}`);
  return { command, score: 100 };
}

describe("command palette status colors", () => {
  test("distinguishes available, dangerous and unavailable commands", async () => {
    const setup = await testRender(
      <CommandPalette
        variant="slash"
        query=""
        results={[result("new"), result("clean"), result("verify")]}
        selectedIndex={0}
        context={CONTEXT}
        compact={false}
        viewportHeight={20}
        theme={DARK_THEME}
        onQuery={() => {}}
        onSelect={() => {}}
        onHover={() => {}}
      />,
      { width: 100, height: 24 },
    );
    await act(async () => setup.renderOnce());

    const frame = setup.captureCharFrame();
    expect(frame).toContain("✓ /new");
    expect(frame).toContain("! /clean");
    expect(frame).toContain("× /verify");

    const spans = setup.captureSpans().lines.flatMap((line) => line.spans);
    const colorOf = (marker: string) =>
      spans.find((span) => span.text.includes(marker))?.fg.toInts();
    expect(colorOf("✓")).toEqual(RGBA.fromHex(DARK_THEME.success).toInts());
    expect(colorOf("!")).toEqual(RGBA.fromHex(DARK_THEME.warning).toInts());
    expect(colorOf("×")).toEqual(RGBA.fromHex(DARK_THEME.danger).toInts());

    await act(async () => setup.renderer.destroy());
  });
});
