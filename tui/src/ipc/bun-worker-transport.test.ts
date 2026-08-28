import { describe, expect, test } from "bun:test";

import { BunWorkerTransport } from "./bun-worker-transport.ts";

describe("Bun worker transport lifecycle", () => {
  test("closes a worker that exits after stdin EOF", async () => {
    const python = Bun.which("python3");
    if (python === null) throw new Error("python3 is required for this test");
    const transport = new BunWorkerTransport({
      command: [
        python,
        "-c",
        "import sys; sys.stdin.buffer.read()",
      ],
      cwd: process.cwd(),
      environment: { PATH: process.env.PATH ?? "/usr/bin:/bin" },
    });

    transport.close();
    await transport.waitForShutdown();

    expect(await transport.waitForExit()).toBe(0);
  });

  test("escalates to kill for a worker that ignores EOF and TERM", async () => {
    const python = Bun.which("python3");
    if (python === null) throw new Error("python3 is required for this test");
    const transport = new BunWorkerTransport({
      command: [
        python,
        "-c",
        [
          "import signal, time",
          "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
          "time.sleep(30)",
        ].join("; "),
      ],
      cwd: process.cwd(),
      environment: { PATH: process.env.PATH ?? "/usr/bin:/bin" },
    });
    await Bun.sleep(50);

    transport.close();
    await transport.waitForShutdown();

    expect(await transport.waitForExit()).not.toBe(0);
  });
});
