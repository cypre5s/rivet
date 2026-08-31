import type { JsonValue } from "../contracts/ipc.ts";

export type ApiKeyAction = "keep" | "replace" | "clear";

export interface PublicRuntimeConfiguration {
  baseUrl: string;
  credentialConfigured: boolean;
  maxCostUsd: string | null;
  maxRounds: number;
  maxTotalTokens: number;
  model: string;
  models: string[];
  safeMode: boolean;
}

export interface ConfigurationDraft {
  apiKey: string;
  apiKeyAction: ApiKeyAction;
  baseUrl: string;
  maxCostUsd: string;
  maxRounds: string;
  maxTotalTokens: string;
  model: string;
  modelsText: string;
  safeMode: boolean;
}

const DEFAULT_CONFIGURATION: PublicRuntimeConfiguration = {
  baseUrl: "https://api.deepseek.com",
  credentialConfigured: false,
  maxCostUsd: null,
  maxRounds: 24,
  maxTotalTokens: 128_000,
  model: "deepseek-v4-pro",
  models: ["deepseek-v4-pro", "deepseek-v4-flash"],
  safeMode: false,
};

export function createConfigurationDraft(
  configuration: PublicRuntimeConfiguration = DEFAULT_CONFIGURATION,
): ConfigurationDraft {
  return {
    apiKey: "",
    apiKeyAction: "keep",
    baseUrl: configuration.baseUrl,
    maxCostUsd: configuration.maxCostUsd ?? "",
    maxRounds: String(configuration.maxRounds),
    maxTotalTokens: String(configuration.maxTotalTokens),
    model: configuration.model,
    modelsText: configuration.models.join("\n"),
    safeMode: configuration.safeMode,
  };
}

export function parseModelList(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((model) => model.trim())
    .filter(Boolean);
}

export function validateConfigurationDraft(
  draft: ConfigurationDraft,
): string[] {
  const errors: string[] = [];
  const models = parseModelList(draft.modelsText);
  if (models.length === 0 || models.length > 32) {
    errors.push("模型列表需要包含 1 到 32 个模型");
  }
  if (new Set(models).size !== models.length) {
    errors.push("模型名称不能重复");
  }
  if (models.some((model) => !validModelName(model))) {
    errors.push("模型名称仅支持小写字母、数字、点、下划线和连字符");
  }
  if (!models.includes(draft.model.trim())) {
    errors.push("默认模型必须在模型列表中");
  }
  try {
    const endpoint = new URL(draft.baseUrl);
    if (
      !["http:", "https:"].includes(endpoint.protocol) ||
      endpoint.username ||
      endpoint.password ||
      endpoint.search ||
      endpoint.hash
    ) {
      errors.push("API 地址必须是不携带凭据、查询或片段的 HTTP(S) 地址");
    }
  } catch {
    errors.push("API 地址必须是有效的 HTTP(S) 绝对地址");
  }
  if (!integerInRange(draft.maxRounds, 1, 128)) {
    errors.push("最大轮次必须是 1 到 128 的整数");
  }
  if (!integerInRange(draft.maxTotalTokens, 1, 10_000_000)) {
    errors.push("Token 上限必须是 1 到 10000000 的整数");
  }
  if (draft.maxCostUsd.trim()) {
    const cost = Number(draft.maxCostUsd);
    if (!Number.isFinite(cost) || cost < 0) {
      errors.push("费用上限必须是非负数或留空");
    }
  }
  if (
    draft.apiKeyAction === "replace" &&
    (!draft.apiKey ||
      draft.apiKey.length > 4_096 ||
      /[\s\u0000-\u001f\u007f]/u.test(draft.apiKey))
  ) {
    errors.push("API Key 不能为空、不能含空白，且最多 4096 字符");
  }
  return errors;
}

export function configurationPayload(
  draft: ConfigurationDraft,
): Record<string, JsonValue> {
  const payload: Record<string, JsonValue> = {
    api_key_action: draft.apiKeyAction,
    base_url: draft.baseUrl.trim(),
    max_cost_usd: draft.maxCostUsd.trim() || null,
    max_rounds: Number(draft.maxRounds),
    max_total_tokens: Number(draft.maxTotalTokens),
    model: draft.model.trim(),
    models: parseModelList(draft.modelsText),
    safe_mode: draft.safeMode,
  };
  if (draft.apiKeyAction === "replace") payload.api_key = draft.apiKey;
  return payload;
}

export function maskApiKey(value: string): string {
  return "•".repeat(Array.from(value).length);
}

function validModelName(value: string): boolean {
  return /^[a-z0-9][a-z0-9._-]{0,127}$/u.test(value);
}

function integerInRange(
  value: string,
  minimum: number,
  maximum: number,
): boolean {
  if (!/^\d+$/u.test(value.trim())) return false;
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= minimum && number <= maximum;
}
