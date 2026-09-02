import type {
  EvidenceLog,
  EvidenceSummary,
  TransactionSummary,
  VerificationSummary,
} from "../state/reducer.ts";

export type EvidenceLineTone =
  | "primary"
  | "muted"
  | "accent"
  | "success"
  | "warning"
  | "danger";

export interface EvidenceLine {
  key: string;
  text: string;
  tone: EvidenceLineTone;
}

export interface EvidencePanelModel {
  lines: EvidenceLine[];
  transactionId: string;
}

export function evidencePanelModel({
  evidence,
  evidenceLog,
  transactions,
  selectedIndex,
  expanded,
}: {
  evidence: EvidenceSummary;
  evidenceLog: EvidenceLog | null;
  transactions: readonly TransactionSummary[];
  selectedIndex: number;
  expanded: boolean;
}): EvidencePanelModel {
  const safeIndex = clampIndex(selectedIndex, transactions.length);
  const selectedTransaction = transactions[safeIndex];
  const transactionId =
    evidence.transactionId || selectedTransaction?.transactionId || "无";
  const passedChecks = evidence.verificationResults.filter(
    (result) => result.status.toUpperCase() === "PASSED",
  ).length;
  const totalChecks = evidence.verificationResults.length;
  const failedChecks = evidence.verificationResults.filter(
    (result) => result.status.toUpperCase() !== "PASSED",
  );
  const apply = applyPresentation(evidence);
  const lines: EvidenceLine[] = [];
  if (transactions.length > 1) {
    lines.push(
      line(
        "transaction",
        `${safeIndex + 1}/${transactions.length} · ${compactIdentifier(transactionId)}`,
        "muted",
      ),
    );
  }
  lines.push(
    line(
      "verdict",
      `${verdictIcon(evidence.verdictStatus)} ${verdictLabel(evidence.verdictStatus)}`,
      verdictTone(evidence.verdictStatus),
    ),
    line(
      "checks",
      totalChecks === 0
        ? "尚无检查"
        : evidenceCountLine(
            passedChecks,
            totalChecks,
            evidence.changedFiles.length,
          ),
      failedChecks.length === 0 ? "primary" : "warning",
    ),
    line(
      "apply",
      apply.text,
      apply.tone,
    ),
  );

  for (const [index, result] of failedChecks.slice(0, 3).entries()) {
    lines.push(
      line(
        `failure-${index}`,
        `× ${result.kind} · ${statusLabel(result.status)}`,
        result.status.toUpperCase() === "FAILED" ? "danger" : "warning",
      ),
    );
  }
  if (failedChecks.length > 3) {
    lines.push(
      line("failure-more", `+${failedChecks.length - 3} 个异常`, "warning"),
    );
  }
  if (showNextAction(evidence) && evidence.nextAction.trim()) {
    lines.push(
      line("next", `→ ${ellipsize(evidence.nextAction.trim(), 72)}`, "accent"),
    );
  }
  const hints = [
    transactions.length > 1 ? "↑↓" : "",
    `D ${expanded ? "收起" : "详情"}`,
    totalChecks > 0 ? "L 日志" : "",
  ].filter(Boolean);
  lines.push(line("hint", hints.join(" · "), "muted"));

  if (expanded) appendEvidenceDetails(lines, evidence);
  if (
    expanded &&
    evidenceLog?.transactionId === transactionId &&
    evidenceLog.evidenceId === evidence.evidenceId
  ) {
    appendEvidenceLog(lines, evidenceLog);
  }
  return { lines, transactionId };
}

export function compactIdentifier(value: string, limit = 24): string {
  const characters = Array.from(value);
  if (characters.length <= limit) return value;
  const prefixLength = Math.max(8, limit - 9);
  return `${characters.slice(0, prefixLength).join("")}…${characters.slice(-8).join("")}`;
}

function appendEvidenceDetails(
  lines: EvidenceLine[],
  evidence: EvidenceSummary,
): void {
  lines.push(
    line(
      "transaction-id",
      `事务 ${compactIdentifier(evidence.transactionId || "无", 18)}`,
      "primary",
    ),
    line(
      "evidence-id",
      `证据 ${compactIdentifier(evidence.evidenceId ?? "无", 18)}`,
      "primary",
    ),
    line(
      "patch-id",
      `补丁 ${compactIdentifier(evidence.patchId ?? "无", 18)}`,
      "primary",
    ),
    line(
      "base-commit",
      `基线提交 ${compactIdentifier(evidence.baseCommit, 18)}`,
      "muted",
    ),
    line(
      "acceptance-hash",
      `验收哈希 ${compactHash(evidence.acceptanceSha256)}`,
      "muted",
    ),
    line(
      "patch-hash",
      `补丁哈希 ${compactHash(evidence.patchSha256)}`,
      "muted",
    ),
    line(
      "manifest-hash",
      `清单哈希 ${compactHash(evidence.manifestSha256)}`,
      "muted",
    ),
  );
  if (evidence.changedFiles.length > 0) {
    lines.push(line("files-heading", "变更", "accent"));
    for (const [index, path] of evidence.changedFiles.entries()) {
      lines.push(line(`changed-file-${index}`, `  ${path}`, "primary"));
    }
  }
  if (evidence.verificationResults.length > 0) {
    lines.push(line("checks-heading", "检查", "accent"));
    for (const [index, result] of evidence.verificationResults.entries()) {
      appendVerification(lines, result, index);
    }
  }
  if (evidence.files.length > 0) {
    lines.push(line("artifacts-heading", "文件", "accent"));
    for (const [index, file] of evidence.files.entries()) {
      lines.push(
        line(
          `artifact-${index}`,
          `${file.path} · ${formatBytes(file.sizeBytes)} · ${compactHash(file.sha256)}`,
          "muted",
        ),
      );
    }
  }
}

function appendVerification(
  lines: EvidenceLine[],
  result: VerificationSummary,
  index: number,
): void {
  const passed = result.status.toUpperCase() === "PASSED";
  lines.push(
    line(
      `check-${index}`,
      `${passed ? "✓" : "×"} ${result.kind} · ${statusLabel(result.status)} · ${formatDuration(result.durationMs)}`,
      passed ? "success" : result.status.toUpperCase() === "FAILED" ? "danger" : "warning",
    ),
  );
  if (result.argv.length > 0) {
    lines.push(
      line(
        `check-command-${index}`,
        `  ${ellipsize(result.argv.join(" "), 96)}`,
        "muted",
      ),
    );
  }
  if (result.logPath !== null) {
    lines.push(
      line(
        `check-log-${index}`,
        `  ${result.logPath}${result.logSha256 === null ? "" : ` · ${compactHash(result.logSha256)}`}`,
        "muted",
      ),
    );
  }
}

function appendEvidenceLog(lines: EvidenceLine[], evidenceLog: EvidenceLog): void {
  lines.push(
    line(
      "log-heading",
      `日志 · ${evidenceLog.stepId} · ${statusLabel(evidenceLog.status)}`,
      "accent",
    ),
    line(
      "log-path",
      `${evidenceLog.logPath} · ${compactHash(evidenceLog.logSha256)}`,
      "muted",
    ),
    line("log-content", evidenceLog.content || "（空）", "primary"),
  );
  if (evidenceLog.truncated) {
    lines.push(line("log-truncated", "…已截断", "warning"));
  }
}

function line(key: string, text: string, tone: EvidenceLineTone): EvidenceLine {
  return { key, text, tone };
}

function clampIndex(index: number, count: number): number {
  if (count <= 0) return 0;
  return Math.max(0, Math.min(index, count - 1));
}

function verdictIcon(status: string): string {
  const normalized = status.toUpperCase();
  if (normalized === "PASSED") return "✓";
  if (normalized === "FAILED") return "×";
  if (normalized === "BLOCKED" || normalized === "INCONCLUSIVE") return "!";
  return "—";
}

function verdictLabel(status: string): string {
  return verificationStatusText(status);
}

export function verificationStatusText(status: string): string {
  const labels: Readonly<Record<string, string>> = {
    PASSED: "通过",
    FAILED: "失败",
    BLOCKED: "阻塞",
    INCONCLUSIVE: "不确定",
    NOT_RUN: "未验证",
  };
  return labels[status.toUpperCase()] ?? status;
}

function verdictTone(status: string): EvidenceLineTone {
  const normalized = status.toUpperCase();
  if (normalized === "PASSED") return "success";
  if (normalized === "FAILED") return "danger";
  if (normalized === "BLOCKED" || normalized === "INCONCLUSIVE") {
    return "warning";
  }
  return "muted";
}

function applyPresentation(evidence: EvidenceSummary): {
  text: string;
  tone: EvidenceLineTone;
} {
  if (evidence.state.toUpperCase() === "APPLIED") {
    return { text: "✓ 已应用", tone: "success" };
  }
  if (evidence.applyEligible && evidence.evidenceVerified) {
    return { text: "✓ 可应用", tone: "success" };
  }
  return { text: "— 不可应用", tone: "muted" };
}

function showNextAction(evidence: EvidenceSummary): boolean {
  if (evidence.state.toUpperCase() === "APPLIED") return false;
  return !(
    evidence.verdictStatus.toUpperCase() === "PASSED" &&
    evidence.applyEligible &&
    evidence.evidenceVerified
  );
}

function evidenceCountLine(
  passed: number,
  total: number,
  files: number,
): string {
  const parts = [`${passed}/${total}`];
  if (files > 0) parts.push(`${files} 文件`);
  return parts.join(" · ");
}

function statusLabel(status: string): string {
  const labels: Readonly<Record<string, string>> = {
    PASSED: "通过",
    FAILED: "失败",
    BLOCKED: "阻塞",
    INCONCLUSIVE: "不确定",
    SKIPPED: "跳过",
    NOT_RUN: "未运行",
  };
  return labels[status.toUpperCase()] ?? status;
}

function ellipsize(value: string, limit: number): string {
  const characters = Array.from(value);
  if (characters.length <= limit) return value;
  return `${characters.slice(0, Math.max(1, limit - 1)).join("")}…`;
}

function formatDuration(durationMs: number): string {
  if (durationMs < 1_000) return `${durationMs}ms`;
  return `${(durationMs / 1_000).toFixed(1)}s`;
}

function formatBytes(size: number): string {
  if (size < 1_024) return `${size} B`;
  return `${(size / 1_024).toFixed(1)} KiB`;
}

function compactHash(value: string): string {
  const normalized = value.startsWith("sha256:") ? value.slice(7) : value;
  return compactIdentifier(normalized, 16);
}
