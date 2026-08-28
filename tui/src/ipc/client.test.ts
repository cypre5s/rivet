import { describe, expect, test } from "bun:test";

import type { IpcMessage } from "../contracts/ipc.ts";
import {
  WorkerClient,
  WorkerResponseError,
  type WorkerTransport,
} from "./client.ts";
import { decodeIpcLine } from "./codec.ts";

class FakeTransport implements WorkerTransport {
  readonly writes: string[] = [];
  private stdoutListeners = new Set<(chunk: string) => void>();
  private stderrListeners = new Set<(chunk: string) => void>();
  private exitListeners = new Set<(exitCode: number | null) => void>();

  write(line: string): void {
    this.writes.push(line);
  }

  onStdout(listener: (chunk: string) => void): () => void {
    this.stdoutListeners.add(listener);
    return () => this.stdoutListeners.delete(listener);
  }

  onStderr(listener: (chunk: string) => void): () => void {
    this.stderrListeners.add(listener);
    return () => this.stderrListeners.delete(listener);
  }

  onExit(listener: (exitCode: number | null) => void): () => void {
    this.exitListeners.add(listener);
    return () => this.exitListeners.delete(listener);
  }

  close(): void {}

  emit(message: IpcMessage): void {
    const chunk = `${JSON.stringify(message)}\n`;
    for (const listener of this.stdoutListeners) listener(chunk);
  }

  emitStderr(chunk: string): void {
    for (const listener of this.stderrListeners) listener(chunk);
  }

  exit(exitCode: number | null): void {
    for (const listener of this.exitListeners) listener(exitCode);
  }
}

function response(requestId: string, result: string): IpcMessage {
  return {
    schema_version: 1,
    message_type: "response",
    protocol_version: 1,
    request_id: requestId,
    ok: true,
    result,
    error: null,
  };
}

describe("WorkerClient", () => {
  test("handshakes, correlates responses and forwards events", async () => {
    const transport = new FakeTransport();
    let nextId = 0;
    const client = new WorkerClient(transport, {
      requestIdFactory: () => `request_test_${nextId++}`,
    });
    const events: string[] = [];
    client.onEvent((event) => events.push(event.event_type));
    const start = client.start();
    const handshake = decodeIpcLine((transport.writes[0] ?? "").trimEnd());
    expect(handshake.message_type).toBe("request");
    transport.emit(response("request_test_0", "ready"));
    await expect(start).resolves.toBe("ready");

    const pending = client.request("worker.ping", {});
    transport.emit({
      schema_version: 1,
      message_type: "event",
      protocol_version: 1,
      event_id: "event_ready",
      event_type: "worker.ready",
      sequence: 0,
      payload: {},
    });
    transport.emit(response("request_test_1", "pong"));

    await expect(pending).resolves.toBe("pong");
    expect(events).toEqual(["worker.ready"]);
  });

  test("sends a dedicated cancel and rejects pending requests on crash", async () => {
    const transport = new FakeTransport();
    let nextId = 0;
    const client = new WorkerClient(transport, {
      requestIdFactory: () => `request_cancel_${nextId++}`,
      requireHandshake: false,
    });
    const pending = client.request("worker.wait", { milliseconds: 1000 });
    const request = decodeIpcLine((transport.writes[0] ?? "").trimEnd());
    if (request.message_type !== "request") throw new Error("expected request");

    client.cancel(request.request_id);
    const cancellation = decodeIpcLine((transport.writes[1] ?? "").trimEnd());
    expect(cancellation).toMatchObject({
      message_type: "cancel",
      target_request_id: request.request_id,
    });
    transport.exit(17);

    await expect(pending).rejects.toThrow("worker exited with code 17");
  });

  test("keeps worker diagnostics separate from protocol events", () => {
    const transport = new FakeTransport();
    const diagnostics: string[] = [];
    const client = new WorkerClient(transport, { requireHandshake: false });
    client.onDiagnostic((line) => diagnostics.push(line));

    transport.emitStderr("诊断信息\n");

    expect(diagnostics).toEqual(["诊断信息\n"]);
  });

  test("preserves actionable worker error fields", async () => {
    const transport = new FakeTransport();
    const client = new WorkerClient(transport, {
      requireHandshake: false,
      requestIdFactory: () => "request_error_one",
    });
    const pending = client.request("module.operation", {});
    transport.emit({
      schema_version: 1,
      message_type: "response",
      protocol_version: 1,
      request_id: "request_error_one",
      ok: false,
      result: null,
      error: {
        schema_version: 1,
        code: "module.lease_blocked",
        summary: "模块存在活动 Lease",
        next_action: "等待任务结束后重试",
        retryable: true,
        run_id: null,
        session_id: null,
        transaction_id: null,
        trace_event_id: "event_module_blocked",
        cause_redacted: null,
      },
    });

    const error = await pending.catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(WorkerResponseError);
    expect(error).toMatchObject({
      code: "module.lease_blocked",
      nextAction: "等待任务结束后重试",
      retryable: true,
      traceEventId: "event_module_blocked",
    });
  });

  test("requests graceful worker shutdown before closing transport", async () => {
    const transport = new FakeTransport();
    let nextId = 0;
    const client = new WorkerClient(transport, {
      requestIdFactory: () => `request_shutdown_${nextId++}`,
    });
    const start = client.start();
    transport.emit(response("request_shutdown_0", "ready"));
    await start;

    client.close();

    expect(decodeIpcLine((transport.writes[1] ?? "").trimEnd())).toMatchObject({
      message_type: "request",
      method: "worker.shutdown",
    });
  });
});
