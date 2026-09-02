import type { RivetState } from "../state/reducer.ts";
import {
  findCommand,
  type CommandDescriptor,
} from "./command-registry.ts";

export function createPaletteResources(
  state: RivetState,
  files: string[],
  models: readonly string[],
): CommandDescriptor[] {
  const resources: CommandDescriptor[] = [];
  for (const [index, session] of state.sessions.slice(0, 8).entries()) {
    resources.push(
      resource("resume", session, `恢复 ${session}`, `session-${index}`),
    );
  }
  for (const [index, path] of files.slice(0, 12).entries()) {
    resources.push(resource("read", path, `读取 ${path}`, `file-${index}`));
  }
  for (const [index, model] of models.entries()) {
    resources.push(resource("model", model, `选择 ${model}`, `model-${index}`));
  }
  const moduleIds = state.moduleStatuses.length
    ? state.moduleStatuses.map((status) => status.moduleId)
    : state.taskModules;
  for (const [index, moduleId] of moduleIds.slice(0, 8).entries()) {
    resources.push(
      resource("modules", moduleId, `查看 ${moduleId}`, `module-${index}`),
    );
  }
  if (state.transaction !== "无") {
    resources.push(
      resource(
        "diff",
        state.transaction,
        `查看 ${state.transaction}`,
        "current-transaction",
      ),
    );
  }
  if (state.evidenceId !== "无") {
    resources.push({
      ...requiredCommand("evidence"),
      id: "resource.current-evidence",
      title: `查看 ${state.evidenceId}`,
    });
  }
  return resources;
}

function resource(
  commandName: string,
  argument: string,
  title: string,
  id: string,
): CommandDescriptor {
  const command = requiredCommand(commandName);
  return {
    ...command,
    id: `resource.${id}`,
    name: `${command.name} ${argument}`,
    aliases: [],
    title,
    description: `${command.description} · 最近资源`,
    usage: `/${command.name} ${argument}`,
    argumentKind: "none",
  };
}

function requiredCommand(name: string): CommandDescriptor {
  const command = findCommand(name);
  if (command === null) throw new Error(`palette command is missing: ${name}`);
  return command;
}
