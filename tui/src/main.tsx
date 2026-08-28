import { createCliRenderer } from "@opentui/core";
import { createDefaultOpenTuiKeymap } from "@opentui/keymap/opentui";
import { KeymapProvider } from "@opentui/keymap/react";
import { createRoot } from "@opentui/react";

import { RivetApp } from "./app.tsx";
import { BunWorkerTransport } from "./ipc/bun-worker-transport.ts";
import { WorkerClient } from "./ipc/client.ts";

const repository = process.env.RIVET_REPOSITORY ?? process.cwd();
const workerCommand = parseWorkerCommand(
  process.env.RIVET_WORKER_COMMAND_JSON,
  repository,
);
const renderer = await createCliRenderer({
  clearOnShutdown: true,
  consoleMode: "disabled",
  exitOnCtrlC: false,
  openConsoleOnError: false,
});
const keymap = createDefaultOpenTuiKeymap(renderer);
const root = createRoot(renderer);
let transport = createTransport();
let client = new WorkerClient(transport);
let recovering = false;

const renderApplication = () => {
  root.render(
    <KeymapProvider keymap={keymap}>
      <RivetApp client={client} onRecover={() => void recoverWorker()} />
    </KeymapProvider>,
  );
};
renderApplication();

let shuttingDown = false;
const shutdown = async () => {
  if (shuttingDown) return;
  shuttingDown = true;
  root.unmount();
  client.close();
  await transport.waitForShutdown();
  renderer.destroy();
};
process.once("SIGTERM", () => void shutdown());
process.once("SIGHUP", () => void shutdown());
process.once("beforeExit", () => void shutdown());

async function recoverWorker(): Promise<void> {
  if (shuttingDown || recovering) return;
  recovering = true;
  const previousTransport = transport;
  client.close();
  await previousTransport.waitForShutdown();
  transport = createTransport();
  client = new WorkerClient(transport);
  recovering = false;
  renderApplication();
}

function createTransport(): BunWorkerTransport {
  return new BunWorkerTransport({
    command: workerCommand,
    cwd: repository,
    environment: workerEnvironment(),
  });
}

function parseWorkerCommand(value: string | undefined, cwd: string): string[] {
  if (value === undefined) {
    return [
      process.env.RIVET_PYTHON ?? "python3",
      "-m",
      "rivet",
      "internal",
      "worker",
      "--stdio",
      "--repository",
      cwd,
    ];
  }
  const parsed: unknown = JSON.parse(value);
  if (
    !Array.isArray(parsed) ||
    parsed.length === 0 ||
    !parsed.every((item) => typeof item === "string" && item.length > 0)
  ) {
    throw new Error("RIVET_WORKER_COMMAND_JSON 必须是非空 argv 数组");
  }
  return parsed;
}

function workerEnvironment(): Record<string, string> {
  const allowed = [
    "DEEPSEEK_API_KEY",
    "LANG",
    "LC_ALL",
    "PATH",
    "RIVET_BWRAP_PATH",
    "TERM",
    "TZ",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
  ];
  const environment: Record<string, string> = {};
  for (const name of allowed) {
    const value = process.env[name];
    if (value !== undefined) environment[name] = value;
  }
  return environment;
}
