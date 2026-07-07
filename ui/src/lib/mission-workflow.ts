import type {
  DeploymentReadinessCommand,
  Device,
  Rollout,
  RuntimeTarget,
  RuntimeValidation
} from "../types";
import { deviceId, runtimeTargetId } from "./hub-format";
import { asRecord, stringOf } from "./json";
import type { MissionDraft } from "./mission-spec";
import {
  formatPerformanceSlo,
  performanceSloDetail,
  performanceSloLabel,
  performanceSloTone,
  targetSupportsModel
} from "./runtime-fit";
import type {
  EdgeRuntimeFit,
  HubStage,
  HubStageItem,
  HubStageRunbook,
  MissionPackageStageStatus,
  ModelRecord,
  ReadinessGate,
  ReadinessGateAction,
  ReadinessVerdict,
  RuntimeFitDisplay,
  WorkflowTarget
} from "./workbench-types";

export function workflowTargetLabel(target: WorkflowTarget): string {
  const labels: Record<WorkflowTarget, string> = {
    model: "Selected model",
    deployment: "Deployment path",
    plans: "Rollout coordination",
    rollouts: "Rollout activation",
    ddil: "DDIL readiness",
    evidence: "Mission proof",
    assets: "Asset enrollment"
  };
  return labels[target];
}

export interface BuildHubStagesOptions {
  ddilDetail: string;
  deadLetteredOperations: number;
  evidenceBundleCount: number;
  evidenceDetail: string;
  evidenceValue: number;
  latestRollout: Rollout | undefined;
  missionDraft: MissionDraft;
  missionPackageStageStatus: MissionPackageStageStatus;
  missionProofComplete: boolean;
  missionReady: boolean;
  missionRolloutCount: number;
  offlineMode: boolean;
  proofEvents: number;
  replayBlockedOperations: number;
  rolloutDetail: string;
  runtimeFitDisplay: RuntimeFitDisplay;
  selectedModel: ModelRecord | undefined;
  selectedRuntime: RuntimeTarget | undefined;
}

export function buildHubStages({
  ddilDetail,
  deadLetteredOperations,
  evidenceBundleCount,
  evidenceDetail,
  evidenceValue,
  latestRollout,
  missionDraft,
  missionPackageStageStatus,
  missionProofComplete,
  missionReady,
  missionRolloutCount,
  offlineMode,
  proofEvents,
  replayBlockedOperations,
  rolloutDetail,
  runtimeFitDisplay,
  selectedModel,
  selectedRuntime
}: BuildHubStagesOptions): HubStageItem[] {
  const handlingDetail = `${missionDraft.switchPolicy.replace(/_/g, " ")}; fallback ${
    missionDraft.fallbackModelId || "auto"
  }; ${missionDraft.ddilMode.replace(/_/g, " ")}`;
  const deployTone =
    replayBlockedOperations || latestRollout?.state === "failed"
      ? "bad"
      : latestRollout
        ? "good"
        : "warn";
  const fieldTone =
    replayBlockedOperations || deadLetteredOperations
      ? "bad"
      : missionProofComplete || proofEvents
        ? "good"
        : "warn";

  return [
    {
      id: "mission",
      label: "Mission",
      value: missionReady ? (missionDraft.yaml ? "YAML loaded" : "goal defined") : "define goal",
      detail: missionReady ? missionDraft.goal || "mission YAML ready" : "goal or mission YAML",
      decision: "Describe the field objective or paste the mission YAML.",
      outcome: "Mission intent is ready to bind to model, runtime, handling, and package evidence.",
      tone: missionReady ? "good" : "warn"
    },
    {
      id: "model",
      label: "Model Plan",
      value: selectedModel?.name ?? "select model",
      detail: selectedModel
        ? `${selectedModel.format}; ${formatPerformanceSlo(selectedModel)}`
        : "choose candidate models",
      decision: "Choose the model package that should satisfy the mission.",
      outcome: "A signed model candidate is selected for runtime fit and edge deployment.",
      tone: selectedModel ? "good" : "warn"
    },
    {
      id: "runtime",
      label: "Runtime Fit",
      value: selectedRuntime ? runtimeTargetId(selectedRuntime) : "select runtime",
      detail: runtimeFitDisplay.detail,
      decision: "Pick the on-device runtime target with proof of compatibility and SLO fit.",
      outcome: "The model has a concrete runtime target and capability proof path.",
      tone: selectedRuntime ? runtimeFitDisplay.tone : "warn"
    },
    {
      id: "handling",
      label: "Sensor Handling",
      value: missionDraft.sensor || "sensor pending",
      detail: handlingDetail,
      decision: "Set sensor input, confidence switching, fallback model, and DDIL behavior.",
      outcome: "The edge daemon knows when to run, switch, queue, or require review.",
      tone: missionDraft.sensor && missionDraft.slot ? "good" : "warn"
    },
    {
      id: "package",
      label: "Package Handoff",
      value: missionPackageStageStatus.value,
      detail: missionPackageStageStatus.detail,
      decision: "Hash the mission, model, runtime plan, and handling policy into one deployable handoff.",
      outcome: "A signed mission package and deployment intent can be staged on the edge device.",
      tone: missionPackageStageStatus.tone
    },
    {
      id: "deploy",
      label: "Edge Deploy",
      value: latestRollout?.state ?? (missionRolloutCount ? `${missionRolloutCount} rollouts` : "not assigned"),
      detail: replayBlockedOperations ? `${replayBlockedOperations} DDIL replay blocked` : rolloutDetail,
      decision: "Stage the planned mission package to the selected edge device or rollout batch.",
      outcome: "The edge deployment is assigned, approved, activated, or blocked with explicit evidence.",
      tone: deployTone
    },
    {
      id: "field",
      label: "Field Ops",
      value: String(evidenceValue || `${evidenceBundleCount} bundles`),
      detail: offlineMode ? `DDIL offline; ${ddilDetail}` : evidenceDetail,
      decision: "Monitor evidence, DDIL queues, rollback, and runtime repair while the mission runs.",
      outcome: "Operators can prove what happened and recover safely under connectivity loss.",
      tone: fieldTone
    }
  ];
}

export function hubStageForWorkflowTarget(target: WorkflowTarget): HubStage {
  if (target === "model") return "model";
  if (target === "deployment" || target === "plans" || target === "rollouts" || target === "ddil") {
    return "deploy";
  }
  if (target === "evidence" || target === "assets") return "field";
  return "runtime";
}

export function hubStageRunbookFor({
  activeStage,
  currentStage,
  deadLetteredOperations,
  latestRollout,
  missionPackageStageStatus,
  missionProofComplete,
  missionReady,
  offlineMode,
  proofEvents,
  replayBlockedOperations,
  runtimeFitDisplay,
  selectedDevice,
  selectedModel,
  selectedRuntime,
  onDownloadPackage,
  onGenerateProof,
  onGoDeploy,
  onGoFieldOps,
  onGoHandling,
  onGoModels,
  onGoPackage,
  onGoRuntime,
  onPlanPackage,
  onStageDeploy,
  onSync
}: {
  activeStage: HubStage;
  currentStage: HubStageItem;
  deadLetteredOperations: number;
  latestRollout: Rollout | undefined;
  missionPackageStageStatus: MissionPackageStageStatus;
  missionProofComplete: boolean;
  missionReady: boolean;
  offlineMode: boolean;
  proofEvents: number;
  replayBlockedOperations: number;
  runtimeFitDisplay: RuntimeFitDisplay;
  selectedDevice: Device | undefined;
  selectedModel: ModelRecord | undefined;
  selectedRuntime: RuntimeTarget | undefined;
  onDownloadPackage: () => void;
  onGenerateProof: () => void;
  onGoDeploy: () => void;
  onGoFieldOps: () => void;
  onGoHandling: () => void;
  onGoModels: () => void;
  onGoPackage: () => void;
  onGoRuntime: () => void;
  onPlanPackage: () => void;
  onStageDeploy: () => void;
  onSync: () => void;
}): HubStageRunbook {
  const pathReady = Boolean(selectedModel && selectedRuntime && selectedDevice);
  const packagePlanned = missionPackageStageStatus.planned;
  const packageDownloaded = missionPackageStageStatus.downloaded;
  const packageStageable = missionPackageStageStatus.stageable;
  const rolloutState = stringOf(latestRollout?.state, "not assigned");
  const runtimeReady = runtimeFitDisplay.tone === "good";
  const missionPath = [
    selectedModel?.id || "model pending",
    selectedRuntime ? runtimeTargetId(selectedRuntime) : "runtime pending",
    selectedDevice ? deviceId(selectedDevice) : "edge pending"
  ].join(" -> ");

  switch (activeStage) {
    case "mission":
      return {
        objective: "Capture the mission intent before choosing artifacts.",
        ready: missionReady ? "Mission goal or YAML is present." : "Mission goal or YAML is still required.",
        risk: "An unbound mission creates a rollout that cannot explain why the edge switched models.",
        status: currentStage.detail,
        tone: currentStage.tone,
        actions: [
          {
            detail: "Move from intent capture to signed model/package selection.",
            icon: "arrow",
            label: "Model Plan",
            onClick: onGoModels,
            variant: "secondary"
          },
          {
            detail: "Create the first advisory mission package manifest for this path.",
            disabled: !missionReady,
            icon: "package",
            label: "Plan package",
            onClick: onPlanPackage
          }
        ]
      };
    case "model":
      return {
        objective: "Select the signed model package that should satisfy the mission.",
        ready: selectedModel
          ? `${selectedModel.name} from ${selectedModel.packageId} is selected.`
          : "A model package is still required.",
        risk: "Runtime proof and deployment intent will bind the wrong artifact if model selection is vague.",
        status: currentStage.detail,
        tone: currentStage.tone,
        actions: [
          {
            detail: "Evaluate the selected model against the edge runtime target.",
            disabled: !selectedModel,
            icon: "cpu",
            label: "Runtime Fit",
            onClick: onGoRuntime
          }
        ]
      };
    case "runtime":
      return {
        objective: "Bind the model to an on-device runtime with measured fit.",
        ready: runtimeReady ? `${missionPath}; ${runtimeFitDisplay.label}.` : runtimeFitDisplay.detail,
        risk: "Edge deploy is not credible without runtime/provider, benchmark, and capability-lock evidence.",
        status: currentStage.detail,
        tone: currentStage.tone,
        actions: [
          {
            detail: "Generate the runtime proof for the selected model/runtime/device path.",
            disabled: !pathReady,
            icon: "shield",
            label: "Generate proof",
            onClick: onGenerateProof
          },
          {
            detail: "Move to sensor, fallback, and DDIL handling policy.",
            disabled: !pathReady,
            icon: "arrow",
            label: "Sensor Handling",
            onClick: onGoHandling,
            variant: "secondary"
          }
        ]
      };
    case "handling":
      return {
        objective: "Lock sensor input, SLOs, switching, fallback, and DDIL behavior.",
        ready: currentStage.tone === "good" ? currentStage.detail : "Sensor and slot policy need review.",
        risk: "The edge daemon cannot switch or queue safely if handling policy is outside the package boundary.",
        status: `${selectedModel?.name ?? "model pending"} / ${
          selectedDevice ? deviceId(selectedDevice) : "edge pending"
        }`,
        tone: currentStage.tone,
        actions: [
          {
            detail: "Hash mission, selection, runtime plan, and handling policy into the package plan.",
            disabled: !missionReady || !pathReady,
            icon: "package",
            label: "Plan package",
            onClick: onPlanPackage
          },
          {
            detail: "Open the package handoff boundary.",
            icon: "arrow",
            label: "Package Handoff",
            onClick: onGoPackage,
            variant: "secondary"
          }
        ]
      };
    case "package":
      return {
        objective: "Produce the package identity and deployment intent for the edge.",
        ready: packageDownloaded
          ? packageStageable
            ? "Mission package file and digest headers are retained."
            : "Mission package file is retained; proof gate must pass before staging."
          : packagePlanned
            ? packageStageable
              ? "Mission package identity exists; download or stage next."
              : "Mission package identity exists; proof gate must pass before staging."
            : missionPackageStageStatus.detail,
        risk: "Staging before package planning leaves rollout intent detached from the hashed mission handoff.",
        status: missionPackageStageStatus.detail,
        tone: missionPackageStageStatus.tone,
        actions: [
          {
            detail: "Compute or refresh the mission package identity and deployment intent.",
            disabled: !missionReady || !pathReady,
            icon: "package",
            label: "Plan package",
            onClick: onPlanPackage
          },
          {
            detail: "Retain the exact mission package artifact and digest headers.",
            disabled: !missionReady || !pathReady,
            icon: "download",
            label: "Download package",
            onClick: onDownloadPackage,
            variant: "secondary"
          },
          {
            detail: packageStageable
              ? "Create the rollout from the package deployment intent."
              : "Stage rollout unlocks only after the mission package proof gate passes.",
            disabled: !packageStageable,
            icon: "rocket",
            label: "Stage rollout",
            onClick: onStageDeploy
          }
        ]
      };
    case "deploy":
      return {
        objective: "Stage and operate the planned package on the selected edge.",
        ready: latestRollout
          ? `Latest rollout is ${rolloutState}.`
          : "No rollout has been assigned for the selected package path.",
        risk: "A package that is never staged never becomes an edge-controlled runtime state.",
        status: currentStage.detail,
        tone: currentStage.tone,
        actions: [
          {
            detail: packageStageable
              ? "Create the rollout from the current mission package deployment intent."
              : "Stage rollout unlocks only after the mission package proof gate passes.",
            disabled: !packageStageable,
            icon: "rocket",
            label: "Stage rollout",
            onClick: onStageDeploy
          },
          {
            detail: "Inspect DDIL queues and evidence after rollout staging.",
            icon: "activity",
            label: "Field Ops",
            onClick: onGoFieldOps,
            variant: "secondary"
          }
        ]
      };
    case "field":
    default:
      return {
        objective: "Monitor DDIL, rollout evidence, and runtime repair while the mission runs.",
        ready: missionProofComplete
          ? "Mission replay is complete."
          : proofEvents
            ? `${proofEvents} proof events are available.`
            : "Mission evidence has not been exported yet.",
        risk: replayBlockedOperations || deadLetteredOperations
          ? "Blocked or quarantined DDIL operations need review before field replay."
          : offlineMode
            ? "Offline operation is active; sync gates must be checked before replay."
            : "Evidence gaps make the field story harder to prove after the demo.",
        status: currentStage.detail,
        tone: currentStage.tone,
        actions: [
          {
            detail: "Refresh Hub state from the daemon.",
            icon: "refresh",
            label: "Sync",
            onClick: onSync,
            variant: "secondary"
          },
          {
            detail: "Return to package handoff if the mission path needs another artifact.",
            icon: "package",
            label: "Package Handoff",
            onClick: onGoPackage
          }
        ]
      };
  }
}

export function readinessActionTitle(action: ReadinessGateAction): string {
  const context = readinessActionContext(action);
  const command = readinessCommand(action);
  const prefix = command
    ? `Review ${command.method ?? "command"} ${command.path}`
    : `Focus ${workflowTargetLabel(workflowTargetForReadinessAction(action))}`;
  return `${prefix}${context ? ` for ${context}` : ""}`;
}

export function readinessApiCommandText(command: DeploymentReadinessCommand): string {
  const method = command.method || "POST";
  const body =
    command.body && Object.keys(command.body).length
      ? ` -H "Content-Type: application/json" -d '${JSON.stringify(command.body)}'`
      : "";
  return `curl -fsS -X ${method} ${command.path}${body}`;
}

export function edgeReadinessCommandReason(
  action: ReadinessGateAction,
  command: DeploymentReadinessCommand
): string {
  const note = command.edge_command_note;
  if (note) return note;
  if (action.kind === "refresh_edge_inventory") {
    return "Run on the edge node to refresh heartbeat, runtime/provider inventory, and capability-lock freshness.";
  }
  if (action.kind === "record_benchmark") {
    return "Run on the edge node so latency, throughput, and provider evidence are measured on the actual runtime.";
  }
  if (action.kind === "validate_runtime") {
    return "Run against the target runtime image or edge node so validation proof is tied to the actual execution surface.";
  }
  return "Run on the edge node so TEMMS records proof from the actual on-device runtime.";
}

export function readinessCommand(action: ReadinessGateAction): DeploymentReadinessCommand | undefined {
  return readinessCommandFromValue(action.command);
}

export function readinessCommandFromValue(value: unknown): DeploymentReadinessCommand | undefined {
  const record = asRecord(value);
  const method = stringOf(record.method, "").toUpperCase();
  const path = stringOf(record.path, "");
  if (!method || !path) return undefined;
  const body = "body" in record ? asRecord(record.body) : undefined;
  return {
    method,
    path,
    ...(body === undefined ? {} : { body }),
    requires_edge_execution: record.requires_edge_execution === true,
    edge_command: Array.isArray(record.edge_command)
      ? record.edge_command.map((part) => String(part))
      : undefined,
    edge_command_text: stringOf(record.edge_command_text, ""),
    edge_command_note: stringOf(record.edge_command_note, "")
  };
}

export function readinessActionContext(action: ReadinessGateAction): string {
  const refs = asRecord(action.refs);
  const deviceIds = Array.isArray(refs.device_ids)
    ? refs.device_ids.map((item) => String(item)).filter(Boolean)
    : [];
  const model = stringOf(refs.model_id, "");
  const device = stringOf(refs.device_id, deviceIds.join(", "));
  const runtime = stringOf(refs.runtime_target_id, "");
  const slot = stringOf(refs.slot, "");
  const parts = [];
  if (model) parts.push(model);
  if (device) parts.push(`on ${device}`);
  if (runtime) parts.push(`via ${runtime}`);
  if (slot) parts.push(`slot ${slot}`);
  return parts.join(" ");
}

export function workflowTargetForReadinessAction(action: ReadinessGateAction): WorkflowTarget {
  const key = action.kind || action.id;
  if (["register_package", "enroll_device", "enroll_edge_target"].includes(key)) {
    return "assets";
  }
  if (["promote_package", "select_package"].includes(key)) return "model";
  if (
    [
      "register_runtime_target",
      "refresh_edge_inventory",
      "record_benchmark",
      "select_context",
      "select_runtime_target",
      "validate_runtime"
    ].includes(key)
  ) {
    return "deployment";
  }
  if (key === "create_rollout") return "deployment";
  if (key === "create_rollout_plan") return "plans";
  if (["approve_rollout", "apply_rollout", "rollback_rollout", "inspect_rollout"].includes(key)) {
    return "rollouts";
  }
  if (
    [
      "restore_connectivity",
      "restore_online",
      "sync_pending",
      "quarantine_blocked",
      "acknowledge_dead_letters"
    ].includes(key)
  ) {
    return "ddil";
  }
  if (key === "export_replay") return "evidence";

  if (action.gateId === "model_package") return action.id.includes("register") ? "assets" : "model";
  if (action.gateId === "runtime_target") return "deployment";
  if (action.gateId === "performance_fit") return "deployment";
  if (action.gateId === "resource_envelope") return "deployment";
  if (action.gateId === "edge_target") return "assets";
  if (action.gateId === "rollout_gate") return "rollouts";
  if (action.gateId === "ddil_queue") return "ddil";
  if (action.gateId === "evidence_chain") return "evidence";
  return "deployment";
}

export function buildReadinessVerdict({
  deadLetteredOperations,
  edgeRuntimeFit,
  evidenceValue,
  invalidPendingOperations,
  latestRollout,
  missionPhaseTotal,
  missionProofComplete,
  offlineMode,
  pendingOperations,
  proofEvents,
  replayBlockedOperations,
  runtimeOptimizationAdvisories,
  resourceEnvelopeFit,
  selectedDevice,
  selectedModel,
  selectedRuntime,
  selectedRuntimeValidation,
  signedEvidenceImports
}: {
  deadLetteredOperations: number;
  edgeRuntimeFit: EdgeRuntimeFit;
  evidenceValue: number;
  invalidPendingOperations: number;
  latestRollout: Rollout | undefined;
  missionPhaseTotal: number;
  missionProofComplete: boolean;
  offlineMode: boolean;
  pendingOperations: number;
  proofEvents: number;
  replayBlockedOperations: number;
  runtimeOptimizationAdvisories: number;
  resourceEnvelopeFit: EdgeRuntimeFit;
  selectedDevice: Device | undefined;
  selectedModel: ModelRecord | undefined;
  selectedRuntime: RuntimeTarget | undefined;
  selectedRuntimeValidation: RuntimeValidation | undefined;
  signedEvidenceImports: number;
}): ReadinessVerdict {
  const runtimeCompatible =
    selectedModel && selectedRuntime ? targetSupportsModel(selectedRuntime, selectedModel) : false;
  const approvalRequired = latestRollout?.approval_required === true;
  const approvalReady = !approvalRequired || latestRollout?.approval?.approved === true;
  const rolloutState = latestRollout?.state ?? "";
  const rolloutTerminal = ["activated", "rolled_back"].includes(rolloutState);
  const rolloutFailed = ["failed", "blocked"].includes(rolloutState);
  const performanceTone = selectedModel ? performanceSloTone(selectedModel) : "bad";
  const gates: ReadinessGate[] = [
    selectedModel
      ? selectedModel.signed
        ? selectedModel.packagePromotion === "released"
          ? {
              label: "Model package",
              state: "released",
              detail: `${selectedModel.name} is signed and released`,
              tone: "good"
            }
          : {
              label: "Model package",
              state: selectedModel.packagePromotion,
              detail: "Promote the signed package to released before field assignment",
              tone: "warn"
            }
        : {
            label: "Model package",
            state: "unsigned",
            detail: "Register a package with verified signature metadata",
            tone: "bad"
          }
      : {
          label: "Model package",
          state: "missing",
          detail: "Register a signed TEMMS package with model metadata",
          tone: "bad"
        },
    selectedRuntime
      ? runtimeCompatible
        ? edgeRuntimeFit.tone === "bad"
          ? {
              label: "Runtime target",
              state: "edge mismatch",
              detail: edgeRuntimeFit.detail,
              tone: "bad"
            }
          : selectedRuntimeValidation
            ? {
                label: "Runtime target",
                state: "validated",
                detail: `${runtimeTargetId(selectedRuntime)} has passing package validation`,
                tone: "good"
              }
            : {
                label: "Runtime target",
                state: edgeRuntimeFit.label,
                detail: edgeRuntimeFit.detail,
                tone: edgeRuntimeFit.tone === "good" ? "good" : "warn"
              }
        : {
            label: "Runtime target",
            state: "incompatible",
            detail: "Selected runtime target does not satisfy model constraints",
            tone: "bad"
          }
      : {
          label: "Runtime target",
          state: "missing",
          detail: "Register or select a runtime target for this model",
          tone: "bad"
        },
    selectedModel
      ? {
          label: "Performance fit",
          state: performanceSloLabel(selectedModel),
          detail: performanceSloDetail(selectedModel),
          tone: performanceTone === "neutral" ? "good" : performanceTone
        }
      : {
          label: "Performance fit",
          state: "missing",
          detail: "Select a model before evaluating on-device SLO evidence",
          tone: "bad"
        },
    selectedModel
      ? {
          label: "Resource envelope",
          state: resourceEnvelopeFit.label,
          detail: resourceEnvelopeFit.detail,
          tone: resourceEnvelopeFit.tone === "neutral" ? "good" : resourceEnvelopeFit.tone
        }
      : {
          label: "Resource envelope",
          state: "missing",
          detail: "Select a model and edge target before evaluating resource fit",
          tone: "bad"
        },
    selectedDevice
      ? {
          label: "Edge target",
          state: selectedDevice.status ?? "registered",
          detail: `${deviceId(selectedDevice)} reports profile ${selectedDevice.profile ?? "unknown"}`,
          tone: selectedDevice.status === "offline" ? "warn" : "good"
        }
      : {
          label: "Edge target",
          state: "missing",
          detail: "Enroll an edge node or connect a simulated device",
          tone: "bad"
        },
    latestRollout
      ? rolloutFailed
        ? {
            label: "Rollout gate",
            state: rolloutState,
            detail: "Inspect rollout failure before advancing this model",
            tone: "bad"
          }
        : approvalReady
          ? {
              label: "Rollout gate",
              state: rolloutState || "assigned",
              detail: rolloutTerminal
                ? "Latest rollout reached a terminal audited outcome"
                : "Rollout can advance through apply",
              tone: rolloutTerminal ? "good" : "warn"
            }
          : {
              label: "Rollout gate",
              state: "approval pending",
              detail: "Approve the rollout policy before edge apply",
              tone: "warn"
            }
      : {
          label: "Rollout gate",
          state: "not assigned",
          detail: "Create a rollout or staged rollout plan for the selected model",
          tone: "warn"
        },
    invalidPendingOperations || replayBlockedOperations
      ? {
          label: "DDIL queue",
          state: "blocked",
          detail: `${invalidPendingOperations + replayBlockedOperations} unsafe intent${
            invalidPendingOperations + replayBlockedOperations === 1 ? "" : "s"
          } need quarantine or review`,
          tone: "bad"
        }
      : pendingOperations
        ? {
            label: "DDIL queue",
            state: runtimeOptimizationAdvisories
              ? "runtime advisory"
              : offlineMode
                ? "offline queued"
                : "pending replay",
            detail: runtimeOptimizationAdvisories
              ? `${pendingOperations} signed intent${
                  pendingOperations === 1 ? "" : "s"
                } replayable; ${runtimeOptimizationAdvisories} runtime target advisor${
                  runtimeOptimizationAdvisories === 1 ? "y" : "ies"
                }`
              : `${pendingOperations} signed intent${
                  pendingOperations === 1 ? "" : "s"
                } waiting for reconciliation`,
            tone: "warn"
          }
        : deadLetteredOperations
          ? {
              label: "DDIL queue",
              state: "quarantined",
              detail: `${deadLetteredOperations} unresolved quarantined intent${
                deadLetteredOperations === 1 ? "" : "s"
              }`,
              tone: "warn"
            }
          : {
              label: "DDIL queue",
              state: offlineMode ? "offline" : "clear",
              detail: offlineMode
                ? "Link is intentionally offline with no queued intents"
                : "No pending or blocked DDIL intents",
              tone: offlineMode ? "warn" : "good"
            },
    missionProofComplete
      ? {
          label: "Evidence chain",
          state: "complete",
          detail: `${missionPhaseTotal} replay phases complete`,
          tone: "good"
        }
      : evidenceValue && signedEvidenceImports
        ? {
            label: "Evidence chain",
            state: "partial",
            detail: `${proofEvents || evidenceValue} proof events with signed package evidence`,
            tone: "warn"
          }
        : {
            label: "Evidence chain",
            state: "missing",
            detail: "Generate mission proof after rollout or DDIL activity",
            tone: "warn"
          }
  ];
  const blocker = gates.find((gate) => gate.tone === "bad");
  const warning = gates.find((gate) => gate.tone === "warn");
  if (blocker) {
    return {
      label: "blocked",
      headline: "Deployment is blocked",
      detail: "One or more safety gates prevent field rollout for the selected model.",
      nextAction: blocker.detail,
      tone: "bad",
      gates
    };
  }
  if (warning) {
    return {
      label: "attention",
      headline: "Deployment is stageable with operator action",
      detail:
        "The selected model has no hard blockers, but one runtime, resource, performance, or proof gate still needs review.",
      nextAction: warning.detail,
      tone: "warn",
      gates
    };
  }
  return {
    label: "go",
    headline: "Deployment loop is ready",
    detail:
      "Model package, runtime target, performance SLO, resource envelope, edge target, rollout, DDIL queue, and evidence chain are aligned.",
    nextAction: "Export mission replay or stage the next rollout batch",
    tone: "good",
    gates
  };
}

export function ddilStatusDetail({
  deploymentStateName,
  invalidPendingOperations,
  pendingOperations,
  replayBlockedOperations,
  replayReadyOperations,
  runtimeOptimizationAdvisories,
  supersededOperations,
  verifiedPendingOperations
}: {
  deploymentStateName: string;
  invalidPendingOperations: number;
  pendingOperations: number;
  replayBlockedOperations: number;
  replayReadyOperations: number;
  runtimeOptimizationAdvisories: number;
  supersededOperations: number;
  verifiedPendingOperations: number;
}): string {
  if (!pendingOperations) return deploymentStateName.toLowerCase();
  if (replayBlockedOperations) {
    return `${replayBlockedOperations} blocked intent${replayBlockedOperations === 1 ? "" : "s"}`;
  }
  if (invalidPendingOperations) {
    return `${invalidPendingOperations} invalid intent${invalidPendingOperations === 1 ? "" : "s"}`;
  }
  if (runtimeOptimizationAdvisories) {
    return `${runtimeOptimizationAdvisories} runtime target advisor${
      runtimeOptimizationAdvisories === 1 ? "y" : "ies"
    }`;
  }
  if (supersededOperations) {
    return `${supersededOperations} superseded intent${supersededOperations === 1 ? "" : "s"}`;
  }
  if (replayReadyOperations === pendingOperations) {
    return `${replayReadyOperations} replay-ready intent${replayReadyOperations === 1 ? "" : "s"}`;
  }
  if (verifiedPendingOperations === pendingOperations) {
    return `${verifiedPendingOperations} verified intent${verifiedPendingOperations === 1 ? "" : "s"}`;
  }
  return `${pendingOperations} pending ops`;
}
