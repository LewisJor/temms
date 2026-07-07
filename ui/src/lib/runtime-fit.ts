import type { Benchmark, DeploymentReadiness, Device, HubPackage, JsonObject, RuntimeTarget, RuntimeValidation } from "../types";
import { compactDate, deviceId, packageId, runtimeTargetId } from "./hub-format";
import { asRecord, latestByTime, numberOf, stringOf, stringsOf } from "./json";
import type { EdgeRuntimeFit, GateTone, ModelRecord, RuntimeFitDisplay } from "./workbench-types";

const DEFAULT_BENCHMARK_STALE_SECONDS = 86400;

export function modelsForPackage(pkg: HubPackage): ModelRecord[] {
  const pkgId = packageId(pkg);
  const metadata = asRecord(pkg.metadata);
  const validation = asRecord(metadata.validation);
  const models = Array.isArray(metadata.models) ? metadata.models : [];
  return models.map((rawModel, index) => {
    const model = asRecord(rawModel);
    const benchmark = asRecord(model.benchmark);
    const constraints = asRecord(model.runtime_constraints);
    const modelMetadata = asRecord(model.metadata);
    const performanceSlo = asRecord(model.performance_slo ?? model.slo ?? modelMetadata.performance_slo);
    const resourceRequirements = asRecord(
      model.resource_requirements ??
        model.resources ??
        modelMetadata.resource_requirements ??
        modelMetadata.resources ??
        asRecord(constraints.resource_requirements)
    );
    const provenance = asRecord(model.provenance);
    const name = stringOf(model.name, `model-${index + 1}`);
    const artifactSizeBytes = numberOf(model.size_bytes);
    return {
      id: stringOf(model.id, `${pkgId}:${name}`),
      name,
      version: stringOf(model.version, pkg.version ?? "-"),
      format: stringOf(model.format, "model"),
      packageId: pkgId,
      packageName: pkg.name ?? pkgId,
      packageVersion: pkg.version ?? "-",
      packagePromotion: pkg.promotion?.state ?? "candidate",
      profiles: stringsOf(constraints.device_profiles ?? metadata.compatibility),
      runtimes: stringsOf(constraints.runtimes),
      providers: stringsOf(constraints.providers),
      latencyMs: numberOf(benchmark.latency_ms_p95),
      maxLatencyP95Ms: numberOf(
        performanceSlo.max_latency_ms_p95 ??
          performanceSlo.latency_ms_p95_max ??
          performanceSlo.p95_latency_ms_max
      ),
      minThroughputIps: numberOf(
        performanceSlo.min_throughput_ips ??
          performanceSlo.throughput_ips_min ??
          performanceSlo.min_inferences_per_second
      ),
      maxBenchmarkAgeSeconds: numberOf(
        performanceSlo.max_benchmark_age_seconds ??
          performanceSlo.benchmark_stale_after_seconds ??
          performanceSlo.benchmark_freshness_seconds ??
          performanceSlo.max_age_seconds
      ),
      minMemoryAvailableMb: numberOf(
        resourceRequirements.min_memory_available_mb ??
          resourceRequirements.min_available_memory_mb ??
          resourceRequirements.memory_available_mb_min ??
          resourceRequirements.min_memory_mb ??
          resourceRequirements.min_ram_mb ??
          resourceRequirements.peak_memory_mb
      ),
      minStorageAvailableMb: numberOf(
        resourceRequirements.min_storage_available_mb ??
          resourceRequirements.min_available_storage_mb ??
          resourceRequirements.storage_available_mb_min ??
          resourceRequirements.min_disk_available_mb ??
          resourceRequirements.min_storage_mb ??
          resourceRequirements.min_disk_mb
      ),
      maxTemperatureC: numberOf(
        resourceRequirements.max_temperature_c ??
          resourceRequirements.max_cpu_temp_c ??
          resourceRequirements.max_thermal_c ??
          resourceRequirements.temperature_c_max
      ),
      minBatteryPercent: numberOf(
        resourceRequirements.min_battery_percent ??
          resourceRequirements.battery_percent_min ??
          resourceRequirements.min_battery_pct
      ),
      requiredPowerSource: stringOf(
        resourceRequirements.required_power_source ?? resourceRequirements.power_source,
        ""
      ),
      artifactSizeMb:
        artifactSizeBytes === undefined ? undefined : Math.round((artifactSizeBytes / (1024 * 1024)) * 1000) / 1000,
      signed: validation.signature_verified === true,
      source: stringOf(provenance.source, stringOf(metadata.source_registry, "package")),
      updatedAt: pkg.updated_at ?? pkg.created_at
    };
  });
}

export function withBenchmarkEvidence(models: ModelRecord[], benchmarks: Benchmark[]): ModelRecord[] {
  return models.map((model) => {
    const benchmark = benchmarkForModel(model, benchmarks);
    if (!benchmark) return model;
    const result = asRecord(benchmark.result);
    const latency = asRecord(result.latency_ms);
    const throughput = asRecord(result.throughput);
    return {
      ...model,
      latencyMs: numberOf(latency.p95) ?? model.latencyMs,
      throughputIps: numberOf(throughput.inferences_per_second),
      benchmarkDeviceId: benchmark.device_id,
      benchmarkRuntimeId: benchmark.runtime_target_id,
      benchmarkedAt: benchmark.created_at
    };
  });
}

function benchmarkForModel(model: ModelRecord, benchmarks: Benchmark[]): Benchmark | undefined {
  const matching = benchmarks.filter(
    (benchmark) => benchmark.package_id === model.packageId && benchmark.model_id === model.id
  );
  return latestByTime(matching);
}

export function runtimeValidationForModel(
  model: ModelRecord,
  runtime: RuntimeTarget | undefined,
  validations: RuntimeValidation[]
): RuntimeValidation | undefined {
  const runtimeId = runtime ? runtimeTargetId(runtime) : "";
  return latestByTime(
    validations.filter((validation) => {
      const result = asRecord(validation.result);
      return (
        validation.package_id === model.packageId &&
        (!runtimeId || validation.runtime_target_id === runtimeId) &&
        result.ok === true &&
        result.dry_run !== true
      );
    })
  );
}

export function formatBenchmark(model: ModelRecord): string {
  if (!model.latencyMs && !model.throughputIps) return "no benchmark";
  const parts = [];
  if (model.latencyMs) parts.push(`${formatMetricNumber(model.latencyMs)} ms p95`);
  if (model.throughputIps) parts.push(`${formatThroughput(model.throughputIps)} ips`);
  return parts.join(" / ");
}

export function formatBenchmarkTarget(model: ModelRecord): string {
  if (!model.benchmarkDeviceId && !model.benchmarkRuntimeId) return "no benchmark target";
  return [model.benchmarkDeviceId, model.benchmarkRuntimeId].filter(Boolean).join(" / ");
}

export function formatBenchmarkFreshness(model: ModelRecord): string {
  const freshness = benchmarkFreshness(model);
  if (!freshness.createdAt) return "not recorded";
  if (freshness.ageSeconds === undefined) return "timestamp invalid";
  return `${formatAge(freshness.ageSeconds)} old / ${formatAge(freshness.staleAfterSeconds)} budget`;
}

export function formatPerformanceSlo(model: ModelRecord): string {
  const parts = [];
  if (model.maxLatencyP95Ms) parts.push(`p95 <= ${formatMetricNumber(model.maxLatencyP95Ms)} ms`);
  if (model.minThroughputIps) parts.push(`>= ${formatThroughput(model.minThroughputIps)} ips`);
  return parts.length ? parts.join(" / ") : "not declared";
}

export function formatResourceEnvelope(model: ModelRecord): string {
  const parts = [];
  if (model.minMemoryAvailableMb) parts.push(`RAM >= ${Math.round(model.minMemoryAvailableMb)} MB`);
  if (model.minStorageAvailableMb) parts.push(`storage >= ${Math.round(model.minStorageAvailableMb)} MB`);
  if (model.maxTemperatureC) parts.push(`temp <= ${Math.round(model.maxTemperatureC)} C`);
  if (model.minBatteryPercent) parts.push(`battery >= ${Math.round(model.minBatteryPercent)}%`);
  if (model.requiredPowerSource) parts.push(`power ${model.requiredPowerSource}`);
  return parts.length ? parts.join(" / ") : "not declared";
}

export function performanceSloLabel(model: ModelRecord | undefined): string {
  if (!model) return "missing";
  if (!model.maxLatencyP95Ms && !model.minThroughputIps) return "not required";
  const failures = performanceSloFailures(model);
  if (!model.latencyMs && !model.throughputIps) return "needs benchmark";
  const freshness = benchmarkFreshness(model);
  if (freshness.state === "stale") return "benchmark stale";
  if (freshness.state === "unknown") return "age unknown";
  return failures.length ? "SLO miss" : "SLO met";
}

export function performanceSloDetail(model: ModelRecord): string {
  const slo = formatPerformanceSlo(model);
  if (slo === "not declared") return "no model performance budget declared";
  const failures = performanceSloFailures(model);
  if (!model.latencyMs && !model.throughputIps) return `${slo}; benchmark required`;
  const freshness = benchmarkFreshness(model);
  if (freshness.state !== "fresh") return benchmarkFreshnessDetail(freshness);
  if (failures.length) return failures.join("; ");
  return `${formatBenchmark(model)} meets ${slo}`;
}

export function performanceSloTone(model: ModelRecord | undefined): GateTone {
  if (!model) return "bad";
  if (!model.maxLatencyP95Ms && !model.minThroughputIps) return "neutral";
  if (!model.latencyMs && !model.throughputIps) return "warn";
  if (benchmarkFreshness(model).state !== "fresh") return "warn";
  return performanceSloFailures(model).length ? "warn" : "good";
}

function performanceSloFailures(model: ModelRecord): string[] {
  const failures: string[] = [];
  if (model.maxLatencyP95Ms && model.latencyMs && model.latencyMs > model.maxLatencyP95Ms) {
    failures.push(
      `p95 ${formatMetricNumber(model.latencyMs)} ms exceeds ${formatMetricNumber(model.maxLatencyP95Ms)} ms`
    );
  } else if (model.maxLatencyP95Ms && !model.latencyMs) {
    failures.push("missing p95 latency");
  }
  if (
    model.minThroughputIps &&
    model.throughputIps &&
    model.throughputIps < model.minThroughputIps
  ) {
    failures.push(`${formatThroughput(model.throughputIps)} ips below ${formatThroughput(model.minThroughputIps)} ips`);
  } else if (model.minThroughputIps && !model.throughputIps) {
    failures.push("missing throughput");
  }
  return failures;
}

export function formatMetricNumber(value: number): string {
  if (!Number.isFinite(value)) return "";
  const abs = Math.abs(value);
  if (abs >= 100) return String(Math.round(value));
  if (abs >= 10) return String(Math.round(value * 10) / 10);
  return String(Math.round(value * 10) / 10);
}

export function formatArtifactSizeMb(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value) || value <= 0) return "";
  if (value < 0.1) return "<0.1 MB artifact";
  return `${formatMetricNumber(value)} MB artifact`;
}

export function formatThroughput(value: number): string {
  if (!Number.isFinite(value)) return "";
  return String(Math.round(value));
}

export function compactMetricDetail(detail: string): string {
  return detail
    .replace(/(-?\d+(?:\.\d+)?)\s*ms/g, (_match, value) => `${formatMetricNumber(Number(value))} ms`)
    .replace(/(-?\d+(?:\.\d+)?)\s*ips/g, (_match, value) => `${formatThroughput(Number(value))} ips`)
    .replace(/(-?\d+(?:\.\d+)?)\s*MB/g, (_match, value) => `${Math.round(Number(value))} MB`);
}

export function artifactLaneValue(artifactLane: JsonObject): string {
  const state = stringOf(artifactLane.state, "");
  if (state) return state.replace(/_/g, " ");
  const format = stringOf(artifactLane.model_format, "");
  return format ? `${format} artifact` : "not classified";
}

export function artifactLaneDetail(artifactLane: JsonObject): string {
  const detail = stringOf(artifactLane.detail, "");
  if (detail) return detail;
  const nativeFormats = stringsOf(artifactLane.native_formats);
  if (nativeFormats.length) return `native formats: ${nativeFormats.join(", ")}`;
  return "artifact format has not been evaluated for this runtime lane";
}

export function artifactLaneTone(artifactLane: JsonObject): GateTone {
  const status = stringOf(artifactLane.status, "");
  if (status === "go") return "good";
  if (status === "blocked") return "bad";
  if (status === "attention") return "warn";
  return "neutral";
}

export function runtimeLaneFor(
  runtimeFit: Record<string, unknown>,
  runtime: RuntimeTarget | undefined
): JsonObject {
  const readinessLane = asRecord(runtimeFit.runtime_lane);
  if (Object.keys(readinessLane).length) return readinessLane;
  return asRecord(runtime?.runtime_lane);
}

export function runtimeLaneValue(lane: JsonObject): string {
  return stringOf(lane.label, stringOf(lane.lane_id, "not classified"));
}

export function runtimeLaneDetail(lane: JsonObject): string {
  const parts = [
    stringOf(lane.execution_engine, ""),
    stringOf(lane.acceleration, "").replace(/_/g, " "),
    stringOf(lane.optimization_goal, "")
  ].filter(Boolean);
  const providers = stringsOf(lane.providers);
  if (providers.length) {
    parts.splice(Math.min(2, parts.length), 0, `providers ${providers.join(", ")}`);
  }
  return parts.join(" / ") || "runtime target has no lane metadata";
}

export function runtimeLaneTone(lane: JsonObject): GateTone {
  const laneId = stringOf(lane.lane_id, "");
  if (!laneId) return "neutral";
  return laneId === "device-inventory" ? "warn" : "good";
}

export function runtimeTargetImageValue(runtime: RuntimeTarget | undefined): string {
  if (!runtime) return "runtime missing";
  return runtime.image || runtime.name || runtime.runtime_target_id || runtime.id || "image pending";
}

export function runtimeTargetImageDetail(runtime: RuntimeTarget | undefined): string {
  if (!runtime) return "select a runtime target";
  const platform = [runtime.os, runtime.arch].filter(Boolean).join("/");
  const profiles = stringsOf(runtime.device_profiles);
  return [
    platform,
    profiles.length ? `profiles ${profiles.join(", ")}` : "",
    runtime.updated_at ? `updated ${compactDate(runtime.updated_at)}` : ""
  ]
    .filter(Boolean)
    .join(" / ") || "runtime image metadata pending";
}

export function runtimeProviderValue(lane: JsonObject): string {
  const providers = stringsOf(lane.providers);
  if (providers.length) return providers.join(", ");
  return stringOf(lane.execution_engine, "provider pending");
}

export function runtimeProviderDetail(lane: JsonObject, device: Device | undefined): string {
  const engine = stringOf(lane.execution_engine, "");
  const acceleration = stringOf(lane.acceleration, "").replace(/_/g, " ");
  const accelerators = stringsOf(lane.accelerators);
  const edgeProfile = device?.profile ?? "";
  return [
    engine,
    acceleration,
    accelerators.length ? `accelerators ${accelerators.join(", ")}` : "",
    edgeProfile ? `edge profile ${edgeProfile}` : ""
  ]
    .filter(Boolean)
    .join(" / ") || "provider and accelerator metadata pending";
}

export function runtimeProviderTone(lane: JsonObject, device: Device | undefined): GateTone {
  if (!Object.keys(lane).length) return "neutral";
  const inventoryTone = runtimeInventoryTone(device);
  if (inventoryTone === "bad") return "bad";
  return runtimeLaneTone(lane);
}

export function runtimeTargetSelectionValue(selection: JsonObject): string {
  const status = stringOf(selection.status, "");
  const selectedRank = numberOf(selection.selected_rank);
  const eligibleCount = numberOf(selection.eligible_target_count);
  if (status === "best") {
    return selectedRank !== undefined && eligibleCount !== undefined
      ? `#${selectedRank} of ${eligibleCount}`
      : "best target";
  }
  if (status === "upgrade_available") return "upgrade available";
  if (status === "selected_not_eligible") return "not eligible";
  if (status === "no_eligible_targets") return "no target";
  return "comparison pending";
}

export function runtimeTargetSelectionDetail(selection: JsonObject): string {
  const detail = stringOf(selection.detail, "");
  if (detail) return detail;
  const bestTarget = stringOf(selection.best_runtime_target_id, "");
  const bestScore = numberOf(selection.best_score);
  if (bestTarget) {
    return bestScore !== undefined
      ? `Best measured target is ${bestTarget} at ${bestScore}/100`
      : `Best measured target is ${bestTarget}`;
  }
  return "No alternate runtime target comparison is available yet.";
}

export function runtimeTargetSelectionTone(selection: JsonObject): GateTone {
  const status = stringOf(selection.status, "");
  if (status === "best") return "good";
  if (status === "upgrade_available") return "warn";
  if (status === "selected_not_eligible" || status === "no_eligible_targets") return "bad";
  return "neutral";
}

export function productionAdmissionValue(admission: JsonObject): string {
  if (admission.apply_allowed === true) return "permitted";
  if (admission.apply_allowed === false) return "blocked";
  return "pending";
}

export function productionAdmissionDetail(admission: JsonObject): string {
  const detail = stringOf(admission.detail, "");
  const blockers = numberOf(admission.blocking_gate_count);
  if (detail && blockers && blockers > 0) return `${detail}; ${blockers} blocking gate${blockers === 1 ? "" : "s"}`;
  if (detail) return detail;
  return "waiting for Hub admission gates";
}

export function productionAdmissionTone(admission: JsonObject): GateTone {
  if (admission.apply_allowed === true) return "good";
  if (admission.apply_allowed === false) return "bad";
  return "neutral";
}

export function capabilityLockValue(lock: JsonObject): string {
  const status = stringOf(lock.status, "");
  if (status) return status.replace(/_/g, " ");
  return stringOf(lock.capability_sha256, "") ? "hash locked" : "not locked";
}

export function capabilityLockDetail(lock: JsonObject): string {
  const failures = stringsOf(lock.failures);
  if (failures.length) return compactMetricDetail(failures[0]);
  const runtimeTarget = asRecord(lock.runtime_target);
  const edgeInventory = asRecord(lock.edge_inventory);
  const runtimeTargetValue = stringOf(
    lock.runtime_target_id,
    stringOf(runtimeTarget.runtime_target_id, "runtime target")
  );
  const edgeProfile = stringOf(edgeInventory.device_profile, "edge profile");
  const freshness = capabilityLockFreshnessDetail(lock);
  const digest = stringOf(lock.capability_sha256, "");
  const digestLabel = digest ? `capability ${digest.slice(0, 12)}` : "";
  return [runtimeTargetValue, edgeProfile, freshness, digestLabel].filter(Boolean).join(" / ") || "capability basis pending";
}

export function capabilityLockTone(lock: JsonObject): GateTone {
  const status = stringOf(lock.status, "");
  if (status === "locked") return "good";
  if (status === "blocked") return "bad";
  if (status === "attention") return "warn";
  return stringOf(lock.capability_sha256, "") ? "good" : "neutral";
}

export function runtimeCapabilityLockForProof(readiness: DeploymentReadiness | undefined): JsonObject {
  const contract = asRecord(readiness?.edge_execution_contract);
  const runtimeDecision = asRecord(readiness?.runtime_decision);
  const runtimeFit = asRecord(readiness?.runtime_fit);
  const contractLock = asRecord(contract.runtime_capability_lock);
  if (Object.keys(contractLock).length) return contractLock;
  const decisionLock = asRecord(runtimeDecision.runtime_capability_lock);
  if (Object.keys(decisionLock).length) return decisionLock;
  return asRecord(runtimeFit.runtime_capability_lock);
}

export function runtimeFitScoreForProof(
  readiness: DeploymentReadiness | undefined,
  runtimeFitDisplay: RuntimeFitDisplay
): number | undefined {
  const runtimeFit = asRecord(readiness?.runtime_fit);
  const score = numberOf(runtimeFit.score);
  if (score !== undefined) return score;
  const match = runtimeFitDisplay.label.match(/^(\d+(?:\.\d+)?)\/100/);
  return match ? Number(match[1]) : undefined;
}

function capabilityLockFreshnessDetail(lock: JsonObject): string {
  const freshness = asRecord(asRecord(lock.edge_inventory).telemetry_freshness);
  const state = stringOf(freshness.state, stringOf(freshness.status, "")).replace(/_/g, " ");
  const ageSeconds = numberOf(freshness.heartbeat_age_seconds);
  const budgetSeconds = numberOf(freshness.heartbeat_stale_after_seconds);
  if (ageSeconds !== undefined && budgetSeconds !== undefined) {
    const label = state || "telemetry";
    return `${label}: heartbeat ${formatAge(ageSeconds)} old / ${formatAge(budgetSeconds)} budget`;
  }
  const detail = stringOf(freshness.detail, "");
  if (detail) return compactMetricDetail(detail);
  return "";
}

export function runtimeFitComponentRows(runtimeFit: Record<string, unknown>): [string, string][] {
  const components = asRecord(runtimeFit.components);
  const rows = [
    runtimeFitComponentRow("Compatibility", asRecord(components.compatibility)),
    runtimeFitComponentRow("Validation", asRecord(components.runtime_validation)),
    runtimeFitComponentRow("Performance", asRecord(components.performance)),
    runtimeFitComponentRow("Resource", asRecord(components.resource)),
    runtimeFitComponentRow("Telemetry", asRecord(components.telemetry))
  ].filter((row): row is [string, string] => row !== undefined);
  return rows.length ? rows : [["Runtime score", "waiting for readiness evidence"]];
}

function runtimeFitComponentRow(
  label: string,
  component: Record<string, unknown>
): [string, string] | undefined {
  const score = numberOf(component.score);
  const maxScore = numberOf(component.max_score);
  const state = stringOf(component.state, stringOf(component.status, "unknown")).replace(/_/g, " ");
  if (score === undefined && maxScore === undefined && state === "unknown") return undefined;
  const parts = [];
  if (score !== undefined && maxScore !== undefined) parts.push(`${score}/${maxScore}`);
  else if (score !== undefined) parts.push(`${score}`);
  parts.push(state);

  const failures = stringsOf(component.failures);
  if (failures.length) parts.push(failures.slice(0, 2).join("; "));

  if (label === "Performance") {
    const latencyHeadroom = numberOf(component.latency_headroom_pct);
    const throughputHeadroom = numberOf(component.throughput_headroom_pct);
    if (latencyHeadroom !== undefined) parts.push(`latency ${formatSignedPercent(latencyHeadroom)}`);
    if (throughputHeadroom !== undefined) parts.push(`throughput ${formatSignedPercent(throughputHeadroom)}`);
  }
  if (label === "Resource") {
    const memoryHeadroom = numberOf(component.memory_headroom_mb);
    const storageHeadroom = numberOf(component.storage_headroom_mb);
    if (memoryHeadroom !== undefined) parts.push(`RAM ${formatSignedMb(memoryHeadroom)}`);
    if (storageHeadroom !== undefined) parts.push(`storage ${formatSignedMb(storageHeadroom)}`);
  }
  return [label, parts.join(", ")];
}

function formatSignedPercent(value: number): string {
  const rounded = Math.round(value * 10) / 10;
  return `${rounded >= 0 ? "+" : ""}${rounded}%`;
}

function formatSignedMb(value: number): string {
  const rounded = Math.round(value);
  return `${rounded >= 0 ? "+" : ""}${rounded} MB`;
}

export interface BenchmarkFreshness {
  state: "fresh" | "stale" | "unknown";
  createdAt?: string;
  ageSeconds?: number;
  staleAfterSeconds: number;
}

export function benchmarkFreshness(model: ModelRecord): BenchmarkFreshness {
  const staleAfterSeconds = model.maxBenchmarkAgeSeconds ?? DEFAULT_BENCHMARK_STALE_SECONDS;
  if (!model.benchmarkedAt) {
    return { state: "unknown", staleAfterSeconds };
  }
  const createdAtMs = Date.parse(model.benchmarkedAt);
  if (Number.isNaN(createdAtMs)) {
    return { state: "unknown", createdAt: model.benchmarkedAt, staleAfterSeconds };
  }
  const ageSeconds = Math.max(0, Math.floor((Date.now() - createdAtMs) / 1000));
  return {
    state: ageSeconds > staleAfterSeconds ? "stale" : "fresh",
    createdAt: model.benchmarkedAt,
    ageSeconds,
    staleAfterSeconds
  };
}

function benchmarkFreshnessDetail(freshness: BenchmarkFreshness): string {
  if (!freshness.createdAt || freshness.ageSeconds === undefined) {
    return "benchmark timestamp missing; record fresh edge performance proof";
  }
  return `benchmark evidence is ${formatAge(freshness.ageSeconds)} old; freshness budget is ${formatAge(freshness.staleAfterSeconds)}`;
}

export function formatAge(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}

export function formatMb(value: number | undefined): string {
  return value === undefined ? "not reported" : `${Math.round(value)} MB`;
}

export function formatTemperature(value: number | undefined): string {
  return value === undefined ? "not reported" : `${Math.round(value)} C`;
}

export function formatPower(observed: Record<string, number | string | undefined>): string {
  const source = stringOf(observed.powerSource, "");
  const battery = numberOf(observed.batteryPercent);
  if (source && battery !== undefined) return `${source} / ${Math.round(battery)}%`;
  if (source) return source;
  if (battery !== undefined) return `${Math.round(battery)}%`;
  return "not reported";
}

export function resourceEnvelopeCapabilityFit(
  model: ModelRecord | undefined,
  device: Device | undefined
): EdgeRuntimeFit {
  if (!model || !device) {
    return {
      label: "missing context",
      detail: "select a model and edge node",
      tone: "bad",
      failures: ["missing model or edge node"]
    };
  }
  if (!resourceEnvelopeDeclared(model)) {
    return {
      label: "not declared",
      detail: "no model resource envelope declared",
      tone: "neutral",
      failures: []
    };
  }
  const observed = deviceResourceSnapshot(device);
  const failures = resourceEnvelopeFailures(model, observed);
  const missing = resourceEnvelopeMissing(model, observed);
  if (failures.length) {
    return {
      label: "constrained",
      detail: failures[0],
      tone: "bad",
      failures
    };
  }
  if (missing.length) {
    return {
      label: "telemetry missing",
      detail: `missing ${missing.join(", ")}`,
      tone: "warn",
      failures: []
    };
  }
  return {
    label: "met",
    detail: resourceEnvelopeObservedDetail(model, observed),
    tone: "good",
    failures: []
  };
}

function resourceEnvelopeDeclared(model: ModelRecord): boolean {
  return Boolean(
    model.minMemoryAvailableMb ||
      model.minStorageAvailableMb ||
      model.maxTemperatureC ||
      model.minBatteryPercent ||
      model.requiredPowerSource
  );
}

export function deviceResourceSnapshot(device: Device): Record<string, number | string | undefined> {
  const inventory = asRecord(device.inventory);
  const memory = asRecord(inventory.memory);
  const storage = asRecord(inventory.storage);
  const disk = asRecord(inventory.disk);
  const thermal = asRecord(inventory.thermal);
  const power = asRecord(inventory.power);
  return {
    memoryAvailableMb: numberOf(
      memory.available_mb ??
        inventory.memory_available_mb ??
        inventory.available_memory_mb ??
        inventory.free_memory_mb
    ),
    storageAvailableMb: numberOf(
      storage.available_mb ??
        disk.available_mb ??
        inventory.storage_available_mb ??
        inventory.disk_available_mb ??
        inventory.disk_free_mb
    ),
    temperatureC: numberOf(
      thermal.temperature_c ??
        thermal.cpu_temp_c ??
        thermal.max_observed_c ??
        inventory.temperature_c ??
        inventory.cpu_temp_c
    ),
    batteryPercent: numberOf(
      power.battery_percent ?? inventory.battery_percent ?? inventory.battery_pct
    ),
    powerSource: stringOf(power.source ?? inventory.power_source, "")
  };
}

function resourceEnvelopeFailures(
  model: ModelRecord,
  observed: Record<string, number | string | undefined>
): string[] {
  const failures: string[] = [];
  const memory = numberOf(observed.memoryAvailableMb);
  if (model.minMemoryAvailableMb && memory !== undefined && memory < model.minMemoryAvailableMb) {
    failures.push(`RAM ${Math.round(memory)} MB below ${Math.round(model.minMemoryAvailableMb)} MB`);
  }
  const storage = numberOf(observed.storageAvailableMb);
  if (model.minStorageAvailableMb && storage !== undefined && storage < model.minStorageAvailableMb) {
    failures.push(`storage ${Math.round(storage)} MB below ${Math.round(model.minStorageAvailableMb)} MB`);
  }
  const temperature = numberOf(observed.temperatureC);
  if (model.maxTemperatureC && temperature !== undefined && temperature > model.maxTemperatureC) {
    failures.push(`temperature ${Math.round(temperature)} C exceeds ${Math.round(model.maxTemperatureC)} C`);
  }
  const battery = numberOf(observed.batteryPercent);
  if (model.minBatteryPercent && battery !== undefined && battery < model.minBatteryPercent) {
    failures.push(`battery ${Math.round(battery)}% below ${Math.round(model.minBatteryPercent)}%`);
  }
  const powerSource = stringOf(observed.powerSource, "").toLowerCase();
  if (model.requiredPowerSource && powerSource && powerSource !== model.requiredPowerSource.toLowerCase()) {
    failures.push(`power source ${powerSource} does not match ${model.requiredPowerSource}`);
  }
  return failures;
}

function resourceEnvelopeMissing(
  model: ModelRecord,
  observed: Record<string, number | string | undefined>
): string[] {
  const missing = [];
  if (model.minMemoryAvailableMb && numberOf(observed.memoryAvailableMb) === undefined) missing.push("RAM");
  if (model.minStorageAvailableMb && numberOf(observed.storageAvailableMb) === undefined) missing.push("storage");
  if (model.maxTemperatureC && numberOf(observed.temperatureC) === undefined) missing.push("temperature");
  if (model.minBatteryPercent && numberOf(observed.batteryPercent) === undefined) missing.push("battery");
  if (model.requiredPowerSource && !stringOf(observed.powerSource, "")) missing.push("power source");
  return missing;
}

function resourceEnvelopeObservedDetail(
  model: ModelRecord,
  observed: Record<string, number | string | undefined>
): string {
  const parts = [];
  const memory = numberOf(observed.memoryAvailableMb);
  if (memory !== undefined && model.minMemoryAvailableMb) parts.push(`${Math.round(memory)} MB RAM`);
  const storage = numberOf(observed.storageAvailableMb);
  if (storage !== undefined && model.minStorageAvailableMb) parts.push(`${Math.round(storage)} MB storage`);
  const temperature = numberOf(observed.temperatureC);
  if (temperature !== undefined && model.maxTemperatureC) parts.push(`${Math.round(temperature)} C`);
  const powerSource = stringOf(observed.powerSource, "");
  if (powerSource && model.requiredPowerSource) parts.push(`power ${powerSource}`);
  return parts.length ? `${parts.join(" / ")} satisfies declared envelope` : "resource envelope met";
}

export function edgeRuntimeCapabilityFit(
  model: ModelRecord | undefined,
  device: Device | undefined,
  target: RuntimeTarget | undefined,
  validation: RuntimeValidation | undefined
): EdgeRuntimeFit {
  if (!model || !device || !target) {
    return {
      label: "missing context",
      detail: "select a model, edge node, and runtime target",
      tone: "bad",
      failures: ["missing model, edge node, or runtime target"]
    };
  }
  const targetModelMismatch = !targetSupportsModel(target, model);

  const inventory = asRecord(device.inventory);
  const reportsInventory =
    Object.keys(asRecord(inventory.runtimes)).length > 0 ||
    Object.keys(asRecord(inventory.accelerators)).length > 0;
  if (targetModelMismatch && !reportsInventory) {
    return {
      label: "target mismatch",
      detail: "runtime target does not satisfy the selected model constraints",
      tone: "bad",
      failures: ["runtime target does not satisfy the selected model constraints"]
    };
  }
  if (!reportsInventory) {
    return {
      label: "inventory missing",
      detail: `${deviceId(device)} has not reported runtime/provider inventory`,
      tone: "warn",
      failures: []
    };
  }

  const failures = runtimeTargetInventoryFailures(target, device);
  if (targetModelMismatch && failures.length) {
    return {
      label: "edge mismatch",
      detail: `runtime target does not satisfy model constraints; ${failures[0]}`,
      tone: "bad",
      failures: ["runtime target does not satisfy the selected model constraints", ...failures]
    };
  }
  if (targetModelMismatch) {
    return {
      label: "target mismatch",
      detail: "runtime target does not satisfy the selected model constraints",
      tone: "bad",
      failures: ["runtime target does not satisfy the selected model constraints"]
    };
  }
  if (failures.length) {
    return {
      label: "edge mismatch",
      detail: failures[0],
      tone: "bad",
      failures
    };
  }

  if (validation) {
    return {
      label: "validated",
      detail: `${runtimeTargetId(target)} passed package validation for ${model.packageId}`,
      tone: "good",
      failures: []
    };
  }
  if (model.benchmarkDeviceId === deviceId(device) && model.benchmarkRuntimeId === runtimeTargetId(target)) {
    return {
      label: "benchmarked",
      detail: `${formatBenchmark(model)} on ${deviceId(device)} / ${runtimeTargetId(target)}`,
      tone: "good",
      failures: []
    };
  }
  return {
    label: "inventory match",
    detail: "edge inventory matches the target; run validation before field rollout",
    tone: "warn",
    failures: []
  };
}

export function runtimeTargetInventoryFailures(target: RuntimeTarget, device: Device): string[] {
  const inventory = asRecord(device.inventory);
  const liveRuntimes = asRecord(inventory.runtimes);
  const liveAccelerators = asRecord(inventory.accelerators);
  const constraints = runtimeTargetInventoryConstraints(target);
  const failures: string[] = [];

  const missingRuntimes = constraints.runtimes.filter((runtime) => !runtimeAvailable(liveRuntimes, runtime));
  if (missingRuntimes.length) failures.push(`missing runtimes: ${missingRuntimes.join(", ")}`);

  const availableProviders = new Set(stringsOf(asRecord(liveRuntimes.onnxruntime).providers));
  const missingProviders = constraints.providers.filter((provider) => !availableProviders.has(provider));
  if (missingProviders.length) failures.push(`missing ONNX providers: ${missingProviders.join(", ")}`);
  if (
    constraints.preferredProviders.length &&
    !constraints.preferredProviders.some((provider) => availableProviders.has(provider))
  ) {
    failures.push(`none of preferred ONNX providers are available: ${constraints.preferredProviders.join(", ")}`);
  }

  const missingAccelerators = constraints.accelerators.filter(
    (accelerator) => asRecord(liveAccelerators[accelerator]).available !== true
  );
  if (missingAccelerators.length) failures.push(`missing accelerators: ${missingAccelerators.join(", ")}`);
  if (
    constraints.requiresGpu &&
    !Object.values(liveAccelerators).some((accelerator) => asRecord(accelerator).available === true)
  ) {
    failures.push("GPU accelerator is required but none was reported");
  }
  return failures;
}

export function runtimeTargetInventoryConstraints(target: RuntimeTarget): {
  runtimes: string[];
  providers: string[];
  preferredProviders: string[];
  accelerators: string[];
  requiresGpu: boolean;
} {
  const constraints = asRecord(target.runtime_constraints);
  const runtimes = asRecord(target.runtimes);
  const accelerators = asRecord(target.accelerators);
  const onnxruntime = asRecord(runtimes.onnxruntime);

  const requiredRuntimes = stringsOf(constraints.runtimes);
  const inferredRuntimes = Object.entries(runtimes)
    .filter(([, status]) => asRecord(status).available !== false)
    .map(([runtime]) => runtime);
  const providers = stringsOf(constraints.providers);
  const preferredProviders =
    stringsOf(constraints.provider_order).length > 0
      ? stringsOf(constraints.provider_order)
      : stringsOf(constraints.preferred_providers).length > 0
        ? stringsOf(constraints.preferred_providers)
        : providers.length
          ? []
          : stringsOf(onnxruntime.providers);
  const requiredAccelerators = stringsOf(constraints.accelerators);
  const inferredAccelerators = Object.entries(accelerators)
    .filter(([, status]) => asRecord(status).available === true)
    .map(([accelerator]) => accelerator);

  return {
    runtimes: requiredRuntimes.length ? requiredRuntimes : inferredRuntimes,
    providers,
    preferredProviders,
    accelerators: requiredAccelerators.length ? requiredAccelerators : inferredAccelerators,
    requiresGpu: constraints.requires_gpu === true
  };
}

function runtimeAvailable(runtimes: Record<string, unknown>, name: string): boolean {
  const normalized = name.toLowerCase().replaceAll("-", "_");
  const aliases: Record<string, string> = {
    onnx: "onnxruntime",
    ort: "onnxruntime",
    tflite: "tflite_runtime",
    torchscript: "torch",
    trt: "tensorrt"
  };
  const key = aliases[normalized] ?? normalized;
  return asRecord(runtimes[key]).available === true;
}

export function runtimeInventoryLabel(device: Device | undefined): string {
  if (!device) return "missing";
  const inventory = asRecord(device.inventory);
  const runtimes = Object.entries(asRecord(inventory.runtimes)).filter(([, status]) => asRecord(status).available === true);
  return runtimes.length ? `${runtimes.length} runtime${runtimes.length === 1 ? "" : "s"}` : "not reported";
}

export function runtimeInventoryDetail(device: Device | undefined): string {
  if (!device) return "select an edge node";
  const inventory = asRecord(device.inventory);
  const runtimes = Object.entries(asRecord(inventory.runtimes))
    .filter(([, status]) => asRecord(status).available === true)
    .map(([runtime]) => runtime);
  const accelerators = Object.entries(asRecord(inventory.accelerators))
    .filter(([, status]) => asRecord(status).available === true)
    .map(([accelerator]) => accelerator);
  const parts = [];
  if (runtimes.length) parts.push(`runtimes ${runtimes.join(", ")}`);
  if (accelerators.length) parts.push(`accelerators ${accelerators.join(", ")}`);
  return parts.join(" / ") || `${deviceId(device)} has not reported runtime inventory`;
}

export function runtimeInventoryTone(device: Device | undefined): GateTone {
  if (!device) return "bad";
  const inventory = asRecord(device.inventory);
  return Object.keys(asRecord(inventory.runtimes)).length || Object.keys(asRecord(inventory.accelerators)).length
    ? "good"
    : "warn";
}

export function runtimeTargetCapabilityDetail(target: RuntimeTarget | undefined): string {
  if (!target) return "select a runtime target";
  const constraints = runtimeTargetInventoryConstraints(target);
  const parts = [];
  if (constraints.runtimes.length) parts.push(`requires ${constraints.runtimes.join(", ")}`);
  if (constraints.providers.length) parts.push(`providers ${constraints.providers.join(", ")}`);
  if (constraints.preferredProviders.length) parts.push(`prefers ${constraints.preferredProviders.join(", ")}`);
  if (constraints.accelerators.length) parts.push(`accelerators ${constraints.accelerators.join(", ")}`);
  return parts.join(" / ") || `${runtimeTargetId(target)} has no declared runtime constraints`;
}

export function providerDisplayForModel(
  model: ModelRecord,
  target: RuntimeTarget | undefined
): string {
  if (model.providers.length) return model.providers.join(", ");
  if (!target) return "runtime target required";
  const constraints = runtimeTargetInventoryConstraints(target);
  if (constraints.providers.length) return constraints.providers.join(", ");
  if (constraints.preferredProviders.length) return constraints.preferredProviders.join(", ");
  const onnxruntime = asRecord(asRecord(target.runtimes).onnxruntime);
  const providers = stringsOf(onnxruntime.providers);
  if (providers.length) return providers.join(", ");
  return "provider inherited from runtime target";
}

export function runtimeForModel(targets: RuntimeTarget[], model?: ModelRecord): RuntimeTarget | undefined {
  if (!model) return targets[0];
  return targets.find((target) => targetSupportsModel(target, model)) ?? targets[0];
}

export function targetSupportsModel(target: RuntimeTarget, model: ModelRecord): boolean {
  const targetProfiles = target.device_profiles ?? [];
  if (model.profiles.length && targetProfiles.length && !model.profiles.some((profile) => targetProfiles.includes(profile))) {
    return false;
  }
  const runtimeMap = asRecord(target.runtimes);
  if (model.runtimes.length && !model.runtimes.some((runtime) => runtime in runtimeMap)) {
    return false;
  }
  return true;
}

export function isSigned(pkg: HubPackage): boolean {
  const validation = asRecord(asRecord(pkg.metadata).validation);
  return validation.signature_verified === true;
}

export function runtimeFitDisplayFor(
  readiness: DeploymentReadiness | undefined,
  fallback: EdgeRuntimeFit,
  runtime: RuntimeTarget | undefined
): RuntimeFitDisplay {
  const runtimeFit = asRecord(readiness?.runtime_fit);
  const score = numberOf(runtimeFit.score);
  const tier = stringOf(runtimeFit.tier, "").replace(/_/g, " ");
  const detail = stringOf(runtimeFit.detail, fallback.detail);
  const runtimeId = stringOf(runtimeFit.runtime_target_id, runtime ? runtimeTargetId(runtime) : "");
  if (score === undefined) {
    return {
      ...fallback,
      tileDetail: runtimeId ? `${fallback.label} on ${runtimeId}` : fallback.detail
    };
  }
  const label = tier ? `${score}/100 ${tier}` : `${score}/100`;
  return {
    label,
    detail,
    tone: runtimeFitTone(runtimeFit, fallback.tone),
    failures: fallback.failures,
    tileDetail: runtimeId ? `${label} on ${runtimeId}` : label
  };
}

function runtimeFitTone(runtimeFit: Record<string, unknown>, fallback: GateTone): GateTone {
  const tier = stringOf(runtimeFit.tier, "");
  if (tier === "blocked") return "bad";
  if (tier === "needs_evidence") return "warn";
  return numberOf(runtimeFit.score) !== undefined ? "good" : fallback;
}
