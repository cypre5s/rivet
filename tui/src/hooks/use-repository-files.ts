import { useEffect, useState } from "react";

import type { WorkerClient } from "../ipc/client.ts";
import { resultPaths } from "../ui/app-model.ts";

export function useRepositoryFiles(
  client: WorkerClient | undefined,
  initialFiles: string[],
  query: string | null,
  onError: (summary: string) => void,
): { files: string[]; loading: boolean } {
  const [files, setFiles] = useState(initialFiles);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (query === null || client === undefined) {
      setLoading(false);
      return;
    }
    let active = true;
    const timer = setTimeout(() => {
      setLoading(true);
      void client
        .request("workspace.files", { limit: 200, query })
        .then((result) => {
          if (active) setFiles(resultPaths(result));
        })
        .catch((error: unknown) => {
          if (active) {
            onError(
              error instanceof Error ? error.message : "文件清单加载失败",
            );
          }
        })
        .finally(() => {
          if (active) setLoading(false);
        });
    }, 120);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [client, onError, query]);

  return { files, loading };
}
