import { useMemo } from "react";

import { rankFilePaths } from "../components/file-picker.tsx";
import type { RivetState } from "../state/reducer.ts";
import type { Overlay } from "../ui/app-model.ts";
import { COMMAND_REGISTRY } from "../ui/command-registry.ts";
import { searchCommands } from "../ui/command-search.ts";
import {
  commandArgumentCompletions,
  slashQuery,
  type CommandArgumentRequest,
} from "../ui/commands.ts";

export function useCommandOptions({
  state,
  topOverlay,
  input,
  selectedModel,
  files,
  fileQuery,
  selectedFiles,
  argumentRequest,
  models,
}: {
  state: RivetState;
  topOverlay: Overlay | null;
  input: string;
  selectedModel: string;
  files: string[];
  fileQuery: string;
  selectedFiles: string[];
  argumentRequest: CommandArgumentRequest | null;
  models: string[];
}) {
  const slashResults = useMemo(
    () => searchCommands(COMMAND_REGISTRY, slashQuery(input) ?? ""),
    [input],
  );
  const modelOptions = models.map((model) => ({
    id: model,
    title: model,
    description: model === selectedModel ? "当前" : "",
    marker: model === selectedModel ? "●" : "○",
  }));
  const rankedFiles = useMemo(
    () => rankFilePaths(files, fileQuery, selectedFiles),
    [fileQuery, files, selectedFiles],
  );
  const transactions = useMemo(
    () => [
      ...(state.transaction === "无" ? [] : [state.transaction]),
      ...state.transactions
        .map((transaction) => transaction.transactionId)
        .filter((transactionId) => transactionId !== state.transaction),
    ],
    [state.transaction, state.transactions],
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
      { models, transactions },
    ).map((value) => ({ id: value, title: value }));
  }, [argumentRequest, models, topOverlay, transactions]);

  return {
    argumentOptions,
    modelOptions,
    rankedFiles,
    slashResults,
  };
}
