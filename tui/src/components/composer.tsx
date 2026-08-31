import {
  decodePasteBytes,
  type TextareaOptions,
  type TextareaRenderable,
} from "@opentui/core";
import { usePaste } from "@opentui/react";
import { useEffect, useRef } from "react";

import type { WorkMode } from "../ui/command-registry.ts";
import type { RivetTheme } from "./theme.ts";

export interface PasteAttachment {
  id: string;
  content: string;
  lines: number;
  characters: number;
}

const COMPOSER_KEY_BINDINGS: NonNullable<TextareaOptions["keyBindings"]> = [
  { name: "return", action: "submit" },
  { name: "return", shift: true, action: "newline" },
  { name: "j", ctrl: true, action: "newline" },
];
const LARGE_PASTE_CHARACTERS = 800;
const LARGE_PASTE_LINES = 8;
export const MAX_PASTE_ATTACHMENTS = 8;
export const MAX_TOTAL_PASTE_CHARACTERS = 48_000;

export function Composer({
  value,
  placeholder,
  mode,
  modelLabel,
  modelCount,
  credentialConfigured,
  focused,
  compact,
  running,
  contextFiles,
  attachments,
  error,
  theme,
  onInput,
  onSubmit,
  onRemoveContext,
  onRemoveAttachment,
  onLargePaste,
  onPathPaste,
  onOpenModels,
  onOpenConfig,
}: {
  value: string;
  placeholder: string;
  mode: WorkMode;
  modelLabel: string;
  modelCount: number;
  credentialConfigured: boolean;
  focused: boolean;
  compact: boolean;
  running: boolean;
  contextFiles: string[];
  attachments: PasteAttachment[];
  error: string | null;
  theme: RivetTheme;
  onInput(value: string): void;
  onSubmit(value: string): boolean;
  onRemoveContext(path: string): void;
  onRemoveAttachment(id: string): void;
  onLargePaste(content: string): void;
  onPathPaste(path: string): void;
  onOpenModels(): void;
  onOpenConfig(): void;
}) {
  const editor = useRef<TextareaRenderable | null>(null);

  useEffect(() => {
    const current = editor.current;
    if (current === null || current.plainText === value) return;
    current.setText(value);
    current.cursorOffset = value.length;
  });

  usePaste((event) => {
    if (!focused) return;
    const content = decodePasteBytes(event.bytes);
    const lines = lineCount(content);
    if (content.length >= LARGE_PASTE_CHARACTERS || lines >= LARGE_PASTE_LINES) {
      event.preventDefault();
      event.stopPropagation();
      onLargePaste(content);
      return;
    }
    const normalized = content.trim();
    if (!normalized.includes("\n") && looksLikePath(normalized)) {
      onPathPaste(normalized);
    }
  });

  const attachmentRows = contextFiles.length + attachments.length;
  const editorHeight = compact ? 1 : 2;
  const metadataHeight = compact ? 1 : 2;
  const totalHeight = editorHeight + metadataHeight + Math.min(attachmentRows, 2) + 2;

  return (
    <box
      height={totalHeight}
      minHeight={compact ? 4 : 6}
      backgroundColor={theme.surface}
      flexDirection="row"
    >
      <box width={1} backgroundColor={theme.accent} />
      <box flexGrow={1} flexDirection="column" paddingX={2} paddingY={1}>
        {contextFiles.length === 0 && attachments.length === 0 ? null : (
          <box height={Math.min(attachmentRows, 2)} flexDirection="row" gap={1}>
            {contextFiles.slice(0, 3).map((path) => (
              <box
                key={path}
                backgroundColor={theme.surfaceHover}
                paddingX={1}
                onMouseDown={() => onRemoveContext(path)}
              >
                <text fg={theme.accent} content={`@${path} ×`} />
              </box>
            ))}
            {attachments.slice(0, 2).map((attachment) => (
              <box
                key={attachment.id}
                backgroundColor={theme.surfaceHover}
                paddingX={1}
                onMouseDown={() => onRemoveAttachment(attachment.id)}
              >
                <text
                  fg={theme.textSecondary}
                  content={`粘贴 ${attachment.lines} 行 · ${attachment.characters} 字 ×`}
                />
              </box>
            ))}
          </box>
        )}
        <textarea
          ref={editor}
          initialValue={value}
          placeholder={placeholder}
          placeholderColor={theme.textMuted}
          focused={focused}
          height={editorHeight}
          flexGrow={1}
          wrapMode="word"
          backgroundColor={theme.surface}
          focusedBackgroundColor={theme.surface}
          textColor={theme.textPrimary}
          focusedTextColor={theme.textPrimary}
          cursorColor={theme.accent}
          keyBindings={COMPOSER_KEY_BINDINGS}
          onContentChange={() => onInput(editor.current?.plainText ?? "")}
          onSubmit={() => {
            const current = editor.current;
            const submitted = current?.plainText ?? value;
            if (!onSubmit(submitted) || current === null) return;
            current.setText("");
            current.cursorOffset = 0;
            onInput("");
          }}
        />
        <box height={1} flexDirection="row" justifyContent="space-between">
          <box flexDirection="row" gap={1}>
            <box onMouseDown={onOpenModels}>
              <text
                fg={theme.textSecondary}
                content={`${mode} · 模型 ${modelLabel} ▾ · ${modelCount} 个可用模型`}
              />
            </box>
            {compact ? null : (
              <box onMouseDown={onOpenConfig}>
                <text
                  fg={credentialConfigured ? theme.success : theme.warning}
                  content={credentialConfigured ? "Key ●" : "Key ○ · Ctrl+G 配置"}
                />
              </box>
            )}
          </box>
          <text
            fg={running ? theme.warning : theme.textMuted}
            content={
              running
                ? "◌ 运行中 · Ctrl+C 取消"
                : compact
                  ? "Enter 发送"
                  : "Enter 发送 · Shift+Enter 换行"
            }
          />
        </box>
        {error === null ? null : (
          <text fg={theme.danger} content={`! ${error}`} />
        )}
      </box>
    </box>
  );
}

export function createPasteAttachment(
  content: string,
  index: number,
): PasteAttachment {
  return {
    id: `paste_${index}`,
    content,
    lines: lineCount(content),
    characters: content.length,
  };
}

export function pasteAttachmentError(
  attachments: PasteAttachment[],
  nextContent: string,
): string | null {
  if (attachments.length >= MAX_PASTE_ATTACHMENTS) {
    return `最多保留 ${MAX_PASTE_ATTACHMENTS} 个粘贴附件`;
  }
  const totalCharacters = attachments.reduce(
    (total, attachment) => total + attachment.characters,
    nextContent.length,
  );
  if (totalCharacters > MAX_TOTAL_PASTE_CHARACTERS) {
    return `粘贴附件总量不能超过 ${MAX_TOTAL_PASTE_CHARACTERS} 字符`;
  }
  return null;
}

function lineCount(value: string): number {
  return value.length === 0 ? 0 : value.split(/\r\n|\r|\n/).length;
}

function looksLikePath(value: string): boolean {
  return (
    value.length > 0 &&
    value.length <= 4_096 &&
    !value.startsWith("/") &&
    !value.includes("\0") &&
    (value.includes("/") || /\.[A-Za-z0-9]{1,10}$/.test(value))
  );
}
