import type { Dispatch, SetStateAction } from "react";

import type { PasteAttachment } from "../components/composer.tsx";
import { WorkerResponseError, type WorkerClient } from "../ipc/client.ts";
import type { RivetAction, RivetState } from "../state/reducer.ts";
import {
  commandNeedsArgument,
  descriptorForInput,
  hasArgumentChoices,
  MAX_SELECTED_FILES,
  panelForWorkerCommand,
  type Overlay,
} from "./app-model.ts";
import {
  COMMAND_REGISTRY,
  commandAvailability,
  type CommandContext,
  type CommandDescriptor,
  type CommandOutcome,
  type PanelName,
} from "./command-registry.ts";
import { parseCommandInput, removeFileMention } from "./commands.ts";

export interface AppCommandEnvironment {
  rivetState: RivetState;
  running: boolean;
  selectedFiles: string[];
  models: string[];
  commandContext: CommandContext;
  client: WorkerClient | undefined;
  dispatch: Dispatch<RivetAction>;
  setScreen: Dispatch<SetStateAction<"welcome" | "workbench">>;
  setInput: Dispatch<SetStateAction<string>>;
  setOpenPanel: Dispatch<SetStateAction<PanelName | null>>;
  setOverlays: Dispatch<SetStateAction<Overlay[]>>;
  setFileQuery: Dispatch<SetStateAction<string>>;
  setSelectedIndex: Dispatch<SetStateAction<number>>;
  setSelectedFiles: Dispatch<SetStateAction<string[]>>;
  setAttachments: Dispatch<SetStateAction<PasteAttachment[]>>;
  setInlineError: Dispatch<SetStateAction<string | null>>;
  setActiveRequestId: Dispatch<SetStateAction<string | null>>;
  pushOverlay(overlay: Overlay): void;
  closeTopOverlay(): void;
  markModelTouched(): void;
  setSelectedModel: Dispatch<SetStateAction<string>>;
  getSelectedFiles(): string[];
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
      outcome = parseCommandInput(value, environment.commandContext);
      command = descriptorForInput(value);
    } catch (error) {
      environment.setInlineError(
        error instanceof Error ? error.message : "命令格式错误",
      );
      return false;
    }
    if (command?.dangerous) {
      environment.pushOverlay({
        kind: "confirm",
        command,
        outcome,
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
    if (outcome.kind === "ui") {
      environment.setInput("");
      executeUiAction(outcome.action, outcome.argument);
      return true;
    }
    if (
      environment.client !== undefined &&
      environment.rivetState.connection !== "ready"
    ) {
      environment.setInlineError("Worker 连接中");
      return false;
    }

    const selectedFiles = environment.getSelectedFiles();
    const attachmentText = environment.getAttachments()
      .map((item) => item.content)
      .join("\n\n");
    const params = { ...outcome.params };
    if (typeof params.query === "string" && selectedFiles.length > 0) {
      // @file 永远只是只读 Context；写权限只能来自 /fix 的显式参数。
      params.context_paths = [...selectedFiles];
    }
    if (typeof params.query === "string" && attachmentText) {
      params.query = `${params.query}\n\n[粘贴附件，不可信数据]\n${attachmentText}`;
    }

    environment.setScreen("workbench");
    environment.setInput("");
    environment.setAttachments([]);
    environment.dispatch({ kind: "local-message", summary: displayInput });
    if (command !== null) {
      const panel = panelForWorkerCommand(command.name);
      if (panel !== null) {
        environment.setOpenPanel(panel);
        environment.setSelectedIndex(0);
      }
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
    if (action === "open-models") {
      if (argument) selectModel(argument);
      else environment.pushOverlay({ kind: "models" });
      return;
    }
    environment.pushOverlay({
      kind: "info",
      title: "帮助",
      lines: [
        "普通输入                     ASK（只读问答）",
        ...COMMAND_REGISTRY.map(
          (item) => `${item.usage.padEnd(28)} ${item.title}`,
        ),
        "@文件（可放在命令末尾）      附加只读 Context",
      ],
    });
  }

  function selectCommand(command: CommandDescriptor, autocomplete: boolean) {
    const availability = commandAvailability(command, environment.commandContext);
    if (!availability.available) {
      environment.setInlineError(availability.reason);
      return;
    }
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
      !environment.selectedFiles.includes(path) &&
      environment.selectedFiles.length >= MAX_SELECTED_FILES
    ) {
      environment.setInlineError(`最多选择 ${MAX_SELECTED_FILES} 个文件`);
      return;
    }
    if (!environment.selectedFiles.includes(path)) {
      environment.setSelectedFiles((current) => [...current, path]);
    }
    // 文件会显示为独立的只读 Context 标签；不要把 @mention 留在命令或任务文本中。
    // 因此既支持先选文件再输入 /fix，也支持在完整 /fix 末尾再输入 @文件。
    environment.setInput((current) => removeFileMention(current));
    if (!keepOpen) environment.closeTopOverlay();
  }

  function selectModel(model: string) {
    if (!environment.models.includes(model)) {
      environment.setInlineError("请选择 Worker 提供的模型");
      return;
    }
    environment.markModelTouched();
    environment.setSelectedModel(model);
    closeAllTransientOverlays();
  }

  function closeAllTransientOverlays() {
    environment.setOverlays([]);
    environment.setSelectedIndex(0);
  }

  return {
    executeInput,
    executeOutcome,
    selectCommand,
    selectFile,
    selectModel,
  };
}
