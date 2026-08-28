import { useMemo } from "react";

import { rankFilePaths } from "../components/file-picker.tsx";
import type { RivetState } from "../state/reducer.ts";
import { MODELS, type Overlay } from "../ui/app-model.ts";
import { COMMAND_REGISTRY } from "../ui/command-registry.ts";
import { searchCommands } from "../ui/command-search.ts";
import {
  commandArgumentCompletions,
  moduleCommandUnavailableReason,
  slashQuery,
  type CommandArgumentRequest,
} from "../ui/commands.ts";
import { searchHistory } from "../ui/history.ts";
import { createPaletteResources } from "../ui/palette-resources.ts";

export function useCommandOptions({
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
}: {
  state: RivetState;
  topOverlay: Overlay | null;
  input: string;
  overlayQuery: string;
  selectedModel: string;
  recentCommandIds: string[];
  history: string[];
  files: string[];
  fileQuery: string;
  contextFiles: string[];
  argumentRequest: CommandArgumentRequest | null;
}) {
  const slashResults = useMemo(
    () =>
      searchCommands(
        COMMAND_REGISTRY,
        slashQuery(input) ?? "",
        recentCommandIds,
      ),
    [input, recentCommandIds],
  );
  const paletteResources = useMemo(
    () => createPaletteResources(state, files, MODELS),
    [files, state],
  );
  const paletteResults = useMemo(
    () =>
      searchCommands(
        [...COMMAND_REGISTRY, ...paletteResources],
        overlayQuery,
        recentCommandIds,
      ),
    [overlayQuery, paletteResources, recentCommandIds],
  );
  const historyOptions = useMemo(
    () =>
      searchHistory(history, overlayQuery).map((value, index) => ({
        id: `history-${index}-${value}`,
        title: value,
      })),
    [history, overlayQuery],
  );
  const modelOptions = MODELS.filter((model) =>
    model.toLocaleLowerCase().includes(overlayQuery.toLocaleLowerCase()),
  ).map((model) => ({
    id: model,
    title: model,
    description: model === selectedModel ? "当前" : "",
    marker: model === selectedModel ? "●" : "○",
  }));
  const rankedFiles = useMemo(
    () => rankFilePaths(files, fileQuery, contextFiles),
    [contextFiles, fileQuery, files],
  );
  const argumentOptions = useMemo(() => {
    if (
      topOverlay?.kind !== "arguments" ||
      argumentRequest?.commandName !== topOverlay.commandName
    ) {
      return [];
    }
    return commandArgumentCompletions(
      topOverlay.commandName,
      argumentRequest.query,
      {
        models: [...MODELS],
        sessions: state.sessions,
        transactions:
          state.transaction === "无" ? [] : [state.transaction],
        modules: state.modules,
        moduleStatuses: state.moduleStatuses,
        files,
        contextFiles,
      },
    ).map((value) => {
      const unavailableReason =
        topOverlay.commandName === "modules"
          ? moduleCommandUnavailableReason(value, state.moduleStatuses)
          : null;
      return {
        id: value,
        title: value,
        ...(unavailableReason === null
          ? topOverlay.commandName === "modules" && /^(disable|sleep)\b/.test(value)
            ? { description: "高影响操作 · 执行前需要确认" }
            : {}
          : { available: false, description: unavailableReason }),
      };
    });
  }, [
    argumentRequest,
    contextFiles,
    files,
    state.modules,
    state.moduleStatuses,
    state.sessions,
    state.transaction,
    topOverlay,
  ]);

  return {
    argumentOptions,
    historyOptions,
    modelOptions,
    paletteResults,
    rankedFiles,
    slashResults,
  };
}
