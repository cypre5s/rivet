import { describe, expect, test } from "bun:test";
import { testRender } from "@opentui/react/test-utils";
import { act } from "react";

import { RivetApp } from "./app.tsx";
import type { IpcRequest } from "./contracts/ipc.ts";
import { WorkerClient, type WorkerTransport } from "./ipc/client.ts";
import { initialRivetState, type RivetState } from "./state/reducer.ts";

class CaptureTransport implements WorkerTransport {
  readonly writes: string[] = [];
  private nextSequence = 0;
  private readonly stdoutListeners = new Set<(chunk: string) => void>();
  private readonly stderrListeners = new Set<(chunk: string) => void>();
  private readonly exitListeners = new Set<(exitCode: number | null) => void>();

  write(line: string): void {
    this.writes.push(line);
    const request = JSON.parse(line) as IpcRequest;
    if (
      request.method === "worker.handshake" ||
      request.method === "sessions.list" ||
      request.method === "transactions.list" ||
      request.method === "command.read" ||
      request.method === "config.update"
    ) {
      queueMicrotask(() => {
        if (request.method === "sessions.list") {
          const event = `${JSON.stringify({
            schema_version: 1,
            message_type: "event",
            protocol_version: 1,
            event_id: "event_sessions",
            event_type: "sessions.snapshot",
            sequence: this.nextSequence++,
            payload: { sessions: ["session_recent"] },
          })}\n`;
          for (const listener of this.stdoutListeners) listener(event);
        }
        if (request.method === "transactions.list") {
          const event = `${JSON.stringify({
            schema_version: 1,
            message_type: "event",
            protocol_version: 1,
            event_id: "event_transactions",
            event_type: "transactions.snapshot",
            sequence: this.nextSequence++,
            payload: {
              transactions: [
                {
                  transaction_id: "tx_recent",
                  state: "VERIFIED",
                  evidence_id: "evidence_recent",
                },
              ],
            },
          })}\n`;
          for (const listener of this.stdoutListeners) listener(event);
        }
        const configResult =
          request.method === "config.update"
            ? {
                base_url: request.params.base_url,
                credential_configured:
                  request.params.api_key_action !== "clear",
                max_cost_usd: request.params.max_cost_usd,
                max_rounds: request.params.max_rounds,
                max_total_tokens: request.params.max_total_tokens,
                model: request.params.model,
                models: request.params.models,
                safe_mode: request.params.safe_mode,
                sources: {},
              }
            : null;
        const response = `${JSON.stringify({
          schema_version: 1,
          message_type: "response",
          protocol_version: 1,
          request_id: request.request_id,
          ok: true,
          result:
            request.method === "sessions.list"
              ? { sessions: ["session_recent"] }
              : request.method === "transactions.list"
                ? {
                    transactions: [
                      {
                        transaction_id: "tx_recent",
                        state: "VERIFIED",
                        evidence_id: "evidence_recent",
                      },
                    ],
                  }
              : request.method === "command.read"
                ? {
                    detected_format: "image",
                    source_path: request.params.file,
                    reader_id: "reader.image",
                    status: "DEGRADED",
                    content: "OCR dependency unavailable",
                  }
                : configResult ?? { status: "ready" },
          error: null,
        })}\n`;
        for (const listener of this.stdoutListeners) listener(response);
      });
    }
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

  emitEvent(eventType: string, payload: Record<string, unknown>, sequence = 0): void {
    const line = `${JSON.stringify({
      schema_version: 1,
      message_type: "event",
      protocol_version: 1,
      event_id: `event_capture_${sequence}`,
      event_type: eventType,
      sequence,
      payload,
    })}\n`;
    for (const listener of this.stdoutListeners) listener(line);
  }
}

function readyState(overrides: Partial<RivetState> = {}): RivetState {
  return {
    ...initialRivetState(),
    connection: "ready",
    repository: "/home/tester/rivet",
    branch: "main",
    model: "deepseek-v4-pro",
    models: ["deepseek-v4-pro", "deepseek-v4-flash"],
    credentialConfigured: true,
    ...overrides,
  };
}

describe("Rivet OpenTUI experience", () => {
  test("refreshes the model picker from a live Worker ready event", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp
        initialState={initialRivetState()}
        noColor={true}
        client={client}
      />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());
    transport.emitEvent("worker.ready", {
      repository: "/repo",
      branch: "main",
      credential_configured: true,
      model: "live-reasoner",
      models: ["live-chat", "live-reasoner"],
      base_url: "https://gateway.example.test/v1",
      max_rounds: 12,
      max_total_tokens: 64_000,
      max_cost_usd: null,
      safe_mode: false,
    });
    await act(async () => Bun.sleep(30));
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("模型 live-reasoner");

    await act(async () => setup.mockInput.pressKey("k", { ctrl: true }));
    await setup.flush();
    const picker = setup.captureCharFrame();
    expect(picker).toContain("live-chat");
    expect(picker).not.toContain("deepseek-v4-flash");
    await act(async () => setup.renderer.destroy());
  });

  test("renders the configured model below the composer and selects dynamic models", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp
        initialState={readyState({
          model: "team-chat",
          models: ["team-chat", "team-reasoner", "team-code"],
        })}
        noColor={true}
        client={client}
      />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());

    const initial = setup.captureCharFrame();
    expect(initial).toContain("模型 team-chat");
    expect(initial).toContain("3 个可用模型");
    await act(async () => setup.mockInput.pressKey("k", { ctrl: true }));
    await setup.flush();
    const picker = setup.captureCharFrame();
    expect(picker).toContain("team-reasoner");
    expect(picker).toContain("team-code");
    expect(picker).not.toContain("deepseek-v4-flash");
    await act(async () => setup.mockInput.pressArrow("down"));
    await act(async () => setup.mockInput.pressEnter());
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("模型 team-reasoner");
    await act(async () => setup.mockInput.typeText("解释配置"));
    await act(async () => setup.mockInput.pressEnter());
    await setup.flush();
    const command = transport.writes
      .map((line) => JSON.parse(line) as IpcRequest)
      .find((item) => item.method === "command.ask");
    expect(command?.params.model).toBe("team-reasoner");
    await act(async () => setup.renderer.destroy());
  });

  test("opens quick configuration without rendering or echoing the API key", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp
        initialState={readyState({ credentialConfigured: false })}
        noColor={true}
        client={client}
      />,
      { width: 110, height: 32 },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.mockInput.pressKey("g", { ctrl: true }));
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("连接与模型配置");
    expect(setup.captureCharFrame()).toContain("仅保存在当前 Worker 会话");

    const secret = "fixture-tui-value-that-must-be-masked";
    await act(async () => setup.mockInput.typeText(secret));
    await setup.flush();
    const masked = setup.captureCharFrame();
    expect(masked).not.toContain(secret);
    expect(masked).toContain("•".repeat(secret.length));

    await act(async () => setup.mockInput.pressKey("s", { ctrl: true }));
    await act(async () => Bun.sleep(30));
    await setup.flush();
    const request = transport.writes
      .map((line) => JSON.parse(line) as IpcRequest)
      .find((item) => item.method === "config.update");
    expect(request?.params.api_key_action).toBe("replace");
    expect(request?.params.api_key).toBe(secret);
    expect(setup.captureCharFrame()).not.toContain(secret);
    expect(setup.captureCharFrame()).toContain("配置已保存");
    await act(async () => setup.renderer.destroy());
  });

  test("clears a session API key only after an explicit configuration save", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} client={client} />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.mockInput.pressKey("g", { ctrl: true }));
    await setup.flush();
    await act(async () => setup.mockInput.pressKey("d", { ctrl: true }));
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("保存后清除当前会话 Key");
    await act(async () => setup.mockInput.pressKey("s", { ctrl: true }));
    await setup.flush();

    const request = transport.writes
      .map((line) => JSON.parse(line) as IpcRequest)
      .find((item) => item.method === "config.update");
    expect(request?.params.api_key_action).toBe("clear");
    expect(request?.params.api_key).toBeUndefined();
    expect(setup.captureCharFrame()).toContain("Key ○");
    await act(async () => setup.renderer.destroy());
  });

  test("keeps the configuration dialog open when endpoint validation fails", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} client={client} />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.mockInput.pressKey("g", { ctrl: true }));
    await setup.flush();
    await act(async () => setup.mockInput.pressTab());
    await setup.flush();
    await act(async () => setup.mockInput.pressKey("a", { ctrl: true }));
    await act(async () => setup.mockInput.typeText("file:///tmp/provider"));
    await setup.flush();
    await act(async () => setup.mockInput.pressKey("s", { ctrl: true }));
    await setup.flush();

    expect(setup.captureCharFrame()).toContain("HTTP(S)");
    expect(
      transport.writes.some(
        (line) =>
          (JSON.parse(line) as IpcRequest).method === "config.update",
      ),
    ).toBeFalse();
    await act(async () => setup.renderer.destroy());
  });

  test("keeps every configuration field reachable in a 40x12 terminal", async () => {
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} />,
      { width: 40, height: 12 },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.mockInput.pressKey("g", { ctrl: true }));
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("连接与模型配置");

    for (let index = 0; index < 7; index += 1) {
      await act(async () => setup.mockInput.pressTab());
      await setup.flush();
    }
    const compact = setup.captureCharFrame();
    expect(compact).toContain("Safe Mode");
    expect(compact.split("\n")).toHaveLength(13);
    await act(async () => setup.renderer.destroy());
  });

  test("renders a focused minimal welcome screen at every supported breakpoint", async () => {
    const sizes = [
      [40, 12],
      [60, 18],
      [80, 24],
      [100, 28],
      [120, 30],
      [160, 40],
    ] as const;
    for (const [width, height] of sizes) {
      const setup = await testRender(
        <RivetApp initialState={readyState()} noColor={true} />,
        { width, height },
      );
      await act(async () => setup.renderOnce());
      const frame = setup.captureCharFrame();

      expect(frame).toContain("修复失败的测试");
      expect(frame).toContain("ASK");
      expect(frame).not.toContain("Repository / Context");
      expect(frame).not.toContain("Chat / Trace");
      expect(setup.renderer.currentFocusedEditor).not.toBeNull();
      await act(async () => setup.renderer.destroy());
    }
  });

  test("opens, searches, completes and closes the Slash menu", async () => {
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());

    await act(async () => setup.mockInput.typeText("/ver"));
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("/verify");

    await act(async () => setup.mockInput.pressTab());
    await setup.flush();
    const completed = setup.captureCharFrame();
    expect(completed).toContain("/verify");
    expect(completed).not.toContain("全部操作");

    await act(async () => setup.mockInput.pressEscape());
    await act(async () => setup.renderer.destroy());
  });

  test("offers second-level argument completion without submitting early", async () => {
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());

    await act(async () => setup.mockInput.typeText("/mode f"));
    await setup.flush();
    const menu = setup.captureCharFrame();
    expect(menu).toContain("/mode 参数");
    expect(menu).toContain("fix");

    await act(async () => setup.mockInput.pressEnter());
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("/mode fix");
    await act(async () => setup.renderer.destroy());
  });

  test("routes module lifecycle mutations through the real worker request", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp
        initialState={readyState({ modules: ["reader.pdf"] })}
        noColor={true}
        client={client}
      />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.mockInput.typeText("/modules reader"));
    await setup.flush();

    const frame = setup.captureCharFrame();
    expect(frame).toContain("reader.pdf");
    expect(frame).toContain("enable reader.pdf");
    expect(frame).not.toContain("当前 CLI 仅支持查看");

    await act(async () => setup.mockInput.pressArrow("down"));
    await act(async () => setup.mockInput.pressEnter());
    await act(async () => Bun.sleep(30));
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("/modules enable reader.pdf");
    await act(async () => setup.mockInput.pressEnter());
    await setup.flush();
    const request = transport.writes
      .map((line) => JSON.parse(line) as IpcRequest)
      .find((item) => item.method === "module.operation");
    expect(request?.params).toEqual({
      operation: "enable",
      module_id: "reader.pdf",
    });
    await act(async () => setup.renderer.destroy());
  });

  test("mentions repository files as removable context chips", async () => {
    const setup = await testRender(
      <RivetApp
        initialState={readyState({
          fileTree: ["tests/app.test.ts", "src/app.ts", "src/service.ts"],
        })}
        noColor={true}
      />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());

    await act(async () => setup.mockInput.typeText("查看 @app"));
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("src/app.ts");
    await act(async () => setup.mockInput.pressEnter());
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("@src/app.ts ×");
    await act(async () => setup.renderer.destroy());
  });

  test("adds and clears explicit context through Slash commands", async () => {
    const setup = await testRender(
      <RivetApp
        initialState={readyState({ fileTree: ["src/app.ts"] })}
        noColor={true}
      />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());

    await act(async () => setup.mockInput.typeText("/context add src/app.ts"));
    await setup.flush();
    await act(async () => setup.mockInput.pressEnter());
    await setup.flush();
    await act(async () => setup.mockInput.pressEnter());
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("@src/app.ts ×");

    await act(async () => setup.mockInput.typeText("/context clear"));
    await setup.flush();
    await act(async () => setup.mockInput.pressEnter());
    await setup.flush();
    await act(async () => setup.mockInput.pressEnter());
    await setup.flush();
    expect(setup.captureCharFrame()).not.toContain("@src/app.ts ×");
    await act(async () => setup.renderer.destroy());
  });

  test("sends selected file chips as validated IPC context paths", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp
        initialState={readyState({ fileTree: ["src/service.py"] })}
        noColor={true}
        client={client}
      />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.mockInput.typeText("解释 @service"));
    await setup.flush();
    await act(async () => setup.mockInput.pressEnter());
    await setup.flush();
    await act(async () => setup.mockInput.pressEnter());
    await setup.flush();

    const requests = transport.writes.map(
      (line) => JSON.parse(line) as IpcRequest,
    );
    const command = requests.find(
      (request) => request.method === "command.ask",
    );
    expect(command?.params.context_paths).toEqual(["src/service.py"]);
    await act(async () => setup.renderer.destroy());
  });

  test("turns bracketed large paste into an attachment without auto-submit", async () => {
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());
    const pasted = Array.from(
      { length: 9 },
      (_, index) => `line ${index}`,
    ).join("\n");

    await act(async () => setup.mockInput.pasteBracketedText(pasted));
    await setup.flush();
    const frame = setup.captureCharFrame();
    expect(frame).toContain("粘贴 9 行");
    expect(frame).not.toContain("YOU");
    await act(async () => setup.renderer.destroy());
  });

  test("reflows on resize while keeping the composer visible", async () => {
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} />,
      { width: 120, height: 30 },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.resize(40, 12));
    await setup.flush();

    const frame = setup.captureCharFrame();
    expect(frame).toContain("R I V E T");
    expect(frame).toContain("ASK");
    await act(async () => setup.renderer.destroy());
  });

  test("keeps the Slash menu bounded in the smallest supported terminal", async () => {
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} />,
      { width: 40, height: 12 },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.mockInput.typeText("/"));
    await setup.flush();

    const frame = setup.captureCharFrame();
    expect(frame).toContain("全部操作");
    expect(frame.split("\n")).toHaveLength(13);
    await act(async () => setup.renderer.destroy());
  });

  test("opens global palette, file picker and Leader help from any main focus", async () => {
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());

    await act(async () => setup.mockInput.pressKey("p", { ctrl: true }));
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("搜索命令、面板和资源");
    await act(async () => setup.mockInput.typeText("deepseek"));
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("deepseek-v4-pro");
    await act(async () => setup.mockInput.pressEscape());
    await act(async () => Bun.sleep(30));
    await setup.flush();

    await act(async () => setup.mockInput.pressKey("o", { ctrl: true }));
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("搜索仓库内文件");
    await act(async () => setup.mockInput.pressEscape());
    await act(async () => Bun.sleep(30));
    await setup.flush();

    await act(async () => setup.mockInput.pressKey("x", { ctrl: true }));
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("v Verify");
    await act(async () => setup.mockInput.pressEscape());
    await act(async () => setup.renderer.destroy());
  });

  test("closes the Slash overlay before exiting on a second Ctrl+C", async () => {
    let exitCount = 0;
    const setup = await testRender(
      <RivetApp
        initialState={readyState()}
        noColor={true}
        onExit={() => exitCount++}
      />,
      { width: 100, height: 28, exitOnCtrlC: false, kittyKeyboard: true },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.mockInput.typeText("/"));
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("/全部操作");

    await act(async () => setup.mockInput.pressKey("c", { ctrl: true }));
    await setup.flush();
    expect(exitCount).toBe(0);
    expect(setup.captureCharFrame()).not.toContain("/全部操作");

    await act(async () => setup.mockInput.pressKey("c", { ctrl: true }));
    expect(exitCount).toBe(1);
    await act(async () => setup.renderer.destroy());
  });

  test("closes the file picker before exiting on a second Ctrl+C", async () => {
    let exitCount = 0;
    const setup = await testRender(
      <RivetApp
        initialState={readyState()}
        noColor={true}
        onExit={() => exitCount++}
      />,
      { width: 100, height: 28, exitOnCtrlC: false, kittyKeyboard: true },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.mockInput.pressKey("o", { ctrl: true }));
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("搜索仓库内文件");

    await act(async () => setup.mockInput.pressKey("c", { ctrl: true }));
    await setup.flush();
    expect(exitCount).toBe(0);
    expect(setup.captureCharFrame()).not.toContain("搜索仓库内文件");

    await act(async () => setup.mockInput.pressKey("c", { ctrl: true }));
    expect(exitCount).toBe(1);
    await act(async () => setup.renderer.destroy());
  });

  test("loads recent sessions through IPC only when a session view is requested", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} client={client} />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());
    expect(
      transport.writes.some(
        (line) => (JSON.parse(line) as IpcRequest).method === "sessions.list",
      ),
    ).toBeFalse();

    await act(async () => setup.mockInput.pressKey("p", { ctrl: true }));
    await act(async () => Bun.sleep(30));
    await setup.flush();

    expect(
      transport.writes.some(
        (line) => (JSON.parse(line) as IpcRequest).method === "sessions.list",
      ),
    ).toBeTrue();
    expect(setup.captureCharFrame()).toContain("session_recent");
    await act(async () => setup.renderer.destroy());
  });

  test("switches naturally to the session timeline after the first message", async () => {
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} />,
      { width: 120, height: 30 },
    );
    await act(async () => setup.renderOnce());

    await act(async () => {
      await setup.mockInput.typeText("解释当前架构");
      setup.mockInput.pressEnter();
    });
    await setup.flush();
    const frame = setup.captureCharFrame();

    expect(frame).toContain("Rivet · ASK");
    expect(frame).toContain("YOU");
    expect(frame).toContain("解释当前架构");
    expect(frame).not.toContain("● Tip");
    await act(async () => setup.renderer.destroy());
  });

  test("clears the composer after submitting a worker command", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} client={client} />,
      { width: 120, height: 30 },
    );
    await act(async () => setup.renderOnce());

    await act(async () => {
      await setup.mockInput.typeText("/read samples/image.png --ocr");
      setup.mockInput.pressEnter();
      await setup.flush();
    });

    expect(setup.renderer.currentFocusedEditor?.plainText).toBe("");
    const request = transport.writes
      .map((line) => JSON.parse(line) as IpcRequest)
      .find((item) => item.method === "command.read");
    expect(request?.params).toEqual({
      file: "samples/image.png",
      ocr: true,
    });

    await act(async () => {
      await setup.mockInput.typeText("/read samples/report.pdf --ocr");
      setup.mockInput.pressEnter();
      await setup.flush();
    });
    const readRequests = transport.writes
      .map((line) => JSON.parse(line) as IpcRequest)
      .filter((item) => item.method === "command.read");
    expect(readRequests).toHaveLength(2);
    expect(readRequests[1]?.params).toEqual({
      file: "samples/report.pdf",
      ocr: true,
    });
    expect(setup.renderer.currentFocusedEditor?.plainText).toBe("");
    expect(
      setup.captureCharFrame().split("\n").slice(-4).join("\n"),
    ).not.toContain("/read samples/report.pdf");
    await act(async () => setup.renderer.destroy());
  });

  test("keeps a rejected command in the composer for correction", async () => {
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} />,
      { width: 120, height: 30 },
    );
    await act(async () => setup.renderOnce());

    await act(async () => {
      await setup.mockInput.typeText("/read sample.png --frames 21");
      setup.mockInput.pressEnter();
      await setup.flush();
    });

    expect(setup.renderer.currentFocusedEditor?.plainText).toBe(
      "/read sample.png --frames 21",
    );
    expect(setup.captureCharFrame()).toContain("--frames");
    await act(async () => setup.renderer.destroy());
  });

  test("loads a verified historical transaction into the apply picker", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp initialState={readyState()} noColor={true} client={client} />,
      { width: 120, height: 30 },
    );
    await act(async () => setup.renderOnce());

    await act(async () => {
      await setup.mockInput.typeText("/apply ");
    });
    await act(async () => Bun.sleep(30));
    await setup.flush();

    expect(
      transport.writes
        .map((line) => JSON.parse(line) as IpcRequest)
        .some((request) => request.method === "transactions.list"),
    ).toBeTrue();
    expect(setup.captureCharFrame()).toContain("tx_recent");
    await act(async () => setup.renderer.destroy());
  });

  test("keeps details hidden until a panel is requested", async () => {
    const state = readyState({
      sessionId: "session_one",
      sessions: ["session_one"],
    });
    const setup = await testRender(
      <RivetApp initialState={state} noColor={true} />,
      { width: 120, height: 30 },
    );
    await act(async () => setup.renderOnce());
    expect(setup.captureCharFrame()).not.toContain("当前还没有上下文来源");

    await act(async () => setup.mockInput.pressKey("x", { ctrl: true }));
    await act(async () => setup.mockInput.pressKey("c"));
    await setup.flush();
    expect(setup.captureCharFrame()).toContain("当前还没有上下文来源");

    await act(async () => setup.mockInput.pressEscape());
    await act(async () => setup.renderer.destroy());
  });

  test("controls the selected module from the Modules panel through IPC", async () => {
    const transport = new CaptureTransport();
    const client = new WorkerClient(transport, { requireHandshake: false });
    const setup = await testRender(
      <RivetApp
        initialState={readyState({
          sessionId: "session_modules",
          modules: ["reader.pdf"],
          moduleStatuses: [
            {
              moduleId: "reader.pdf",
              manifestDefaultEnabled: false,
              persistedOverride: false,
              configuredEnabled: false,
              effectiveEnabled: false,
              runtimeState: "INACTIVE",
              activation: "on_demand",
              scope: "workspace",
              manualControl: true,
              sleepPolicy: "automatic",
              dependencies: [],
              dependents: [],
              providedCapabilities: ["reader.pdf.extract"],
              leaseCount: 0,
              activeResourceCount: 0,
              lastError: null,
            },
          ],
        })}
        noColor={true}
        client={client}
      />,
      { width: 120, height: 30 },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.mockInput.pressKey("x", { ctrl: true }));
    await act(async () => setup.mockInput.pressKey("m"));
    await setup.flush();

    const panel = setup.captureCharFrame();
    expect(panel).toContain("reader.pdf");
    expect(panel).toContain("E 启用");
    await act(async () => setup.mockInput.pressKey("e"));
    await setup.flush();

    const request = transport.writes
      .map((line) => JSON.parse(line) as IpcRequest)
      .find(
        (item) =>
          item.method === "module.operation" &&
          item.params.operation === "enable",
      );
    expect(request?.params.module_id).toBe("reader.pdf");
    await act(async () => setup.renderer.destroy());
  });

  test("resolves permission only from explicit input and shows the full impact", async () => {
    const decisions: Array<[string, boolean]> = [];
    const setup = await testRender(
      <RivetApp
        initialState={readyState({
          permission: {
            requestId: "request_permission",
            permission: "WRITE",
            reason: "修改事务文件",
            argv: "无",
            cwd: ".",
            paths: "src/app.py",
            network: "禁用",
            timeoutSeconds: 60,
          },
        })}
        noColor={true}
        onPermission={(requestId, approved) =>
          decisions.push([requestId, approved])
        }
      />,
      { width: 120, height: 30 },
    );
    await act(async () => setup.renderOnce());
    expect(setup.captureCharFrame()).toContain("src/app.py");

    await act(async () => setup.mockInput.pressKey("a"));
    expect(decisions).toEqual([["request_permission", true]]);
    await act(async () => setup.renderer.destroy());
  });

  test("treats Escape on a permission prompt as an explicit denial", async () => {
    const decisions: Array<[string, boolean]> = [];
    const setup = await testRender(
      <RivetApp
        initialState={readyState({
          permission: {
            requestId: "request_escape",
            permission: "WRITE",
            reason: "修改事务文件",
            argv: "无",
            cwd: ".",
            paths: "src/app.py",
            network: "禁用",
            timeoutSeconds: 60,
          },
        })}
        noColor={true}
        onPermission={(requestId, approved) =>
          decisions.push([requestId, approved])
        }
      />,
      { width: 80, height: 24 },
    );
    await act(async () => setup.renderOnce());
    await act(async () => setup.mockInput.pressEscape());
    await act(async () => Bun.sleep(80));
    await setup.flush();

    expect(decisions).toEqual([["request_escape", false]]);
    await act(async () => setup.renderer.destroy());
  });

  test("requires an explicit confirmation before a dangerous command", async () => {
    const setup = await testRender(
      <RivetApp
        initialState={readyState({
          transaction: "tx_one",
          verifyStatus: "PASSED",
        })}
        noColor={true}
      />,
      { width: 100, height: 28 },
    );
    await act(async () => setup.renderOnce());
    await act(async () => {
      await setup.mockInput.typeText("/apply tx_one");
      setup.mockInput.pressEnter();
    });
    await setup.flush();

    expect(setup.captureCharFrame()).toContain("影响范围：主工作区 · tx_one");
    await act(async () => setup.mockInput.pressKey("n"));
    await setup.flush();
    expect(setup.captureCharFrame()).not.toContain("影响范围：主工作区");
    await act(async () => setup.renderer.destroy());
  });

  test("recovers with Ctrl+Shift+R and exits only after a second Ctrl+C", async () => {
    let recoverCount = 0;
    let exitCount = 0;
    const setup = await testRender(
      <RivetApp
        initialState={readyState({ connection: "crashed", error: "Worker 退出" })}
        noColor={true}
        onRecover={() => recoverCount++}
        onExit={() => exitCount++}
      />,
      { width: 80, height: 24, exitOnCtrlC: false, kittyKeyboard: true },
    );
    await act(async () => setup.renderOnce());
    expect(setup.captureCharFrame()).toContain("Worker 退出");

    await act(async () =>
      setup.mockInput.pressKey("r", { ctrl: true, shift: true }),
    );
    expect(recoverCount).toBe(1);

    await act(async () => setup.mockInput.pressKey("c", { ctrl: true }));
    expect(exitCount).toBe(0);
    await act(async () => setup.mockInput.pressKey("c", { ctrl: true }));
    expect(exitCount).toBe(1);
    await act(async () => setup.renderer.destroy());
  });
});
