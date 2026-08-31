import { describe, expect, test } from "bun:test";

import { buildWorkerEnvironment } from "./worker-environment.ts";

describe("TUI worker environment", () => {
  test("forwards public runtime configuration and the one approved credential", () => {
    const environment = buildWorkerEnvironment({
      DEEPSEEK_API_KEY: "session-secret",
      PATH: "/usr/bin",
      RIVET_BASE_URL: "https://gateway.example.test/v1",
      RIVET_MAX_ROUNDS: "12",
      RIVET_MAX_TOTAL_TOKENS: "64000",
      RIVET_MAX_COST_USD: "2.50",
      RIVET_MODEL: "team-reasoner",
      RIVET_MODELS: "team-chat,team-reasoner",
      RIVET_SAFE_MODE: "true",
      UNRELATED_SECRET: "must-not-pass",
    });

    expect(environment.RIVET_MODEL).toBe("team-reasoner");
    expect(environment.RIVET_MODELS).toBe("team-chat,team-reasoner");
    expect(environment.RIVET_BASE_URL).toBe(
      "https://gateway.example.test/v1",
    );
    expect(environment.DEEPSEEK_API_KEY).toBe("session-secret");
    expect(environment.UNRELATED_SECRET).toBeUndefined();
  });
});
