import type { DeploymentReadinessCommand, JsonObject, RuntimeTarget } from "../types";

export interface ModelRecord {
  id: string;
  name: string;
  version: string;
  format: string;
  packageId: string;
  packageName: string;
  packageVersion: string;
  packagePromotion: string;
  profiles: string[];
  runtimes: string[];
  providers: string[];
  latencyMs?: number;
  throughputIps?: number;
  maxLatencyP95Ms?: number;
  minThroughputIps?: number;
  maxBenchmarkAgeSeconds?: number;
  minMemoryAvailableMb?: number;
  minStorageAvailableMb?: number;
  maxTemperatureC?: number;
  minBatteryPercent?: number;
  requiredPowerSource?: string;
  artifactSizeMb?: number;
  benchmarkDeviceId?: string;
  benchmarkRuntimeId?: string;
  benchmarkedAt?: string;
  signed: boolean;
  source: string;
  updatedAt?: string;
}

export type GateTone = "good" | "warn" | "bad" | "neutral";

export interface EdgeRuntimeFit {
  label: string;
  detail: string;
  tone: GateTone;
  failures: string[];
}

export interface RuntimeFitDisplay extends EdgeRuntimeFit {
  tileDetail: string;
}

export interface EdgeMissionMetric {
  label: string;
  value: string;
  detail: string;
  tone: GateTone;
}

export interface EdgeRuntimeMission {
  headline: string;
  detail: string;
  tone: GateTone;
  path: string;
  metrics: EdgeMissionMetric[];
  focus: string[];
}

export interface EdgeProofWorkflow {
  status: string;
  detail: string;
  tone: GateTone;
  proofPath: string;
  gatePolicy: string;
  attestation: string;
  capabilityLock: string;
  capabilityLockDetail: string;
  capabilityLockTone: GateTone;
  runtimeFit: string;
  generateCommand: string;
  verifyCommand: string;
  verifyJsonCommand: string;
  missing: string[];
}

export interface EdgeProofTraceStatus {
  commandCount: number;
  detail: string;
  errors: string[];
  rowCount: number;
  schema: string;
  status: "consistent" | "mismatch" | "missing" | "not_generated" | "stale";
  tone: GateTone;
  value: string;
}

export interface EdgeProofComponentDigestStatus {
  detail: string;
  digestCount: number;
  digests: Array<{ key: string; label: string; value: string }>;
  errors: string[];
  schema: string;
  status: "consistent" | "mismatch" | "retained" | "missing" | "not_generated" | "stale" | "verifying";
  tone: GateTone;
  value: string;
}

export interface RuntimeWorkbenchRow {
  actionKind: string;
  actionLabel: string;
  actionRequiresEdge: boolean;
  benchmark: string;
  best: boolean;
  capabilitySha256: string;
  compatible: boolean;
  detail: string;
  inventory: string;
  lane: string;
  penalties: string[];
  rank?: number;
  reasons: string[];
  remediation: JsonObject;
  score?: number;
  selected: boolean;
  status: string;
  target: RuntimeTarget;
  targetId: string;
  tone: GateTone;
  traceMetrics: RuntimeWorkbenchTraceMetric[];
  validated: boolean;
}

export interface RuntimeWorkbenchTraceMetric {
  detail: string;
  label: string;
  tone: GateTone;
  value: string;
}

export interface RuntimeRepairProof {
  actor: string;
  benchmarkId: string;
  bestRuntime: string;
  blockedTargetCount?: number;
  capabilityLockStatus: string;
  capabilitySha256: string;
  detail: string;
  eligibleTargetCount?: number;
  headline: string;
  occurredAt: string;
  operation?: Record<string, unknown>;
  previousRuntime: string;
  proofStatus: string;
  reason: string;
  runtimeFitScore?: number;
  selectedIsBest?: boolean;
  selectedRuntime: string;
  source: "pending" | "replayed" | "mission";
  status: "repair_available" | "proved";
  targetCount?: number;
  targetSelectionStatus: string;
  tone: GateTone;
  validationId: string;
  workbenchSchema: string;
}

export interface RuntimeRemediationContext {
  packageId: string;
  modelId: string;
  deviceId: string;
  slot: string;
}

export interface RuntimeRemediationCommand {
  action: string;
  label: string;
  command: string;
  note: string;
  edgeRun: boolean;
}

export type WorkflowTarget = "model" | "deployment" | "plans" | "rollouts" | "ddil" | "evidence" | "assets";
export type HubStage = "mission" | "model" | "runtime" | "handling" | "package" | "deploy" | "field";

export interface HubStageItem {
  id: HubStage;
  label: string;
  value: string;
  detail: string;
  decision: string;
  outcome: string;
  tone: GateTone;
}

export interface HubStageRunbookAction {
  label: string;
  detail: string;
  disabled?: boolean;
  icon: "activity" | "arrow" | "cpu" | "download" | "package" | "refresh" | "rocket" | "shield";
  onClick: () => void;
  variant?: "primary" | "secondary";
}

export interface HubStageRunbook {
  objective: string;
  ready: string;
  risk: string;
  status: string;
  tone: GateTone;
  actions: HubStageRunbookAction[];
}

export interface MissionWorkflowSignal {
  label: string;
  value: string;
  detail: string;
  tone: GateTone;
}

export interface MissionPackageStageStatus {
  detail: string;
  downloaded: boolean;
  gateStatus: string;
  planned: boolean;
  stageable: boolean;
  tone: GateTone;
  value: string;
}

export interface ReadinessGateAction {
  id: string;
  label: string;
  kind: string;
  gateId: string;
  refs?: JsonObject;
  command?: DeploymentReadinessCommand;
}

export interface ReadinessGate {
  label: string;
  state: string;
  detail: string;
  tone: GateTone;
  actions?: ReadinessGateAction[];
}

export interface ReadinessVerdict {
  label: string;
  headline: string;
  detail: string;
  nextAction: string;
  tone: GateTone;
  gates: ReadinessGate[];
}
