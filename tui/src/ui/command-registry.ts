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

const modulesCommand: CommandHandler = (argument) => {
  const tokens = argument.trim().split(/\s+/).filter(Boolean);
  const operations = new Set(["list", "show", "enable", "disable", "wake", "sleep"]);
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
      (operation === "enable" || operation === "wake")
    ) params.with_dependencies = true;
    else if (
      option === "--cascade" &&
      (operation === "disable" || operation === "sleep")
    ) params.cascade = true;
    else if (
      option === "--wait" &&
      (operation === "disable" || operation === "sleep")
    ) params.wait = true;
    else if (
      option === "--yes" &&
      (operation === "disable" || operation === "sleep")
    ) params.confirmed = true;
    else if (
      option === "--timeout" &&
      (operation === "disable" || operation === "sleep")
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
    "new", "会话", "新建会话", "清空当前视图并开始新会话", "", "none",
    ui("new-session"),
    { aliases: ["新建"], shortcut: "Ctrl+X N" },
  ),
  descriptor(
    "sessions", "会话", "会话列表", "查看当前与最近会话", "", "none",
    ui("open-sessions"),
    { aliases: ["会话"], shortcut: "Ctrl+X S" },
  ),
  descriptor(
    "resume", "会话", "恢复会话", "恢复一个持久化会话", "SESSION_ID", "session",
    worker("resume", "session"),
    { aliases: ["恢复"] },
  ),
  descriptor(
    "clear", "会话", "清理当前显示", "只清空时间线显示，不删除 Trace", "", "none",
    ui("clear-timeline"),
    { aliases: ["清屏"], shortcut: "Ctrl+L" },
  ),
  descriptor(
    "history", "会话", "输入历史", "模糊搜索本次运行中的输入历史", "[QUERY]", "none",
    ui("open-history"),
    { aliases: ["历史"], shortcut: "Ctrl+R" },
  ),
  descriptor(
    "quit", "会话", "安全退出", "清理 Worker、IPC 与终端状态后退出", "", "none",
    ui("quit"),
    { aliases: ["exit", "退出"], shortcut: "Ctrl+X Q" },
  ),
  descriptor(
    "ask", "任务", "只读问答", "理解仓库并回答，不创建修改事务", "QUERY", "query",
    worker("ask", "query", { requiresModel: true }),
    { aliases: ["问答", "解释"], requiresModel: true },
  ),
  descriptor(
    "plan", "任务", "生成计划", "生成可验证任务计划，不修改代码", "TASK", "query",
    worker("plan", "query", { requiresModel: true }),
    { aliases: ["计划"], requiresModel: true, shortcut: "Ctrl+X P" },
  ),
  descriptor(
    "fix", "任务", "隔离修复", "在 Git Worktree 中修改并验证", "TASK", "query",
    worker("fix", "query", { requiresModel: true }),
    { aliases: ["修复"], requiresModel: true, shortcut: "Ctrl+X F" },
  ),
  descriptor(
    "verify", "事务与验证", "验证事务", "运行冻结的确定性验证矩阵",
    "[TX_ID]", "optional-transaction", worker("verify", "optional-transaction"),
    { aliases: ["验证"], requiresTransaction: true, shortcut: "Ctrl+X V" },
  ),
  descriptor(
    "diff", "事务与验证", "查看修改", "打开当前隔离事务的补丁",
    "[TX_ID]", "optional-transaction", worker("diff", "optional-transaction"),
    { aliases: ["补丁", "修改"], requiresTransaction: true, shortcut: "Ctrl+X D" },
  ),
  descriptor(
    "evidence", "事务与验证", "查看验证证据", "打开当前 Evidence 详情", "", "none",
    ui("open-evidence"),
    { aliases: ["证据"], requiresEvidence: true, shortcut: "Ctrl+X E" },
  ),
  descriptor(
    "apply", "事务与验证", "应用通过的事务", "把已验证补丁显式应用到主工作区",
    "[TX_ID]", "transaction", worker("apply", "transaction"),
    { aliases: ["应用"], dangerous: true, requiresTransaction: true },
  ),
  descriptor(
    "abort", "事务与验证", "放弃事务", "清理指定隔离事务，不触碰未知成果",
    "[TX_ID]", "transaction", worker("abort", "transaction"),
    { aliases: ["放弃"], dangerous: true, requiresTransaction: true },
  ),
  descriptor(
    "read", "文件与上下文", "读取文件", "使用 Rivet Reader 安全读取仓库内文件",
    "FILE", "path", worker("read", "path"),
    { aliases: ["读取"], shortcut: "Ctrl+O" },
  ),
  descriptor(
    "files", "文件与上下文", "选择文件", "打开按需加载的仓库文件选择器",
    "[QUERY]", "none", ui("open-files"),
    { aliases: ["文件"], shortcut: "Ctrl+O" },
  ),
  descriptor(
    "context", "文件与上下文", "管理上下文", "查看或调整当前上下文文件",
    "[add|remove|list|clear]", "context", ui("open-context"),
    { aliases: ["上下文"], shortcut: "Ctrl+X C" },
  ),
  descriptor(
    "search", "文件与上下文", "搜索仓库", "按文件名和路径搜索仓库清单",
    "QUERY", "query", ui("open-search"),
    { aliases: ["搜索", "查找"] },
  ),
  descriptor(
    "model", "模型与运行时", "选择模型", "选择本次运行使用的 DeepSeek 模型",
    "[MODEL]", "model", ui("open-models"),
    { aliases: ["模型"] },
  ),
  descriptor(
    "mode", "模型与运行时", "切换模式", "切换 ASK、PLAN 或 FIX 工作模式",
    "ask|plan|fix", "mode", ui("change-mode"),
    { aliases: ["模式"], shortcut: "Tab" },
  ),
  descriptor(
    "modules", "模型与运行时", "按需模块", "查看并安全控制模块生命周期",
    "[list|show|enable|disable|wake|sleep]", "module", modulesCommand,
    { aliases: ["模块"], shortcut: "Ctrl+X M" },
  ),
  descriptor(
    "trace", "模型与运行时", "执行轨迹", "打开脱敏 Trace 视图",
    "[RUN_ID]", "none", ui("open-trace"),
    { aliases: ["轨迹"], shortcut: "Ctrl+X T" },
  ),
  descriptor(
    "status", "模型与运行时", "任务状态", "查看连接、阶段与事务状态", "", "none",
    ui("show-status"),
    { aliases: ["状态"] },
  ),
  descriptor(
    "cost", "模型与运行时", "用量与费用", "查看当前 token、费用与耗时", "", "none",
    ui("show-cost"),
    { aliases: ["费用", "token"] },
  ),
  descriptor(
    "init", "项目与系统", "初始化项目", "创建最小 Rivet 项目配置",
    "[PATH]", "optional-path", worker("init", "optional-path"),
    { aliases: ["初始化"], dangerous: true },
  ),
  descriptor(
    "doctor", "项目与系统", "环境诊断", "检查环境，不安装系统软件", "", "none",
    worker("doctor", "none"),
    { aliases: ["诊断"] },
  ),
  descriptor(
    "benchmark", "项目与系统", "运行评测", "运行项目内置的有界评测套件", "", "none",
    worker("benchmark", "none"),
    { aliases: ["评测"] },
  ),
  descriptor(
    "config", "项目与系统", "查看配置", "查看非秘密有效配置及来源", "", "none",
    worker("config", "none"),
    { aliases: ["配置"] },
  ),
  descriptor(
    "clean", "项目与系统", "清理运行产物", "只清理带 Rivet ownership marker 的资源",
    "", "none", worker("clean", "none"),
    { aliases: ["清理"], dangerous: true },
  ),
  descriptor(
    "help", "项目与系统", "帮助", "查看全部命令和交互说明", "", "none",
    ui("open-help"),
    { aliases: ["帮助"], shortcut: "Ctrl+X H" },
  ),
  descriptor(
    "keys", "项目与系统", "快捷键", "查看完整键盘操作", "", "none",
    ui("open-keys"),
    { aliases: ["快捷键"] },
  ),
  descriptor(
    "theme", "项目与系统", "切换主题", "切换 dark 或 light 主题",
    "dark|light", "theme", ui("change-theme"),
    { aliases: ["主题"] },
  ),
  descriptor(
    "export", "项目与系统", "导出文件", "原子导出 Trace、Evidence 或 Session 并返回 SHA-256",
    "trace|evidence|session [PATH]", "export", exportCommand,
    { aliases: ["导出"] },
  ),
] as const;

export function commandAvailability(
  command: CommandDescriptor,
  context: CommandContext,
): CommandAvailability {
  if (command.requiresModel && !context.modelConfigured) {
    return { available: false, reason: "尚未配置模型凭据" };
  }
  if (command.requiresSession && !context.hasSession) {
    return { available: false, reason: "当前没有活动会话" };
  }
  if (command.requiresTransaction && context.transactionId === null) {
    return { available: false, reason: "当前没有活动事务" };
  }
  if (
    command.name === "apply" &&
    context.verificationStatus.toUpperCase() !== "PASSED"
  ) {
    return { available: false, reason: "只有验证通过的事务可以应用" };
  }
  if (command.requiresEvidence && context.evidenceId === null) {
    return { available: false, reason: "当前会话没有验证证据" };
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
