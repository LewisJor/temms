import type {
  Benchmark,
  CompatibilityMatrix,
  DeploymentReadiness,
  DeploymentReadinessCommand,
  Device,
  EvidenceBundleRecord,
  EvidenceSummary,
  HubPackage,
  HubSnapshot,
  JsonObject,
  MissionReplay,
  Rollout,
  RolloutPlan,
  RuntimeTarget,
  RuntimeValidation
} from "./types";

const HUB_API = "/v1/hub";
const CONTROL_API = "/v1/control";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly payload: unknown
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function parse(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function request<T>(
  base: string,
  method: "GET" | "POST",
  path: string,
  token: string,
  body?: unknown
): Promise<T> {
  const headers: HeadersInit = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;

  const response = await fetch(`${base}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const payload = await parse(response);
  if (!response.ok) {
    const detail =
      typeof payload === "object" && payload !== null && "detail" in payload
        ? String((payload as { detail?: unknown }).detail)
        : response.statusText;
    throw new ApiError(response.status, detail, payload);
  }
  return payload as T;
}

const hubRequest = <T>(method: "GET" | "POST", path: string, token: string, body?: unknown): Promise<T> =>
  request<T>(HUB_API, method, path, token, body);

const controlRequest = <T>(method: "GET" | "POST", path: string, token: string, body?: unknown): Promise<T> =>
  request<T>(CONTROL_API, method, path, token, body);

const fallback = <T extends object>(value: T): T => value;

export interface ReadinessQuery {
  package_id?: string;
  model_id?: string;
  device_id?: string;
  runtime_target_id?: string;
  slot?: string;
}

export interface EdgeProofQuery extends ReadinessQuery {
  source_action?: "readiness" | "edge-runtime-mission";
  require_go?: boolean;
  min_runtime_fit?: number;
  require_best_runtime?: boolean;
  require_capability_lock?: boolean;
}

export interface MissionPackagePlanRequest extends ReadinessQuery {
  confidence_threshold?: number;
  ddil_mode?: string;
  fallback_model_id?: string;
  goal?: string;
  latency_budget_ms?: number;
  min_throughput_ips?: number;
  mission_yaml?: string;
  require_best_runtime?: boolean;
  require_capability_lock?: boolean;
  require_go?: boolean;
  require_proof_signature?: boolean;
  sensor?: string;
  switch_policy?: string;
  min_runtime_fit?: number;
}

export interface EdgeProofArtifact {
  fileName: string;
  handoff: EdgeProofDownloadHandoff;
  payload: JsonObject;
}

export interface MissionPackageArtifact {
  fileName: string;
  handoff: MissionPackageDownloadHandoff;
  payload: JsonObject;
}

export interface EdgeProofDownloadHandoff {
  attestation: string;
  edgeExecutionManifestSha256: string;
  fileName: string;
  gateStatus: string;
  keyFingerprint: string;
  payloadSha256: string;
  runtimeDecisionTraceSha256: string;
  runtimeWorkbenchSha256: string;
}

export interface MissionPackageDownloadHandoff {
  deploymentIntentSha256: string;
  edgeHandoffSha256: string;
  fileName: string;
  missionContractSha256: string;
  missionSha256: string;
  packageIdentitySha256: string;
  payloadSha256: string;
  runtimeCapabilityLockSha256: string;
  runtimePlanSha256: string;
}

function withQuery<T extends object>(path: string, params: T): string {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
  });
  const text = query.toString();
  return text ? `${path}?${text}` : path;
}

export function loadReadiness(
  token: string,
  params: ReadinessQuery = {}
): Promise<DeploymentReadiness> {
  return hubRequest<DeploymentReadiness>("GET", withQuery("/readiness", params), token);
}

export function loadEdgeRuntimeProof(
  token: string,
  params: EdgeProofQuery
): Promise<JsonObject> {
  return hubRequest<JsonObject>("GET", withQuery("/edge-runtime-proof", params), token);
}

export function planMissionPackage(
  token: string,
  payload: MissionPackagePlanRequest
): Promise<JsonObject> {
  return hubRequest<JsonObject>("POST", "/mission-package/plan", token, payload);
}

export function stageMissionPackage(
  token: string,
  payload: {
    actor?: string;
    mission_package: JsonObject;
    reason?: string;
    rollout_id?: string;
  }
): Promise<JsonObject> {
  return hubRequest<JsonObject>("POST", "/mission-package/stage", token, payload);
}

export async function downloadMissionPackage(
  token: string,
  body: MissionPackagePlanRequest
): Promise<MissionPackageArtifact> {
  const headers: HeadersInit = { Accept: "application/json", "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;

  const response = await fetch(`${HUB_API}/mission-package/download`, {
    method: "POST",
    headers,
    body: JSON.stringify(body)
  });
  const text = await response.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = text;
  }
  if (!response.ok) {
    const detail =
      typeof payload === "object" && payload !== null && "detail" in payload
        ? String((payload as { detail?: unknown }).detail)
        : response.statusText;
    throw new ApiError(response.status, detail, payload);
  }
  const fileName =
    filenameFromDisposition(response.headers.get("Content-Disposition")) ||
    response.headers.get("X-TEMMS-Mission-Package-Filename") ||
    "temms-edge-mission-package.json";
  return {
    fileName,
    handoff: missionPackageDownloadHandoff(response.headers, fileName),
    payload: payload as JsonObject
  };
}

export async function downloadEdgeRuntimeProof(
  token: string,
  params: EdgeProofQuery
): Promise<EdgeProofArtifact> {
  const headers: HeadersInit = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;

  const response = await fetch(`${HUB_API}${withQuery("/edge-runtime-proof/download", params)}`, {
    method: "GET",
    headers
  });
  const text = await response.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = text;
  }
  if (!response.ok) {
    const detail =
      typeof payload === "object" && payload !== null && "detail" in payload
        ? String((payload as { detail?: unknown }).detail)
        : response.statusText;
    throw new ApiError(response.status, detail, payload);
  }
  const fileName =
    filenameFromDisposition(response.headers.get("Content-Disposition")) ||
    response.headers.get("X-TEMMS-Edge-Proof-Filename") ||
    "temms-edge-runtime-proof.json";
  return {
    fileName,
    handoff: edgeProofDownloadHandoff(response.headers, fileName),
    payload: payload as JsonObject
  };
}

function missionPackageDownloadHandoff(
  headers: Headers,
  fileName: string
): MissionPackageDownloadHandoff {
  return {
    deploymentIntentSha256:
      headers.get("X-TEMMS-Mission-Package-Deployment-Intent-SHA256") || "",
    edgeHandoffSha256:
      headers.get("X-TEMMS-Mission-Package-Edge-Handoff-SHA256") || "",
    fileName,
    missionContractSha256:
      headers.get("X-TEMMS-Mission-Package-Mission-Contract-SHA256") || "",
    missionSha256: headers.get("X-TEMMS-Mission-Package-Mission-SHA256") || "",
    packageIdentitySha256:
      headers.get("X-TEMMS-Mission-Package-Identity-SHA256") || "",
    payloadSha256: headers.get("X-TEMMS-Mission-Package-SHA256") || "",
    runtimeCapabilityLockSha256:
      headers.get("X-TEMMS-Mission-Package-Runtime-Capability-Lock-SHA256") || "",
    runtimePlanSha256:
      headers.get("X-TEMMS-Mission-Package-Runtime-Plan-SHA256") || ""
  };
}

function edgeProofDownloadHandoff(headers: Headers, fileName: string): EdgeProofDownloadHandoff {
  return {
    attestation: headers.get("X-TEMMS-Edge-Proof-Attestation") || "",
    edgeExecutionManifestSha256:
      headers.get("X-TEMMS-Edge-Proof-Execution-Manifest-SHA256") || "",
    fileName,
    gateStatus: headers.get("X-TEMMS-Edge-Proof-Gate-Status") || "",
    keyFingerprint: headers.get("X-TEMMS-Edge-Proof-Key-Fingerprint") || "",
    payloadSha256: headers.get("X-TEMMS-Edge-Proof-SHA256") || "",
    runtimeDecisionTraceSha256:
      headers.get("X-TEMMS-Edge-Proof-Runtime-Decision-Trace-SHA256") || "",
    runtimeWorkbenchSha256:
      headers.get("X-TEMMS-Edge-Proof-Runtime-Workbench-SHA256") || ""
  };
}

function filenameFromDisposition(value: string | null): string | undefined {
  if (!value) return undefined;
  const quoted = value.match(/filename="([^"]+)"/);
  if (quoted?.[1]) return quoted[1];
  const unquoted = value.match(/filename=([^;]+)/);
  return unquoted?.[1]?.trim();
}

export function executeReadinessCommand(
  command: DeploymentReadinessCommand,
  token: string
): Promise<JsonObject> {
  if (command.requires_edge_execution) {
    throw new Error("Run this readiness command on the selected edge node.");
  }
  const method = command.method?.toUpperCase();
  const path = command.path?.trim();
  if (method !== "GET" && method !== "POST") {
    throw new Error(`Unsupported readiness command method: ${command.method || "missing"}`);
  }
  if (!path || (!path.startsWith("/v1/hub/") && !path.startsWith("/v1/control/"))) {
    throw new Error(`Unsupported readiness command path: ${path || "missing"}`);
  }
  return request<JsonObject>("", method, path, token, method === "GET" ? undefined : command.body);
}

export async function loadSnapshot(token: string): Promise<HubSnapshot> {
  const [
    devices,
    packagesPayload,
    runtimeTargets,
    rollouts,
    rolloutPlans,
    runtimeValidations,
    benchmarks,
    evidence,
    evidenceSummary,
    missionReplay,
    readiness,
    compatibilityMatrix
  ] = await Promise.all([
    hubRequest<{ devices: Device[] }>("GET", "/devices", token).catch(() => fallback({ devices: [] })),
    hubRequest<{ packages: HubPackage[] }>("GET", "/packages", token).catch(() => fallback({ packages: [] })),
    hubRequest<{ runtime_targets: RuntimeTarget[] }>("GET", "/runtime-targets", token).catch(() =>
      fallback({ runtime_targets: [] })
    ),
    hubRequest<{ rollouts: Rollout[] }>("GET", "/rollouts", token).catch(() => fallback({ rollouts: [] })),
    hubRequest<{ rollout_plans: RolloutPlan[] }>("GET", "/rollout-plans", token).catch(() =>
      fallback({ rollout_plans: [] })
    ),
    hubRequest<{ runtime_validations: RuntimeValidation[] }>(
      "GET",
      "/runtime-targets/validations",
      token
    ).catch(() => fallback({ runtime_validations: [] })),
    hubRequest<{ benchmarks: Benchmark[] }>("GET", "/benchmarks", token).catch(() =>
      fallback({ benchmarks: [] })
    ),
    hubRequest<{ evidence_bundles: EvidenceBundleRecord[] }>("GET", "/evidence", token).catch(() =>
      fallback({ evidence_bundles: [] })
    ),
    hubRequest<EvidenceSummary>("POST", "/evidence/export", token, {
      summary: true,
      summary_limit: 5,
      decision_limit: 50
    }).catch(() => undefined),
    hubRequest<MissionReplay>("POST", "/evidence/export", token, {
      replay: true,
      replay_limit: 50
    }).catch(() => undefined),
    loadReadiness(token).catch(() => undefined),
    hubRequest<CompatibilityMatrix>("POST", "/compatibility/matrix", token, {
      include_device_inventory: true
    }).catch(() => undefined)
  ]);

  return {
    devices: devices.devices,
    packages: packagesPayload.packages,
    runtimeTargets: runtimeTargets.runtime_targets,
    rollouts: rollouts.rollouts,
    rolloutPlans: rolloutPlans.rollout_plans,
    runtimeValidations: runtimeValidations.runtime_validations,
    benchmarks: benchmarks.benchmarks,
    evidenceBundles: evidence.evidence_bundles,
    evidenceSummary,
    missionReplay,
    readiness,
    compatibilityMatrix
  };
}

export const hubApi = {
  enrollDevice: (body: JsonObject, token: string) => hubRequest<JsonObject>("POST", "/devices/enroll", token, body),
  registerPackage: (body: JsonObject, token: string) =>
    hubRequest<JsonObject>("POST", "/packages/register", token, body),
  promotePackage: (packageId: string, body: JsonObject, token: string) =>
    hubRequest<JsonObject>("POST", `/packages/${encodeURIComponent(packageId)}/promote`, token, body),
  registerRuntimeTarget: (body: JsonObject, token: string) =>
    hubRequest<JsonObject>("POST", "/runtime-targets", token, body),
  previewCompatibility: (body: JsonObject, token: string) =>
    hubRequest<JsonObject>("POST", "/compatibility/preview", token, body),
  compatibilityMatrix: (body: JsonObject, token: string) =>
    hubRequest<JsonObject>("POST", "/compatibility/matrix", token, body),
  assignRollout: (body: JsonObject, token: string) => hubRequest<JsonObject>("POST", "/rollouts", token, body),
  approveRollout: (rolloutId: string, body: JsonObject, token: string) =>
    hubRequest<JsonObject>("POST", `/rollouts/${encodeURIComponent(rolloutId)}/approve`, token, body),
  applyRollout: (rolloutId: string, body: JsonObject, token: string) =>
    hubRequest<JsonObject>("POST", `/rollouts/${encodeURIComponent(rolloutId)}/apply`, token, body),
  rollbackRollout: (rolloutId: string, body: JsonObject, token: string) =>
    hubRequest<JsonObject>("POST", `/rollouts/${encodeURIComponent(rolloutId)}/rollback`, token, body),
  createRolloutPlan: (body: JsonObject, token: string) =>
    hubRequest<JsonObject>("POST", "/rollout-plans", token, body),
  advanceRolloutPlan: (planId: string, body: JsonObject, token: string) =>
    hubRequest<JsonObject>("POST", `/rollout-plans/${encodeURIComponent(planId)}/advance`, token, body),
  pauseRolloutPlan: (planId: string, body: JsonObject, token: string) =>
    hubRequest<JsonObject>("POST", `/rollout-plans/${encodeURIComponent(planId)}/pause`, token, body),
  resumeRolloutPlan: (planId: string, body: JsonObject, token: string) =>
    hubRequest<JsonObject>("POST", `/rollout-plans/${encodeURIComponent(planId)}/resume`, token, body),
  exportEvidence: (body: JsonObject, token: string) =>
    hubRequest<JsonObject>("POST", "/evidence/export", token, body),
  exportAirgap: (body: JsonObject, token: string) => hubRequest<JsonObject>("POST", "/airgap/export", token, body),
  importAirgap: (body: JsonObject, token: string) => hubRequest<JsonObject>("POST", "/airgap/import", token, body)
};

export const controlApi = {
  setOffline: (token: string) => controlRequest<JsonObject>("POST", "/offline", token),
  setOnline: (token: string) => controlRequest<JsonObject>("POST", "/online", token),
  previewSync: (token: string) => controlRequest<JsonObject>("GET", "/sync/preview", token),
  quarantineBlocked: (body: JsonObject, token: string) =>
    controlRequest<JsonObject>("POST", "/sync/quarantine-blocked", token, body),
  retargetRuntime: (body: JsonObject, token: string) =>
    controlRequest<JsonObject>("POST", "/sync/retarget-runtime", token, body),
  acknowledgeDeadLetters: (body: JsonObject, token: string) =>
    controlRequest<JsonObject>("POST", "/sync/acknowledge-dead-letters", token, body),
  requeueDeadLetters: (body: JsonObject, token: string) =>
    controlRequest<JsonObject>("POST", "/sync/requeue-dead-letters", token, body),
  syncPending: (token: string) => controlRequest<JsonObject>("POST", "/sync", token),
  requestDeploy: (body: JsonObject, token: string) => controlRequest<JsonObject>("POST", "/deploy", token, body),
  updateConditions: (body: JsonObject, token: string) =>
    controlRequest<JsonObject>("POST", "/conditions", token, body)
};
