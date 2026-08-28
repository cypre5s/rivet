import type { WorkerTransport } from "./client.ts";

type ChunkListener = (chunk: string) => void;
type ExitListener = (exitCode: number | null) => void;

export interface BunWorkerTransportOptions {
  command: string[];
  cwd: string;
  environment: Record<string, string>;
}

export class BunWorkerTransport implements WorkerTransport {
  private readonly process: Bun.Subprocess<"pipe", "pipe", "pipe">;
  private readonly stdoutListeners = new Set<ChunkListener>();
  private readonly stderrListeners = new Set<ChunkListener>();
  private readonly exitListeners = new Set<ExitListener>();
  private closed = false;
  private shutdown: Promise<void> | null = null;

  constructor(options: BunWorkerTransportOptions) {
    this.process = Bun.spawn(options.command, {
      cwd: options.cwd,
      env: options.environment,
      stdin: "pipe",
      stdout: "pipe",
      stderr: "pipe",
    });
    void this.pump(this.process.stdout, this.stdoutListeners).catch(() => {});
    void this.pump(this.process.stderr, this.stderrListeners).catch(() => {});
    void this.process.exited.then((exitCode) => {
      for (const listener of this.exitListeners) listener(exitCode);
    });
  }

  write(line: string): void {
    if (this.closed) throw new Error("worker transport is closed");
    this.process.stdin.write(line);
    void this.process.stdin.flush();
  }

  onStdout(listener: ChunkListener): () => void {
    this.stdoutListeners.add(listener);
    return () => this.stdoutListeners.delete(listener);
  }

  onStderr(listener: ChunkListener): () => void {
    this.stderrListeners.add(listener);
    return () => this.stderrListeners.delete(listener);
  }

  onExit(listener: ExitListener): () => void {
    this.exitListeners.add(listener);
    return () => this.exitListeners.delete(listener);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.process.stdin.end();
    this.shutdown = this.stopWithinBounds();
  }

  async waitForExit(): Promise<number> {
    return await this.process.exited;
  }

  async waitForShutdown(): Promise<void> {
    await this.shutdown;
  }

  private async pump(
    stream: ReadableStream<Uint8Array>,
    listeners: Set<ChunkListener>,
  ): Promise<void> {
    const decoder = new TextDecoder();
    const reader = stream.getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const text = decoder.decode(value, { stream: true });
        if (text.length > 0) {
          for (const listener of listeners) listener(text);
        }
      }
    } finally {
      reader.releaseLock();
    }
    const tail = decoder.decode();
    if (tail.length > 0) {
      for (const listener of listeners) listener(tail);
    }
  }

  private async stopWithinBounds(): Promise<void> {
    if (await this.exitedWithin(500)) return;
    if (this.process.exitCode === null) this.process.kill("SIGTERM");
    if (await this.exitedWithin(500)) return;
    if (this.process.exitCode === null) this.process.kill("SIGKILL");
    await this.process.exited;
  }

  private async exitedWithin(milliseconds: number): Promise<boolean> {
    if (this.process.exitCode !== null) return true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timedOut = new Promise<false>((resolve) => {
      timer = setTimeout(() => resolve(false), milliseconds);
    });
    const exited = this.process.exited.then(() => true as const);
    const result = await Promise.race([exited, timedOut]);
    if (timer !== undefined) clearTimeout(timer);
    return result;
  }
}
