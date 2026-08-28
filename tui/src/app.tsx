import { useKeyboard, useTerminalDimensions } from "@opentui/react";
import { useEffect, useReducer, useState } from "react";

import { CommandPalette } from "./components/command-palette.tsx";
import { Header } from "./components/header.tsx";
import { InputBar } from "./components/input-bar.tsx";
import { InspectorPanel } from "./components/inspector-panel.tsx";
import { PermissionModal } from "./components/permission-modal.tsx";
import { RepositoryPanel } from "./components/repository-panel.tsx";
import { createTheme } from "./components/theme.ts";
import { TimelinePanel } from "./components/timeline-panel.tsx";
import type { WorkerClient } from "./ipc/client.ts";
import {
  initialRivetState,
  reduceRivetState,
  type InspectorTab,
  type RivetState,
} from "./state/reducer.ts";
import { resolveKeyCommand } from "./ui/keymap.ts";
import { computeLayout } from "./ui/layout.ts";
import { parseCommandInput } from "./ui/commands.ts";
import { INSPECTOR_TABS } from "./ui/view-model.ts";

export interface RivetAppProps {
  initialState?: RivetState;
  noColor?: boolean;
  client?: WorkerClient;
  onPermission?: (requestId: string, approved: boolean) => void;
  onRecover?: () => void;
}

export function RivetApp({
  initialState = initialRivetState(),
  noColor = process.env.NO_COLOR !== undefined,
  client,
  onPermission,
  onRecover,
}: RivetAppProps) {
  const [state, dispatch] = useReducer(reduceRivetState, initialState);
  const [input, setInput] = useState("");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [focusIndex, setFocusIndex] = useState(0);
  const [activeTab, setActiveTab] = useState<InspectorTab>(state.inspectorTab);
  const [hiddenBefore, setHiddenBefore] = useState(-1);
  const [activeRequestId, setActiveRequestId] = useState<string | null>(null);
  const dimensions = useTerminalDimensions();
  const layout = computeLayout(dimensions.width, dimensions.height);
  const theme = createTheme(noColor);

  useEffect(() => {
    if (client === undefined) return;
    const unsubscribeEvent = client.onEvent((event) =>
      dispatch({ kind: "trace", event }),
    );
    const unsubscribeStatus = client.onStatus((status) =>
      dispatch({ kind: "worker-status", ...status }),
    );
    const unsubscribeDiagnostic = client.onDiagnostic(() => {});
    void client.start().catch((error: unknown) => {
      dispatch({
        kind: "worker-status",
        state: "crashed",
        summary: error instanceof Error ? error.message : "Worker 握手失败",
      });
    });
    return () => {
      unsubscribeEvent();
      unsubscribeStatus();
      unsubscribeDiagnostic();
      client.close();
    };
  }, [client]);

  const submit = (value: string) => {
    const trimmed = value.trim();
    if (trimmed.length === 0) return;
    setInput("");
    if (client === undefined) return;
    let command;
    try {
      command = parseCommandInput(trimmed);
    } catch (error) {
      dispatch({
        kind: "local-error",
        summary: error instanceof Error ? error.message : "命令格式错误",
      });
      return;
    }
    const tracked = client.beginRequest(command.method, command.params);
    setActiveRequestId(tracked.requestId);
    void tracked.result
      .catch((error: unknown) => {
        dispatch({
          kind: "local-error",
          summary: error instanceof Error ? error.message : "任务提交失败",
        });
      })
      .finally(() => setActiveRequestId(null));
  };

  useKeyboard((key) => {
    const command = resolveKeyCommand({
      name: key.name.toLowerCase(),
      shift: key.shift,
      ctrl: key.ctrl,
    });
    if (state.permission !== null && (key.name === "a" || key.name === "d")) {
      key.preventDefault();
      const approved = key.name === "a";
      onPermission?.(state.permission.requestId, approved);
      if (client !== undefined) {
        void client
          .request("permission.resolve", {
            request_id: state.permission.requestId,
            approved,
          })
          .catch(() => {});
      }
      return;
    }
    switch (command) {
      case "focus.next":
        key.preventDefault();
        setFocusIndex((value) => (value + 1) % 4);
        return;
      case "focus.previous":
        key.preventDefault();
        setFocusIndex((value) => (value + 3) % 4);
        return;
      case "palette.open":
        key.preventDefault();
        setPaletteOpen(true);
        return;
      case "timeline.clear":
        key.preventDefault();
        setHiddenBefore(state.lastSequence);
        return;
      case "task.cancel":
        key.preventDefault();
        if (client !== undefined && activeRequestId !== null) {
          client.cancel(activeRequestId);
        }
        return;
      case "worker.recover":
        key.preventDefault();
        if (state.connection === "crashed") onRecover?.();
        return;
      case "overlay.close":
        key.preventDefault();
        setPaletteOpen(false);
        return;
      default:
        if (key.name === "right" && focusIndex === 2) {
          const index = INSPECTOR_TABS.indexOf(activeTab);
          setActiveTab(INSPECTOR_TABS[(index + 1) % INSPECTOR_TABS.length] ?? "Plan");
        }
    }
  });

  return (
    <box
      width="100%"
      height="100%"
      flexDirection="column"
      backgroundColor={theme.background}
    >
      <Header state={state} theme={theme} />
      <box flexGrow={1} flexDirection="row">
        {layout.visiblePanels.includes("repository") ? (
          <RepositoryPanel state={state} theme={theme} />
        ) : null}
        <TimelinePanel state={state} theme={theme} hiddenBefore={hiddenBefore} />
        {layout.visiblePanels.includes("inspector") ? (
          <InspectorPanel state={state} theme={theme} activeTab={activeTab} />
        ) : null}
      </box>
      <InputBar
        value={input}
        theme={theme}
        focused={focusIndex === 0}
        onInput={setInput}
        onSubmit={submit}
      />
      {paletteOpen ? <CommandPalette theme={theme} /> : null}
      {state.permission === null ? null : (
        <PermissionModal permission={state.permission} theme={theme} />
      )}
    </box>
  );
}
