import { useEffect } from "react";

import type { WorkerClient } from "../ipc/client.ts";

export function useEvidenceDetail(
  client: WorkerClient | undefined,
  requested: boolean,
  transactionId: string | null,
  onError: (summary: string) => void,
): void {
  useEffect(() => {
    if (!requested || client === undefined || transactionId === null) return;
    let active = true;
    void client
      .request("evidence.get", { transaction_id: transactionId })
      .catch((error: unknown) => {
        if (active) {
          onError(error instanceof Error ? error.message : "Evidence 加载失败");
        }
      });
    return () => {
      active = false;
    };
  }, [client, onError, requested, transactionId]);
}
