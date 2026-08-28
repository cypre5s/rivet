export const IPC_PROTOCOL_VERSION = 1 as const;
export const IPC_SCHEMA_SHA256 = "0fa44dfd84f6513cf72daf535c65afc6982f83f37da9d51cede4c3d21ba1f45b";
export const IPC_FIELD_MAP = {"IpcEvent":["schema_version","message_type","protocol_version","event_id","event_type","sequence","payload"],"IpcRequest":["schema_version","message_type","protocol_version","request_id","method","params"],"IpcResponse":["schema_version","message_type","protocol_version","request_id","ok","result","error"]} as const;

export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

export interface ErrorDetail {
  schema_version: 1;
  code: string;
  summary: string;
  next_action: string;
  retryable: boolean;
  run_id: string | null;
  session_id: string | null;
  transaction_id: string | null;
  trace_event_id: string | null;
  cause_redacted: string | null;
}

export interface IpcRequest {
  schema_version: 1;
  message_type: "request";
  protocol_version: 1;
  request_id: string;
  method: string;
  params: { [key: string]: JsonValue };
}

export interface IpcResponse {
  schema_version: 1;
  message_type: "response";
  protocol_version: 1;
  request_id: string;
  ok: boolean;
  result: JsonValue | null;
  error: ErrorDetail | null;
}

export interface IpcEvent {
  schema_version: 1;
  message_type: "event";
  protocol_version: 1;
  event_id: string;
  event_type: string;
  sequence: number;
  payload: { [key: string]: JsonValue };
}

export type IpcMessage = IpcRequest | IpcResponse | IpcEvent;
