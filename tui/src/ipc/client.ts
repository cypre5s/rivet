import type {
  IpcCancel,
  IpcEvent,
  IpcRequest,
  IpcResponse,
  JsonValue,
} from "../contracts/ipc.ts";
import { NdjsonDecoder, encodeIpcMessage } from "./codec.ts";

type Unsubscribe = () => void;

export interface WorkerTransport {
  write(line: string): void;
  onStdout(listener: (chunk: string) => void): Unsubscribe;
  onStderr(listener: (chunk: string) => void): Unsubscribe;
  onExit(listener: (exitCode: number | null) => void): Unsubscribe;
  close(): void;
}

interface PendingRequest {
  resolve(value: JsonValue): void;
  reject(reason: Error): void;
}

export interface TrackedRequest {
  requestId: string;
  result: Promise<JsonValue>;
}

export interface WorkerStatus {
  state: "ready" | "crashed" | "closed";
  summary: string;
}

export interface WorkerClientOptions {
  requestIdFactory?: () => string;
  requireHandshake?: boolean;
}

export class WorkerResponseError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly nextAction: string,
    readonly retryable: boolean,
    readonly traceEventId: string | null,
  ) {
    super(message);
    this.name = "WorkerResponseError";
  }
}

export class WorkerClient {
  private readonly decoder = new NdjsonDecoder();
  private readonly pending = new Map<string, PendingRequest>();
  private readonly eventListeners = new Set<(event: IpcEvent) => void>();
  private readonly diagnosticListeners = new Set<(line: string) => void>();
  private readonly statusListeners = new Set<(status: WorkerStatus) => void>();
  private readonly unsubscribers: Unsubscribe[];
  private readonly requestIdFactory: () => string;
  private readonly requireHandshake: boolean;
  private ready = false;
  private closed = false;

  constructor(
    private readonly transport: WorkerTransport,
    options: WorkerClientOptions = {},
  ) {
    this.requestIdFactory = options.requestIdFactory ?? defaultRequestId;
    this.requireHandshake = options.requireHandshake ?? true;
    this.unsubscribers = [
      transport.onStdout((chunk) => this.handleStdout(chunk)),
      transport.onStderr((chunk) => this.handleDiagnostic(chunk)),
      transport.onExit((exitCode) => this.handleExit(exitCode)),
    ];
  }

  async start(): Promise<JsonValue> {
    const result = await this.sendRequest("worker.handshake", { client: "rivet-tui" });
    this.ready = true;
    this.emitStatus({ state: "ready", summary: "Worker 已连接" });
    return result;
  }

  request(method: string, params: Record<string, JsonValue>): Promise<JsonValue> {
    if (this.requireHandshake && !this.ready) {
      return Promise.reject(new Error("worker handshake has not completed"));
    }
    return this.sendRequest(method, params);
  }

  beginRequest(method: string, params: Record<string, JsonValue>): TrackedRequest {
    if (this.requireHandshake && !this.ready) {
      const requestId = this.requestIdFactory();
      return {
        requestId,
        result: Promise.reject(new Error("worker handshake has not completed")),
      };
    }
    return this.createRequest(method, params);
  }

  cancel(targetRequestId: string): void {
    this.assertOpen();
    const cancellation: IpcCancel = {
      schema_version: 1,
      message_type: "cancel",
      protocol_version: 1,
      request_id: this.requestIdFactory(),
      target_request_id: targetRequestId,
    };
    this.transport.write(encodeIpcMessage(cancellation));
  }

  onEvent(listener: (event: IpcEvent) => void): Unsubscribe {
    this.eventListeners.add(listener);
    return () => this.eventListeners.delete(listener);
  }

  onDiagnostic(listener: (line: string) => void): Unsubscribe {
    this.diagnosticListeners.add(listener);
    return () => this.diagnosticListeners.delete(listener);
  }

  onStatus(listener: (status: WorkerStatus) => void): Unsubscribe {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  close(): void {
    if (this.closed) return;
    if (this.ready) {
      const shutdown: IpcRequest = {
        schema_version: 1,
        message_type: "request",
        protocol_version: 1,
        request_id: this.requestIdFactory(),
        method: "worker.shutdown",
        params: {},
      };
      try {
        this.transport.write(encodeIpcMessage(shutdown));
      } catch {}
    }
    this.closed = true;
    this.ready = false;
    for (const unsubscribe of this.unsubscribers) unsubscribe();
    this.transport.close();
    this.rejectPending(new Error("worker client closed"));
    this.emitStatus({ state: "closed", summary: "Worker 连接已关闭" });
  }

  private sendRequest(
    method: string,
    params: Record<string, JsonValue>,
  ): Promise<JsonValue> {
    return this.createRequest(method, params).result;
  }

  private createRequest(
    method: string,
    params: Record<string, JsonValue>,
  ): TrackedRequest {
    this.assertOpen();
    const requestId = this.requestIdFactory();
    const request: IpcRequest = {
      schema_version: 1,
      message_type: "request",
      protocol_version: 1,
      request_id: requestId,
      method,
      params,
    };
    const result = new Promise<JsonValue>((resolve, reject) => {
      this.pending.set(requestId, { resolve, reject });
      this.transport.write(encodeIpcMessage(request));
    });
    return { requestId, result };
  }

  private handleStdout(chunk: string): void {
    try {
      for (const message of this.decoder.push(chunk)) {
        if (message.message_type === "event") {
          for (const listener of this.eventListeners) listener(message);
        } else if (message.message_type === "response") {
          this.handleResponse(message);
        }
      }
    } catch (error) {
      this.closed = true;
      this.rejectPending(
        error instanceof Error ? error : new Error("invalid worker output"),
      );
      this.transport.close();
      this.emitStatus({ state: "crashed", summary: "Worker 输出违反 IPC 协议" });
    }
  }

  private handleResponse(response: IpcResponse): void {
    const pending = this.pending.get(response.request_id);
    if (pending === undefined) return;
    this.pending.delete(response.request_id);
    if (response.ok) {
      pending.resolve(response.result);
      return;
    }
    pending.reject(
      new WorkerResponseError(
        response.error?.code ?? "ipc.response_invalid",
        response.error?.summary ?? "worker request failed",
        response.error?.next_action ?? "刷新状态后重试",
        response.error?.retryable ?? false,
        response.error?.trace_event_id ?? null,
      ),
    );
  }

  private handleDiagnostic(chunk: string): void {
    for (const listener of this.diagnosticListeners) listener(chunk);
  }

  private handleExit(exitCode: number | null): void {
    if (this.closed) return;
    this.closed = true;
    const rendered = exitCode === null ? "signal" : `code ${exitCode}`;
    this.rejectPending(new Error(`worker exited with ${rendered}`));
    this.emitStatus({ state: "crashed", summary: `Worker 退出：${rendered}` });
  }

  private rejectPending(error: Error): void {
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
  }

  private assertOpen(): void {
    if (this.closed) throw new Error("worker client is closed");
  }

  private emitStatus(status: WorkerStatus): void {
    for (const listener of this.statusListeners) listener(status);
  }
}

function defaultRequestId(): string {
  return `request_${crypto.randomUUID().replaceAll("-", "")}`;
}
