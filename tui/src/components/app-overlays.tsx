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
import { ConfirmDialog } from "./confirm-dialog.tsx";
import { FilePicker } from "./file-picker.tsx";
import { InfoOverlay } from "./info-overlay.tsx";
import { OptionPicker, type PickerOption } from "./option-picker.tsx";
import { PermissionModal } from "./permission-modal.tsx";
import { SlashMenu } from "./slash-menu.tsx";
import type { RivetTheme } from "./theme.ts";

export interface OverlayDisplayState {
  topOverlay: Overlay | null;
  input: string;
  fileQuery: string;
  slashResults: CommandSearchResult[];
  files: string[];
  modelOptions: PickerOption[];
  argumentRequest: CommandArgumentRequest | null;
  argumentOptions: PickerOption[];
  selectedIndex: number;
  filesLoading: boolean;
  selectedFiles: string[];
  notice: string | null;
}

export interface OverlayActions {
  queryFiles(value: string): void;
  queryArgument(commandName: string, value: string): void;
  selectCommand(command: CommandDescriptor): void;
  selectFile(path: string): void;
  selectModel(option: PickerOption): void;
  selectArgument(commandName: string, option: PickerOption): void;
  hover(index: number): void;
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
      {overlay?.kind === "slash" ? (
        <SlashMenu
          query={slashQuery(display.input) ?? ""}
          results={display.slashResults}
          selectedIndex={display.selectedIndex}
          context={commandContext}
          compact={compact}
          viewportHeight={viewportHeight}
          theme={theme}
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
          selectedPaths={display.selectedFiles}
          compact={compact}
          viewportHeight={viewportHeight}
          theme={theme}
          onQuery={actions.queryFiles}
          onSelect={actions.selectFile}
          onHover={actions.hover}
        />
      ) : null}
      {overlay?.kind === "models" ? (
        <OptionPicker
          title="模型"
          placeholder="搜索"
          query=""
          options={display.modelOptions}
          selectedIndex={display.selectedIndex}
          compact={compact}
          viewportHeight={viewportHeight}
          theme={theme}
          onQuery={() => {}}
          onSelect={actions.selectModel}
          onHover={actions.hover}
        />
      ) : null}
      {overlay?.kind === "arguments" && display.argumentRequest !== null ? (
        <OptionPicker
          title={`/${overlay.commandName}`}
          placeholder="搜索"
          query={display.argumentRequest.query}
          options={display.argumentOptions}
          selectedIndex={display.selectedIndex}
          compact={compact}
          viewportHeight={viewportHeight}
          theme={theme}
          onQuery={(value) => actions.queryArgument(overlay.commandName, value)}
          onSelect={(option) => actions.selectArgument(overlay.commandName, option)}
          onHover={actions.hover}
        />
      ) : null}
      {overlay?.kind === "info" ? (
        <InfoOverlay
          title={overlay.title}
          lines={overlay.lines}
          compact={compact}
          viewportHeight={viewportHeight}
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
