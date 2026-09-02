const WORKER_ENVIRONMENT_NAMES = [
  "DEEPSEEK_API_KEY",
  "LANG",
  "LC_ALL",
  "PATH",
  "RIVET_BASE_URL",
  "RIVET_BWRAP_PATH",
  "RIVET_MAX_COST_USD",
  "RIVET_MAX_ROUNDS",
  "RIVET_MAX_TOTAL_TOKENS",
  "RIVET_MODEL",
  "RIVET_MODELS",
  "TERM",
  "TZ",
  "XDG_CACHE_HOME",
  "XDG_CONFIG_HOME",
  "XDG_DATA_HOME",
  "XDG_STATE_HOME",
] as const;

export function buildWorkerEnvironment(
  source: Readonly<Record<string, string | undefined>> = process.env,
): Record<string, string> {
  const environment: Record<string, string> = {};
  for (const name of WORKER_ENVIRONMENT_NAMES) {
    const value = source[name];
    if (value !== undefined) environment[name] = value;
  }
  return environment;
}
