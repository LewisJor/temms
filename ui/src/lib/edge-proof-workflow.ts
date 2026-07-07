import type { ReadinessQuery } from "../api";
import type { DeploymentReadiness, Device, JsonObject, RuntimeTarget } from "../types";
import { currentHubUrl, deviceId, runtimeTargetId } from "./hub-format";
import { asRecord, numberOf, stringOf } from "./json";
import {
  EDGE_PROOF_COMPONENT_DIGEST_TARGETS,
  canonicalJsonStringify,
  isSha256Digest,
  sha256Hex
} from "./proof-hash";
import { formatProofCommand } from "./proof-command";
import {
  capabilityLockDetail,
  capabilityLockTone,
  capabilityLockValue,
  runtimeCapabilityLockForProof,
  runtimeFitScoreForProof
} from "./runtime-fit";
import { selectionMatchesContext } from "./readiness";
import type {
  EdgeProofComponentDigestStatus,
  EdgeProofTraceStatus,
  EdgeProofWorkflow,
  GateTone,
  ModelRecord,
  ReadinessVerdict,
  RuntimeFitDisplay
} from "./workbench-types";

const EDGE_PROOF_MAX_AGE_SECONDS = 900;

export function buildEdgeProofWorkflow({
  device,
  model,
  readiness,
  readinessVerdict,
  runtime,
  runtimeFitDisplay
}: {
  device: Device | undefined;
  model: ModelRecord | undefined;
  readiness: DeploymentReadiness | undefined;
  readinessVerdict: ReadinessVerdict;
  runtime: RuntimeTarget | undefined;
  runtimeFitDisplay: RuntimeFitDisplay;
}): EdgeProofWorkflow {
  const runtimeId = runtime ? runtimeTargetId(runtime) : "";
  const edgeId = device ? deviceId(device) : "";
  const missing = [
    model ? "" : "model",
    runtimeId ? "" : "runtime target",
    edgeId ? "" : "edge device"
  ].filter(Boolean);
  const runtimeFitScore = runtimeFitScoreForProof(readiness, runtimeFitDisplay);
  const runtimeFitLabel =
    runtimeFitScore !== undefined ? `runtime fit ${runtimeFitScore}/100` : runtimeFitDisplay.label;
  const gatePolicy = "go + best runtime + capability lock + fit >= 95 + proof <= 15m + path bound";
  const proofPath = `/tmp/${proofFileName(model?.id, runtimeId, edgeId)}`;
  const hubUrl = currentHubUrl();
  const capabilityLock = runtimeCapabilityLockForProof(readiness);

  let tone: GateTone = "warn";
  let status = "Proof context incomplete";
  if (!missing.length) {
    if (readinessVerdict.label === "go" && runtimeFitScore !== undefined && runtimeFitScore >= 95) {
      tone = "good";
      status = "Edge proof ready";
    } else if (readinessVerdict.tone === "bad" || (runtimeFitScore !== undefined && runtimeFitScore < 95)) {
      tone = "bad";
      status = "Edge proof will fail";
    } else {
      tone = "warn";
      status = "Edge proof needs evidence";
    }
  }

  const detail = missing.length
    ? `Missing ${missing.join(", ")} for proof export.`
    : `${model?.id ?? "model"} -> ${runtimeId} -> ${edgeId}; ${readinessVerdict.nextAction}; offline verifier fails stale proofs or proofs for a different path.`;

  const generateCommand = formatProofCommand([
    "uv",
    "run",
    "temms",
    "hub",
    "edge-runtime-mission",
    "--hub-url",
    hubUrl,
    "--package-id",
    model?.packageId ?? "<package-id>",
    "--model-id",
    model?.id ?? "<model-id>",
    "--device-id",
    edgeId || "<device-id>",
    "--runtime-target-id",
    runtimeId || "<runtime-target-id>",
    "--slot",
    "vision",
    "--require-go",
    "--require-best-runtime",
    "--require-capability-lock",
    "--min-runtime-fit",
    "95",
    "--output",
    proofPath
  ]);
  const verifyCommand = formatProofCommand([
    "uv",
    "run",
    "temms",
    "hub",
    "verify-edge-proof",
    proofPath,
    "--require-go",
    "--require-best-runtime",
    "--require-capability-lock",
    "--min-runtime-fit",
    "95",
    "--max-proof-age-seconds",
    String(EDGE_PROOF_MAX_AGE_SECONDS),
    "--package-id",
    model?.packageId ?? "<package-id>",
    "--model-id",
    model?.id ?? "<model-id>",
    "--device-id",
    edgeId || "<device-id>",
    "--runtime-target-id",
    runtimeId || "<runtime-target-id>",
    "--slot",
    "vision",
    "--require-proof-signature"
  ]);
  const verifyJsonCommand = formatProofCommand([
    "uv",
    "run",
    "temms",
    "hub",
    "verify-edge-proof",
    proofPath,
    "--require-go",
    "--require-best-runtime",
    "--require-capability-lock",
    "--min-runtime-fit",
    "95",
    "--max-proof-age-seconds",
    String(EDGE_PROOF_MAX_AGE_SECONDS),
    "--package-id",
    model?.packageId ?? "<package-id>",
    "--model-id",
    model?.id ?? "<model-id>",
    "--device-id",
    edgeId || "<device-id>",
    "--runtime-target-id",
    runtimeId || "<runtime-target-id>",
    "--slot",
    "vision",
    "--require-proof-signature",
    "--json"
  ]);

  return {
    status,
    detail,
    tone,
    proofPath,
    gatePolicy,
    attestation: "signed attestation required",
    capabilityLock: `Capability lock: ${capabilityLockValue(capabilityLock)}`,
    capabilityLockDetail: capabilityLockDetail(capabilityLock),
    capabilityLockTone: capabilityLockTone(capabilityLock),
    runtimeFit: runtimeFitLabel,
    generateCommand,
    verifyCommand,
    verifyJsonCommand,
    missing
  };
}

export function edgeProofTraceStatus(
  proof: JsonObject | undefined,
  context: ReadinessQuery
): EdgeProofTraceStatus {
  if (!proof) {
    return {
      commandCount: 0,
      detail: "Generate or download a proof to inspect its signed runtime decision trace.",
      errors: [],
      rowCount: 0,
      schema: "",
      status: "not_generated",
      tone: "neutral",
      value: "not generated"
    };
  }
  if (proof.schema_version !== "temms-edge-runtime-proof/v1") {
    return {
      commandCount: 0,
      detail: "The latest payload is not a TEMMS edge runtime proof.",
      errors: ["payload schema is not temms-edge-runtime-proof/v1"],
      rowCount: 0,
      schema: stringOf(proof.schema_version, ""),
      status: "missing",
      tone: "warn",
      value: "not a proof"
    };
  }
  const selection = asRecord(proof.selection);
  const trace = asRecord(proof.runtime_decision_trace);
  if (!selectionMatchesContext(selection, context)) {
    return {
      commandCount: edgeProofTraceCommands(trace).length,
      detail: edgeProofTracePathDetail(selection),
      errors: ["latest proof does not match the selected model/runtime/edge path"],
      rowCount: edgeProofTraceRows(trace).length,
      schema: stringOf(trace.schema_version, ""),
      status: "stale",
      tone: "warn",
      value: "different path"
    };
  }

  const workbench = asRecord(proof.runtime_workbench);
  const rows = edgeProofTraceRows(trace);
  const commands = edgeProofTraceCommands(trace);
  const schema = stringOf(trace.schema_version, "");
  if (schema !== "temms-runtime-decision-trace/v1") {
    return {
      commandCount: commands.length,
      detail: "Proof does not retain a runtime decision trace.",
      errors: [`trace schema is ${schema || "missing"}`],
      rowCount: rows.length,
      schema,
      status: "missing",
      tone: "warn",
      value: "trace missing"
    };
  }
  if (workbench.schema_version !== "temms-runtime-workbench/v1") {
    return {
      commandCount: commands.length,
      detail: "Proof does not retain the canonical runtime workbench needed for browser consistency checks.",
      errors: ["runtime_workbench schema is missing"],
      rowCount: rows.length,
      schema,
      status: "missing",
      tone: "warn",
      value: "workbench missing"
    };
  }

  const errors = edgeProofTraceConsistencyErrors(trace, workbench);
  return {
    commandCount: commands.length,
    detail: errors.length
      ? "Signed trace disagrees with the canonical runtime workbench."
      : "Signed trace agrees with the canonical runtime workbench.",
    errors,
    rowCount: rows.length,
    schema,
    status: errors.length ? "mismatch" : "consistent",
    tone: errors.length ? "bad" : "good",
    value: errors.length ? "trace mismatch" : "trace consistent"
  };
}

export function edgeProofComponentDigestStatus(
  proof: JsonObject | undefined,
  context: ReadinessQuery
): EdgeProofComponentDigestStatus {
  if (!proof) {
    return {
      detail: "Generate or download a proof to inspect component-level hashes.",
      digestCount: 0,
      digests: [],
      errors: [],
      schema: "",
      status: "not_generated",
      tone: "neutral",
      value: "not generated"
    };
  }
  if (proof.schema_version !== "temms-edge-runtime-proof/v1") {
    return {
      detail: "The latest payload is not a TEMMS edge runtime proof.",
      digestCount: 0,
      digests: [],
      errors: ["payload schema is not temms-edge-runtime-proof/v1"],
      schema: "",
      status: "missing",
      tone: "warn",
      value: "not a proof"
    };
  }
  const selection = asRecord(proof.selection);
  const componentDigests = asRecord(proof.component_digests);
  const digests = EDGE_PROOF_COMPONENT_DIGEST_TARGETS
    .map(({ key, label }) => ({ key, label, value: stringOf(componentDigests[key], "") }))
    .filter((digest) => digest.value);
  if (!selectionMatchesContext(selection, context)) {
    return {
      detail: edgeProofTracePathDetail(selection),
      digestCount: digests.length,
      digests,
      errors: ["latest proof does not match the selected model/runtime/edge path"],
      schema: stringOf(componentDigests.schema_version, ""),
      status: "stale",
      tone: "warn",
      value: "different path"
    };
  }

  const schema = stringOf(componentDigests.schema_version, "");
  const errors: string[] = [];
  if (schema !== "temms-edge-runtime-proof-component-digests/v1") {
    errors.push(`component digest schema is ${schema || "missing"}`);
  }
  EDGE_PROOF_COMPONENT_DIGEST_TARGETS.forEach(({ key, label, component }) => {
    const digest = stringOf(componentDigests[key], "");
    const componentPresent = Object.keys(asRecord(proof[component])).length > 0;
    if (componentPresent && !digest) errors.push(`${label} digest is missing`);
    if (digest && !isSha256Digest(digest)) errors.push(`${label} digest is not a sha256 hex value`);
    if (digest && !componentPresent) errors.push(`${label} digest is recorded but component is missing`);
  });

  const digestCount = digests.length;
  return {
    detail: errors.length
      ? errors.slice(0, 2).join(" / ")
      : "Runtime workbench, trace, and execution manifest hashes are retained; browser verification starts automatically.",
    digestCount,
    digests,
    errors,
    schema,
    status: errors.length ? "missing" : "retained",
    tone: errors.length ? "warn" : "good",
    value: errors.length ? "digest evidence incomplete" : "digests retained"
  };
}

export async function verifyEdgeProofComponentDigestStatus(
  proof: JsonObject,
  baseStatus: EdgeProofComponentDigestStatus
): Promise<EdgeProofComponentDigestStatus> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) {
    return {
      ...baseStatus,
      detail: "Browser crypto is unavailable; use verify-edge-proof for component digest recomputation.",
      status: "retained",
      tone: "warn",
      value: "digests retained"
    };
  }
  const recorded = new Map(baseStatus.digests.map((digest) => [digest.key, digest.value]));
  const errors: string[] = [];
  await Promise.all(
    EDGE_PROOF_COMPONENT_DIGEST_TARGETS.map(async ({ key, label, component }) => {
      const value = asRecord(proof[component]);
      if (!Object.keys(value).length) return;
      const expected = recorded.get(key);
      if (!expected) {
        errors.push(`${label} digest is missing`);
        return;
      }
      const computed = await sha256Hex(canonicalJsonStringify(value));
      if (expected.replace(/^sha256:/, "").toLowerCase() !== computed) {
        errors.push(`${label} digest mismatch`);
      }
    })
  );
  return {
    ...baseStatus,
    detail: errors.length
      ? errors.slice(0, 2).join(" / ")
      : "Browser recomputed workbench, trace, and manifest hashes against the proof payload.",
    errors,
    status: errors.length ? "mismatch" : "consistent",
    tone: errors.length ? "bad" : "good",
    value: errors.length ? "digest mismatch" : "digests verified"
  };
}

function edgeProofTraceConsistencyErrors(trace: JsonObject, workbench: JsonObject): string[] {
  const errors: string[] = [];
  const summary = asRecord(workbench.summary);
  const targetSelection = asRecord(workbench.target_selection);
  const topLevel: Array<[string, unknown]> = [
    ["selected_runtime_target_id", workbench.selected_runtime_target_id],
    ["best_runtime_target_id", workbench.best_runtime_target_id],
    ["selected_is_best", summary.selected_is_best],
    ["target_count", summary.target_count],
    ["eligible_target_count", summary.eligible_target_count],
    ["blocked_target_count", summary.blocked_target_count],
    ["target_selection_status", targetSelection.status],
    ["selected_rank", targetSelection.selected_rank],
    ["selected_score", targetSelection.selected_score],
    ["best_score", targetSelection.best_score],
    ["score_delta", targetSelection.score_delta]
  ];
  topLevel.forEach(([field, expected]) => {
    const actual = trace[field];
    if (!edgeProofTraceValuesEqual(actual, expected)) {
      errors.push(`${field} ${edgeProofValueLabel(actual)} != ${edgeProofValueLabel(expected)}`);
    }
  });

  const traceRows = new Map(edgeProofTraceRows(trace).map((row) => [stringOf(row.runtime_target_id, ""), row]));
  const workbenchRows = Array.isArray(workbench.targets)
    ? workbench.targets.map(asRecord).filter((row) => stringOf(row.runtime_target_id, ""))
    : [];
  workbenchRows.forEach((expectedRow) => {
    const targetId = stringOf(expectedRow.runtime_target_id, "");
    const traceRow = traceRows.get(targetId);
    if (!traceRow) {
      errors.push(`missing trace row ${targetId}`);
      return;
    }
    edgeProofTraceRowFields(expectedRow).forEach(([field, expected]) => {
      if (!edgeProofTraceValuesEqual(traceRow[field], expected)) {
        errors.push(`${targetId}.${field} ${edgeProofValueLabel(traceRow[field])} != ${edgeProofValueLabel(expected)}`);
      }
    });
    const expectedProof = asRecord(expectedRow.proof);
    const traceLock = asRecord(traceRow.capability_lock);
    const lockChecks: Array<[string, unknown]> = [
      ["status", expectedProof.capability_lock_status],
      ["capability_sha256", expectedProof.capability_sha256],
      ["telemetry_state", expectedProof.telemetry_state],
      ["telemetry_status", expectedProof.telemetry_status]
    ];
    lockChecks.forEach(([field, expected]) => {
      if (!edgeProofTraceValuesEqual(traceLock[field], expected)) errors.push(`${targetId}.capability_lock.${field} mismatch`);
    });
    const traceComponents = asRecord(traceRow.proof_components);
    edgeProofTraceComponentChecks(expectedProof).forEach(([component, field, expected]) => {
      const actual = asRecord(traceComponents[component])[field];
      if (!edgeProofTraceValuesEqual(actual, expected)) errors.push(`${targetId}.${component}.${field} mismatch`);
    });
  });
  traceRows.forEach((_row, targetId) => {
    if (!workbenchRows.some((row) => stringOf(row.runtime_target_id, "") === targetId)) {
      errors.push(`unexpected trace row ${targetId}`);
    }
  });

  const traceCommands = new Map(edgeProofTraceCommands(trace).map((command) => [stringOf(command.runtime_target_id, ""), command]));
  workbenchRows.forEach((row) => {
    const targetId = stringOf(row.runtime_target_id, "");
    const expected = edgeProofWorkbenchCommand(row);
    const actual = traceCommands.get(targetId);
    if (expected && !actual) errors.push(`missing trace command ${targetId}`);
    if (!expected && actual) errors.push(`unexpected trace command ${targetId}`);
    if (expected && actual) {
      ["action", "label", "kind", "requires_edge_execution", "command_text"].forEach((field) => {
        if (!edgeProofTraceValuesEqual(actual[field], expected[field])) errors.push(`${targetId}.command.${field} mismatch`);
      });
    }
  });

  return errors;
}

function edgeProofTraceRows(trace: JsonObject): JsonObject[] {
  return Array.isArray(trace.rows) ? trace.rows.map(asRecord) : [];
}

function edgeProofTraceCommands(trace: JsonObject): JsonObject[] {
  return Array.isArray(trace.commands) ? trace.commands.map(asRecord) : [];
}

function edgeProofTraceRowFields(row: JsonObject): Array<[string, unknown]> {
  const proof = asRecord(row.proof);
  return [
    ["rank", row.rank],
    ["status", row.status],
    ["eligible", row.eligible],
    ["selected", row.selected === true],
    ["best", row.best === true],
    ["score", row.score],
    ["tier", row.tier],
    ["detail", row.detail],
    ["validation_id", proof.validation_id],
    ["benchmark_id", proof.benchmark_id],
    ["latency_ms_p95", proof.latency_ms_p95],
    ["throughput_ips", proof.throughput_ips]
  ];
}

function edgeProofTraceComponentChecks(proof: JsonObject): Array<[string, string, unknown]> {
  return [
    ["runtime_validation", "status", proof.runtime_validation_status],
    ["runtime_validation", "state", proof.runtime_validation_state],
    ["runtime_validation", "evidence_id", proof.validation_id],
    ["benchmark", "status", proof.performance_status],
    ["benchmark", "state", proof.performance_state],
    ["benchmark", "evidence_id", proof.benchmark_id],
    ["benchmark", "latency_ms_p95", proof.latency_ms_p95],
    ["benchmark", "throughput_ips", proof.throughput_ips],
    ["resource", "status", proof.resource_status],
    ["resource", "state", proof.resource_state],
    ["telemetry", "status", proof.telemetry_status],
    ["telemetry", "state", proof.telemetry_state],
    ["capability_lock", "status", proof.capability_lock_status],
    ["capability_lock", "capability_sha256", proof.capability_sha256]
  ];
}

function edgeProofWorkbenchCommand(row: JsonObject): JsonObject | undefined {
  const remediation = asRecord(row.remediation);
  if (!Object.keys(remediation).length) return undefined;
  const commandRecord = asRecord(remediation.command);
  const edgeCommandText = stringOf(remediation.edge_command_text, stringOf(commandRecord.edge_command_text, ""));
  const operatorCommandText = stringOf(remediation.operator_command_text, stringOf(commandRecord.operator_command_text, ""));
  const edgeCommand = edgeProofCommandText(remediation.edge_command || commandRecord.edge_command);
  const operatorCommand = edgeProofCommandText(remediation.operator_command || commandRecord.operator_command);
  const commandText = edgeCommandText || operatorCommandText || edgeCommand || operatorCommand;
  if (!commandText) return undefined;
  const kind = edgeCommandText || edgeCommand ? "edge" : "operator";
  return {
    runtime_target_id: stringOf(row.runtime_target_id, ""),
    action: stringOf(remediation.action, ""),
    label: stringOf(remediation.label, stringOf(remediation.action, "Review")),
    kind,
    requires_edge_execution: remediation.requires_edge_execution === true,
    command_text: commandText
  };
}

function edgeProofCommandText(value: unknown): string {
  return Array.isArray(value) ? value.map((part) => String(part)).filter(Boolean).join(" ") : "";
}

function edgeProofTraceValuesEqual(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if ((left === undefined || left === null || left === "") && (right === undefined || right === null || right === "")) return true;
  const leftNumber = numberOf(left);
  const rightNumber = numberOf(right);
  if (leftNumber !== undefined && rightNumber !== undefined) return leftNumber === rightNumber;
  return false;
}

function edgeProofValueLabel(value: unknown): string {
  if (value === undefined || value === null || value === "") return "missing";
  return JSON.stringify(value);
}

function edgeProofTracePathDetail(selection: JsonObject): string {
  const model = stringOf(selection.model_id, "model");
  const runtime = stringOf(selection.runtime_target_id, "runtime");
  const device = stringOf(selection.device_id, "edge");
  return `Latest proof is for ${model} -> ${runtime} -> ${device}.`;
}

function proofFileName(modelId: string | undefined, runtimeId: string, deviceIdValue: string): string {
  const slug = [modelId, runtimeId, deviceIdValue]
    .filter((part): part is string => Boolean(part))
    .map((part) => part.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, ""))
    .filter(Boolean)
    .join("-")
    .slice(0, 140);
  return `temms-edge-runtime-proof${slug ? `-${slug}` : ""}.json`;
}

export function downloadJson(fileName: string, payload: unknown): void {
  const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
