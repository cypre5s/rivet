import type {
  Dispatch,
  SetStateAction,
} from "react";

import type { PasteAttachment } from "../components/composer.tsx";
import type { ThemeName } from "../components/theme.ts";
import { WorkerResponseError, type WorkerClient } from "../ipc/client.ts";
import type { RivetAction, RivetState } from "../state/reducer.ts";
import {
  commandNeedsArgument,
  costLines,
  descriptorForInput,
  hasArgumentChoices,
  keyHelpLines,
  MAX_CONTEXT_FILES,
  panelForAction,
  parseMode,
  statusLines,
  type Overlay,
} from "./app-model.ts";
import {
  COMMAND_REGISTRY,
  commandAvailability,
  findCommand,
  type CommandContext,
  type CommandDescriptor,
  type CommandOutcome,
  type PanelName,
  type WorkMode,
} from "./command-registry.ts";
import { parseCommandInput, replaceFileMention } from "./commands.ts";
import { appendHistory, redactRecentCommand } from "./history.ts";

export interface AppCommandEnvironment {
  rivetState: RivetState;
  mode: WorkMode;
  running: boolean;
  contextFiles: string[];
  models: string[];
  attachments: PasteAttachment[];
  commandContext: CommandContext;
  client: WorkerClient | undefined;
  onExit: (() => void) | undefined;
  dispatch: Dispatch<RivetAction>;
  setScreen: Dispatch<SetStateAction<"welcome" | "session">>;
  setInput: Dispatch<SetStateAction<string>>;
  setMode: Dispatch<SetStateAction<WorkMode>>;
  setThemeName: Dispatch<SetStateAction<ThemeName>>;
  setOpenPanel: Dispatch<SetStateAction<PanelName | null>>;
  setOverlays: Dispatch<SetStateAction<Overlay[]>>;
  setOverlayQuery: Dispatch<SetStateAction<string>>;
  setFileQuery: Dispatch<SetStateAction<string>>;
  setSelectedIndex: Dispatch<SetStateAction<number>>;
  setContextFiles: Dispatch<SetStateAction<string[]>>;
  setAttachments: Dispatch<SetStateAction<PasteAttachment[]>>;
  setHistory: Dispatch<SetStateAction<string[]>>;
  setRecentCommandIds: Dispatch<SetStateAction<string[]>>;
  setInlineError: Dispatch<SetStateAction<string | null>>;
  setActiveRequestId: Dispatch<SetStateAction<string | null>>;
  pushOverlay(overlay: Overlay): void;
  closeTopOverlay(): void;
  markModelTouched(): void;
  setSelectedModel: Dispatch<SetStateAction<string>>;
  getContextFiles(): string[];
  getAttachments(): PasteAttachment[];
}

export interface AppCommandActions {
  executeInput(value: string): boolean;
  executeOutcome(
    outcome: CommandOutcome,
    displayInput: string,
    command: CommandDescriptor | null,
  ): boolean;
  selectCommand(command: CommandDescriptor, autocomplete: boolean): void;
  selectFile(path: string, keepOpen?: boolean): void;
  selectModel(model: string): void;
}

export function createAppCommandActions(
  environment: AppCommandEnvironment,
): AppCommandActions {
  function executeInput(rawValue: string) {
    const value = rawValue.trim();
    if (!value) return false;
    let outcome: CommandOutcome;
    let command: CommandDescriptor | null;
    try {
      outcome = parseCommandInput(
        value,
        environment.commandContext,
        environment.mode,
      );
      command = descriptorForInput(value, environment.mode);
    } catch (error) {
      environment.setInlineError(
        error instanceof Error ? error.message : "命令格式错误",
      );
      return false;
    }
    if (
      command?.name === "fix" &&
      !environment.commandContext.acceptanceReady &&
      outcome.kind === "worker"
    ) {
      environment.pushOverlay({
        kind: "confirm",
        command,
        outcome: {
          ...outcome,
          params: { ...outcome.params, candidate_only: true },
        },
        displayInput: value,
        title: "缺少独立验收",
        description: "仅生成候选补丁；不验证，不可 Apply",
        impact: `${environment.rivetState.acceptanceReason} · 仍会产生模型费用 · ${environment.rivetState.acceptanceAction}`,
      });
      return false;
    }
    if (
      command?.dangerous ||
      (command?.name === "modules" && /^\s*disable\b/.test(value.slice(8)))
    ) {
      const confirmedOutcome =
        command?.name === "modules" && outcome.kind === "worker"
          ? { ...outcome, params: { ...outcome.params, confirmed: true } }
          : command?.name === "init" && outcome.kind === "worker"
            ? { ...outcome, params: { ...outcome.params, confirmed: true } }
          : outcome;
      environment.pushOverlay({
        kind: "confirm",
        command,
        outcome: confirmedOutcome,
        displayInput: value,
      });
      return false;
    }
    return executeOutcome(outcome, value, command);
  }

  function executeOutcome(
    outcome: CommandOutcome,
    displayInput: string,
    command: CommandDescriptor | null,
  ) {
    closeAllTransientOverlays();
    const commandMode = command === null ? null : parseMode(command.name);
    if (commandMode !== null) environment.setMode(commandMode);
    if (command !== null) rememberCommand(command, displayInput);
    if (outcome.kind === "ui") {
      environment.setInput("");
      executeUiAction(outcome.action, outcome.argument);
      return true;
    }
    if (command?.name === "modules") {
      environment.setScreen("session");
      environment.setOpenPanel("Modules");
      environment.setSelectedIndex(0);
    }
    if (
      environment.client !== undefined &&
      environment.rivetState.connection !== "ready"
    ) {
      environment.setInlineError("Worker 连接中");
      return false;
    }
    const contextFiles = environment.getContextFiles();
    const attachmentText = environment.getAttachments()
      .map((item) => item.content)
      .join("\n\n");
    const params = { ...outcome.params };
    const query = params.query;
    if (typeof query === "string" && contextFiles.length > 0) {
      params.context_paths = [...contextFiles];
    }
    if (typeof query === "string" && attachmentText) {
      params.query = `${query}\n\n[粘贴附件，不可信数据]\n${attachmentText}`;
    }
    environment.setScreen("session");
    environment.setInput("");
    environment.setAttachments([]);
    if (command?.name !== "modules") {
      environment.dispatch({ kind: "local-message", summary: displayInput });
    }
    if (environment.client === undefined) return true;
    const tracked = environment.client.beginRequest(outcome.method, params);
    environment.setActiveRequestId(tracked.requestId);
    void tracked.result
      .catch((error: unknown) => {
        environment.setInlineError(
          error instanceof WorkerResponseError
            ? `${error.code}：${error.message} · ${error.nextAction}`
            : error instanceof Error
              ? error.message
              : "任务提交失败",
        );
      })
      .finally(() =>
        environment.setActiveRequestId((current) =>
          current === tracked.requestId ? null : current,
        ),
      );
    return true;
  }

  function executeUiAction(
    action: Extract<CommandOutcome, { kind: "ui" }>["action"],
    argument: string,
  ) {
    if (action === "new-session") {
      environment.dispatch({ kind: "timeline-clear" });
      environment.setScreen("welcome");
      environment.setOpenPanel(null);
      environment.setInput("");
      environment.setContextFiles([]);
      environment.setAttachments([]);
      return;
    }
    if (action === "clear-timeline") {
      environment.dispatch({ kind: "timeline-clear" });
      return;
    }
    if (action === "quit") {
      environment.onExit?.();
      return;
    }
    if (action === "open-files" || action === "open-search") {
      environment.setFileQuery(argument);
      environment.pushOverlay({ kind: "files" });
      return;
    }
    if (action === "open-history") {
      environment.setOverlayQuery(argument);
      environment.pushOverlay({ kind: "history" });
      return;
    }
    if (action === "open-models") {
      if (argument) selectModel(argument);
      else {
        environment.setOverlayQuery("");
        environment.pushOverlay({ kind: "models" });
      }
      return;
    }
    if (action === "open-config") {
      environment.pushOverlay({ kind: "config" });
      return;
    }
    if (action === "change-mode") {
      const nextMode = parseMode(argument);
      if (nextMode === null) {
        environment.setInput("/mode ");
        environment.pushOverlay({ kind: "arguments", commandName: "mode" });
      } else environment.setMode(nextMode);
      return;
    }
    if (action === "change-theme") {
      if (argument === "dark" || argument === "light") {
        environment.setThemeName(argument);
      } else {
        environment.setInput("/theme ");
        environment.pushOverlay({ kind: "arguments", commandName: "theme" });
      }
      return;
    }
    if (action === "open-help") {
      showInfo(
        "帮助",
        COMMAND_REGISTRY.map(
          (item) => `${item.usage.padEnd(28)} ${item.title}`,
        ),
      );
      return;
    }
    if (action === "open-keys") {
      showInfo("快捷键", keyHelpLines());
      return;
    }
    if (action === "show-status") {
      showInfo(
        "状态",
        statusLines(
          environment.rivetState,
          environment.mode,
          environment.running,
        ),
      );
      return;
    }
    if (action === "show-cost") {
      showInfo("用量", costLines(environment.rivetState));
      return;
    }
    if (action === "open-context") {
      manageContext(argument);
      return;
    }
    const panel = panelForAction(action, argument);
    if (panel !== null) {
      environment.setScreen("session");
      environment.setOpenPanel(panel);
      if (panel === "Evidence") environment.setSelectedIndex(0);
    }
  }

  function selectCommand(
    command: CommandDescriptor,
    autocomplete: boolean,
  ) {
    const availability = commandAvailability(
      command,
      environment.commandContext,
    );
    if (!availability.available) {
      environment.setInlineError(availability.reason);
      return;
    }
    const commandMode = parseMode(command.name);
    if (commandMode !== null) environment.setMode(commandMode);
    if (autocomplete || commandNeedsArgument(command)) {
      environment.setInput(
        `/${command.name}${command.argumentKind === "none" ? "" : " "}`,
      );
      if (hasArgumentChoices(command)) {
        environment.setOverlays([
          { kind: "arguments", commandName: command.name },
        ]);
        environment.setSelectedIndex(0);
      } else {
        closeAllTransientOverlays();
      }
      return;
    }
    executeInput(`/${command.name}`);
  }

  function selectFile(path: string, keepOpen = false) {
    if (
      !environment.contextFiles.includes(path) &&
      environment.contextFiles.length >= MAX_CONTEXT_FILES
    ) {
      environment.setInlineError(`最多选择 ${MAX_CONTEXT_FILES} 个上下文文件`);
      return;
    }
    if (!environment.contextFiles.includes(path)) {
      environment.setContextFiles((current) => [...current, path]);
    }
    environment.setInput((current) => replaceFileMention(current, path));
    if (!keepOpen) environment.closeTopOverlay();
  }

  function manageContext(argument: string) {
    const normalized = argument.trim();
    if (!normalized || normalized === "list") {
      environment.setScreen("session");
      environment.setOpenPanel("Context");
      return;
    }
    if (normalized === "clear") {
      environment.setContextFiles([]);
      return;
    }
    const separator = normalized.indexOf(" ");
    const operation = separator < 0 ? normalized : normalized.slice(0, separator);
    const path = separator < 0 ? "" : normalized.slice(separator + 1).trim();
    if (!safeContextPath(path)) {
      environment.setInlineError("请选择仓库内普通文件作为上下文");
      return;
    }
    if (operation === "add") {
      if (environment.contextFiles.includes(path)) {
        return;
      }
      if (environment.contextFiles.length >= MAX_CONTEXT_FILES) {
        environment.setInlineError(`最多选择 ${MAX_CONTEXT_FILES} 个上下文文件`);
        return;
      }
      environment.setContextFiles((current) => [...current, path]);
      return;
    }
    if (operation === "remove") {
      if (!environment.contextFiles.includes(path)) {
        environment.setInlineError(`@${path} 不在显式上下文中`);
        return;
      }
      environment.setContextFiles((current) =>
        current.filter((item) => item !== path),
      );
      return;
    }
    environment.setInlineError("/context 仅支持 add、remove、list 或 clear");
  }

  function selectModel(model: string) {
    if (!environment.models.some((candidate) => candidate === model)) {
      environment.setInlineError("请选择当前配置中的模型");
      return;
    }
    environment.markModelTouched();
    environment.setSelectedModel(model);
    closeAllTransientOverlays();
  }

  function closeAllTransientOverlays() {
    environment.setOverlays([]);
    environment.setSelectedIndex(0);
    environment.setOverlayQuery("");
  }

  function rememberCommand(command: CommandDescriptor, value: string) {
    environment.setHistory((current) => appendHistory(current, value));
    const recentName = redactRecentCommand(value) ?? command.name;
    const commandId = findCommand(recentName)?.id ?? command.id;
    environment.setRecentCommandIds((current) =>
      [...current.filter((id) => id !== commandId), commandId].slice(-20),
    );
  }

  function showInfo(title: string, lines: string[]) {
    environment.pushOverlay({ kind: "info", title, lines });
  }

  return {
    executeInput,
    executeOutcome,
    selectCommand,
    selectFile,
    selectModel,
  };
}

function safeContextPath(path: string): boolean {
  return (
    path.length > 0 &&
    path.length <= 4_096 &&
    !path.startsWith("/") &&
    !path.split("/").includes("..") &&
    !/[\u0000-\u001f\u007f]/.test(path)
  );
}
