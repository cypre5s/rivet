import type { IpcEvent } from "./contracts/ipc.ts";
import { initialRivetState, reduceTraceEvent } from "./state/reducer.ts";
import { buildViewModel } from "./ui/view-model.ts";
import { parseCommandInput } from "./ui/commands.ts";

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
const view = buildViewModel(state, {
  width: 200,
  height: 50,
  noColor: process.env.NO_COLOR !== undefined,
});

process.stdout.write(
  `${JSON.stringify({
    layout: view.layout.mode,
    no_color: view.noColor,
    inspector_tabs: view.inspectorTabs,
    permission_visible: view.permission !== null,
    command_methods: [
      "/ask q",
      "/plan q",
      "/fix q",
      "/verify",
      "/diff",
      "/apply tx_one",
    ].map((command) => parseCommandInput(command).method),
  })}\n`,
);
