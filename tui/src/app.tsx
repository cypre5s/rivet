import { useTerminalDimensions } from "@opentui/react";
import { useEffect, useReducer, useRef, useState } from "react";

import { AppOverlays } from "./components/app-overlays.tsx";
import {
  Composer,
  createPasteAttachment,
  pasteAttachmentError,
  type PasteAttachment,
} from "./components/composer.tsx";
import { createTheme } from "./components/theme.ts";
import { WelcomeScreen } from "./components/welcome-screen.tsx";
import { WorkbenchScreen } from "./components/workbench-screen.tsx";
import { useAppKeyboard } from "./hooks/use-app-keyboard.ts";
import { useCommandOptions } from "./hooks/use-command-options.ts";
import { useEvidenceDetail } from "./hooks/use-evidence-detail.ts";
import { useRepositoryFiles } from "./hooks/use-repository-files.ts";
import { useTransactionList } from "./hooks/use-transaction-list.ts";
import { useWorkerConnection } from "./hooks/use-worker-connection.ts";
import type { WorkerClient } from "./ipc/client.ts";
import {
  initialRivetState,
  reduceRivetState,
  type RivetState,
} from "./state/reducer.ts";
import { createAppCommandActions } from "./ui/app-command-actions.ts";
import { COMPOSER_PLACEHOLDER, type Overlay } from "./ui/app-model.ts";
import { findCommand, type CommandContext, type PanelName } from "./ui/command-registry.ts";
import {
  commandArgumentRequest,
  fileMentionQuery,
  slashQuery,
} from "./ui/commands.ts";
import { computeLayout } from "./ui/layout.ts";

export interface RivetAppProps {
  initialState?: RivetState;
  noColor?: boolean;
  client?: WorkerClient;
  onPermission?: (requestId: string, approved: boolean) => void;
  onRecover?: () => void;
  onExit?: () => void;
}

export function RivetApp({
  initialState: suppliedInitialState,
  noColor = process.env.NO_COLOR !== undefined,
  client,
  onPermission,
  onRecover,
  onExit,
}: RivetAppProps) {
  const initialState = suppliedInitialState ?? initialRivetState();
  const [state, dispatch] = useReducer(reduceRivetState, initialState);
  const [screen, setScreen] = useState<"welcome" | "workbench">(
    initialState.timeline.length > 0 || initialState.transaction !== "无"
      ? "workbench"
      : "welcome",
  );
  const [input, setInput] = useState("");
  const [selectedModel, setSelectedModel] = useState(initialState.model);
  const [openPanel, setOpenPanel] = useState<PanelName | null>(null);
  const [evidenceExpanded, setEvidenceExpanded] = useState(false);
  const [overlays, setOverlays] = useState<Overlay[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [fileQuery, setFileQuery] = useState("");
  const [selectedFiles, setSelectedFiles] = useState<string[]>([]);
  const [attachments, setAttachments] = useState<PasteAttachment[]>([]);
  const [inlineError, setInlineError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [activeRequestId, setActiveRequestId] = useState<string | null>(null);
  const modelTouched = useRef(false);
  const selectedFilesRef = useRef(selectedFiles);
  const attachmentsRef = useRef(attachments);
  const nextAttachmentIndex = useRef(0);
  const dimensions = useTerminalDimensions();
  const layout = computeLayout(dimensions.width, dimensions.height);
  const compact = layout.mode === "minimal";
  const theme = createTheme(noColor);
  const topOverlay = overlays.at(-1) ?? null;
  const running = activeRequestId !== null;
  selectedFilesRef.current = selectedFiles;
  attachmentsRef.current = attachments;

  useWorkerConnection(client, dispatch);

  useEffect(() => {
    if (
      (!modelTouched.current || !state.models.includes(selectedModel)) &&
      state.model !== "未连接"
    ) {
      setSelectedModel(state.model);
    }
  }, [selectedModel, state.model, state.models]);

  useEffect(() => {
    if (openPanel !== "Evidence") setEvidenceExpanded(false);
  }, [openPanel]);

  const argumentRequest = commandArgumentRequest(input);
  const argumentCommand =
    argumentRequest === null ? null : findCommand(argumentRequest.commandName);
  const fileListQuery = topOverlay?.kind === "files" ? fileQuery : null;
  const transactionListRequested =
    openPanel !== null || argumentCommand?.requiresTransaction === true;
  const readyClient = state.connection === "ready" ? client : undefined;

  const { files, loading: filesLoading } = useRepositoryFiles(
    readyClient,
    initialState.fileTree,
    fileListQuery,
    setInlineError,
  );
  useTransactionList(
    readyClient,
    transactionListRequested,
    setInlineError,
  );
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
  const { argumentOptions, modelOptions, rankedFiles, slashResults } =
    useCommandOptions({
      state,
      topOverlay,
      input,
      selectedModel,
      files,
      fileQuery,
      selectedFiles,
      argumentRequest,
      models: state.models,
    });

  const pushOverlay = (overlay: Overlay) => {
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
    if (slashQuery(value) !== null) {
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
    if (mention !== null) {
      setFileQuery(mention);
      if (topOverlay?.kind !== "files") pushOverlay({ kind: "files" });
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
    running,
    selectedFiles,
    models: state.models,
    commandContext,
    client,
    dispatch,
    setScreen,
    setInput,
    setOpenPanel,
    setOverlays,
    setFileQuery,
    setSelectedIndex,
    setSelectedFiles,
    setAttachments,
    setInlineError,
    setActiveRequestId,
    pushOverlay,
    closeTopOverlay,
    markModelTouched: () => {
      modelTouched.current = true;
    },
    setSelectedModel,
    getSelectedFiles: () => selectedFilesRef.current,
    getAttachments: () => attachmentsRef.current,
  });

  useAppKeyboard(
    {
      rivet: state,
      topOverlay,
      openPanel,
      input,
      running,
      activeRequestId,
      selectedIndex,
      evidenceExpanded,
      slashResults,
      rankedFiles,
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
      setSelectedIndex,
      setEvidenceExpanded,
      closeTopOverlay,
      pushOverlay,
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
    openPanel === null;
  const composer = (
    <Composer
      value={input}
      placeholder={COMPOSER_PLACEHOLDER}
      modelLabel={selectedModel}
      credentialConfigured={state.credentialConfigured}
      focused={composerFocused}
      compact={compact}
      running={running}
      selectedFiles={selectedFiles}
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
      onRemoveFile={(path) =>
        setSelectedFiles((current) => current.filter((item) => item !== path))
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
        setAttachments((current) => [...current, attachment]);
      }}
      onPathPaste={(path) => {
        setFileQuery(path);
        pushOverlay({ kind: "files" });
      }}
      onOpenModels={() => pushOverlay({ kind: "models" })}
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
        <WorkbenchScreen
          state={state}
          running={running}
          openPanel={openPanel}
          layout={layout}
          theme={theme}
          composer={composer}
          selectedTransactionIndex={selectedIndex}
          evidenceExpanded={evidenceExpanded}
          onSelectPanel={(panel) => {
            setOpenPanel(panel);
            if (panel === "Evidence") setSelectedIndex(0);
          }}
        />
      )}
      <AppOverlays
        display={{
          topOverlay,
          input,
          fileQuery,
          slashResults,
          files,
          modelOptions,
          argumentRequest,
          argumentOptions,
          selectedIndex,
          filesLoading,
          selectedFiles,
          notice,
        }}
        actions={{
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
          selectModel: (option) => selectModel(option.id),
          selectArgument: (commandName, option) => {
            setInput(`/${commandName} ${option.id}`);
            closeTopOverlay();
          },
          hover: setSelectedIndex,
        }}
        state={state}
        commandContext={commandContext}
        compact={compact}
        viewportHeight={dimensions.height}
        theme={theme}
      />
    </box>
  );
}
