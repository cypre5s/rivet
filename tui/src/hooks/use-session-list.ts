import { useEffect } from "react";

import type { WorkerClient } from "../ipc/client.ts";

export function useSessionList(
  client: WorkerClient | undefined,
  requested: boolean,
  onError: (summary: string) => void,
): void {
  useEffect(() => {
    if (!requested || client === undefined) return;
    let active = true;
    void client.request("sessions.list", { limit: 50 }).catch((error: unknown) => {
      if (active) {
        onError(error instanceof Error ? error.message : "近期会话加载失败");
      }
    });
    return () => {
      active = false;
    };
  }, [client, onError, requested]);
}
