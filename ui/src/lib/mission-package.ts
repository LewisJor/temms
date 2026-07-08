import type {
  MissionPackageDownloadHandoff,
  MissionPackagePlanRequest,
  ReadinessQuery
} from "../api";
import type { Device, JsonObject, RuntimeTarget } from "../types";
import { deviceId, runtimeTargetId } from "./hub-format";
import { asRecord, stringOf } from "./json";
import type { MissionDraft } from "./mission-spec";
import { shortProofDigest } from "./proof-hash";
import type { MissionPackageStageStatus, ModelRecord } from "./workbench-types";

export interface MissionPackageStageRequest {
  actor?: string;
  mission_package: JsonObject;
  reason?: string;
  rollout_id?: string;
}

export function buildMissionPackagePlanRequest({
  draft,
  minRuntimeFit = 95,
  readinessContext,
  requireBestRuntime = true,
  requireCapabilityLock = true,
  requireGo = false,
  requireProofSignature = true
}: {
  draft: MissionDraft;
  minRuntimeFit?: number;
  readinessContext: ReadinessQuery;
  requireBestRuntime?: boolean;
  requireCapabilityLock?: boolean;
  requireGo?: boolean;
  requireProofSignature?: boolean;
}): MissionPackagePlanRequest {
  const latencyBudget = optionalNumber(draft.latencyBudgetMs);
  const minThroughput = optionalNumber(draft.throughputMinIps);
  const confidenceThreshold = optionalNumber(draft.confidenceThreshold);
  return {
    ...readinessContext,
    confidence_threshold: confidenceThreshold,
    ddil_mode: draft.ddilMode || undefined,
    fallback_model_id: draft.fallbackModelId || undefined,
    goal: draft.goal || undefined,
    latency_budget_ms: latencyBudget,
    min_throughput_ips: minThroughput,
    mission_yaml: draft.yaml || undefined,
    require_best_runtime: requireBestRuntime,
    require_capability_lock: requireCapabilityLock,
    require_go: requireGo,
    require_proof_signature: requireProofSignature,
    sensor: draft.sensor || undefined,
    slot: draft.slot || readinessContext.slot,
    switch_policy: draft.switchPolicy || undefined,
    min_runtime_fit: minRuntimeFit
  };
}

export function buildMissionPackageManifest({
  device,
  draft,
  model,
  runtime
}: {
  device: Device | undefined;
  draft: MissionDraft;
  model: ModelRecord | undefined;
  runtime: RuntimeTarget | undefined;
}): JsonObject {
  const latencyBudget = optionalNumber(draft.latencyBudgetMs);
  const throughputMin = optionalNumber(draft.throughputMinIps);
  const confidenceThreshold = optionalNumber(draft.confidenceThreshold);
  return {
    schema_version: "temms-edge-mission-package/v1",
    mission: {
      goal: draft.goal,
      sensor: draft.sensor,
      slot: draft.slot || "vision",
      source_yaml: draft.yaml
    },
    selection: {
      package_id: model?.packageId ?? "",
      model_id: model?.id ?? "",
      runtime_target_id: runtime ? runtimeTargetId(runtime) : "",
      device_id: device ? deviceId(device) : ""
    },
    slo: {
      latency_budget_ms: latencyBudget,
      min_throughput_ips: throughputMin
    },
    model_handling: {
      switch_policy: draft.switchPolicy,
      confidence_threshold: confidenceThreshold,
      fallback_model_id: draft.fallbackModelId || "auto"
    },
    ddil: {
      mode: draft.ddilMode,
      replay_requires_readiness: true,
      proof_required: true
    },
    package: {
      includes: [
        "mission_spec",
        "model_artifacts",
        "runtime_contract",
        "sensor_bindings",
        "model_switch_policy",
        "ddil_replay_policy",
        "edge_runtime_proof"
      ]
    }
  };
}

export function buildMissionPackageStageStatus({
  handoff,
  manifest,
  missionReady,
  plan
}: {
  handoff: MissionPackageDownloadHandoff | undefined;
  manifest: JsonObject;
  missionReady: boolean;
  plan: JsonObject | undefined;
}): MissionPackageStageStatus {
  const selection = asRecord(manifest.selection);
  const deploymentIntent = asRecord(manifest.deployment_intent);
  const deploymentCommand = asRecord(deploymentIntent.command);
  const deploymentRequires = asRecord(deploymentIntent.requires);
  const edgeHandoff = asRecord(manifest.edge_handoff);
  const componentDigests = asRecord(manifest.component_digests);
  const proofGate = asRecord(manifest.proof_gate);
  const integrity = asRecord(manifest.integrity);
  const packageIdentity = asRecord(manifest.package_identity);
  const hasEdgePath = Boolean(
    selection.package_id &&
    selection.model_id &&
    selection.device_id &&
    selection.runtime_target_id
  );
  const rolloutIdValue = String(
    deploymentIntent.rollout_id || (hasEdgePath ? missionPackageRolloutId(manifest) : "")
  );
  const packageIdentitySha256 = handoff?.packageIdentitySha256 || stringOf(
    integrity.package_identity_sha256,
    stringOf(packageIdentity.package_identity_sha256, "")
  );
  const gateStatus = stringOf(proofGate.status, plan || handoff ? "planned" : "");
  const hasPackageArtifact = Boolean(plan || handoff);
  const hasDeploymentIntent = Boolean(deploymentIntent.rollout_id && deploymentCommand.path);
  const hasEdgeHandoffDigest = Boolean(
    Object.keys(edgeHandoff).length &&
    componentDigests.edge_handoff_sha256
  );
  const hasMissionContractDigest = Boolean(
    deploymentIntent.mission_contract_sha256 &&
    deploymentRequires.mission_contract_digest === true
  );
  const hasRuntimeCapabilityLockDigest = Boolean(
    deploymentIntent.runtime_capability_lock_sha256 &&
    deploymentRequires.runtime_capability_lock_digest === true
  );
  const hasRuntimePlanDigest = Boolean(
    deploymentIntent.runtime_plan_sha256 &&
    deploymentRequires.runtime_plan_digest === true
  );

  if (hasPackageArtifact && gateStatus === "failed") {
    return {
      detail: "resolve readiness blockers before staging package to edge",
      downloaded: Boolean(handoff),
      gateStatus,
      planned: hasPackageArtifact,
      stageable: false,
      tone: "bad",
      value: "proof gate failed"
    };
  }

  if (hasPackageArtifact && gateStatus !== "passed") {
    const retainedDetail = handoff && packageIdentitySha256
      ? `identity ${shortProofDigest(packageIdentitySha256)}; `
      : handoff
        ? "package identity retained; "
        : "";
    return {
      detail: `${retainedDetail}proof gate ${gateStatus || "pending"}; pass readiness before staging`,
      downloaded: Boolean(handoff),
      gateStatus: gateStatus || "pending",
      planned: hasPackageArtifact,
      stageable: false,
      tone: "warn",
      value: "proof gate pending"
    };
  }

  if (hasPackageArtifact && !hasDeploymentIntent) {
    return {
      detail: "deployment intent missing; replan package with model, runtime, and edge target",
      downloaded: Boolean(handoff),
      gateStatus,
      planned: hasPackageArtifact,
      stageable: false,
      tone: "warn",
      value: "deploy intent missing"
    };
  }

  if (hasPackageArtifact && !hasEdgeHandoffDigest) {
    return {
      detail: "edge handoff digest missing; replan package before staging",
      downloaded: Boolean(handoff),
      gateStatus,
      planned: hasPackageArtifact,
      stageable: false,
      tone: "warn",
      value: "edge handoff missing"
    };
  }

  if (hasPackageArtifact && !hasMissionContractDigest) {
    return {
      detail: "mission contract digest missing; replan package before staging",
      downloaded: Boolean(handoff),
      gateStatus,
      planned: hasPackageArtifact,
      stageable: false,
      tone: "warn",
      value: "mission contract missing"
    };
  }

  if (hasPackageArtifact && !hasRuntimePlanDigest) {
    return {
      detail: "runtime plan digest missing; replan package before staging",
      downloaded: Boolean(handoff),
      gateStatus,
      planned: hasPackageArtifact,
      stageable: false,
      tone: "warn",
      value: "runtime digest missing"
    };
  }

  if (hasPackageArtifact && !hasRuntimeCapabilityLockDigest) {
    return {
      detail: "runtime capability lock digest missing; replan package before staging",
      downloaded: Boolean(handoff),
      gateStatus,
      planned: hasPackageArtifact,
      stageable: false,
      tone: "warn",
      value: "capability lock missing"
    };
  }

  if (handoff) {
    return {
      detail: `${packageIdentitySha256 ? `identity ${shortProofDigest(packageIdentitySha256)}` : "package identity retained"}; deploy ${rolloutIdValue || "intent retained"}`,
      downloaded: true,
      gateStatus,
      planned: true,
      stageable: true,
      tone: "good",
      value: "downloaded"
    };
  }

  if (plan) {
    return {
      detail: `${packageIdentitySha256 ? `identity ${shortProofDigest(packageIdentitySha256)}` : rolloutIdValue || "deployment intent retained"}; proof gate ${gateStatus}`,
      downloaded: false,
      gateStatus,
      planned: true,
      stageable: true,
      tone: "good",
      value: "package planned"
    };
  }

  if (!missionReady) {
    return {
      detail: "define mission goal or YAML before package planning",
      downloaded: false,
      gateStatus,
      planned: false,
      stageable: false,
      tone: "warn",
      value: "mission pending"
    };
  }

  if (hasEdgePath) {
    return {
      detail: `${rolloutIdValue}; plan package to hash mission handoff`,
      downloaded: false,
      gateStatus,
      planned: false,
      stageable: false,
      tone: "warn",
      value: "draft handoff"
    };
  }

  return {
    detail: "select model, runtime, and edge target",
    downloaded: false,
    gateStatus,
    planned: false,
    stageable: false,
    tone: "warn",
    value: "path pending"
  };
}

export function missionPackageRolloutId(manifest: JsonObject): string {
  const selection = asRecord(manifest.selection);
  const parts = [
    stringOf(selection.model_id, ""),
    stringOf(selection.runtime_target_id, ""),
    stringOf(selection.device_id, "")
  ]
    .map((part) => part.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, ""))
    .filter(Boolean)
    .join("-");
  return `rollout-${parts || "mission-package"}`;
}

export function missionPackageStageBlocker({
  manifest,
  stageStatus
}: {
  manifest: JsonObject;
  stageStatus: MissionPackageStageStatus;
}): { detail: string; title: string } | undefined {
  const deploymentIntent = asRecord(manifest.deployment_intent);
  if (!deploymentIntent.rollout_id) {
    return {
      title: "Plan package first",
      detail: "Stage rollout uses the mission package deployment intent."
    };
  }
  if (!stageStatus.stageable) {
    return {
      title: "Proof gate blocks staging",
      detail:
        stageStatus.gateStatus === "failed"
          ? "Refresh package planning after resolving runtime readiness blockers."
          : "Run readiness/proof planning until the package proof gate passes."
    };
  }
  return undefined;
}

export function buildMissionPackageStageRequest(manifest: JsonObject): MissionPackageStageRequest {
  return {
    actor: "operator:mission-package-workbench",
    mission_package: manifest,
    reason: "mission package deployment handoff",
    rollout_id: missionPackageRolloutId(manifest)
  };
}

function optionalNumber(value: string): number | undefined {
  const trimmedValue = value.trim();
  if (!trimmedValue) return undefined;
  const numericValue = Number(trimmedValue);
  return Number.isFinite(numericValue) ? numericValue : undefined;
}
