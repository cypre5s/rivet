import { describe, expect, test } from "bun:test";
import { testRender } from "@opentui/react/test-utils";
import { act } from "react";

import { RivetApp } from "./app.tsx";
import { initialRivetState } from "./state/reducer.ts";

describe("Rivet OpenTUI workbench", () => {
  test("renders the three-column workbench and permission details", async () => {
    const state = {
      ...initialRivetState(),
      connection: "ready" as const,
      repository: "/repo",
      model: "deepseek",
      permission: {
        requestId: "request_permission",
        permission: "EXECUTE",
        reason: "运行测试",
        argv: "pytest -q",
        cwd: ".",
        paths: "事务工作树",
        network: "禁用",
        timeoutSeconds: 60,
      },
    };
    const setup = await testRender(
      <RivetApp initialState={state} noColor={true} />,
      { width: 200, height: 50 },
    );

    await setup.renderOnce();
    const frame = setup.captureCharFrame();

    expect(frame).toContain("Rivet");
    expect(frame).toContain("Repository / Context");
    expect(frame).toContain("Chat / Trace");
    expect(frame).toContain("Inspector");
    expect(frame).toContain("EXECUTE");
    expect(frame).toContain("pytest -q");
    await act(async () => setup.renderer.destroy());
  });

  test("resizes to a single-column timeline and opens command palette", async () => {
    const setup = await testRender(
      <RivetApp initialState={initialRivetState()} noColor={true} />,
      { width: 80, height: 24 },
    );
    await setup.renderOnce();
    await setup.flush();

    await act(async () => {
      setup.mockInput.pressKey("p", { ctrl: true });
    });
    const frame = await setup.waitForFrame((candidate) =>
      candidate.includes("命令面板"),
    );

    expect(frame).toContain("Chat / Trace");
    expect(frame).not.toContain("Repository / Context");
    expect(frame).toContain("命令面板");
    await act(async () => setup.renderer.destroy());
  });

  test("shows and invokes the worker recovery entry", async () => {
    let recoverCount = 0;
    const setup = await testRender(
      <RivetApp
        initialState={{
          ...initialRivetState(),
          connection: "crashed",
          error: "Worker 退出",
        }}
        noColor={true}
        onRecover={() => recoverCount++}
      />,
      { width: 120, height: 30 },
    );
    await setup.renderOnce();
    expect(setup.captureCharFrame()).toContain("Ctrl+R 恢复 Worker");

    await act(async () => setup.mockInput.pressKey("r", { ctrl: true }));

    expect(recoverCount).toBe(1);
    await act(async () => setup.renderer.destroy());
  });

  test("resolves a permission prompt only from explicit keyboard input", async () => {
    const decisions: Array<[string, boolean]> = [];
    const setup = await testRender(
      <RivetApp
        initialState={{
          ...initialRivetState(),
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
        }}
        noColor={true}
        onPermission={(requestId, approved) =>
          decisions.push([requestId, approved])
        }
      />,
      { width: 120, height: 30 },
    );
    await setup.renderOnce();

    await act(async () => setup.mockInput.pressKey("a"));

    expect(decisions).toEqual([["request_permission", true]]);
    await act(async () => setup.renderer.destroy());
  });

  test("exits safely only after the second prompt Ctrl+C", async () => {
    let exitCount = 0;
    const setup = await testRender(
      <RivetApp
        initialState={initialRivetState()}
        noColor={true}
        onExit={() => exitCount++}
      />,
      { width: 80, height: 24, exitOnCtrlC: false },
    );
    await setup.renderOnce();

    await act(async () => setup.mockInput.pressKey("c", { ctrl: true }));
    expect(exitCount).toBe(0);
    await Bun.sleep(20);
    await act(async () => setup.mockInput.pressKey("c", { ctrl: true }));
    expect(exitCount).toBe(1);

    await act(async () => setup.renderer.destroy());
  });
});
