import type { JsonValue } from "../contracts/ipc.ts";

export type PanelName = "Diff" | "Verify" | "Evidence";
export type CommandCategory = "任务" | "事务与证据" | "界面";
export type UiCommandAction = "open-help" | "open-models";
export type ArgumentKind =
  | "none"
  | "query"
  | "transaction"
  | "optional-transaction"
  | "model";

export interface CommandContext {
  modelConfigured: boolean;
  currentModel: string;
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

export interface CommandDescriptor {
  id: string;
  name: string;
  category: CommandCategory;
  title: string;
  description: string;
  usage: string;
  argumentKind: ArgumentKind;
  dangerous?: boolean;
  requiresModel?: boolean;
  requiresTransaction?: boolean;
  execute(argument: string, context: CommandContext): CommandOutcome;
}

export interface CommandAvailability {
  available: boolean;
  reason: string | null;
}

const worker = (
  name: string,
  argumentKind: ArgumentKind,
  options: { requiresModel?: boolean } = {},
) => (argument: string, context: CommandContext): CommandOutcome => {
  const params: Record<string, JsonValue> = {};
  if (argumentKind === "query") {
    params.query = required(argument, `/${name} 需要任务文本`);
  }
  if (argumentKind === "transaction") {
    params.transaction_id = required(argument, `/${name} 需要事务 ID`);
  }
  if (argumentKind === "optional-transaction") {
    const transactionId = argument || context.transactionId;
    if (transactionId !== null) params.transaction_id = transactionId;
  }
  if (options.requiresModel) params.model = context.currentModel;
  return { kind: "worker", method: `command.${name}`, params };
};

const ui = (action: UiCommandAction) =>
  (argument: string): CommandOutcome => ({ kind: "ui", action, argument });

export const COMMAND_REGISTRY: readonly CommandDescriptor[] = [
  descriptor(
    "help", "界面", "帮助", "查看全部可用命令", "", "none",
    ui("open-help"),
  ),
  descriptor(
    "fix", "任务", "修复", "显式限定写/新建范围并生成证据",
    "[--write PATH] [--new PATH] -- TASK", "query",
    worker("fix", "query", { requiresModel: true }),
    { requiresModel: true },
  ),
  descriptor(
    "diff", "事务与证据", "修改", "查看事务补丁", "[TX_ID]", "optional-transaction",
    worker("diff", "optional-transaction"),
    { requiresTransaction: true },
  ),
  descriptor(
    "verify", "事务与证据", "验证", "独立验证候选补丁", "[TX_ID]", "optional-transaction",
    worker("verify", "optional-transaction"),
    { requiresTransaction: true },
  ),
  descriptor(
    "apply", "事务与证据", "应用", "应用已验证补丁", "TX_ID", "transaction",
    worker("apply", "transaction"),
    { dangerous: true, requiresTransaction: true },
  ),
  descriptor(
    "abort", "事务与证据", "放弃", "清理隔离事务", "TX_ID", "transaction",
    worker("abort", "transaction"),
    { dangerous: true, requiresTransaction: true },
  ),
  descriptor(
    "model", "界面", "模型", "选择当前模型", "[MODEL]", "model",
    ui("open-models"),
  ),
] as const;

export function commandAvailability(
  command: CommandDescriptor,
  context: CommandContext,
): CommandAvailability {
  if (command.requiresModel && !context.modelConfigured) {
    return { available: false, reason: "未配置模型凭据" };
  }
  if (command.name === "fix" && !context.acceptanceReady) {
    return { available: false, reason: "缺少独立 AcceptanceSpec，不能开始修复" };
  }
  const historicalStates = Object.values(context.transactionStates ?? {});
  if (
    command.requiresTransaction &&
    context.transactionId === null &&
    historicalStates.length === 0
  ) {
    return { available: false, reason: "无可用事务" };
  }
  if (
    command.name === "apply" &&
    context.verificationStatus.toUpperCase() !== "PASSED" &&
    !historicalStates.some((state) => state.toUpperCase() === "VERIFIED")
  ) {
    return { available: false, reason: "只有 VERIFIED 事务可以 Apply" };
  }
  return { available: true, reason: null };
}

export function findCommand(nameOrAlias: string): CommandDescriptor | null {
  const normalized = nameOrAlias.trim().toLocaleLowerCase();
  return (
    COMMAND_REGISTRY.find(
      (command) =>
        command.name === normalized,
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
  execute: CommandDescriptor["execute"],
  options: {
    dangerous?: boolean;
    requiresModel?: boolean;
    requiresTransaction?: boolean;
  } = {},
): CommandDescriptor {
  return {
    id: `command.${name}`,
    name,
    category,
    title,
    description,
    usage: `/${name}${usageArgument ? ` ${usageArgument}` : ""}`,
    argumentKind,
    execute,
    ...(options.dangerous === undefined ? {} : { dangerous: options.dangerous }),
    ...(options.requiresModel === undefined
      ? {}
      : { requiresModel: options.requiresModel }),
    ...(options.requiresTransaction === undefined
      ? {}
      : { requiresTransaction: options.requiresTransaction }),
  };
}

function required(value: string, message: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(message);
  return normalized;
}
