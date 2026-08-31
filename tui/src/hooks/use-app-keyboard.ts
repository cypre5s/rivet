import { useKeyboard } from "@opentui/react";
import {
  useEffect,
  useRef,
  type Dispatch,
  type SetStateAction,
} from "react";

import type { PasteAttachment } from "../components/composer.tsx";
import type { PickerOption } from "../components/option-picker.tsx";
import type { WorkerClient } from "../ipc/client.ts";
import type { RivetAction, RivetState } from "../state/reducer.ts";
import {
  clampIndex,
  MODES,
  overlayItemCount,
  type Overlay,
} from "../ui/app-model.ts";
import type {
  CommandDescriptor,
  CommandOutcome,
  PanelName,
  WorkMode,
} from "../ui/command-registry.ts";
import type { CommandSearchResult } from "../ui/command-search.ts";
import {
  resolveCtrlCIntent,
  resolveKeyCommand,
  resolveLeaderCommand,
} from "../ui/keymap.ts";

export interface AppKeyboardState {
  rivet: RivetState;
  topOverlay: Overlay | null;
  openPanel: PanelName | null;
  input: string;
  history: string[];
  historyCursor: number;
  mode: WorkMode;
  running: boolean;
  activeRequestId: string | null;
  selectedIndex: number;
  slashResults: CommandSearchResult[];
  paletteResults: CommandSearchResult[];
  rankedFiles: string[];
  historyOptions: PickerOption[];
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
  setScreen: Dispatch<SetStateAction<"welcome" | "session">>;
  setMode: Dispatch<SetStateAction<WorkMode>>;
  setHistoryCursor: Dispatch<SetStateAction<number>>;
  setSelectedIndex: Dispatch<SetStateAction<number>>;
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
        actions.setNotice("已取消危险操作");
      }
      return;
    }
    if (state.topOverlay?.kind === "leader") {
      key.preventDefault();
      if (key.name === "escape") {
        actions.closeTopOverlay();
        return;
      }
      const commandName = resolveLeaderCommand(key.name);
      actions.closeTopOverlay();
      if (commandName === null) {
        actions.setNotice("未定义的 Leader 快捷键");
      } else {
        executeLeader(commandName);
      }
      return;
    }
    if (state.topOverlay?.kind === "config") {
      return;
    }
    if (state.topOverlay !== null) {
      if (handleOverlayKey(key.name, key.shift)) key.preventDefault();
      return;
    }
    if (state.openPanel === "Modules" && handleModulesPanelKey(key)) {
      key.preventDefault();
      return;
    }
    if (command === "palette.open") {
      key.preventDefault();
      actions.pushOverlay({ kind: "palette" });
    } else if (command === "files.open") {
      key.preventDefault();
      actions.pushOverlay({ kind: "files" });
    } else if (command === "history.open") {
      key.preventDefault();
      actions.pushOverlay({ kind: "history" });
    } else if (command === "models.open") {
      key.preventDefault();
      actions.pushOverlay({ kind: "models" });
    } else if (command === "config.open") {
      key.preventDefault();
      actions.pushOverlay({ kind: "config" });
    } else if (command === "leader.open") {
      key.preventDefault();
      actions.pushOverlay({ kind: "leader" });
    } else if (command === "timeline.clear") {
      key.preventDefault();
      actions.dispatch({ kind: "timeline-clear" });
    } else if (command === "mode.next" || command === "mode.previous") {
      key.preventDefault();
      cycleMode(command === "mode.next" ? 1 : -1);
    } else if (command === "worker.recover") {
      key.preventDefault();
      if (state.rivet.connection === "crashed") services.onRecover?.();
    } else if (command === "overlay.close") {
      key.preventDefault();
      if (state.openPanel !== null) actions.setOpenPanel(null);
      else actions.setInlineError(null);
    } else if (
      state.input.length === 0 &&
      (key.name === "up" || key.name === "down")
    ) {
      key.preventDefault();
      browseHistory(key.name === "up" ? 1 : -1);
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
      actions.setNotice("正在取消当前任务；再次按 Ctrl+C 安全退出");
    } else if (intent === "close-overlay") {
      lastCtrlCAt.current = now;
      actions.closeTopOverlay();
      actions.setNotice("已关闭弹层；再次按 Ctrl+C 安全退出");
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
      actions.setNotice("再次按 Ctrl+C 安全退出");
    }
  }

  function cycleMode(direction: 1 | -1) {
    const current = MODES.indexOf(state.mode);
    const next = (current + direction + MODES.length) % MODES.length;
    actions.setMode(MODES[next] ?? "ASK");
  }

  function browseHistory(direction: 1 | -1) {
    if (state.history.length === 0) return;
    const next = Math.max(
      -1,
      Math.min(state.history.length - 1, state.historyCursor + direction),
    );
    actions.setHistoryCursor(next);
    actions.setInput(
      next < 0 ? "" : state.history[state.history.length - 1 - next] ?? "",
    );
  }

  function executeLeader(commandName: string) {
    if (commandName === "plan" || commandName === "fix") {
      actions.setMode(commandName === "plan" ? "PLAN" : "FIX");
      actions.setNotice(`已切换到 ${commandName.toUpperCase()} 模式`);
      return;
    }
    if (commandName === "quit") {
      services.onExit?.();
      return;
    }
    if (commandName === "modules") {
      actions.executeInput("/modules list");
      return;
    }
    const panelByCommand: Partial<Record<string, PanelName>> = {
      context: "Context",
      diff: "Diff",
      evidence: "Evidence",
      sessions: "Sessions",
      trace: "Trace",
    };
    const panel = panelByCommand[commandName];
    if (panel !== undefined) {
      actions.setScreen("session");
      actions.setOpenPanel(panel);
      return;
    }
    actions.executeInput(`/${commandName}`);
  }

  function handleModulesPanelKey(key: {
    name: string;
    ctrl: boolean;
  }): boolean {
    const modules = state.rivet.moduleStatuses;
    if (key.ctrl || modules.length === 0) return false;
    if (key.name === "up" || key.name === "down") {
      const delta = key.name === "up" ? -1 : 1;
      actions.setSelectedIndex((current) =>
        clampIndex(current + delta, modules.length),
      );
      return true;
    }
    const module = modules[clampIndex(state.selectedIndex, modules.length)];
    if (module === undefined || !["e", "w", "s", "d"].includes(key.name)) {
      return false;
    }
    if (
      !module.manualControl ||
      module.activation === "required" ||
      module.activation === "eager"
    ) {
      actions.setNotice("该模块受 Kernel 策略保护，不能手动控制");
      return true;
    }
    const operation = { e: "enable", w: "wake", s: "sleep", d: "disable" }[
      key.name
    ];
    if (operation === undefined) return false;
    if (operation === "enable" && module.configuredEnabled) {
      actions.setNotice("模块已经启用");
      return true;
    }
    if (operation === "wake" && !module.configuredEnabled) {
      actions.setNotice("模块尚未启用，请先执行 Enable");
      return true;
    }
    if (
      operation === "sleep" &&
      !["ACTIVE", "IDLE"].includes(module.runtimeState)
    ) {
      actions.setNotice("模块当前没有运行，无需休眠");
      return true;
    }
    actions.executeInput(`/modules ${operation} ${module.moduleId}`);
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
      state.paletteResults.length,
      state.rankedFiles.length,
      state.historyOptions.length,
      state.modelOptions.length,
      state.argumentOptions.length,
    );
    if (
      keyName === "up" ||
      keyName === "down" ||
      keyName === "pageup" ||
      keyName === "pagedown"
    ) {
      const delta =
        keyName === "up"
          ? -1
          : keyName === "down"
            ? 1
            : keyName === "pageup"
              ? -5
              : 5;
      actions.setSelectedIndex((current) => clampIndex(current + delta, count));
      return true;
    }
    if (
      keyName === "tab" &&
      (state.topOverlay?.kind === "slash" ||
        state.topOverlay?.kind === "palette")
    ) {
      const results =
        state.topOverlay.kind === "slash"
          ? state.slashResults
          : state.paletteResults;
      const command = results[state.selectedIndex]?.command;
      if (command !== undefined) actions.selectCommand(command, !shift);
      return true;
    }
    if (keyName === "tab" && state.topOverlay?.kind === "arguments") {
      selectArgumentOption();
      return true;
    }
    if (keyName !== "return") return false;
    if (
      state.topOverlay?.kind === "slash" ||
      state.topOverlay?.kind === "palette"
    ) {
      const results =
        state.topOverlay.kind === "slash"
          ? state.slashResults
          : state.paletteResults;
      const command = results[state.selectedIndex]?.command;
      if (command !== undefined) actions.selectCommand(command, false);
    } else if (state.topOverlay?.kind === "files") {
      const path = state.rankedFiles[state.selectedIndex];
      if (path !== undefined) actions.selectFile(path, shift);
    } else if (state.topOverlay?.kind === "history") {
      const option = state.historyOptions[state.selectedIndex];
      if (option !== undefined) {
        actions.setInput(option.title);
        actions.closeTopOverlay();
      }
    } else if (state.topOverlay?.kind === "models") {
      const option = state.modelOptions[state.selectedIndex];
      if (option !== undefined) actions.selectModel(option.id);
    } else if (state.topOverlay?.kind === "arguments") {
      selectArgumentOption();
    }
    return true;
  }

  function selectArgumentOption() {
    if (state.topOverlay?.kind !== "arguments") return;
    const option = state.argumentOptions[state.selectedIndex];
    if (option === undefined) return;
    if (option.available === false) {
      actions.setInlineError(option.description ?? "该参数当前不可用");
      return;
    }
    actions.setInput(`/${state.topOverlay.commandName} ${option.id}`);
    actions.closeTopOverlay();
  }
}
