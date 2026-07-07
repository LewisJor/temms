import type { JsonObject } from "../types";
import {
  currentHubUrl,
  localizeHubCommandPart,
  localizeHubCommandText
} from "./hub-format";
import { asRecord, stringOf, stringsOf } from "./json";
import { formatProofCommand } from "./proof-command";
import type {
  RuntimeRemediationCommand,
  RuntimeRemediationContext,
  RuntimeWorkbenchRow
} from "./workbench-types";

export function runtimeWorkbenchRowRemediationCommand(
  row: RuntimeWorkbenchRow,
  context: RuntimeRemediationContext
): RuntimeRemediationCommand | undefined {
  if (!row.actionKind) return undefined;
  return runtimeTargetAssessmentRemediationCommand(
    {
      remediation: row.remediation,
      runtime_lane: row.target.runtime_lane,
      runtime_target_id: row.targetId
    },
    context
  );
}

export function runtimeTargetAssessmentRemediationCommand(
  assessment: JsonObject,
  context: RuntimeRemediationContext
): RuntimeRemediationCommand | undefined {
  const remediation = asRecord(assessment.remediation);
  const action = stringOf(remediation.action, "");
  if (!action) return undefined;

  const refs = asRecord(remediation.refs);
  const runtimeTargetIdValue = stringOf(
    refs.runtime_target_id,
    stringOf(assessment.runtime_target_id, "runtime target")
  );
  if (!runtimeTargetIdValue || runtimeTargetIdValue === "runtime target") return undefined;

  const actionLabel = stringOf(remediation.label, action.replace(/_/g, " "));
  const contractCommand = runtimeTargetContractRemediationCommand(
    remediation,
    runtimeTargetIdValue,
    action,
    actionLabel
  );
  if (contractCommand) return contractCommand;

  const packageIdValue = context.packageId || stringOf(refs.package_id, "<package-id>");
  const modelIdValue = context.modelId || stringOf(refs.model_id, "<model-id>");
  const deviceIdValue = context.deviceId || stringOf(refs.device_id, "<device-id>");
  const slotValue = context.slot || stringOf(refs.slot, "vision");
  const hubUrl = currentHubUrl();

  if (action === "record_benchmark") {
    return {
      action,
      label: `${runtimeTargetIdValue} benchmark command`,
      edgeRun: true,
      note: "Run on the selected edge after the model package is cached.",
      command: formatProofCommand([
        "temms",
        "benchmark",
        modelIdValue || "<model-id>",
        "--slot",
        slotValue,
        "--samples",
        "10",
        "--warmup",
        "2",
        "--hub-url",
        hubUrl,
        "--device-id",
        deviceIdValue || "<device-id>",
        "--package-id",
        packageIdValue || "<package-id>",
        "--runtime-target-id",
        runtimeTargetIdValue,
        "--actor",
        "edge-agent"
      ])
    };
  }

  if (action === "validate_runtime") {
    return {
      action,
      label: `${runtimeTargetIdValue} validation command`,
      edgeRun: false,
      note: "Replace the package path with the signed TEMMS package artifact.",
      command: formatProofCommand([
        "uv",
        "run",
        "temms",
        "hub",
        "validate-runtime",
        "<package-path>",
        "--hub-url",
        hubUrl,
        "--package-id",
        packageIdValue || "<package-id>",
        "--runtime-target-id",
        runtimeTargetIdValue,
        "--actor",
        "operator:runtime-remediation",
        "--require-signature"
      ])
    };
  }

  if (action === "refresh_edge_inventory") {
    return {
      action,
      label: `${deviceIdValue || "edge"} heartbeat command`,
      edgeRun: true,
      note: "Run on the edge node to refresh runtime/provider inventory and heartbeat freshness.",
      command: formatProofCommand([
        `TEMMS_HUB_URL=${hubUrl}`,
        `TEMMS_DEVICE_ID=${deviceIdValue || "<device-id>"}`,
        "TEMMS_EDGE_HEARTBEAT_INTERVAL_S=10",
        "temms",
        "daemon",
        "start",
        "--foreground"
      ])
    };
  }

  if (action === "package_runtime_artifact") {
    const lane = asRecord(assessment.runtime_lane);
    const providers = stringsOf(lane.providers);
    const accelerators = stringsOf(lane.accelerators);
    const engine = stringOf(lane.execution_engine, "");
    const commandParts = [
      "uv",
      "run",
      "temms",
      "hub",
      "package-from-mlflow",
      "<model-uri>",
      "--hub-url",
      hubUrl,
      "--slot",
      slotValue,
      "--model-artifact",
      "<runtime-native-artifact-path>",
      "--actor",
      "operator:runtime-remediation"
    ];
    if (engine) commandParts.push("--runtime", engine);
    providers.forEach((provider) => commandParts.push("--provider", provider));
    accelerators.forEach((accelerator) => commandParts.push("--accelerator", accelerator));
    return {
      action,
      label: `${runtimeTargetIdValue} packaging command`,
      edgeRun: false,
      note: "Package a runtime-native artifact, then re-run validation and proof.",
      command: formatProofCommand(commandParts)
    };
  }

  if (["select_matching_edge_class", "resolve_runtime_capability", "free_edge_resources", "resolve_target_blocker"].includes(action)) {
    return {
      action,
      label: `${runtimeTargetIdValue} compatibility inspection`,
      edgeRun: false,
      note: `${actionLabel} with live inventory and model/runtime constraints.`,
      command: formatProofCommand([
        "uv",
        "run",
        "temms",
        "hub",
        "compatibility-matrix",
        "--hub-url",
        hubUrl,
        "--device-id",
        deviceIdValue || "<device-id>",
        "--package-id",
        packageIdValue || "<package-id>",
        "--model-id",
        modelIdValue || "<model-id>",
        "--runtime-target-id",
        runtimeTargetIdValue,
        "--include-device-inventory",
        "--json"
      ])
    };
  }

  return {
    action,
    label: `${runtimeTargetIdValue} proof check`,
    edgeRun: false,
    note: `${actionLabel} against the signed edge-runtime gate.`,
    command: formatProofCommand([
      "uv",
      "run",
      "temms",
      "hub",
      "edge-runtime-mission",
      "--hub-url",
      hubUrl,
      "--package-id",
      packageIdValue || "<package-id>",
      "--model-id",
      modelIdValue || "<model-id>",
      "--device-id",
      deviceIdValue || "<device-id>",
      "--runtime-target-id",
      runtimeTargetIdValue,
      "--slot",
      slotValue,
      "--require-go",
      "--require-best-runtime",
      "--require-capability-lock",
      "--min-runtime-fit",
      "95",
      "--json"
    ])
  };
}

export function runtimeTargetContractRemediationCommand(
  remediation: JsonObject,
  runtimeTargetIdValue: string,
  action: string,
  actionLabel: string
): RuntimeRemediationCommand | undefined {
  const commandRecord = asRecord(remediation.command);
  const edgeCommandText = stringOf(
    remediation.edge_command_text,
    stringOf(commandRecord.edge_command_text, "")
  );
  if (edgeCommandText) {
    return {
      action,
      label: `${runtimeTargetIdValue} edge command`,
      edgeRun: true,
      note: stringOf(
        remediation.edge_command_note,
        stringOf(commandRecord.edge_command_note, "Run this command on the selected edge node.")
      ),
      command: localizeHubCommandText(edgeCommandText)
    };
  }

  const operatorCommandText = stringOf(
    remediation.operator_command_text,
    stringOf(commandRecord.operator_command_text, "")
  );
  if (operatorCommandText) {
    return {
      action,
      label: `${runtimeTargetIdValue} operator command`,
      edgeRun: remediation.requires_edge_execution === true,
      note: stringOf(
        remediation.operator_command_note,
        stringOf(commandRecord.operator_command_note, `${actionLabel} against the current edge-runtime contract.`)
      ),
      command: localizeHubCommandText(operatorCommandText)
    };
  }

  const edgeCommand = stringsOf(remediation.edge_command).length
    ? stringsOf(remediation.edge_command)
    : stringsOf(commandRecord.edge_command);
  if (edgeCommand.length) {
    return {
      action,
      label: `${runtimeTargetIdValue} edge command`,
      edgeRun: true,
      note: stringOf(
        remediation.edge_command_note,
        stringOf(commandRecord.edge_command_note, "Run this command on the selected edge node.")
      ),
      command: formatProofCommand(edgeCommand.map(localizeHubCommandPart))
    };
  }

  const operatorCommand = stringsOf(remediation.operator_command).length
    ? stringsOf(remediation.operator_command)
    : stringsOf(commandRecord.operator_command);
  if (operatorCommand.length) {
    return {
      action,
      label: `${runtimeTargetIdValue} operator command`,
      edgeRun: remediation.requires_edge_execution === true,
      note: stringOf(
        remediation.operator_command_note,
        stringOf(commandRecord.operator_command_note, `${actionLabel} against the current edge-runtime contract.`)
      ),
      command: formatProofCommand(operatorCommand.map(localizeHubCommandPart))
    };
  }

  return undefined;
}
