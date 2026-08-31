import type { RivetState } from "../state/reducer.ts";
import type { Overlay } from "../ui/app-model.ts";
import { dangerousImpact } from "../ui/app-model.ts";
import type {
  CommandContext,
  CommandDescriptor,
} from "../ui/command-registry.ts";
import type { CommandSearchResult } from "../ui/command-search.ts";
import type { CommandArgumentRequest } from "../ui/commands.ts";
import { slashQuery } from "../ui/commands.ts";
import type { ConfigurationDraft } from "../ui/runtime-config.ts";
import { CommandPalette } from "./command-palette.tsx";
import { ConfigDialog } from "./config-dialog.tsx";
import { ConfirmDialog } from "./confirm-dialog.tsx";
import { FilePicker } from "./file-picker.tsx";
import { InfoOverlay } from "./info-overlay.tsx";
import { LeaderHelp } from "./leader-help.tsx";
import { OptionPicker, type PickerOption } from "./option-picker.tsx";
import { PermissionModal } from "./permission-modal.tsx";
import type { RivetTheme } from "./theme.ts";

export interface OverlayDisplayState {
  topOverlay: Overlay | null;
  input: string;
  overlayQuery: string;
  fileQuery: string;
  slashResults: CommandSearchResult[];
  paletteResults: CommandSearchResult[];
  files: string[];
  historyOptions: PickerOption[];
  modelOptions: PickerOption[];
  argumentRequest: CommandArgumentRequest | null;
  argumentOptions: PickerOption[];
  selectedIndex: number;
  filesLoading: boolean;
  contextFiles: string[];
  notice: string | null;
  configurationDraft: ConfigurationDraft;
  configurationErrors: string[];
  configurationSaving: boolean;
}

export interface OverlayActions {
  queryPalette(value: string): void;
  queryFiles(value: string): void;
  queryArgument(commandName: string, value: string): void;
  selectCommand(command: CommandDescriptor): void;
  selectFile(path: string): void;
  selectHistory(option: PickerOption): void;
  selectModel(option: PickerOption): void;
  selectArgument(commandName: string, option: PickerOption): void;
  hover(index: number): void;
  changeConfiguration(draft: ConfigurationDraft): void;
  saveConfiguration(): void;
  closeConfiguration(): void;
}

export function AppOverlays({
  display,
  actions,
  state,
  commandContext,
  compact,
  viewportHeight,
  theme,
}: {
  display: OverlayDisplayState;
  actions: OverlayActions;
  state: RivetState;
  commandContext: CommandContext;
  compact: boolean;
  viewportHeight: number;
  theme: RivetTheme;
}) {
  const overlay = display.topOverlay;
  return (
    <>
      {overlay?.kind === "palette" || overlay?.kind === "slash" ? (
        <CommandPalette
          variant={overlay.kind}
          query={
            overlay.kind === "slash"
              ? slashQuery(display.input) ?? ""
              : display.overlayQuery
          }
          results={
            overlay.kind === "slash"
              ? display.slashResults
              : display.paletteResults
          }
          selectedIndex={display.selectedIndex}
          context={commandContext}
          compact={compact}
          viewportHeight={viewportHeight}
          theme={theme}
          onQuery={actions.queryPalette}
          onSelect={actions.selectCommand}
          onHover={actions.hover}
        />
      ) : null}
      {overlay?.kind === "files" ? (
        <FilePicker
          query={display.fileQuery}
          files={display.files}
          selectedIndex={display.selectedIndex}
          loading={display.filesLoading}
          selectedPaths={display.contextFiles}
          compact={compact}
          viewportHeight={viewportHeight}
          theme={theme}
          onQuery={actions.queryFiles}
          onSelect={actions.selectFile}
          onHover={actions.hover}
        />
      ) : null}
      {overlay?.kind === "history" ? (
        <OptionPicker
          title="输入历史"
          placeholder="搜索本次运行中的输入"
          query={display.overlayQuery}
          options={display.historyOptions}
          selectedIndex={display.selectedIndex}
          compact={compact}
          viewportHeight={viewportHeight}
          theme={theme}
          onQuery={actions.queryPalette}
          onSelect={actions.selectHistory}
          onHover={actions.hover}
        />
      ) : null}
      {overlay?.kind === "models" ? (
        <OptionPicker
          title="选择模型"
          placeholder="搜索 Provider 或模型"
          query={display.overlayQuery}
          options={display.modelOptions}
          selectedIndex={display.selectedIndex}
          compact={compact}
          viewportHeight={viewportHeight}
          theme={theme}
          onQuery={actions.queryPalette}
          onSelect={actions.selectModel}
          onHover={actions.hover}
        />
      ) : null}
      {overlay?.kind === "config" ? (
        <ConfigDialog
          draft={display.configurationDraft}
          credentialConfigured={state.credentialConfigured}
          errors={display.configurationErrors}
          saving={display.configurationSaving}
          compact={compact}
          viewportHeight={viewportHeight}
          theme={theme}
          onChange={actions.changeConfiguration}
          onSave={actions.saveConfiguration}
          onClose={actions.closeConfiguration}
        />
      ) : null}
      {overlay?.kind === "arguments" && display.argumentRequest !== null ? (
        <OptionPicker
          title={`/${overlay.commandName} 参数`}
          placeholder="搜索可用参数"
          query={display.argumentRequest.query}
          options={display.argumentOptions}
          selectedIndex={display.selectedIndex}
          compact={compact}
          viewportHeight={viewportHeight}
          theme={theme}
          onQuery={(value) => actions.queryArgument(overlay.commandName, value)}
          onSelect={(option) =>
            actions.selectArgument(overlay.commandName, option)
          }
          onHover={actions.hover}
        />
      ) : null}
      {overlay?.kind === "leader" ? <LeaderHelp theme={theme} /> : null}
      {overlay?.kind === "info" ? (
        <InfoOverlay
          title={overlay.title}
          lines={overlay.lines}
          compact={compact}
          theme={theme}
        />
      ) : null}
      {overlay?.kind === "confirm" ? (
        <ConfirmDialog
          title={overlay.command.title}
          description={overlay.command.description}
          impact={dangerousImpact(overlay.command.name, state)}
          compact={compact}
          viewportHeight={viewportHeight}
          theme={theme}
        />
      ) : null}
      {state.permission === null ? null : (
        <PermissionModal
          permission={state.permission}
          compact={compact}
          viewportHeight={viewportHeight}
          theme={theme}
        />
      )}
      {display.notice === null ? null : (
        <box
          position="absolute"
          zIndex={50}
          bottom={1}
          right={2}
          backgroundColor={theme.surfaceHover}
          paddingX={1}
        >
          <text fg={theme.textSecondary} content={display.notice} />
        </box>
      )}
    </>
  );
}
