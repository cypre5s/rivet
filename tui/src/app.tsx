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
import { useCommandOptions } from "./hooks/use-command-options.ts";
import { useAppKeyboard } from "./hooks/use-app-keyboard.ts";
import { useRepositoryFiles } from "./hooks/use-repository-files.ts";
import { useSessionList } from "./hooks/use-session-list.ts";
import { useTuiPreferences } from "./hooks/use-tui-preferences.ts";
import { useWorkerConnection } from "./hooks/use-worker-connection.ts";
import type { WorkerClient } from "./ipc/client.ts";
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
  PLACEHOLDERS,
  TIPS,
  type Overlay,
} from "./ui/app-model.ts";
import { createAppCommandActions } from "./ui/app-command-actions.ts";
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
  initialState = initialRivetState(),
  noColor = process.env.NO_COLOR !== undefined,
  client,
  onPermission,
  onRecover,
  onExit,
}: RivetAppProps) {
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
  const [activeRequestId, setActiveRequestId] = useState<string | null>(null);
  const [welcomeIndex, setWelcomeIndex] = useState(0);
  const modelTouched = useRef(false);
  const nextAttachmentIndex = useRef(0);
  const dimensions = useTerminalDimensions();
  const layout = computeLayout(dimensions.width, dimensions.height);
  const compact = dimensions.width < 96;
  const theme = createTheme(noColor, themeName);
  const topOverlay = overlays.at(-1) ?? null;
  const running = activeRequestId !== null;

  useWorkerConnection(client, dispatch);
  useTuiPreferences(
    { mode, theme: themeName, panel: openPanel },
    { setMode, setTheme: setThemeName, setPanel: setOpenPanel },
    client !== undefined,
  );

  useEffect(() => {
    if (!modelTouched.current && state.model !== "未连接") {
      setSelectedModel(state.model);
    }
  }, [state.model]);

  useEffect(() => {
    if (screen !== "welcome" || input.length > 0) return;
    const timer = setInterval(
      () => setWelcomeIndex((index) => (index + 1) % PLACEHOLDERS.length),
      8_000,
    );
    return () => clearInterval(timer);
  }, [input.length, screen]);

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
  const readyClient = state.connection === "ready" ? client : undefined;

  const { files, loading: filesLoading } = useRepositoryFiles(
    readyClient,
    initialState.fileTree,
    fileListQuery,
    setInlineError,
  );
  useSessionList(readyClient, sessionListRequested, setInlineError);

  const commandContext: CommandContext = {
    modelConfigured: state.credentialConfigured,
    currentModel:
      selectedModel === "未连接" || selectedModel === "未配置"
        ? "deepseek-v4-pro"
        : selectedModel,
    hasSession: screen === "session" || state.sessionId !== null,
    transactionId: state.transaction === "无" ? null : state.transaction,
    verificationStatus: state.verifyStatus,
    evidenceId: state.evidenceId === "无" ? null : state.evidenceId,
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
    setNotice,
    setActiveRequestId,
    pushOverlay,
    closeTopOverlay,
    markModelTouched: () => {
      modelTouched.current = true;
    },
    setSelectedModel,
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
  const modelLabel = state.credentialConfigured
    ? selectedModel
    : "模型未配置 · 输入 /model 进行设置";
  const composer = (
    <Composer
      value={input}
      placeholder={PLACEHOLDERS[welcomeIndex] ?? PLACEHOLDERS[0]}
      mode={mode}
      modelLabel={modelLabel}
      focused={composerFocused}
      compact={compact}
      running={running}
      contextFiles={contextFiles}
      attachments={attachments}
      error={
        inlineError ??
        (state.error === null
          ? null
          : `${state.error}${state.connection === "crashed" ? " · Ctrl+Shift+R 恢复 Worker" : ""}`)
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
        setNotice("大段粘贴已保存为附件，不会自动提交");
      }}
      onPathPaste={(path) => {
        setNotice("检测到文件路径，是否加入上下文？");
        setFileQuery(path);
        pushOverlay({ kind: "files" });
      }}
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
          tip={TIPS[welcomeIndex] ?? TIPS[0]}
          composer={composer}
        />
      ) : (
        <SessionScreen
          state={state}
          mode={mode}
          running={running}
          openPanel={openPanel}
          layout={layout}
          theme={theme}
          composer={composer}
          selectedContextFiles={contextFiles}
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
