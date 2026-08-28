import { useEffect, useRef, type Dispatch } from "react";

import type { IpcEvent } from "../contracts/ipc.ts";
import type { WorkerClient } from "../ipc/client.ts";
import type { RivetAction } from "../state/reducer.ts";

export function useWorkerConnection(
  client: WorkerClient | undefined,
  dispatch: Dispatch<RivetAction>,
): void {
  const pendingEvents = useRef<IpcEvent[]>([]);
  const eventTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (client === undefined) return;
    const unsubscribeEvent = client.onEvent((event) => {
      pendingEvents.current.push(event);
      if (eventTimer.current !== null) return;
      eventTimer.current = setTimeout(() => {
        const events = pendingEvents.current.splice(0);
        eventTimer.current = null;
        for (const pending of events) dispatch({ kind: "trace", event: pending });
      }, 16);
    });
    const unsubscribeStatus = client.onStatus((status) =>
      dispatch({ kind: "worker-status", ...status }),
    );
    const unsubscribeDiagnostic = client.onDiagnostic(() => {});
    void client.start().catch((error: unknown) => {
      dispatch({
        kind: "worker-status",
        state: "crashed",
        summary: error instanceof Error ? error.message : "Worker 握手失败",
      });
    });
    return () => {
      if (eventTimer.current !== null) clearTimeout(eventTimer.current);
      eventTimer.current = null;
      pendingEvents.current = [];
      unsubscribeEvent();
      unsubscribeStatus();
      unsubscribeDiagnostic();
      client.close();
    };
  }, [client, dispatch]);
}
