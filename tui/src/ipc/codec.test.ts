import { describe, expect, test } from "bun:test";

import {
  IpcProtocolError,
  MAX_IPC_LINE_BYTES,
  NdjsonDecoder,
  decodeIpcLine,
  encodeIpcMessage,
} from "./codec.ts";

describe("NDJSON codec", () => {
  test("decodes fragmented and coalesced messages in order", () => {
    const decoder = new NdjsonDecoder();
    const first =
      '{"schema_version":1,"message_type":"event","protocol_version":1,"event_id":"event_one","event_type":"worker.ready","sequence":0,"payload":{}}\n';
    const second =
      '{"schema_version":1,"message_type":"event","protocol_version":1,"event_id":"event_two","event_type":"worker.idle","sequence":1,"payload":{}}\r\n';

    expect(decoder.push(first.slice(0, 19))).toEqual([]);
    const messages = decoder.push(first.slice(19) + second);

    expect(messages.map((message) => message.message_type)).toEqual([
      "event",
      "event",
    ]);
    expect(decoder.finish()).toEqual([]);
  });

  test("encodes exactly one JSON object and trailing newline", () => {
    const encoded = encodeIpcMessage({
      schema_version: 1,
      message_type: "cancel",
      protocol_version: 1,
      request_id: "request_cancel",
      target_request_id: "request_target",
    });

    expect(encoded.endsWith("\n")).toBeTrue();
    expect(encoded.trim().split("\n")).toHaveLength(1);
    expect(decodeIpcLine(encoded.trim()).message_type).toBe("cancel");
  });

  test("rejects invalid, unknown, mismatched and oversized messages", () => {
    expect(() => decodeIpcLine("not-json")).toThrow(IpcProtocolError);
    expect(() =>
      decodeIpcLine(
        '{"schema_version":1,"message_type":"unknown","protocol_version":1}',
      ),
    ).toThrow(IpcProtocolError);
    expect(() =>
      decodeIpcLine(
        '{"schema_version":1,"message_type":"event","protocol_version":2,"event_id":"event_bad","event_type":"worker.ready","sequence":0,"payload":{}}',
      ),
    ).toThrow(IpcProtocolError);
    expect(() =>
      new NdjsonDecoder(MAX_IPC_LINE_BYTES).push("x".repeat(MAX_IPC_LINE_BYTES + 1)),
    ).toThrow(IpcProtocolError);
  });
});
