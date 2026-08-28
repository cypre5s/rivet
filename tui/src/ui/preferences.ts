import { randomUUID } from "node:crypto";
import {
  lstat,
  mkdir,
  open,
  readFile,
  rename,
  unlink,
} from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, isAbsolute, join } from "node:path";

import type { ThemeName } from "../components/theme.ts";
import type { PanelName, WorkMode } from "./command-registry.ts";

export interface TuiPreferences {
  mode: WorkMode;
  theme: ThemeName;
  panel: PanelName | null;
}

export const DEFAULT_TUI_PREFERENCES: TuiPreferences = {
  mode: "ASK",
  theme: "dark",
  panel: null,
};

const PANELS: readonly PanelName[] = [
  "Plan",
  "Context",
  "Files",
  "Diff",
  "Verify",
  "Evidence",
  "Modules",
  "Trace",
  "Sessions",
];
const MAX_PREFERENCES_BYTES = 4_096;

export function parseTuiPreferences(value: unknown): TuiPreferences {
  if (value === null || Array.isArray(value) || typeof value !== "object") {
    return DEFAULT_TUI_PREFERENCES;
  }
  const fields = value as Record<string, unknown>;
  const mode = ["ASK", "PLAN", "FIX"].includes(String(fields.mode))
    ? (fields.mode as WorkMode)
    : DEFAULT_TUI_PREFERENCES.mode;
  const theme = fields.theme === "light" ? "light" : "dark";
  const panel = PANELS.includes(fields.panel as PanelName)
    ? (fields.panel as PanelName)
    : null;
  return { mode, theme, panel };
}

export async function loadTuiPreferences(
  path = preferencesPath(),
): Promise<TuiPreferences> {
  try {
    const metadata = await lstat(path);
    if (metadata.isSymbolicLink() || !metadata.isFile()) {
      return DEFAULT_TUI_PREFERENCES;
    }
    if (metadata.size > MAX_PREFERENCES_BYTES) return DEFAULT_TUI_PREFERENCES;
    return parseTuiPreferences(JSON.parse(await readFile(path, "utf8")));
  } catch {
    return DEFAULT_TUI_PREFERENCES;
  }
}

export async function saveTuiPreferences(
  preferences: TuiPreferences,
  path = preferencesPath(),
): Promise<void> {
  const directory = dirname(path);
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const directoryMetadata = await lstat(directory);
  if (directoryMetadata.isSymbolicLink() || !directoryMetadata.isDirectory()) return;
  try {
    const targetMetadata = await lstat(path);
    if (targetMetadata.isSymbolicLink() || !targetMetadata.isFile()) return;
  } catch {}
  const temporaryPath = `${path}.${process.pid}.${randomUUID()}.tmp`;
  const handle = await open(temporaryPath, "wx", 0o600);
  try {
    try {
      await handle.writeFile(`${JSON.stringify(preferences)}\n`, "utf8");
    } finally {
      await handle.close();
    }
    await rename(temporaryPath, path);
  } catch (error) {
    await unlink(temporaryPath).catch(() => {});
    throw error;
  }
}

function preferencesPath(): string {
  const configured = process.env.XDG_STATE_HOME;
  const stateRoot =
    configured && isAbsolute(configured)
      ? configured
      : join(homedir(), ".local", "state");
  return join(stateRoot, "rivet", "tui-preferences.json");
}
