import type {
  ErrorDetail,
  IpcMessage,
  JsonValue,
} from "../contracts/ipc.ts";
import { IPC_PROTOCOL_VERSION } from "../contracts/ipc.ts";

export const MAX_IPC_LINE_BYTES = 1024 * 1024;

export class IpcProtocolError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "IpcProtocolError";
  }
}

export function encodeIpcMessage(message: IpcMessage): string {
  validateIpcMessage(message);
  return `${JSON.stringify(message)}\n`;
}

export function decodeIpcLine(line: string): IpcMessage {
  if (new TextEncoder().encode(line).byteLength > MAX_IPC_LINE_BYTES) {
    throw new IpcProtocolError("ipc.line_size_invalid", "IPC line exceeds limit");
  }
  if (line.includes("\n") || line.includes("\r")) {
    throw new IpcProtocolError("ipc.line_invalid", "IPC message must be one line");
  }
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch {
    throw new IpcProtocolError("ipc.json_invalid", "IPC message is not JSON");
  }
  validateIpcMessage(value);
  return value;
}

export class NdjsonDecoder {
  private buffer = "";

  constructor(private readonly maxLineBytes = MAX_IPC_LINE_BYTES) {}

  push(chunk: string): IpcMessage[] {
    this.buffer += chunk;
    this.assertBufferBound();
    const messages: IpcMessage[] = [];
    while (true) {
      const newline = this.buffer.indexOf("\n");
      if (newline < 0) break;
      let line = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + 1);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      if (line.length === 0) {
        throw new IpcProtocolError("ipc.line_invalid", "empty IPC line");
      }
      messages.push(decodeIpcLine(line));
    }
    this.assertBufferBound();
    return messages;
  }

  finish(): IpcMessage[] {
    if (this.buffer.length === 0) return [];
    throw new IpcProtocolError("ipc.line_incomplete", "IPC stream ended mid-line");
  }

  private assertBufferBound(): void {
    if (new TextEncoder().encode(this.buffer).byteLength > this.maxLineBytes) {
      throw new IpcProtocolError("ipc.line_size_invalid", "IPC line exceeds limit");
    }
  }
}

function validateIpcMessage(value: unknown): asserts value is IpcMessage {
  const record = requireRecord(value, "IPC message");
  if (record.schema_version !== 1 || record.protocol_version !== IPC_PROTOCOL_VERSION) {
    throw new IpcProtocolError("ipc.protocol_mismatch", "IPC version mismatch");
  }
  switch (record.message_type) {
    case "request":
      validateRequest(record);
      return;
    case "response":
      validateResponse(record);
      return;
    case "event":
      validateEvent(record);
      return;
    case "cancel":
      validateCancel(record);
      return;
    default:
      throw new IpcProtocolError("ipc.message_invalid", "unknown IPC message type");
  }
}

function validateRequest(record: Record<string, unknown>): void {
  requireExactKeys(record, [
    "schema_version",
    "message_type",
    "protocol_version",
    "request_id",
    "method",
    "params",
  ]);
  requireRequestId(record.request_id);
  requireDottedName(record.method, "method");
  requireJsonRecord(record.params, "params");
}

function validateResponse(
  record: Record<string, unknown>,
): void {
  requireExactKeys(record, [
    "schema_version",
    "message_type",
    "protocol_version",
    "request_id",
    "ok",
    "result",
    "error",
  ]);
  requireRequestId(record.request_id);
  if (typeof record.ok !== "boolean") invalid("response ok must be boolean");
  if (record.ok) {
    if (record.error !== null) invalid("successful response cannot contain error");
  } else {
    validateError(record.error);
  }
  requireJsonValue(record.result, "result");
}

function validateEvent(record: Record<string, unknown>): void {
  requireExactKeys(record, [
    "schema_version",
    "message_type",
    "protocol_version",
    "event_id",
    "event_type",
    "sequence",
    "payload",
  ]);
  if (typeof record.event_id !== "string" || !/^event_[a-z0-9][a-z0-9_-]{0,62}$/.test(record.event_id)) {
    invalid("invalid event id");
  }
  requireDottedName(record.event_type, "event type");
  if (!Number.isSafeInteger(record.sequence) || Number(record.sequence) < 0) {
    invalid("invalid event sequence");
  }
  requireJsonRecord(record.payload, "payload");
}

function validateCancel(record: Record<string, unknown>): void {
  requireExactKeys(record, [
    "schema_version",
    "message_type",
    "protocol_version",
    "request_id",
    "target_request_id",
  ]);
  requireRequestId(record.request_id);
  requireRequestId(record.target_request_id);
  if (record.request_id === record.target_request_id) invalid("cancel cannot target itself");
}

function validateError(value: unknown): asserts value is ErrorDetail {
  const record = requireRecord(value, "error");
  if (
    record.schema_version !== 1 ||
    typeof record.code !== "string" ||
    typeof record.summary !== "string" ||
    typeof record.next_action !== "string" ||
    typeof record.retryable !== "boolean"
  ) {
    invalid("invalid error detail");
  }
}

function requireRequestId(value: unknown): asserts value is string {
  if (typeof value !== "string" || !/^request_[a-z0-9][a-z0-9_-]{0,62}$/.test(value)) {
    invalid("invalid request id");
  }
}

function requireDottedName(value: unknown, label: string): asserts value is string {
  if (typeof value !== "string" || !/^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(value)) {
    invalid(`invalid ${label}`);
  }
}

function requireJsonRecord(
  value: unknown,
  label: string,
): asserts value is Record<string, JsonValue> {
  const record = requireRecord(value, label);
  for (const child of Object.values(record)) requireJsonValue(child, label);
}

function requireJsonValue(value: unknown, label: string): asserts value is JsonValue {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean" ||
    (typeof value === "number" && Number.isFinite(value))
  ) {
    return;
  }
  if (Array.isArray(value)) {
    for (const child of value) requireJsonValue(child, label);
    return;
  }
  if (typeof value === "object") {
    requireJsonRecord(value, label);
    return;
  }
  invalid(`${label} is not JSON`);
}

function requireRecord(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    invalid(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function requireExactKeys(record: Record<string, unknown>, expected: string[]): void {
  const actual = Object.keys(record).sort();
  const wanted = [...expected].sort();
  if (actual.length !== wanted.length || actual.some((key, index) => key !== wanted[index])) {
    invalid("IPC fields do not match contract");
  }
}

function invalid(message: string): never {
  throw new IpcProtocolError("ipc.message_invalid", message);
}
