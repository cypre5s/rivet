import type { JsonValue } from "../contracts/ipc.ts";

export interface ParsedCommand {
  method: string;
  params: Record<string, JsonValue>;
}

const QUERY_COMMANDS = new Set(["ask", "plan", "fix"]);
const TRANSACTION_COMMANDS = new Set(["verify", "diff", "apply"]);

export function parseCommandInput(value: string): ParsedCommand {
  const trimmed = value.trim();
  if (trimmed.length === 0) throw new Error("请输入任务或命令");
  if (!trimmed.startsWith("/")) {
    return { method: "command.ask", params: { query: trimmed } };
  }
  const separator = trimmed.search(/\s/);
  const command = trimmed
    .slice(1, separator < 0 ? undefined : separator)
    .toLowerCase();
  const argument = separator < 0 ? "" : trimmed.slice(separator).trim();
  if (QUERY_COMMANDS.has(command)) {
    if (argument.length === 0) throw new Error(`/${command} 需要任务文本`);
    return { method: `command.${command}`, params: { query: argument } };
  }
  if (TRANSACTION_COMMANDS.has(command)) {
    if (command === "apply" && argument.length === 0) {
      throw new Error("/apply 需要事务 ID");
    }
    return {
      method: `command.${command}`,
      params: argument.length === 0 ? {} : { transaction_id: argument },
    };
  }
  throw new Error(`未知命令：/${command}`);
}
