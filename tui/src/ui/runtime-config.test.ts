import { describe, expect, test } from "bun:test";

import {
  configurationPayload,
  createConfigurationDraft,
  maskApiKey,
  parseModelList,
  validateConfigurationDraft,
} from "./runtime-config.ts";

describe("TUI runtime configuration", () => {
  test("initializes from the worker's public configuration", () => {
    const draft = createConfigurationDraft({
      baseUrl: "https://gateway.example.test/v1",
      credentialConfigured: true,
      maxCostUsd: "2.50",
      maxRounds: 18,
      maxTotalTokens: 64_000,
      model: "reasoner-large",
      models: ["chat-fast", "reasoner-large"],
      safeMode: true,
    });

    expect(draft.model).toBe("reasoner-large");
    expect(draft.modelsText).toBe("chat-fast\nreasoner-large");
    expect(draft.apiKey).toBe("");
    expect(draft.apiKeyAction).toBe("keep");
  });

  test("normalizes model lines and rejects duplicates or a missing default", () => {
    expect(parseModelList(" chat-fast\nreasoner-large \n")).toEqual([
      "chat-fast",
      "reasoner-large",
    ]);
    expect(
      validateConfigurationDraft({
        ...createConfigurationDraft(),
        model: "missing",
        modelsText: "chat-fast\nchat-fast",
      }),
    ).toContain("模型名称不能重复");
    expect(
      validateConfigurationDraft({
        ...createConfigurationDraft(),
        model: "missing",
        modelsText: "chat-fast\nreasoner-large",
      }),
    ).toContain("默认模型必须在模型列表中");
    expect(
      validateConfigurationDraft({
        ...createConfigurationDraft(),
        model: "valid-model",
        modelsText: "valid-model\nBad Model",
      }),
    ).toContain("模型名称仅支持小写字母、数字、点、下划线和连字符");
  });

  test("masks the API key and only includes it for an explicit replacement", () => {
    const secret = "fixture-sensitive-session-value";
    expect(maskApiKey(secret)).toBe("•".repeat(secret.length));
    expect(maskApiKey(secret)).not.toContain(secret);

    const payload = configurationPayload({
      ...createConfigurationDraft(),
      apiKey: secret,
      apiKeyAction: "replace",
    });
    expect(payload.api_key_action).toBe("replace");
    expect(payload.api_key).toBe(secret);

    const kept = configurationPayload(createConfigurationDraft());
    expect(kept.api_key_action).toBe("keep");
    expect(kept.api_key).toBeUndefined();
  });

  test("fails closed on unsafe endpoints, budgets and credential input", () => {
    const errors = validateConfigurationDraft({
      ...createConfigurationDraft(),
      apiKey: "secret with spaces",
      apiKeyAction: "replace",
      baseUrl: "https://user:password@example.test/v1?debug=true",
      maxCostUsd: "-1",
      maxRounds: "0",
      maxTotalTokens: "NaN",
    });

    expect(errors).toContain(
      "API 地址必须是不携带凭据、查询或片段的 HTTP(S) 地址",
    );
    expect(errors).toContain("最大轮次必须是 1 到 128 的整数");
    expect(errors).toContain("Token 上限必须是 1 到 10000000 的整数");
    expect(errors).toContain("费用上限必须是非负数或留空");
    expect(errors).toContain("API Key 不能为空、不能含空白，且最多 4096 字符");
  });
});
