import type { ScrollBoxRenderable } from "@opentui/core";
import { useKeyboard, usePaste } from "@opentui/react";
import {
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";

import {
  maskApiKey,
  type ConfigurationDraft,
} from "../ui/runtime-config.ts";
import type { RivetTheme } from "./theme.ts";

const FIELDS = [
  "apiKey",
  "baseUrl",
  "model",
  "modelsText",
  "maxRounds",
  "maxTotalTokens",
  "maxCostUsd",
  "safeMode",
] as const;
type FieldName = (typeof FIELDS)[number];

export function ConfigDialog({
  draft,
  credentialConfigured,
  errors,
  saving,
  compact,
  viewportHeight,
  theme,
  onChange,
  onSave,
  onClose,
}: {
  draft: ConfigurationDraft;
  credentialConfigured: boolean;
  errors: string[];
  saving: boolean;
  compact: boolean;
  viewportHeight: number;
  theme: RivetTheme;
  onChange(draft: ConfigurationDraft): void;
  onSave(): void;
  onClose(): void;
}) {
  const [activeField, setActiveField] = useFieldFocus();
  const fieldsView = useRef<ScrollBoxRenderable | null>(null);
  const draftRef = useRef(draft);
  const activeFieldRef = useRef(activeField);
  const savingRef = useRef(saving);
  const onChangeRef = useRef(onChange);
  const onSaveRef = useRef(onSave);
  const onCloseRef = useRef(onClose);
  draftRef.current = draft;
  activeFieldRef.current = activeField;
  savingRef.current = saving;
  onChangeRef.current = onChange;
  onSaveRef.current = onSave;
  onCloseRef.current = onClose;
  const applyDraft = (next: ConfigurationDraft) => {
    draftRef.current = next;
    onChangeRef.current(next);
  };

  useEffect(() => {
    fieldsView.current?.scrollChildIntoView(`config-field-${FIELDS[activeField]}`);
  }, [activeField]);

  useKeyboard((key) => {
    if (key.ctrl && key.name === "s") {
      key.preventDefault();
      if (!savingRef.current) onSaveRef.current();
      return;
    }
    if (key.name === "escape") {
      key.preventDefault();
      onCloseRef.current();
      return;
    }
    if (key.name === "tab" || key.name === "up" || key.name === "down") {
      key.preventDefault();
      const direction = key.shift || key.name === "up" ? -1 : 1;
      setActiveField(
        (index) => (index + direction + FIELDS.length) % FIELDS.length,
      );
      return;
    }
    const field = FIELDS[activeFieldRef.current];
    const current = draftRef.current;
    if (
      field === "safeMode" &&
      (key.name === "return" || key.name === "space")
    ) {
      key.preventDefault();
      applyDraft({ ...current, safeMode: !current.safeMode });
      return;
    }
    if (field !== "apiKey") return;
    if (key.ctrl && key.name === "d") {
      key.preventDefault();
      applyDraft({ ...current, apiKey: "", apiKeyAction: "clear" });
      return;
    }
    if (key.ctrl && key.name === "u") {
      key.preventDefault();
      applyDraft({ ...current, apiKey: "", apiKeyAction: "keep" });
      return;
    }
    if (key.name === "backspace" || key.name === "delete") {
      key.preventDefault();
      const characters = Array.from(current.apiKey);
      characters.pop();
      applyDraft({
        ...current,
        apiKey: characters.join(""),
        apiKeyAction: characters.length === 0 ? "keep" : "replace",
      });
      return;
    }
    if (
      !key.ctrl &&
      !key.meta &&
      key.sequence.length > 0 &&
      !/[\u0000-\u001f\u007f\s]/u.test(key.sequence) &&
      current.apiKey.length + key.sequence.length <= 4_096
    ) {
      key.preventDefault();
      applyDraft({
        ...current,
        apiKey: `${current.apiKey}${key.sequence}`,
        apiKeyAction: "replace",
      });
    }
  });

  usePaste((event) => {
    if (FIELDS[activeFieldRef.current] !== "apiKey") return;
    event.preventDefault();
    event.stopPropagation();
    const value = new TextDecoder().decode(event.bytes);
    if (
      value.length > 0 &&
      value.length <= 4_096 &&
      !/[\s\u0000-\u001f\u007f]/u.test(value)
    ) {
      applyDraft({
        ...draftRef.current,
        apiKey: value,
        apiKeyAction: "replace",
      });
    }
  });

  const height = Math.max(10, Math.min(27, viewportHeight));
  const selected = (field: FieldName) => FIELDS[activeField] === field;
  const commonInput = {
    backgroundColor: theme.surfaceHover,
    focusedBackgroundColor: theme.surfaceHover,
    textColor: theme.textPrimary,
    focusedTextColor: theme.textPrimary,
    cursorColor: theme.accent,
  } as const;
  return (
    <box
      position="absolute"
      zIndex={40}
      top={viewportHeight <= height + 2 ? 0 : "8%"}
      left={compact ? "1%" : "12%"}
      width={compact ? "98%" : "76%"}
      height={height}
      backgroundColor={theme.surface}
      border={true}
      borderColor={theme.border}
      flexDirection="column"
      padding={1}
    >
      <box height={2} flexDirection="column">
        <text fg={theme.accent} content="连接与模型配置" />
        <text
          fg={theme.textMuted}
          content={
            compact
              ? "API Key 仅驻留当前 Worker 会话"
              : "API Key 仅保存在当前 Worker 会话；其余设置原子写入用户配置"
          }
        />
      </box>
      <scrollbox
        ref={fieldsView}
        flexGrow={1}
        scrollY={true}
        viewportCulling={true}
        contentOptions={{ flexDirection: "column" }}
      >
        <ConfigRow
          field="apiKey"
          label="API Key"
          active={selected("apiKey")}
          theme={theme}
        >
        <box
          flexGrow={1}
          backgroundColor={theme.surfaceHover}
          onMouseDown={() => setActiveField(0)}
        >
          <text
            fg={draft.apiKey ? theme.textPrimary : theme.textMuted}
            content={
              draft.apiKey
                ? maskApiKey(draft.apiKey)
                : draft.apiKeyAction === "clear"
                  ? "保存后清除当前会话 Key"
                  : credentialConfigured
                    ? "已配置 · 留空保持不变"
                    : "输入或粘贴 API Key"
            }
          />
        </box>
        </ConfigRow>
        <ConfigRow
          field="baseUrl"
          label="API 地址"
          active={selected("baseUrl")}
          theme={theme}
        >
        <input
          {...commonInput}
          value={draft.baseUrl}
          focused={selected("baseUrl")}
          maxLength={2_048}
          onInput={(baseUrl) => onChange({ ...draft, baseUrl })}
        />
        </ConfigRow>
        <ConfigRow
          field="model"
          label="默认模型"
          active={selected("model")}
          theme={theme}
        >
        <input
          {...commonInput}
          value={draft.model}
          focused={selected("model")}
          maxLength={128}
          onInput={(model) => onChange({ ...draft, model })}
        />
        </ConfigRow>
        <ConfigRow
          field="modelsText"
          label="模型列表"
          active={selected("modelsText")}
          theme={theme}
        >
        <input
          {...commonInput}
          value={draft.modelsText.replace(/\n/gu, ", ")}
          focused={selected("modelsText")}
          maxLength={4_096}
          onInput={(modelsText) => onChange({ ...draft, modelsText })}
        />
        </ConfigRow>
        <ConfigRow
          field="maxRounds"
          label="最大轮次"
          active={selected("maxRounds")}
          theme={theme}
        >
        <input
          {...commonInput}
          value={draft.maxRounds}
          focused={selected("maxRounds")}
          maxLength={3}
          onInput={(maxRounds) => onChange({ ...draft, maxRounds })}
        />
        </ConfigRow>
        <ConfigRow
          field="maxTotalTokens"
          label="Token 上限"
          active={selected("maxTotalTokens")}
          theme={theme}
        >
        <input
          {...commonInput}
          value={draft.maxTotalTokens}
          focused={selected("maxTotalTokens")}
          maxLength={8}
          onInput={(maxTotalTokens) => onChange({ ...draft, maxTotalTokens })}
        />
        </ConfigRow>
        <ConfigRow
          field="maxCostUsd"
          label="费用上限 $"
          active={selected("maxCostUsd")}
          theme={theme}
        >
        <input
          {...commonInput}
          value={draft.maxCostUsd}
          focused={selected("maxCostUsd")}
          maxLength={24}
          placeholder="留空表示不限"
          onInput={(maxCostUsd) => onChange({ ...draft, maxCostUsd })}
        />
        </ConfigRow>
        <ConfigRow
          field="safeMode"
          label="Safe Mode"
          active={selected("safeMode")}
          theme={theme}
        >
        <box
          flexGrow={1}
          backgroundColor={theme.surfaceHover}
          onMouseDown={() => {
            setActiveField(7);
            onChange({ ...draft, safeMode: !draft.safeMode });
          }}
        >
          <text
            fg={draft.safeMode ? theme.success : theme.textSecondary}
            content={draft.safeMode ? "● 已启用" : "○ 已关闭"}
          />
        </box>
        </ConfigRow>
      </scrollbox>
      <box minHeight={2} flexDirection="column">
        {errors.slice(0, 2).map((error) => (
          <text key={error} fg={theme.danger} content={`! ${error}`} />
        ))}
      </box>
      <text
        fg={theme.textMuted}
        content={
          saving
            ? "正在保存…"
            : compact
              ? "Tab 切换 · Ctrl+S 保存 · Esc 取消"
              : "Tab/↑↓ 切换 · Ctrl+S 保存 · Ctrl+D 清除会话 Key · Esc 取消"
        }
      />
    </box>
  );
}

function ConfigRow({
  field,
  label,
  active,
  theme,
  children,
}: {
  field: FieldName;
  label: string;
  active: boolean;
  theme: RivetTheme;
  children: ReactNode;
}) {
  return (
    <box id={`config-field-${field}`} height={2} flexDirection="row" gap={1}>
      <text
        width={14}
        fg={active ? theme.accent : theme.textSecondary}
        content={`${active ? "›" : " "} ${label}`}
      />
      {children}
    </box>
  );
}

function useFieldFocus(): [number, Dispatch<SetStateAction<number>>] {
  return useState(0);
}
