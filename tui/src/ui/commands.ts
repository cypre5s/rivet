import {
  COMMAND_REGISTRY,
  commandAvailability,
  findCommand,
  type CommandContext,
  type CommandOutcome,
} from "./command-registry.ts";

export interface CommandSources {
  models: string[];
  transactions: string[];
}

export interface CommandArgumentRequest {
  commandName: string;
  query: string;
}

export interface FixCommandArgument {
  query: string;
  writeScope: string[];
  allowedNewPaths: string[];
}

export const DEFAULT_COMMAND_CONTEXT: CommandContext = {
  modelConfigured: true,
  currentModel: "deepseek-v4-pro",
  transactionId: null,
  verificationStatus: "NOT_RUN",
  evidenceId: null,
  acceptanceReady: true,
};

export function parseCommandInput(
  value: string,
  context: CommandContext = DEFAULT_COMMAND_CONTEXT,
): CommandOutcome {
  const trimmed = value.trim();
  if (!trimmed) throw new Error("请输入任务或命令");
  if (!trimmed.startsWith("/")) {
    if (!context.modelConfigured) throw new Error("未配置模型凭据");
    return {
      kind: "worker",
      method: "command.ask",
      params: { query: trimmed, model: context.currentModel },
    };
  }

  const separator = trimmed.search(/\s/);
  const name = trimmed.slice(1, separator < 0 ? undefined : separator);
  const argument = separator < 0 ? "" : trimmed.slice(separator).trim();
  const command = findCommand(name);
  if (command === null) throw new Error(`未知命令：/${name}`);
  const fixArgument = command.name === "fix" ? parseFixArgument(argument) : null;
  const normalizedArgument = fixArgument?.query ?? argument;
  validateRequiredArgument(command.name, command.argumentKind, normalizedArgument);
  const executionContext = contextForTransaction(
    command.name,
    normalizedArgument,
    context,
  );
  const availability = commandAvailability(command, executionContext);
  if (!availability.available) {
    throw new Error(availability.reason ?? "命令当前不可用");
  }
  const outcome = command.execute(normalizedArgument, executionContext);
  if (fixArgument !== null && outcome.kind === "worker") {
    if (fixArgument.writeScope.length > 0) {
      outcome.params.write_scope = fixArgument.writeScope;
    }
    if (fixArgument.allowedNewPaths.length > 0) {
      outcome.params.allowed_new_paths = fixArgument.allowedNewPaths;
    }
  }
  return outcome;
}

export function parseFixArgument(argument: string): FixCommandArgument {
  const normalized = argument.trim();
  if (!normalized.startsWith("--write ") && !normalized.startsWith("--new ")) {
    return { query: normalized, writeScope: [], allowedNewPaths: [] };
  }
  const tokens = normalized.split(/\s+/);
  const writeScope: string[] = [];
  const allowedNewPaths: string[] = [];
  let index = 0;
  while (index < tokens.length && tokens[index] !== "--") {
    const option = tokens[index];
    if (option !== "--write" && option !== "--new") {
      throw new Error("/fix 范围必须使用 --write PATH、--new PATH，并以 -- 结束");
    }
    const path = tokens[index + 1];
    if (path === undefined || !validScopePath(path)) {
      throw new Error(`${option} 需要安全的仓库相对路径`);
    }
    const target = option === "--write" ? writeScope : allowedNewPaths;
    if (target.includes(path) || (option === "--new" && writeScope.includes(path))) {
      throw new Error(`/fix 范围路径重复：${path}`);
    }
    if (option === "--write" && allowedNewPaths.includes(path)) {
      throw new Error(`/fix 范围路径重复：${path}`);
    }
    target.push(path);
    index += 2;
  }
  if (tokens[index] !== "--") {
    throw new Error("/fix 范围后必须使用 -- 分隔任务文本");
  }
  const query = tokens.slice(index + 1).join(" ").trim();
  if (!query) throw new Error("/fix 需要任务文本");
  return { query, writeScope, allowedNewPaths };
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

export function commandArgumentRequest(
  value: string,
): CommandArgumentRequest | null {
  const match = /^\/([a-z][a-z-]*)\s+(.*)$/i.exec(value);
  if (match?.[1] === undefined || match[2] === undefined) return null;
  const command = findCommand(match[1]);
  if (
    command === null ||
    !["transaction", "optional-transaction", "model"].includes(
      command.argumentKind,
    )
  ) {
    return null;
  }
  return { commandName: command.name, query: match[2] };
}

export function commandArgumentCompletions(
  name: string,
  query: string,
  sources: CommandSources,
): string[] {
  const command = findCommand(name);
  if (command === null) return [];
  const choices =
    command.argumentKind === "model"
      ? sources.models
      : ["transaction", "optional-transaction"].includes(command.argumentKind)
        ? sources.transactions
        : [];
  const normalized = query.toLocaleLowerCase();
  return choices
    .filter((choice) => choice.toLocaleLowerCase().includes(normalized))
    .slice(0, 20);
}

export function commandNames(): string[] {
  return COMMAND_REGISTRY.map((command) => command.name);
}

function contextForTransaction(
  commandName: string,
  argument: string,
  context: CommandContext,
): CommandContext {
  if (!["diff", "verify", "apply", "abort"].includes(commandName) || !argument) {
    return context;
  }
  return {
    ...context,
    transactionId: argument,
    verificationStatus:
      argument === context.transactionId
        ? context.verificationStatus
        : context.transactionStates?.[argument]?.toUpperCase() === "VERIFIED"
          ? "PASSED"
          : "NOT_RUN",
    transactionStates: {},
  };
}

function validateRequiredArgument(
  commandName: string,
  argumentKind: (typeof COMMAND_REGISTRY)[number]["argumentKind"],
  argument: string,
): void {
  if (argumentKind === "query" && !argument) {
    throw new Error(`/${commandName} 需要任务文本`);
  }
  if (argumentKind === "transaction" && !argument) {
    throw new Error(`/${commandName} 需要事务 ID`);
  }
}

function validScopePath(path: string): boolean {
  const parts = path.split("/");
  return (
    path.length > 0 &&
    path.length <= 4_096 &&
    !path.startsWith("/") &&
    !path.includes("\\") &&
    !path.includes("\0") &&
    parts.every((part) => part !== "" && part !== "." && part !== "..")
  );
}
