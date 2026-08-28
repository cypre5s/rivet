import {
  COMMAND_REGISTRY,
  commandAvailability,
  findCommand,
  type CommandContext,
  type CommandOutcome,
  type WorkMode,
} from "./command-registry.ts";
import type { ModuleStatus } from "../state/reducer.ts";

export interface CommandSources {
  models: string[];
  sessions: string[];
  transactions: string[];
  modules: string[];
  moduleStatuses?: ModuleStatus[];
  files: string[];
  contextFiles: string[];
}

export interface CommandArgumentRequest {
  commandName: string;
  query: string;
}

export const DEFAULT_COMMAND_CONTEXT: CommandContext = {
  modelConfigured: true,
  currentModel: "deepseek-v4-pro",
  hasSession: true,
  transactionId: null,
  verificationStatus: "NOT_RUN",
  evidenceId: null,
};

export function parseCommandInput(
  value: string,
  context: CommandContext = DEFAULT_COMMAND_CONTEXT,
  mode: WorkMode = "ASK",
): CommandOutcome {
  const trimmed = value.trim();
  if (!trimmed) throw new Error("请输入任务或命令");
  if (!trimmed.startsWith("/")) {
    const command = findCommand(mode.toLocaleLowerCase());
    if (command === null) throw new Error("当前工作模式无效");
    const availability = commandAvailability(command, context);
    if (!availability.available) {
      throw new Error(availability.reason ?? "命令当前不可用");
    }
    return command.execute(trimmed, context);
  }
  const separator = trimmed.search(/\s/);
  const name = trimmed.slice(1, separator < 0 ? undefined : separator);
  const argument = separator < 0 ? "" : trimmed.slice(separator).trim();
  const command = findCommand(name);
  if (command === null) throw new Error(`未知命令：/${name}`);
  validateRequiredArgument(command.name, command.argumentKind, argument);
  const executionContext =
    command.requiresTransaction && argument
      ? {
          ...context,
          transactionId: argument,
          verificationStatus:
            argument === context.transactionId
              ? context.verificationStatus
              : "NOT_RUN",
        }
      : context;
  const availability = commandAvailability(command, executionContext);
  if (!availability.available) {
    throw new Error(availability.reason ?? "命令当前不可用");
  }
  return command.execute(argument, executionContext);
}

export function slashQuery(value: string): string | null {
  if (!value.startsWith("/") || value.slice(1).includes(" ")) return null;
  return value.slice(1);
}

export function fileMentionQuery(value: string): string | null {
  const match = /(?:^|\s)@([^\s@]*)$/.exec(value);
  return match?.[1] ?? null;
}

export function replaceFileMention(value: string, path: string): string {
  return value.replace(/(?:^|\s)@[^\s@]*$/, (match) => {
    const prefix = match.startsWith(" ") ? " " : "";
    return `${prefix}@${path} `;
  });
}

export function commandArgumentRequest(value: string): CommandArgumentRequest | null {
  const match = /^\/([a-z][a-z-]*)\s+(.*)$/i.exec(value);
  if (match?.[1] === undefined || match[2] === undefined) return null;
  const command = findCommand(match[1]);
  if (command === null || !hasFiniteChoices(command.argumentKind)) return null;
  return { commandName: command.name, query: match[2] };
}

export function commandArgumentCompletions(
  name: string,
  query: string,
  sources: CommandSources,
): string[] {
  const command = findCommand(name);
  if (command === null) return [];
  const choices = completionChoices(command.argumentKind, sources);
  const normalized = query.toLocaleLowerCase();
  return choices
    .filter((choice) => choice.toLocaleLowerCase().includes(normalized))
    .slice(0, 20);
}

export function commandNames(): string[] {
  return COMMAND_REGISTRY.map((command) => command.name);
}

export function moduleCommandUnavailableReason(
  value: string,
  statuses: ModuleStatus[],
): string | null {
  const [operation, moduleId] = value.split(/\s+/, 2);
  if (moduleId === undefined || !["enable", "disable", "wake", "sleep"].includes(operation ?? "")) {
    return null;
  }
  const status = statuses.find((item) => item.moduleId === moduleId);
  if (status === undefined) return null;
  if (!status.manualControl) return "内部模块仅由 Kernel 控制";
  if (
    ["required", "eager"].includes(status.activation) &&
    (operation === "disable" || operation === "sleep")
  ) {
    return "系统必需或常驻模块不能执行该操作";
  }
  if ((operation === "disable" || operation === "sleep") && status.leaseCount > 0) {
    return `当前有 ${status.leaseCount} 个活动 Lease`;
  }
  return null;
}

function completionChoices(
  kind: (typeof COMMAND_REGISTRY)[number]["argumentKind"],
  sources: CommandSources,
): string[] {
  if (kind === "model") return sources.models;
  if (kind === "mode") return ["ask", "plan", "fix"];
  if (kind === "session") return sources.sessions;
  if (kind === "transaction" || kind === "optional-transaction") {
    return sources.transactions;
  }
  if (kind === "module") {
    return moduleChoices(sources);
  }
  if (kind === "path" || kind === "optional-path") return sources.files;
  if (kind === "context") {
    return [
      "list",
      "clear",
      ...sources.files.map((path) => `add ${path}`),
      ...sources.contextFiles.map((path) => `remove ${path}`),
    ];
  }
  if (kind === "theme") return ["dark", "light"];
  if (kind === "export") return ["trace", "evidence", "session"];
  return [];
}

function moduleChoices(sources: CommandSources): string[] {
  const statuses = sources.moduleStatuses ?? [];
  if (statuses.length === 0) {
    return [
      "list",
      ...sources.modules.flatMap((moduleId) => [
        `show ${moduleId}`,
        `enable ${moduleId}`,
        `wake ${moduleId}`,
        `sleep ${moduleId}`,
        `disable ${moduleId}`,
      ]),
    ];
  }
  const choices = ["list"];
  for (const status of statuses) {
    choices.push(`show ${status.moduleId}`);
    if (
      !status.manualControl ||
      status.activation === "required" ||
      status.activation === "eager"
    ) {
      if (["ACTIVE", "IDLE"].includes(status.runtimeState)) {
        choices.push(
          `sleep ${status.moduleId}`,
          `disable ${status.moduleId}`,
        );
      }
      continue;
    }
    if (!status.configuredEnabled) {
      choices.push(
        `enable ${status.moduleId}`,
        `enable ${status.moduleId} --with-dependencies`,
      );
      continue;
    }
    if (["INACTIVE", "SLEEPING"].includes(status.runtimeState)) {
      choices.push(
        `wake ${status.moduleId}`,
        `wake ${status.moduleId} --with-dependencies`,
      );
    }
    if (["ACTIVE", "IDLE"].includes(status.runtimeState)) {
      choices.push(
        `sleep ${status.moduleId}`,
        `sleep ${status.moduleId} --wait --timeout 30`,
      );
      if (status.dependents.length > 0) {
        choices.push(`sleep ${status.moduleId} --cascade --yes`);
      }
    }
    choices.push(`disable ${status.moduleId}`);
    if (status.dependents.length > 0) {
      choices.push(`disable ${status.moduleId} --cascade --yes`);
    }
  }
  return choices;
}

function hasFiniteChoices(
  kind: (typeof COMMAND_REGISTRY)[number]["argumentKind"],
): boolean {
  return [
    "path",
    "optional-path",
    "transaction",
    "optional-transaction",
    "session",
    "mode",
    "model",
    "theme",
    "module",
    "context",
    "export",
  ].includes(kind);
}

function validateRequiredArgument(
  commandName: string,
  argumentKind: (typeof COMMAND_REGISTRY)[number]["argumentKind"],
  argument: string,
): void {
  if (argument || !["query", "path", "transaction", "session"].includes(argumentKind)) {
    return;
  }
  if (argumentKind === "query") throw new Error(`/${commandName} 需要任务文本`);
  if (argumentKind === "path") throw new Error(`/${commandName} 需要仓库内文件路径`);
  if (argumentKind === "session") throw new Error(`/${commandName} 需要会话 ID`);
  throw new Error(`/${commandName} 需要事务 ID`);
}
