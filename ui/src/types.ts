export type JsonObject = Record<string, unknown>;

export interface Device {
  device_id?: string;
  id?: string;
  profile?: string;
  status?: string;
  labels?: Record<string, string>;
  inventory?: JsonObject;
  updated_at?: string;
  last_seen?: string;
}

export interface Promotion {
  state?: string;
  actor?: string;
  reason?: string;
  updated_at?: string;
  history?: JsonObject[];
}

export interface HubPackage {
  package_id?: string;
  id?: string;
  name?: string;
  version?: string;
  path?: string;
  package_path?: string;
  device_profiles?: string[];
  metadata?: JsonObject;
  promotion?: Promotion;
  created_at?: string;
  updated_at?: string;
}

export interface RuntimeTarget {
  runtime_target_id?: string;
  id?: string;
  name?: string;
  image?: string;
  os?: string;
  arch?: string;
  device_profiles?: string[];
  runtimes?: JsonObject;
  accelerators?: JsonObject;
  runtime_lane?: JsonObject;
  runtime_constraints?: JsonObject;
  updated_at?: string;
}

export interface RolloutApproval {
  state?: string;
  approved?: boolean;
  actor?: string;
  reason?: string;
  updated_at?: string;
}

export interface Rollout {
  rollout_id?: string;
  id?: string;
  device_id?: string;
  package_id?: string;
  model_id?: string;
  slot?: string;
  state?: string;
  runtime_target_id?: string;
  approval_required?: boolean;
  approval?: RolloutApproval;
  created_at?: string;
  updated_at?: string;
}

export interface RolloutPlanTarget {
  device_id?: string;
  rollout_id?: string;
  state?: string;
  assigned_at?: string;
  updated_at?: string;
  last_actor?: string;
  blockers?: string[];
  compatible?: boolean;
  runtime_validation_ready?: boolean;
  runtime_validation?: JsonObject | null;
}

export interface RolloutPlan {
  plan_id?: string;
  package_id?: string;
  model_id?: string;
  slot?: string;
  state?: string;
  batch_size?: number;
  runtime_target_id?: string;
  require_runtime_validation?: boolean;
  require_approval?: boolean;
  targets?: RolloutPlanTarget[];
  counts?: JsonObject;
  current_batch?: number;
  updated_at?: string;
}

export interface RuntimeValidation {
  validation_id?: string;
  package_id?: string;
  runtime_target_id?: string;
  actor?: string;
  result?: JsonObject;
  created_at?: string;
}

export interface Benchmark {
  benchmark_id?: string;
  device_id?: string;
  package_id?: string;
  runtime_target_id?: string;
  model_id?: string;
  result?: JsonObject;
  created_at?: string;
}

export interface EvidenceBundleRecord {
  evidence_id?: string;
  device_id?: string;
  schema_version?: string;
  integrity?: JsonObject;
  summary?: JsonObject;
  created_at?: string;
}

export interface EvidenceSummary {
  headline?: string;
  runtime?: JsonObject;
  counts?: JsonObject;
  trust?: JsonObject;
  active_slots?: JsonObject[];
  timeline?: JsonObject[];
}

export interface MissionReplayPhase {
  phase?: string;
  label?: string;
  status?: string;
  summary?: string;
  evidence_refs?: string[];
}

export interface MissionReplayOutcome {
  completed_phases?: number;
  incomplete_phases?: string[];
  counts?: JsonObject;
  trust?: JsonObject;
  active_slots?: JsonObject[];
}

export interface MissionReplay {
  schema_version?: string;
  exported_at?: string;
  headline?: string;
  outcome?: MissionReplayOutcome;
  phases?: MissionReplayPhase[];
  incidents?: JsonObject;
  events?: JsonObject[];
  summary?: EvidenceSummary;
}

export type DeploymentReadinessStatus = "go" | "attention" | "blocked";

export interface DeploymentReadinessCommand {
  method?: string;
  path?: string;
  body?: JsonObject;
  requires_edge_execution?: boolean;
  edge_command?: string[];
  edge_command_text?: string;
  edge_command_note?: string;
  operator_command?: string[];
  operator_command_text?: string;
  operator_command_note?: string;
}

export interface DeploymentReadinessAction {
  action_id?: string;
  label?: string;
  kind?: string;
  gate_id?: string;
  refs?: JsonObject;
  command?: DeploymentReadinessCommand;
}

export interface DeploymentReadinessGate {
  gate_id?: string;
  label?: string;
  status?: DeploymentReadinessStatus | string;
  state?: string;
  detail?: string;
  refs?: JsonObject;
  actions?: DeploymentReadinessAction[];
}

export interface DeploymentReadiness {
  schema_version?: string;
  status?: DeploymentReadinessStatus | string;
  headline?: string;
  detail?: string;
  next_action?: string;
  checked_at?: string;
  selection?: JsonObject;
  summary?: JsonObject;
  gates?: DeploymentReadinessGate[];
  actions?: DeploymentReadinessAction[];
  runtime_fit?: JsonObject;
  runtime_decision?: JsonObject;
  runtime_workbench?: JsonObject;
  edge_execution_contract?: JsonObject;
  production_admission?: JsonObject;
  edge_runtime_mission?: JsonObject;
}

export interface EdgeRecommendation {
  rank?: number | null;
  score?: number;
  decision?: string;
  confidence?: string;
  package_id?: string;
  model_id?: string | null;
  device_id?: string;
  runtime_target_id?: string | null;
  runtime_mode?: string;
  primary_reason?: string;
  required_actions?: string[];
  warnings?: string[];
  fit?: JsonObject;
  optimization?: JsonObject;
  runtime_lane?: JsonObject;
  artifact_lane?: JsonObject;
  runtime_fit?: JsonObject;
}

export interface CompatibilityMatrix {
  schema_version?: string;
  generated_at?: string;
  counts?: JsonObject;
  dimensions?: JsonObject;
  recommendations?: EdgeRecommendation[];
  cells?: JsonObject[];
}

export interface HubSnapshot {
  devices: Device[];
  packages: HubPackage[];
  runtimeTargets: RuntimeTarget[];
  rollouts: Rollout[];
  rolloutPlans: RolloutPlan[];
  runtimeValidations: RuntimeValidation[];
  benchmarks: Benchmark[];
  evidenceBundles: EvidenceBundleRecord[];
  evidenceSummary?: EvidenceSummary;
  missionReplay?: MissionReplay;
  readiness?: DeploymentReadiness;
  compatibilityMatrix?: CompatibilityMatrix;
}

export type EvidenceExportMode = "summary" | "replay" | "full";

export interface Toast {
  tone: "success" | "error" | "info";
  title: string;
  detail?: string;
}

export interface Preview {
  title: string;
  payload: unknown;
}
