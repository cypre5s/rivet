import type { IpcEvent } from "./contracts/ipc.ts";
import { initialRivetState, reduceTraceEvent } from "./state/reducer.ts";
import { computeLayout } from "./ui/layout.ts";
import { parseCommandInput } from "./ui/commands.ts";

const commandContext = {
  modelConfigured: true,
  currentModel: "deepseek-v4-pro",
  transactionId: "tx_one",
  verificationStatus: "PASSED",
  evidenceId: "evidence_one",
  acceptanceReady: true,
};

const permissionEvent: IpcEvent = {
  schema_version: 1,
  message_type: "event",
  protocol_version: 1,
  event_id: "event_smoke_permission",
  event_type: "permission.requested",
  sequence: 0,
  payload: {
    request_id: "request_smoke_permission",
    permission: "EXECUTE",
    reason: "运行验收测试",
    argv: "pytest -q",
    cwd: ".",
    timeout_seconds: 60,
  },
};

const state = reduceTraceEvent(initialRivetState(), permissionEvent);
const layout = computeLayout(200, 50);
const commandMethods = [
  "explain this repository",
  "/fix q",
  "/verify",
  "/diff",
  "/apply tx_one",
  "/abort tx_one",
].map((command) => {
  const outcome = parseCommandInput(command, commandContext);
  return outcome.kind === "worker" ? outcome.method : outcome.action;
});

process.stdout.write(
  `${JSON.stringify({
    layout: layout.mode,
    no_color: process.env.NO_COLOR !== undefined,
    inspector_tabs: ["Diff", "Verify", "Evidence"],
    permission_visible: state.permission !== null,
    command_methods: commandMethods,
  })}\n`,
);
