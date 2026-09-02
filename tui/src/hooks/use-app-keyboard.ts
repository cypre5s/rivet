import { useKeyboard } from "@opentui/react";
import { useEffect, useRef, type Dispatch, type SetStateAction } from "react";

import type { PasteAttachment } from "../components/composer.tsx";
import type { PickerOption } from "../components/option-picker.tsx";
import type { WorkerClient } from "../ipc/client.ts";
import type { RivetAction, RivetState } from "../state/reducer.ts";
import { clampIndex, overlayItemCount, type Overlay } from "../ui/app-model.ts";
import type {
  CommandDescriptor,
  CommandOutcome,
  PanelName,
} from "../ui/command-registry.ts";
import type { CommandSearchResult } from "../ui/command-search.ts";
import { resolveCtrlCIntent, resolveKeyCommand } from "../ui/keymap.ts";

export interface AppKeyboardState {
  rivet: RivetState;
  topOverlay: Overlay | null;
  openPanel: PanelName | null;
  input: string;
  running: boolean;
  activeRequestId: string | null;
  selectedIndex: number;
  evidenceExpanded: boolean;
  slashResults: CommandSearchResult[];
  rankedFiles: string[];
  modelOptions: PickerOption[];
  argumentOptions: PickerOption[];
}

export interface AppKeyboardActions {
  dispatch: Dispatch<RivetAction>;
  setInput: Dispatch<SetStateAction<string>>;
  setAttachments: Dispatch<SetStateAction<PasteAttachment[]>>;
  setInlineError: Dispatch<SetStateAction<string | null>>;
  setNotice: Dispatch<SetStateAction<string | null>>;
  setOpenPanel: Dispatch<SetStateAction<PanelName | null>>;
  setScreen: Dispatch<SetStateAction<"welcome" | "workbench">>;
  setSelectedIndex: Dispatch<SetStateAction<number>>;
  setEvidenceExpanded: Dispatch<SetStateAction<boolean>>;
  closeTopOverlay(): void;
  pushOverlay(overlay: Overlay): void;
  selectCommand(command: CommandDescriptor, autocomplete: boolean): void;
  selectFile(path: string, keepOpen?: boolean): void;
  selectModel(model: string): void;
  executeInput(value: string): void;
  executeOutcome(
    outcome: CommandOutcome,
    displayInput: string,
    command: CommandDescriptor | null,
  ): void;
}

export interface AppKeyboardServices {
  client: WorkerClient | undefined;
  onPermission: ((requestId: string, approved: boolean) => void) | undefined;
  onRecover: (() => void) | undefined;
  onExit: (() => void) | undefined;
}

export function useAppKeyboard(
  state: AppKeyboardState,
  actions: AppKeyboardActions,
  services: AppKeyboardServices,
): void {
  const lastCtrlCAt = useRef<number | null>(null);

  useEffect(() => {
    if (state.input.length > 0 && !state.running) lastCtrlCAt.current = null;
  }, [state.input, state.running]);

  useKeyboard((key) => {
    const command = resolveKeyCommand({
      name: key.name.toLocaleLowerCase(),
      shift: key.shift,
      ctrl: key.ctrl,
    });
    if (command === "task.cancel") {
      key.preventDefault();
      handleCtrlC();
      return;
    }
    if (
      state.rivet.permission !== null &&
      (key.name === "a" || key.name === "d" || key.name === "escape")
    ) {
      key.preventDefault();
      resolvePermission(key.name === "a");
      return;
    }
    if (state.topOverlay?.kind === "confirm") {
      if (key.name === "y") {
        key.preventDefault();
        const pending = state.topOverlay;
        actions.closeTopOverlay();
        actions.executeOutcome(
          pending.outcome,
          pending.displayInput,
          pending.command,
        );
      } else if (key.name === "n" || key.name === "escape") {
        key.preventDefault();
        actions.closeTopOverlay();
      }
      return;
    }
    if (state.topOverlay !== null) {
      if (handleOverlayKey(key.name, key.shift)) key.preventDefault();
      return;
    }
    if (command === "models.open") {
      key.preventDefault();
      actions.pushOverlay({ kind: "models" });
      return;
    }
    if (command === "worker.recover") {
      key.preventDefault();
      if (state.rivet.connection === "crashed") services.onRecover?.();
      return;
    }
    if (command === "overlay.close") {
      key.preventDefault();
      if (state.openPanel !== null) actions.setOpenPanel(null);
      else actions.setInlineError(null);
      return;
    }
    if (state.openPanel !== null && !key.ctrl) {
      const panel = ({ d: "Diff", v: "Verify", e: "Evidence" } as const)[
        key.name as "d" | "v" | "e"
      ];
      if (panel !== undefined) {
        key.preventDefault();
        actions.setOpenPanel(panel);
        if (panel === "Evidence") actions.setSelectedIndex(0);
        return;
      }
    }
    if (state.openPanel === "Evidence" && handleEvidencePanelKey(key)) {
      key.preventDefault();
    }
  });

  function resolvePermission(approved: boolean) {
    const permission = state.rivet.permission;
    if (permission === null) return;
    services.onPermission?.(permission.requestId, approved);
    if (services.client !== undefined) {
      void services.client
        .request("permission.resolve", {
          request_id: permission.requestId,
          approved,
        })
        .catch(() => {});
    }
  }

  function handleCtrlC() {
    const now = Date.now();
    const intent = resolveCtrlCIntent(lastCtrlCAt.current, now, {
      running: state.running,
      inputEmpty: state.input.length === 0,
      overlayOpen: state.topOverlay !== null,
    });
    if (intent === "cancel-task") {
      lastCtrlCAt.current = now;
      if (services.client !== undefined && state.activeRequestId !== null) {
        services.client.cancel(state.activeRequestId);
      }
      actions.setNotice("取消中 · 再按 Ctrl+C 退出");
    } else if (intent === "close-overlay") {
      lastCtrlCAt.current = now;
      actions.closeTopOverlay();
      actions.setNotice("再按 Ctrl+C 退出");
    } else if (intent === "clear-input") {
      lastCtrlCAt.current = null;
      actions.setInput("");
      actions.setAttachments([]);
      actions.setInlineError(null);
    } else if (intent === "exit") {
      lastCtrlCAt.current = null;
      services.onExit?.();
    } else {
      lastCtrlCAt.current = now;
      actions.setNotice("再按 Ctrl+C 退出");
    }
  }

  function handleEvidencePanelKey(key: { name: string; ctrl: boolean }): boolean {
    if (key.ctrl) return false;
    const transactions = state.rivet.transactions;
    if (key.name === "up" || key.name === "down") {
      if (transactions.length === 0) return true;
      const delta = key.name === "up" ? -1 : 1;
      actions.setEvidenceExpanded(false);
      actions.setSelectedIndex((current) =>
        clampIndex(current + delta, transactions.length),
      );
      return true;
    }
    if (key.name !== "l" && key.name !== "return") return false;
    const transactionId =
      transactions[clampIndex(state.selectedIndex, transactions.length)]
        ?.transactionId ??
      (state.rivet.transaction === "无" ? null : state.rivet.transaction);
    if (transactionId === null || services.client === undefined) {
      actions.setNotice("暂无日志");
      return true;
    }
    actions.setEvidenceExpanded(true);
    void services.client
      .request("evidence.log", { transaction_id: transactionId })
      .catch((error: unknown) =>
        actions.setInlineError(
          error instanceof Error ? error.message : "日志加载失败",
        ),
      );
    return true;
  }

  function handleOverlayKey(keyName: string, shift: boolean): boolean {
    if (keyName === "escape") {
      actions.closeTopOverlay();
      return true;
    }
    if (state.topOverlay?.kind === "info") return false;
    const count = overlayItemCount(
      state.topOverlay,
      state.slashResults.length,
      state.rankedFiles.length,
      state.modelOptions.length,
      state.argumentOptions.length,
    );
    if (["up", "down", "pageup", "pagedown"].includes(keyName)) {
      const delta =
        keyName === "up" ? -1 : keyName === "down" ? 1 : keyName === "pageup" ? -5 : 5;
      actions.setSelectedIndex((current) => clampIndex(current + delta, count));
      return true;
    }
    if (keyName === "tab" && state.topOverlay?.kind === "slash") {
      const command = state.slashResults[state.selectedIndex]?.command;
      if (command !== undefined) actions.selectCommand(command, !shift);
      return true;
    }
    if (keyName === "tab" && state.topOverlay?.kind === "arguments") {
      selectArgumentOption();
      return true;
    }
    if (keyName !== "return") return false;
    if (state.topOverlay?.kind === "slash") {
      const command = state.slashResults[state.selectedIndex]?.command;
      if (command !== undefined) actions.selectCommand(command, false);
    } else if (state.topOverlay?.kind === "files") {
      const path = state.rankedFiles[state.selectedIndex];
      if (path !== undefined) actions.selectFile(path, shift);
    } else if (state.topOverlay?.kind === "models") {
      const option = state.modelOptions[state.selectedIndex];
      if (option !== undefined) actions.selectModel(option.id);
    } else if (state.topOverlay?.kind === "arguments") {
      selectArgumentOption(true);
    }
    return true;
  }

  function selectArgumentOption(submitExact = false) {
    if (state.topOverlay?.kind !== "arguments") return;
    const option = state.argumentOptions[state.selectedIndex];
    if (option === undefined) return;
    const completed = `/${state.topOverlay.commandName} ${option.id}`;
    actions.closeTopOverlay();
    if (submitExact && state.input.trim() === completed) {
      actions.executeInput(completed);
      return;
    }
    actions.setInput(completed);
  }
}
