import { describe, expect, test } from "bun:test";

import {
  createPasteAttachment,
  pasteAttachmentError,
} from "./composer.tsx";

describe("composer attachments", () => {
  test("summarizes large paste without discarding the original content", () => {
    const attachment = createPasteAttachment("第一行\nsecond\n第三行", 2);

    expect(attachment).toEqual({
      id: "paste_2",
      content: "第一行\nsecond\n第三行",
      lines: 3,
      characters: 14,
    });
  });

  test("rejects attachment counts and totals beyond the bounded composer budget", () => {
    const attachments = Array.from({ length: 8 }, (_, index) =>
      createPasteAttachment("x", index),
    );

    expect(pasteAttachmentError(attachments, "next")).toContain("最多保留");
    expect(pasteAttachmentError([], "x".repeat(48_001))).toContain(
      "不能超过",
    );
  });
});
