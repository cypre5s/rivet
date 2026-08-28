import {
  useEffect,
  useRef,
  type Dispatch,
  type SetStateAction,
} from "react";

import type { ThemeName } from "../components/theme.ts";
import type { PanelName, WorkMode } from "../ui/command-registry.ts";
import {
  loadTuiPreferences,
  saveTuiPreferences,
} from "../ui/preferences.ts";

export function useTuiPreferences(
  values: { mode: WorkMode; theme: ThemeName; panel: PanelName | null },
  setters: {
    setMode: Dispatch<SetStateAction<WorkMode>>;
    setTheme: Dispatch<SetStateAction<ThemeName>>;
    setPanel: Dispatch<SetStateAction<PanelName | null>>;
  },
  enabled: boolean,
): void {
  const loaded = useRef(false);

  useEffect(() => {
    if (!enabled) return;
    let active = true;
    void loadTuiPreferences().then((preferences) => {
      if (!active) return;
      setters.setMode(preferences.mode);
      setters.setTheme(preferences.theme);
      setters.setPanel(preferences.panel);
      loaded.current = true;
    });
    return () => {
      active = false;
    };
  }, [enabled, setters.setMode, setters.setPanel, setters.setTheme]);

  useEffect(() => {
    if (!enabled || !loaded.current) return;
    const timer = setTimeout(() => {
      void saveTuiPreferences(values).catch(() => {});
    }, 150);
    return () => clearTimeout(timer);
  }, [enabled, values.mode, values.panel, values.theme]);
}
