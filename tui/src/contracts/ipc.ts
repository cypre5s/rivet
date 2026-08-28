export const IPC_PROTOCOL_VERSION = 1 as const;
export const IPC_SCHEMA_SHA256 = "4e92ee8ba4cea3a034d4ed5b85267452c8765e880b421e40603473656132d902";
export const IPC_FIELD_MAP = {"IpcCancel":["schema_version","message_type","protocol_version","request_id","target_request_id"],"IpcEvent":["schema_version","message_type","protocol_version","event_id","event_type","sequence","payload"],"IpcRequest":["schema_version","message_type","protocol_version","request_id","method","params"],"IpcResponse":["schema_version","message_type","protocol_version","request_id","ok","result","error"]} as const;

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

export interface IpcCancel {
  schema_version: 1;
  message_type: "cancel";
  protocol_version: 1;
  request_id: string;
  target_request_id: string;
}

export type IpcMessage = IpcRequest | IpcResponse | IpcEvent | IpcCancel;
