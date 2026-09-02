import { describe, expect, test } from "bun:test";

import type {
  EvidenceSummary,
  TransactionSummary,
  VerificationSummary,
} from "../state/reducer.ts";
import {
  compactIdentifier,
  evidencePanelModel,
} from "./evidence-presentation.ts";

function check(kind: string, status = "PASSED"): VerificationSummary {
  return {
    stepId: `step_${kind}`,
    kind,
    name: kind,
    status,
    required: true,
    argv: ["pytest", "-q", `tests/${kind}.py`],
    durationMs: 1_250,
    exitCode: status === "PASSED" ? 0 : 1,
    logPath: `${kind}.log`,
    logSha256: "f".repeat(64),
    stdoutSummary: "",
    stderrSummary: "",
  };
}

function evidence(results: VerificationSummary[]): EvidenceSummary {
  return {
    transactionId: "tx_1234567890abcdefghijklmnopqrstuvwxyz",
    state: "VERIFIED",
    verdictStatus: results.every((result) => result.status === "PASSED")
      ? "PASSED"
      : "FAILED",
    passed: results.every((result) => result.status === "PASSED"),
    applyEligible: results.every((result) => result.status === "PASSED"),
    evidenceVerified: true,
    evidenceId: "evidence_1234567890abcdefghijklmnopqrstuvwxyz",
    patchId: "patch_1234567890abcdefghijklmnopqrstuvwxyz",
    baseCommit: "e".repeat(40),
    acceptanceSha256: "a".repeat(64),
    patchSha256: "b".repeat(64),
    manifestSha256: "c".repeat(64),
    changedFiles: ["src/app.py"],
    verificationResults: results,
    files: [
      { path: "manifest.json", sha256: "d".repeat(64), sizeBytes: 2_048 },
    ],
    updatedAt: "2026-09-02T00:00:00Z",
    decidedAt: "2026-09-02T00:00:00Z",
    nextAction: "审查修改后显式 Apply",
  };
}

function transaction(): TransactionSummary {
  return {
    transactionId: "tx_1234567890abcdefghijklmnopqrstuvwxyz",
    state: "VERIFIED",
    evidenceId: "evidence_one",
    patchId: "patch_one",
    patchSha256: "b".repeat(64),
    updatedAt: "2026-09-02T00:00:00Z",
    applyEligible: true,
  };
}

describe("concise evidence presentation", () => {
  test("shows only the decision and progress before details are requested", () => {
    const model = evidencePanelModel({
      evidence: evidence([check("BASELINE"), check("RESOURCE")]),
      evidenceLog: null,
      transactions: [transaction()],
      selectedIndex: 0,
      expanded: false,
    });
    const text = model.lines.map((line) => line.text).join("\n");

    expect(text).toContain("✓ 通过");
    expect(text).toContain("2/2 · 1 文件");
    expect(text).toContain("✓ 可应用");
    expect(text).toContain("D 详情");
    expect(text).not.toContain("验收哈希");
    expect(text).not.toContain("pytest -q");
    expect(text).not.toContain("manifest.json");
    expect(text).not.toContain("tx_1234567890");
    expect(text).not.toContain("审查修改后显式 Apply");
    expect(model.lines.length).toBeLessThanOrEqual(4);
  });

  test("reveals hashes, commands and file index only on demand", () => {
    const model = evidencePanelModel({
      evidence: evidence([check("BASELINE"), check("BEHAVIOR")]),
      evidenceLog: null,
      transactions: [transaction()],
      selectedIndex: 0,
      expanded: true,
    });
    const text = model.lines.map((line) => line.text).join("\n");

    expect(text).toContain("验收哈希");
    expect(text).toContain("基线提交");
    expect(text).toContain("事务 tx_123456");
    expect(text).not.toContain("sha256:");
    expect(text).toContain("补丁哈希");
    expect(text).toContain("pytest -q tests/BEHAVIOR.py");
    expect(text).toContain("manifest.json");
    expect(text).toContain("D 收起");
    expect(
      model.lines
        .filter((line) => line.key.startsWith("changed-symbol-"))
        .every((line) => Array.from(line.text).length <= 24),
    ).toBeTrue();
  });

  test("keeps failed checks visible in the compact summary", () => {
    const model = evidencePanelModel({
      evidence: evidence([check("BASELINE"), check("BEHAVIOR", "FAILED")]),
      evidenceLog: null,
      transactions: [transaction()],
      selectedIndex: 0,
      expanded: false,
    });

    expect(model.lines.map((line) => line.text).join("\n")).toContain(
      "× BEHAVIOR · 失败",
    );
  });

  test("hides a previously loaded log after details are collapsed", () => {
    const model = evidencePanelModel({
      evidence: evidence([check("BASELINE")]),
      evidenceLog: {
        transactionId: "tx_1234567890abcdefghijklmnopqrstuvwxyz",
        evidenceId: "evidence_1234567890abcdefghijklmnopqrstuvwxyz",
        stepId: "step_BASELINE",
        status: "PASSED",
        logPath: "baseline.log",
        logSha256: "f".repeat(64),
        content: "private technical log",
        truncated: false,
      },
      transactions: [transaction()],
      selectedIndex: 0,
      expanded: false,
    });

    expect(model.lines.map((line) => line.text).join("\n")).not.toContain(
      "private technical log",
    );
  });

  test("describes an applied transaction as complete instead of unavailable", () => {
    const applied = {
      ...evidence([check("BASELINE")]),
      state: "APPLIED",
      applyEligible: false,
    };
    const model = evidencePanelModel({
      evidence: applied,
      evidenceLog: null,
      transactions: [],
      selectedIndex: 0,
      expanded: false,
    });
    const text = model.lines.map((line) => line.text).join("\n");

    expect(text).toContain("✓ 已应用");
    expect(text).not.toContain("不可应用");
    expect(text).not.toContain("审查修改后显式 Apply");
  });

  test("shortens identifiers without losing both ends", () => {
    expect(compactIdentifier("tx_1234567890abcdefghijklmnopqrstuvwxyz", 20)).toBe(
      "tx_12345678…stuvwxyz",
    );
  });
});
