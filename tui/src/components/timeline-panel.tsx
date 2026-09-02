import { SyntaxStyle } from "@opentui/core";
import { useEffect, useMemo, useState } from "react";

import type { RivetState, TimelineItem } from "../state/reducer.ts";
import type { RivetTheme } from "./theme.ts";

export function TimelinePanel({
  state,
  theme,
  running,
}: {
  state: RivetState;
  theme: RivetTheme;
  running: boolean;
}) {
  const visible = state.timeline.filter(showInTimeline).slice(-120);
  const entries = useMemo(() => groupTimeline(visible), [visible]);
  const [expandedGroups, setExpandedGroups] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const syntaxStyle = useMemo(
    () =>
      SyntaxStyle.fromStyles({
        default: { fg: theme.textPrimary },
        heading: { fg: theme.accent, bold: true },
        code: { fg: theme.accent },
        link: { fg: theme.accent, underline: true },
      }),
    [theme.accent, theme.textPrimary],
  );
  useEffect(() => () => syntaxStyle.destroy(), [syntaxStyle]);

  return (
    <box
      backgroundColor={theme.background}
      flexGrow={1}
      flexDirection="column"
      paddingX={2}
      paddingY={1}
    >
      <scrollbox flexGrow={1} focused={false} stickyScroll={true} stickyStart="bottom">
        {visible.length === 0 ? null : (
          entries.map((entry) => {
            if (entry.kind === "group") {
              const expanded = expandedGroups.has(entry.id);
              const failed = entry.items.some(
                (item) => item.status === "failed" || item.status === "blocked",
              );
              return (
                <box key={entry.id} flexDirection="column" marginBottom={1}>
                  <box
                    height={1}
                    flexDirection="row"
                    onMouseDown={() =>
                      setExpandedGroups((current) => toggleGroup(current, entry.id))
                    }
                  >
                    <text
                      fg={failed ? theme.warning : theme.accent}
                      content={`${expanded ? "⌄" : "›"} ${entry.title}${expanded ? ` · ${entry.items.length} 条审计` : ""}${failed ? " · !" : ""}`}
                    />
                  </box>
                  {expanded
                    ? entry.items.map((item) => (
                        <StatusRow key={item.eventId} item={item} theme={theme} />
                      ))
                    : null}
                </box>
              );
            }
            const item = entry.item;
            if (item.kind === "user") {
              return (
                <box key={item.eventId} flexDirection="column" marginBottom={1}>
                  <text fg={theme.textMuted} content="›" />
                  <text fg={theme.textPrimary} content={item.title} />
                </box>
              );
            }
            if (item.kind === "assistant") {
              return (
                <box key={item.eventId} flexDirection="column" marginBottom={1}>
                  <text fg={theme.accent} content="◆" />
                  <markdown
                    content={item.detail || item.title}
                    syntaxStyle={syntaxStyle}
                    conceal={true}
                    streaming={running && item === visible.at(-1)}
                  />
                </box>
              );
            }
            return <StatusRow key={item.eventId} item={item} theme={theme} />;
          })
        )}
      </scrollbox>
      {state.error === null ? null : (
        <text
          fg={theme.danger}
          content={`${state.error}${state.connection === "crashed" ? " · Ctrl+Shift+R 重连" : ""}`}
        />
      )}
    </box>
  );
}

const QUIET_TIMELINE_EVENTS = new Set([
  "budget.updated",
  "command.completed",
  "evidence.log",
  "permission.resolved",
  "run.started",
  "run.completed",
  "worker.ready",
  "workspace.tree_updated",
]);

function showInTimeline(item: TimelineItem): boolean {
  if (
    item.operationId?.startsWith("model:") &&
    (item.eventType === "module.released" || item.eventType === "module.slept")
  )
    return false;
  return (
    !item.eventType.endsWith(".snapshot") &&
    !QUIET_TIMELINE_EVENTS.has(item.eventType)
  );
}

type TimelineEntry =
  | { kind: "item"; item: TimelineItem }
  | { kind: "group"; id: string; title: string; items: TimelineItem[] };

function groupTimeline(items: TimelineItem[]): TimelineEntry[] {
  const entries: TimelineEntry[] = [];
  let pending: TimelineItem[] = [];
  const flush = () => {
    const first = pending[0];
    if (first !== undefined) {
      const demand = pending.find((item) => item.eventType === "demand.created");
      const operation = [...pending]
        .reverse()
        .find(
          (item) =>
            item.eventType === "tool.completed" ||
            item.eventType === "operation.executed",
        );
      entries.push({
        kind: "group",
        id: `progress-${first.eventId}`,
        title: demand?.title ?? operation?.title ?? `${pending.length} 项运行状态`,
        items: pending,
      });
    }
    pending = [];
  };
  for (const item of items) {
    if (item.kind === "user" || item.kind === "assistant") {
      flush();
      entries.push({ kind: "item", item });
    } else {
      const previous = pending.at(-1);
      const pendingDemandIds = new Set(
        pending.flatMap((pendingItem) =>
          pendingItem.demandId === null ? [] : [pendingItem.demandId],
        ),
      );
      const causallyRelated =
        item.demandId === previous?.demandId ||
        (item.parentDemandId !== null && pendingDemandIds.has(item.parentDemandId)) ||
        (item.demandId !== null &&
          pending.some(
            (pendingItem) => pendingItem.parentDemandId === item.demandId,
          ));
      if (
        previous !== undefined &&
        !causallyRelated &&
        (previous.demandId !== null || item.demandId !== null)
      )
        flush();
      pending.push(item);
    }
  }
  flush();
  return entries;
}

function toggleGroup(current: ReadonlySet<string>, id: string): ReadonlySet<string> {
  const next = new Set(current);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

function StatusRow({
  item,
  theme,
}: {
  item: TimelineItem;
  theme: RivetTheme;
}) {
  return (
    <box flexDirection="row" minHeight={1}>
      <text
        width={2}
        fg={statusColor(item.status, theme)}
        content={statusIcon(item.status)}
      />
      <text fg={theme.textSecondary} content={item.title} />
      {item.detail ? (
        <text fg={theme.textMuted} content={`  ${item.detail}`} />
      ) : null}
    </box>
  );
}

function statusIcon(status: RivetState["timeline"][number]["status"]): string {
  if (status === "running") return "◌";
  if (status === "success") return "✓";
  if (status === "failed") return "×";
  if (status === "blocked") return "!";
  return "−";
}

function statusColor(
  status: RivetState["timeline"][number]["status"],
  theme: RivetTheme,
): string {
  if (status === "running") return theme.accent;
  if (status === "success") return theme.success;
  if (status === "failed") return theme.danger;
  if (status === "blocked") return theme.warning;
  return theme.textMuted;
}
