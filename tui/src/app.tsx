import { useTerminalDimensions } from "@opentui/react";
import {
  useEffect,
  useReducer,
  useRef,
  useState,
} from "react";

import { AppOverlays } from "./components/app-overlays.tsx";
import {
  Composer,
  createPasteAttachment,
  pasteAttachmentError,
  type PasteAttachment,
} from "./components/composer.tsx";
import { SessionScreen } from "./components/session-screen.tsx";
import { createTheme, type ThemeName } from "./components/theme.ts";
import { WelcomeScreen } from "./components/welcome-screen.tsx";
import type { JsonValue } from "./contracts/ipc.ts";
import { useCommandOptions } from "./hooks/use-command-options.ts";
import { useEvidenceDetail } from "./hooks/use-evidence-detail.ts";
import { useAppKeyboard } from "./hooks/use-app-keyboard.ts";
import { useRepositoryFiles } from "./hooks/use-repository-files.ts";
import { useSessionList } from "./hooks/use-session-list.ts";
import { useTransactionList } from "./hooks/use-transaction-list.ts";
import { useTuiPreferences } from "./hooks/use-tui-preferences.ts";
import { useWorkerConnection } from "./hooks/use-worker-connection.ts";
import { WorkerResponseError, type WorkerClient } from "./ipc/client.ts";
import {
  initialRivetState,
  reduceRivetState,
  type RivetState,
} from "./state/reducer.ts";
import {
  findCommand,
  type CommandContext,
  type PanelName,
  type WorkMode,
} from "./ui/command-registry.ts";
import {
  COMPOSER_PLACEHOLDER,
  explicitTaskMode,
  type Overlay,
} from "./ui/app-model.ts";
import { createAppCommandActions } from "./ui/app-command-actions.ts";
import {
  commandArgumentRequest,
  fileMentionQuery,
  slashQuery,
} from "./ui/commands.ts";
import { computeLayout } from "./ui/layout.ts";
import {
  configurationPayload,
  createConfigurationDraft,
  validateConfigurationDraft,
  type ConfigurationDraft,
  type PublicRuntimeConfiguration,
} from "./ui/runtime-config.ts";

export interface RivetAppProps {
  initialState?: RivetState;
  loadPreferences?: boolean;
  noColor?: boolean;
  client?: WorkerClient;
  onPermission?: (requestId: string, approved: boolean) => void;
  onRecover?: () => void;
  onExit?: () => void;
}

export function RivetApp({
  initialState: suppliedInitialState,
  loadPreferences,
  noColor = process.env.NO_COLOR !== undefined,
  client,
  onPermission,
  onRecover,
  onExit,
}: RivetAppProps) {
  const initialState = suppliedInitialState ?? initialRivetState();
  const [state, dispatch] = useReducer(reduceRivetState, initialState);
  const [screen, setScreen] = useState<"welcome" | "session">(
    initialState.sessionId !== null || initialState.timeline.length > 0
      ? "session"
      : "welcome",
  );
  const [input, setInput] = useState("");
  const [mode, setMode] = useState<WorkMode>("ASK");
  const [selectedModel, setSelectedModel] = useState(initialState.model);
  const [themeName, setThemeName] = useState<ThemeName>("dark");
  const [openPanel, setOpenPanel] = useState<PanelName | null>(null);
  const [evidenceExpanded, setEvidenceExpanded] = useState(false);
  const [overlays, setOverlays] = useState<Overlay[]>([]);
  const [overlayQuery, setOverlayQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [fileQuery, setFileQuery] = useState("");
  const [contextFiles, setContextFiles] = useState<string[]>([]);
  const [attachments, setAttachments] = useState<PasteAttachment[]>([]);
  const [history, setHistory] = useState<string[]>([]);
  const [historyCursor, setHistoryCursor] = useState(-1);
  const [recentCommandIds, setRecentCommandIds] = useState<string[]>([]);
  const [inlineError, setInlineError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [configurationDraft, setConfigurationDraft] = useState(
    createConfigurationDraft(configurationFromState(initialState)),
  );
  const [configurationErrors, setConfigurationErrors] = useState<string[]>([]);
  const [configurationSaving, setConfigurationSaving] = useState(false);
  const [activeRequestId, setActiveRequestId] = useState<string | null>(null);
  const modelTouched = useRef(false);
  const contextFilesRef = useRef(contextFiles);
  const attachmentsRef = useRef(attachments);
  const nextAttachmentIndex = useRef(0);
  const dimensions = useTerminalDimensions();
  const layout = computeLayout(dimensions.width, dimensions.height);
  const compact = dimensions.width < 96;
  const theme = createTheme(noColor, themeName);
  const topOverlay = overlays.at(-1) ?? null;
  const running = activeRequestId !== null;
  contextFilesRef.current = contextFiles;
  attachmentsRef.current = attachments;

  useWorkerConnection(client, dispatch);
  useTuiPreferences(
    { mode, theme: themeName, panel: openPanel },
    { setMode, setTheme: setThemeName, setPanel: setOpenPanel },
    client !== undefined &&
      (loadPreferences ?? suppliedInitialState === undefined),
  );

  useEffect(() => {
    if (
      (!modelTouched.current || !state.models.includes(selectedModel)) &&
      state.model !== "未连接"
    ) {
      setSelectedModel(state.model);
    }
  }, [selectedModel, state.model, state.models]);

  useEffect(() => {
    const commandMode = explicitTaskMode(input);
    if (commandMode !== null && commandMode !== mode) setMode(commandMode);
  }, [input, mode]);

  useEffect(() => {
    if (openPanel !== "Evidence") setEvidenceExpanded(false);
  }, [openPanel]);

  const argumentRequest = commandArgumentRequest(input);
  const argumentCommand =
    argumentRequest === null ? null : findCommand(argumentRequest.commandName);
  const fileListQuery =
    topOverlay?.kind === "files"
      ? fileQuery
      : topOverlay?.kind === "arguments" &&
          argumentRequest?.commandName === topOverlay.commandName &&
          argumentCommand !== null &&
          ["path", "optional-path"].includes(argumentCommand.argumentKind)
        ? argumentRequest.query
        : null;
  const sessionListRequested =
    openPanel === "Sessions" ||
    topOverlay?.kind === "palette" ||
    (topOverlay?.kind === "arguments" &&
      argumentCommand?.name === "resume");
  const transactionListRequested =
    openPanel === "Evidence" ||
    topOverlay?.kind === "palette" ||
    (topOverlay?.kind === "arguments" &&
      argumentCommand?.requiresTransaction === true);
  const readyClient = state.connection === "ready" ? client : undefined;

  const { files, loading: filesLoading } = useRepositoryFiles(
    readyClient,
    initialState.fileTree,
    fileListQuery,
    setInlineError,
  );
  useSessionList(readyClient, sessionListRequested, setInlineError);
  useTransactionList(readyClient, transactionListRequested, setInlineError);
  const selectedTransaction =
    state.transactions.length > 0
      ? state.transactions[
          Math.max(0, Math.min(selectedIndex, state.transactions.length - 1))
        ]?.transactionId ?? null
      : state.transaction === "无"
        ? null
        : state.transaction;
  useEvidenceDetail(
    readyClient,
    openPanel === "Evidence",
    selectedTransaction,
    setInlineError,
  );

  const commandContext: CommandContext = {
    modelConfigured: state.credentialConfigured,
    currentModel:
      selectedModel === "未连接" || selectedModel === "未配置"
        ? state.models[0] ?? "deepseek-v4-pro"
        : selectedModel,
    hasSession: screen === "session" || state.sessionId !== null,
    transactionId: state.transaction === "无" ? null : state.transaction,
    verificationStatus: state.verifyStatus,
    evidenceId: state.evidenceId === "无" ? null : state.evidenceId,
    acceptanceReady: state.acceptanceReady,
    transactionStates: Object.fromEntries(
      state.transactions.map((transaction) => [
        transaction.transactionId,
        transaction.state,
      ]),
    ),
  };
  const {
    argumentOptions,
    historyOptions,
    modelOptions,
    paletteResults,
    rankedFiles,
    slashResults,
  } = useCommandOptions({
    state,
    topOverlay,
    input,
    overlayQuery,
    selectedModel,
    recentCommandIds,
    history,
    files,
    fileQuery,
    contextFiles,
    argumentRequest,
    models: state.models,
  });

  const pushOverlay = (overlay: Overlay) => {
    if (overlay.kind === "config") {
      setConfigurationDraft(
        createConfigurationDraft(configurationFromState(state)),
      );
      setConfigurationErrors([]);
    }
    setSelectedIndex(0);
    setOverlays((current) => [
      ...current.filter((item) => item.kind !== overlay.kind),
      overlay,
    ]);
  };
  const closeTopOverlay = () => {
    setOverlays((current) => current.slice(0, -1));
    setSelectedIndex(0);
  };

  const handleInput = (value: string) => {
    if (value === input) return;
    setInput(value);
    setInlineError(null);
    setHistoryCursor(-1);
    const slash = slashQuery(value);
    if (slash !== null) {
      if (topOverlay?.kind !== "slash") pushOverlay({ kind: "slash" });
      return;
    }
    const argument = commandArgumentRequest(value);
    if (argument !== null) {
      setOverlays((current) => [
        ...current.filter(
          (overlay) =>
            overlay.kind !== "slash" && overlay.kind !== "arguments",
        ),
        { kind: "arguments", commandName: argument.commandName },
      ]);
      return;
    }
    setOverlays((current) =>
      current.filter(
        (overlay) =>
          overlay.kind !== "slash" && overlay.kind !== "arguments",
      ),
    );
    const mention = fileMentionQuery(value);
    if (mention !== null && topOverlay?.kind !== "files") {
      setFileQuery(mention);
      pushOverlay({ kind: "files" });
    }
  };

  const {
    executeInput,
    executeOutcome,
    selectCommand,
    selectFile,
    selectModel,
  } = createAppCommandActions({
    rivetState: state,
    mode,
    running,
    contextFiles,
    models: state.models,
    attachments,
    commandContext,
    client,
    onExit,
    dispatch,
    setScreen,
    setInput,
    setMode,
    setThemeName,
    setOpenPanel,
    setOverlays,
    setOverlayQuery,
    setFileQuery,
    setSelectedIndex,
    setContextFiles,
    setAttachments,
    setHistory,
    setRecentCommandIds,
    setInlineError,
    setActiveRequestId,
    pushOverlay,
    closeTopOverlay,
    markModelTouched: () => {
      modelTouched.current = true;
    },
    setSelectedModel,
    getContextFiles: () => contextFilesRef.current,
    getAttachments: () => attachmentsRef.current,
  });

  useAppKeyboard(
    {
      rivet: state,
      topOverlay,
      openPanel,
      input,
      history,
      historyCursor,
      mode,
      running,
      activeRequestId,
      selectedIndex,
      evidenceExpanded,
      slashResults,
      paletteResults,
      rankedFiles,
      historyOptions,
      modelOptions,
      argumentOptions,
    },
    {
      dispatch,
      setInput,
      setAttachments,
      setInlineError,
      setNotice,
      setOpenPanel,
      setScreen,
      setMode,
      setHistoryCursor,
      setSelectedIndex,
      setEvidenceExpanded,
      closeTopOverlay,
      pushOverlay: (overlay) => {
        if (overlay.kind === "palette" || overlay.kind === "history") {
          setOverlayQuery("");
        }
        if (overlay.kind === "files") setFileQuery("");
        pushOverlay(overlay);
      },
      selectCommand,
      selectFile,
      selectModel,
      executeInput,
      executeOutcome,
    },
    { client, onPermission, onRecover, onExit },
  );

  const composerFocused =
    state.permission === null &&
    (topOverlay === null || topOverlay.kind === "slash") &&
    (screen === "welcome" || openPanel === null);
  const saveConfiguration = () => {
    const errors = validateConfigurationDraft(configurationDraft);
    setConfigurationErrors(errors);
    if (errors.length > 0) return;
    if (client === undefined || state.connection !== "ready") {
      setConfigurationErrors(["Worker 未就绪"]);
      return;
    }
    setConfigurationSaving(true);
    void client
      .request("config.update", configurationPayload(configurationDraft))
      .then((result) => {
        if (
          result !== null &&
          !Array.isArray(result) &&
          typeof result === "object"
        ) {
          dispatch({
            kind: "configuration-loaded",
            payload: result as Record<string, JsonValue>,
          });
        }
        modelTouched.current = true;
        setSelectedModel(configurationDraft.model.trim());
        setConfigurationDraft((current) => ({
          ...current,
          apiKey: "",
          apiKeyAction: "keep",
        }));
        setNotice("已保存");
        closeTopOverlay();
      })
      .catch((error: unknown) => {
        setConfigurationErrors([
          error instanceof WorkerResponseError
            ? `${error.code}：${error.message}`
            : error instanceof Error
              ? error.message
              : "配置保存失败",
        ]);
      })
      .finally(() => setConfigurationSaving(false));
  };
  const modelLabel = selectedModel;
  const composer = (
    <Composer
      value={input}
      placeholder={COMPOSER_PLACEHOLDER}
      mode={mode}
      modelLabel={modelLabel}
      credentialConfigured={state.credentialConfigured}
      focused={composerFocused}
      compact={compact}
      running={running}
      contextFiles={contextFiles}
      attachments={attachments}
      error={
        inlineError ??
        (state.error === null
          ? null
          : `${state.error}${state.connection === "crashed" ? " · Ctrl+Shift+R 重连" : ""}`)
      }
      theme={theme}
      onInput={handleInput}
      onSubmit={executeInput}
      onRemoveContext={(path) =>
        setContextFiles((current) => current.filter((item) => item !== path))
      }
      onRemoveAttachment={(id) =>
        setAttachments((current) => current.filter((item) => item.id !== id))
      }
      onLargePaste={(content) => {
        const error = pasteAttachmentError(attachments, content);
        if (error !== null) {
          setInlineError(error);
          return;
        }
        const attachment = createPasteAttachment(
          content,
          nextAttachmentIndex.current++,
        );
        setAttachments((current) => [
          ...current,
          attachment,
        ]);
      }}
      onPathPaste={(path) => {
        setFileQuery(path);
        pushOverlay({ kind: "files" });
      }}
      onOpenModels={() => pushOverlay({ kind: "models" })}
      onOpenConfig={() => pushOverlay({ kind: "config" })}
    />
  );

  return (
    <box
      width="100%"
      height="100%"
      flexDirection="column"
      backgroundColor={theme.background}
    >
      {screen === "welcome" ? (
        <WelcomeScreen
          state={state}
          layout={layout}
          theme={theme}
          composer={composer}
        />
      ) : (
        <SessionScreen
          state={state}
          running={running}
          openPanel={openPanel}
          layout={layout}
          theme={theme}
          composer={composer}
          selectedContextFiles={contextFiles}
          selectedModuleIndex={selectedIndex}
          evidenceExpanded={evidenceExpanded}
        />
      )}
      <AppOverlays
        display={{
          topOverlay,
          input,
          overlayQuery,
          fileQuery,
          slashResults,
          paletteResults,
          files,
          historyOptions,
          modelOptions,
          argumentRequest,
          argumentOptions,
          selectedIndex,
          filesLoading,
          contextFiles,
          notice,
          configurationDraft,
          configurationErrors,
          configurationSaving,
        }}
        actions={{
          queryPalette: (value) => {
            setOverlayQuery(value);
            setSelectedIndex(0);
          },
          queryFiles: (value) => {
            setFileQuery(value);
            setSelectedIndex(0);
          },
          queryArgument: (commandName, value) => {
            setInput(`/${commandName} ${value}`);
            setSelectedIndex(0);
          },
          selectCommand: (command) => selectCommand(command, false),
          selectFile: (path) => selectFile(path),
          selectHistory: (option) => {
            setInput(option.title);
            closeTopOverlay();
          },
          selectModel: (option) => selectModel(option.id),
          selectArgument: (commandName, option) => {
            if (option.available === false) {
              setInlineError(option.description ?? "该参数当前不可用");
              return;
            }
            setInput(`/${commandName} ${option.id}`);
            closeTopOverlay();
          },
          hover: setSelectedIndex,
          changeConfiguration: (draft: ConfigurationDraft) => {
            setConfigurationDraft(draft);
            if (configurationErrors.length > 0) {
              setConfigurationErrors(validateConfigurationDraft(draft));
            }
          },
          saveConfiguration,
          closeConfiguration: closeTopOverlay,
        }}
        state={state}
        commandContext={commandContext}
        compact={compact || dimensions.height < 20}
        viewportHeight={dimensions.height}
        theme={theme}
      />
    </box>
  );

}

function configurationFromState(state: RivetState): PublicRuntimeConfiguration {
  return {
    baseUrl: state.baseUrl,
    credentialConfigured: state.credentialConfigured,
    maxCostUsd: state.maxCostUsd,
    maxRounds: state.maxRounds,
    maxTotalTokens: state.maxTotalTokens,
    model:
      state.model === "未连接"
        ? state.models[0] ?? "deepseek-v4-pro"
        : state.model,
    models: state.models,
    safeMode: state.safeMode,
  };
}
