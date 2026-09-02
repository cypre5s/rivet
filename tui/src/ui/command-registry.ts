import type { JsonValue } from "../contracts/ipc.ts";

export type WorkMode = "ASK" | "PLAN" | "FIX";
export type CommandCategory =
  | "会话"
  | "任务"
  | "事务与验证"
  | "文件与上下文"
  | "模型与运行时"
  | "项目与系统";
export type PanelName =
  | "Plan"
  | "Context"
  | "Files"
  | "Diff"
  | "Verify"
  | "Evidence"
  | "Modules"
  | "Trace"
  | "Sessions";
export type UiCommandAction =
  | "new-session"
  | "open-sessions"
  | "clear-timeline"
  | "open-history"
  | "quit"
  | "open-files"
  | "open-context"
  | "open-search"
  | "open-models"
  | "open-config"
  | "change-mode"
  | "open-modules"
  | "open-trace"
  | "show-status"
  | "show-cost"
  | "open-help"
  | "open-keys"
  | "change-theme"
  | "open-evidence"
  | "export-view";

export type ArgumentKind =
  | "none"
  | "query"
  | "path"
  | "optional-path"
  | "transaction"
  | "optional-transaction"
  | "session"
  | "mode"
  | "model"
  | "theme"
  | "module"
  | "context"
  | "export";

export interface CommandContext {
  modelConfigured: boolean;
  currentModel: string;
  hasSession: boolean;
  transactionId: string | null;
  verificationStatus: string;
  evidenceId: string | null;
  acceptanceReady: boolean;
  transactionStates?: Readonly<Record<string, string>>;
}

export type CommandOutcome =
  | {
      kind: "worker";
      method: string;
      params: Record<string, JsonValue>;
    }
  | {
      kind: "ui";
      action: UiCommandAction;
      argument: string;
    };

export type CommandHandler = (
  argument: string,
  context: CommandContext,
) => CommandOutcome;

export interface CommandDescriptor {
  id: string;
  name: string;
  aliases: string[];
  category: CommandCategory;
  title: string;
  description: string;
  usage: string;
  argumentKind: ArgumentKind;
  shortcut?: string;
  dangerous?: boolean;
  requiresModel?: boolean;
  requiresSession?: boolean;
  requiresTransaction?: boolean;
  requiresEvidence?: boolean;
  execute: CommandHandler;
}

export interface CommandAvailability {
  available: boolean;
  reason: string | null;
}

const worker = (
  name: string,
  argumentKind: ArgumentKind,
  options: { requiresModel?: boolean } = {},
): CommandHandler => {
  return (argument, context) => {
    const params: Record<string, JsonValue> = {};
    if (argumentKind === "query") {
      params.query = required(argument, `/${name} 需要任务文本`);
    }
    if (argumentKind === "path") {
      params.file = required(argument, `/${name} 需要仓库内文件路径`);
    }
    if (argumentKind === "optional-path" && argument) params.file = argument;
    if (argumentKind === "session") {
      params.session_id = required(argument, `/${name} 需要会话 ID`);
    }
    if (argumentKind === "transaction") {
      params.transaction_id = required(
        argument || context.transactionId || "",
        `/${name} 需要事务 ID`,
      );
    }
    if (argumentKind === "optional-transaction") {
      const transactionId = argument || context.transactionId;
      if (transactionId) params.transaction_id = transactionId;
    }
    if (options.requiresModel) params.model = context.currentModel;
    return { kind: "worker", method: `command.${name}`, params };
  };
};

const ui = (action: UiCommandAction): CommandHandler => (argument) => ({
  kind: "ui",
  action,
  argument,
});

const exportCommand: CommandHandler = (argument) => {
  const tokens = argument.trim().split(/\s+/).filter(Boolean);
  const exportKind = tokens.shift();
  if (!exportKind || !["evidence", "trace", "session"].includes(exportKind)) {
    throw new Error("/export 需要 evidence、trace 或 session");
  }
  if (tokens.length > 1) throw new Error("/export 最多接受一个输出路径");
  const params: Record<string, JsonValue> = { export_kind: exportKind };
  if (tokens[0]) params.output_path = tokens[0];
  return { kind: "worker", method: "command.export", params };
};

const readCommand: CommandHandler = (argument) => {
  const trimmed = argument.trim();
  const optionStart = trimmed.search(/\s--/);
  const file = required(
    optionStart < 0 ? trimmed : trimmed.slice(0, optionStart).trim(),
    "/read 需要仓库内文件路径",
  );
  const params: Record<string, JsonValue> = { file };
  if (optionStart < 0) return { kind: "worker", method: "command.read", params };

  const tokens = trimmed.slice(optionStart).trim().split(/\s+/).filter(Boolean);
  const seen = new Set<string>();
  const numericOptions: Record<
    string,
    { key: string; minimum: number; maximum: number }
  > = {
    "--frames": { key: "frames", minimum: 0, maximum: 20 },
    "--max-ocr-pages": { key: "max_ocr_pages", minimum: 0, maximum: 100 },
    "--max-image-pixels": {
      key: "max_image_pixels",
      minimum: 1,
      maximum: 100_000_000,
    },
    "--max-audio-duration": {
      key: "max_audio_duration",
      minimum: 1,
      maximum: 86_400,
    },
    "--max-output-chars": {
      key: "max_output_chars",
      minimum: 1,
      maximum: 4_000_000,
    },
    "--timeout": { key: "timeout", minimum: 1, maximum: 3_600 },
  };
  while (tokens.length > 0) {
    const option = tokens.shift()!;
    if (seen.has(option)) throw new Error(`/read 不允许重复参数 ${option}`);
    seen.add(option);
    if (option === "--ocr" || option === "--transcribe") {
      params[option === "--ocr" ? "ocr" : "transcribe"] = true;
      continue;
    }
    const bounds = numericOptions[option];
    if (bounds === undefined) throw new Error(`/read 不支持参数 ${option}`);
    const rawValue = tokens.shift();
    const value = rawValue === undefined ? Number.NaN : Number(rawValue);
    if (
      !Number.isInteger(value) ||
      value < bounds.minimum ||
      value > bounds.maximum
    ) {
      throw new Error(
        `/read ${option} 必须是 ${bounds.minimum} 到 ${bounds.maximum} 的整数`,
      );
    }
    params[bounds.key] = value;
  }
  return { kind: "worker", method: "command.read", params };
};

const modulesCommand: CommandHandler = (argument) => {
  const tokens = argument.trim().split(/\s+/).filter(Boolean);
  const operations = new Set(["list", "show", "enable", "disable"]);
  let operation = tokens.shift() ?? "list";
  if (!operations.has(operation)) {
    tokens.unshift(operation);
    operation = "show";
  }
  const params: Record<string, JsonValue> = { operation };
  if (operation !== "list") {
    const moduleId = tokens.shift();
    if (moduleId === undefined || moduleId.startsWith("--")) {
      throw new Error(`/modules ${operation} 需要模块 ID`);
    }
    params.module_id = moduleId;
  }
  while (tokens.length > 0) {
    const option = tokens.shift();
    if (
      option === "--with-dependencies" &&
      operation === "enable"
    ) params.with_dependencies = true;
    else if (
      option === "--cascade" &&
      operation === "disable"
    ) params.cascade = true;
    else if (
      option === "--wait" &&
      operation === "disable"
    ) params.wait = true;
    else if (
      option === "--yes" &&
      operation === "disable"
    ) params.confirmed = true;
    else if (
      option === "--timeout" &&
      operation === "disable"
    ) {
      const timeout = Number(tokens.shift());
      if (!Number.isFinite(timeout) || timeout < 0 || timeout > 300) {
        throw new Error("/modules --timeout 必须是 0 到 300 秒");
      }
      params.timeout_seconds = timeout;
    } else {
      throw new Error(`/modules ${operation} 不支持参数：${option ?? ""}`);
    }
  }
  const method =
    operation === "list"
      ? "module.list"
      : operation === "show"
        ? "module.show"
        : "module.operation";
  return { kind: "worker", method, params };
};

export const COMMAND_REGISTRY: readonly CommandDescriptor[] = [
  descriptor(
    "new", "会话", "新会话", "创建会话", "", "none",
    ui("new-session"),
    { aliases: ["新建"], shortcut: "Ctrl+X N" },
  ),
  descriptor(
    "sessions", "会话", "会话", "近期会话", "", "none",
    ui("open-sessions"),
    { aliases: ["会话"], shortcut: "Ctrl+X S" },
  ),
  descriptor(
    "resume", "会话", "恢复", "恢复保存的会话", "SESSION_ID", "session",
    worker("resume", "session"),
    { aliases: ["恢复"] },
  ),
  descriptor(
    "clear", "会话", "清屏", "保留 Trace", "", "none",
    ui("clear-timeline"),
    { aliases: ["清屏"], shortcut: "Ctrl+L" },
  ),
  descriptor(
    "history", "会话", "历史", "搜索输入历史", "[QUERY]", "none",
    ui("open-history"),
    { aliases: ["历史"], shortcut: "Ctrl+R" },
  ),
  descriptor(
    "quit", "会话", "退出", "清理并退出", "", "none",
    ui("quit"),
    { aliases: ["exit", "退出"], shortcut: "Ctrl+X Q" },
  ),
  descriptor(
    "ask", "任务", "问答", "理解仓库，不修改", "QUERY", "query",
    worker("ask", "query", { requiresModel: true }),
    { aliases: ["问答", "解释"], requiresModel: true },
  ),
  descriptor(
    "plan", "任务", "计划", "规划，不修改", "TASK", "query",
    worker("plan", "query", { requiresModel: true }),
    { aliases: ["计划"], requiresModel: true, shortcut: "Ctrl+X P" },
  ),
  descriptor(
    "fix", "任务", "修复", "隔离修改并验证", "TASK", "query",
    worker("fix", "query", { requiresModel: true }),
    { aliases: ["修复"], requiresModel: true, shortcut: "Ctrl+X F" },
  ),
  descriptor(
    "verify", "事务与验证", "验证", "运行验证矩阵",
    "[TX_ID]", "optional-transaction", worker("verify", "optional-transaction"),
    { aliases: ["验证"], requiresTransaction: true, shortcut: "Ctrl+X V" },
  ),
  descriptor(
    "diff", "事务与验证", "修改", "事务补丁",
    "[TX_ID]", "optional-transaction", worker("diff", "optional-transaction"),
    { aliases: ["补丁", "修改"], requiresTransaction: true, shortcut: "Ctrl+X D" },
  ),
  descriptor(
    "evidence", "事务与验证", "证据", "结论与详情", "", "none",
    ui("open-evidence"),
    { aliases: ["证据"], shortcut: "Ctrl+X E" },
  ),
  descriptor(
    "apply", "事务与验证", "应用", "应用已验证补丁",
    "[TX_ID]", "transaction", worker("apply", "transaction"),
    { aliases: ["应用"], dangerous: true, requiresTransaction: true },
  ),
  descriptor(
    "abort", "事务与验证", "放弃", "清理隔离事务",
    "[TX_ID]", "transaction", worker("abort", "transaction"),
    { aliases: ["放弃"], dangerous: true, requiresTransaction: true },
  ),
  descriptor(
    "read", "文件与上下文", "读取", "安全读取仓库文件",
    "FILE [--ocr|--transcribe|--frames N]", "path", readCommand,
    { aliases: ["读取"], shortcut: "Ctrl+O" },
  ),
  descriptor(
    "files", "文件与上下文", "文件", "选择仓库文件",
    "[QUERY]", "none", ui("open-files"),
    { aliases: ["文件"], shortcut: "Ctrl+O" },
  ),
  descriptor(
    "context", "文件与上下文", "上下文", "调整上下文文件",
    "[add|remove|list|clear]", "context", ui("open-context"),
    { aliases: ["上下文"], shortcut: "Ctrl+X C" },
  ),
  descriptor(
    "search", "文件与上下文", "搜索", "搜索文件路径",
    "QUERY", "query", ui("open-search"),
    { aliases: ["搜索", "查找"] },
  ),
  descriptor(
    "model", "模型与运行时", "模型", "选择当前模型",
    "[MODEL]", "model", ui("open-models"),
    { aliases: ["模型"] },
  ),
  descriptor(
    "mode", "模型与运行时", "模式", "切换 ASK/PLAN/FIX",
    "ask|plan|fix", "mode", ui("change-mode"),
    { aliases: ["模式"], shortcut: "Tab" },
  ),
  descriptor(
    "modules", "模型与运行时", "能力", "配置按需能力",
    "[list|show|enable|disable]", "module", modulesCommand,
    { aliases: ["模块"], shortcut: "Ctrl+X M" },
  ),
  descriptor(
    "trace", "模型与运行时", "轨迹", "查看脱敏轨迹",
    "[RUN_ID]", "none", ui("open-trace"),
    { aliases: ["轨迹"], shortcut: "Ctrl+X T" },
  ),
  descriptor(
    "status", "模型与运行时", "状态", "查看运行状态", "", "none",
    ui("show-status"),
    { aliases: ["状态"] },
  ),
  descriptor(
    "cost", "模型与运行时", "用量", "Token、费用和耗时", "", "none",
    ui("show-cost"),
    { aliases: ["费用", "token"] },
  ),
  descriptor(
    "init", "项目与系统", "初始化", "创建项目配置",
    "[PATH]", "optional-path", worker("init", "optional-path"),
    { aliases: ["初始化"], dangerous: true },
  ),
  descriptor(
    "doctor", "项目与系统", "诊断", "检查运行环境", "", "none",
    worker("doctor", "none"),
    { aliases: ["诊断"] },
  ),
  descriptor(
    "benchmark", "项目与系统", "评测", "运行有界评测", "", "none",
    worker("benchmark", "none"),
    { aliases: ["评测"] },
  ),
  descriptor(
    "config", "项目与系统", "配置", "配置模型与预算", "", "none",
    ui("open-config"),
    { aliases: ["配置"], shortcut: "Ctrl+G" },
  ),
  descriptor(
    "clean", "项目与系统", "清理", "只清理 Rivet 产物",
    "", "none", worker("clean", "none"),
    { aliases: ["清理"], dangerous: true },
  ),
  descriptor(
    "help", "项目与系统", "帮助", "查看命令", "", "none",
    ui("open-help"),
    { aliases: ["帮助"], shortcut: "Ctrl+X H" },
  ),
  descriptor(
    "keys", "项目与系统", "快捷键", "查看按键", "", "none",
    ui("open-keys"),
    { aliases: ["快捷键"] },
  ),
  descriptor(
    "theme", "项目与系统", "主题", "选择明暗主题",
    "dark|light", "theme", ui("change-theme"),
    { aliases: ["主题"] },
  ),
  descriptor(
    "export", "项目与系统", "导出", "轨迹、证据或会话",
    "trace|evidence|session [PATH]", "export", exportCommand,
    { aliases: ["导出"] },
  ),
] as const;

export function commandAvailability(
  command: CommandDescriptor,
  context: CommandContext,
): CommandAvailability {
  if (command.requiresModel && !context.modelConfigured) {
    return { available: false, reason: "未配置模型凭据" };
  }
  if (command.requiresSession && !context.hasSession) {
    return { available: false, reason: "无活动会话" };
  }
  const historicalStates = Object.values(context.transactionStates ?? {});
  if (
    command.requiresTransaction &&
    context.transactionId === null &&
    historicalStates.length === 0
  ) {
    return { available: false, reason: "无活动事务" };
  }
  if (
    command.name === "apply" &&
    context.verificationStatus.toUpperCase() !== "PASSED" &&
    !historicalStates.some((state) => state.toUpperCase() === "VERIFIED")
  ) {
    return { available: false, reason: "事务未通过验证" };
  }
  if (command.requiresEvidence && context.evidenceId === null) {
    return { available: false, reason: "无验证证据" };
  }
  return { available: true, reason: null };
}

export function findCommand(nameOrAlias: string): CommandDescriptor | null {
  const normalized = nameOrAlias.trim().toLocaleLowerCase();
  return (
    COMMAND_REGISTRY.find(
      (command) =>
        command.name === normalized ||
        command.aliases.some(
          (alias) => alias.toLocaleLowerCase() === normalized,
        ),
    ) ?? null
  );
}

function descriptor(
  name: string,
  category: CommandCategory,
  title: string,
  description: string,
  usageArgument: string,
  argumentKind: ArgumentKind,
  execute: CommandHandler,
  options: {
    aliases?: string[];
    shortcut?: string;
    dangerous?: boolean;
    requiresModel?: boolean;
    requiresSession?: boolean;
    requiresTransaction?: boolean;
    requiresEvidence?: boolean;
  } = {},
): CommandDescriptor {
  return {
    id: `command.${name}`,
    name,
    aliases: options.aliases ?? [],
    category,
    title,
    description,
    usage: `/${name}${usageArgument ? ` ${usageArgument}` : ""}`,
    argumentKind,
    execute,
    ...(options.shortcut === undefined ? {} : { shortcut: options.shortcut }),
    ...(options.dangerous === undefined ? {} : { dangerous: options.dangerous }),
    ...(options.requiresModel === undefined
      ? {}
      : { requiresModel: options.requiresModel }),
    ...(options.requiresSession === undefined
      ? {}
      : { requiresSession: options.requiresSession }),
    ...(options.requiresTransaction === undefined
      ? {}
      : { requiresTransaction: options.requiresTransaction }),
    ...(options.requiresEvidence === undefined
      ? {}
      : { requiresEvidence: options.requiresEvidence }),
  };
}

function required(value: string, message: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(message);
  return normalized;
}
