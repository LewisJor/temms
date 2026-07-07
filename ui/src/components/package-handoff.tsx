import { Clipboard, Download, PackageCheck, Rocket } from "lucide-react";
import type { MissionPackageDownloadHandoff } from "../api";
import { asRecord, stringOf } from "../lib/json";
import { missionPackageRolloutId } from "../lib/mission-package";
import { normalizeSha256Digest, shortProofDigest } from "../lib/proof-hash";
import type { EdgeProofWorkflow, GateTone, ReadinessVerdict } from "../lib/workbench-types";
import type { JsonObject } from "../types";
import { Badge, CapabilityMetric } from "./ui";

export function EdgePackagePlanPanel({
  canStageDeploy,
  manifest,
  readinessVerdict,
  workflow,
  onCopyManifest,
  onDownloadPackage,
  onPlanPackage,
  onStageDeploy
}: {
  canStageDeploy: boolean;
  manifest: JsonObject;
  readinessVerdict: ReadinessVerdict;
  workflow: EdgeProofWorkflow;
  onCopyManifest: () => void;
  onDownloadPackage: () => void;
  onPlanPackage: () => void;
  onStageDeploy: () => void;
}): JSX.Element {
  const mission = asRecord(manifest.mission);
  const selection = asRecord(manifest.selection);
  const handling = asRecord(manifest.model_handling);
  const slo = asRecord(manifest.slo);
  const ddil = asRecord(manifest.ddil);
  const deploymentIntent = asRecord(manifest.deployment_intent);
  const deploymentCommand = asRecord(deploymentIntent.command);
  const hasEdgePath = Boolean(
    selection.package_id &&
    selection.model_id &&
    selection.device_id &&
    selection.runtime_target_id
  );
  const hasPlannedDeploymentIntent = Boolean(deploymentIntent.rollout_id && deploymentCommand.path);
  const draftRolloutId = hasEdgePath ? missionPackageRolloutId(manifest) : "";
  const deployRolloutId = String(deploymentIntent.rollout_id || draftRolloutId || "rollout pending");
  const deployCommandPath = String(
    deploymentCommand.path || (hasEdgePath ? "/v1/hub/rollouts" : "deployment intent pending")
  );
  const deployTone: GateTone = canStageDeploy ? "good" : hasPlannedDeploymentIntent || hasEdgePath ? "warn" : "warn";
  const deployDetail = deploymentIntent.rollout_id
    ? canStageDeploy
      ? deployCommandPath
      : `${deployCommandPath}; proof gate must pass before staging`
    : hasEdgePath
      ? `${deployCommandPath}; plan package to hash mission handoff`
      : deployCommandPath;

  return (
    <section className={`edge-package-plan edge-package-plan-${workflow.tone}`} aria-labelledby="edge-package-plan-heading">
      <div className="edge-package-plan-header">
        <div>
          <span className="section-kicker">Edge package</span>
          <h2 id="edge-package-plan-heading">Package mission, models, runtime, policy, and proof gates</h2>
          <p>{readinessVerdict.nextAction}</p>
        </div>
        <div className="edge-package-plan-actions">
          <Badge value={workflow.gatePolicy} />
          <button className="button" type="button" onClick={onPlanPackage}>
            <PackageCheck size={16} />
            <span>Plan package</span>
          </button>
          <button className="button" type="button" disabled={!canStageDeploy} onClick={onStageDeploy}>
            <Rocket size={16} />
            <span>Stage rollout</span>
          </button>
          <button className="button button-secondary" type="button" onClick={onDownloadPackage}>
            <Download size={16} />
            <span>Download package</span>
          </button>
          <button className="button button-secondary" type="button" onClick={onCopyManifest}>
            <Clipboard size={16} />
            <span>Copy manifest</span>
          </button>
        </div>
      </div>
      <div className="package-binding-strip" aria-label="Mission package binding chain" data-testid="mission-package-binding">
        <PackageBindingStep
          label="Mission"
          value={String(mission.goal || mission.source_yaml ? "specified" : "pending")}
          detail={String(mission.goal || mission.source_yaml || "mission goal or YAML required")}
          tone={mission.goal || mission.source_yaml ? "good" : "warn"}
        />
        <PackageBindingStep
          label="Model/runtime"
          value={String(selection.model_id || "model pending")}
          detail={`${String(selection.package_id || "package pending")} / ${String(selection.runtime_target_id || "runtime pending")} / ${String(selection.device_id || "edge pending")}`}
          tone={selection.model_id && selection.runtime_target_id && selection.device_id ? "good" : "warn"}
        />
        <PackageBindingStep
          label="Handling"
          value={String(handling.switch_policy || "policy pending").replace(/_/g, " ")}
          detail={`${String(mission.sensor || "sensor pending")} / fallback ${String(handling.fallback_model_id || "auto")} / ${String(ddil.mode || "ddil pending").replace(/_/g, " ")}`}
          tone={handling.switch_policy && mission.sensor && ddil.mode ? "good" : "warn"}
        />
        <PackageBindingStep
          label="Deploy"
          value={deployRolloutId}
          detail={deployDetail}
          tone={deployTone}
        />
      </div>
      <div className="edge-package-plan-grid">
        <CapabilityMetric
          label="Mission"
          value={String(mission.sensor || "sensor pending")}
          detail={String(mission.goal || "mission goal pending")}
          tone={mission.goal || mission.source_yaml ? "good" : "warn"}
        />
        <CapabilityMetric
          label="Model"
          value={String(selection.model_id || "model pending")}
          detail={String(selection.package_id || "package pending")}
          tone={selection.model_id ? "good" : "warn"}
        />
        <CapabilityMetric
          label="Runtime"
          value={String(selection.runtime_target_id || "runtime pending")}
          detail={String(selection.device_id || "edge pending")}
          tone={selection.runtime_target_id && selection.device_id ? "good" : "warn"}
        />
        <CapabilityMetric
          label="Handling"
          value={String(handling.switch_policy || "policy pending").replace(/_/g, " ")}
          detail={`fallback ${String(handling.fallback_model_id || "auto")}; threshold ${String(handling.confidence_threshold || "pending")}`}
          tone={handling.switch_policy ? "good" : "warn"}
        />
        <CapabilityMetric
          label="SLO"
          value={`p95 <= ${String(slo.latency_budget_ms || "pending")} ms`}
          detail={`throughput >= ${String(slo.min_throughput_ips || "pending")} ips`}
          tone={slo.latency_budget_ms ? "good" : "warn"}
        />
        <CapabilityMetric
          label="Proof gate"
          value={workflow.status}
          detail={workflow.detail}
          tone={workflow.tone}
        />
        <CapabilityMetric
          label="Deploy intent"
          value={deployRolloutId}
          detail={deployDetail}
          tone={deployTone}
        />
      </div>
    </section>
  );
}

export function MissionPackageDownloadHandoffCard({
  handoff,
  manifest
}: {
  handoff: MissionPackageDownloadHandoff | undefined;
  manifest: JsonObject;
}): JSX.Element {
  const integrity = asRecord(manifest.integrity);
  const packageIdentity = asRecord(manifest.package_identity);
  const componentDigests = asRecord(manifest.component_digests);
  const identityDigest = stringOf(
    integrity.package_identity_sha256,
    stringOf(packageIdentity.package_identity_sha256, "")
  );
  const payloadDigest = stringOf(integrity.payload_sha256, "");
  const headerDigests = [
    {
      body: stringOf(componentDigests.mission_sha256, ""),
      key: "mission",
      label: "Mission",
      value: handoff?.missionSha256 || ""
    },
    {
      body: stringOf(componentDigests.mission_contract_sha256, ""),
      key: "mission_contract",
      label: "Mission contract",
      value: handoff?.missionContractSha256 || ""
    },
    {
      body: stringOf(componentDigests.runtime_plan_sha256, ""),
      key: "runtime_plan",
      label: "Runtime plan",
      value: handoff?.runtimePlanSha256 || ""
    },
    {
      body: stringOf(componentDigests.runtime_capability_lock_sha256, ""),
      key: "runtime_capability_lock",
      label: "Capability lock",
      value: handoff?.runtimeCapabilityLockSha256 || ""
    },
    {
      body: stringOf(componentDigests.deployment_intent_sha256, ""),
      key: "deployment_intent",
      label: "Deploy intent",
      value: handoff?.deploymentIntentSha256 || ""
    },
    {
      body: stringOf(componentDigests.edge_handoff_sha256, ""),
      key: "edge_handoff",
      label: "Edge handoff",
      value: handoff?.edgeHandoffSha256 || ""
    }
  ];
  const mismatched = handoff
    ? headerDigests.filter((digest) =>
        digest.value &&
        digest.body &&
        normalizeSha256Digest(digest.value) !== normalizeSha256Digest(digest.body)
      )
    : [];
  const missing = handoff
    ? headerDigests.filter((digest) => !digest.value).map((digest) => digest.label)
    : [];
  const payloadMatches = Boolean(
    handoff?.payloadSha256 &&
    payloadDigest &&
    normalizeSha256Digest(handoff.payloadSha256) === normalizeSha256Digest(payloadDigest)
  );
  const identityMatches = Boolean(
    handoff?.packageIdentitySha256 &&
    identityDigest &&
    normalizeSha256Digest(handoff.packageIdentitySha256) === normalizeSha256Digest(identityDigest)
  );
  const identityMismatch = Boolean(
    handoff?.packageIdentitySha256 &&
    identityDigest &&
    !identityMatches
  );
  const identityMissing = Boolean(handoff && !handoff.packageIdentitySha256);
  const tone: GateTone = !handoff
    ? "neutral"
    : identityMismatch || mismatched.length || (handoff.payloadSha256 && payloadDigest && !payloadMatches)
      ? "bad"
      : identityMissing || missing.length
        ? "warn"
        : "good";
  const value = !handoff
    ? "package not downloaded"
    : identityMismatch || mismatched.length
      ? "header mismatch"
      : identityMissing || missing.length
        ? "headers incomplete"
        : "package handoff retained";
  const detail = !handoff
    ? "Download the mission package to retain filename and digest headers."
    : identityMismatch
      ? "Package identity header disagrees with package body"
      : mismatched.length
        ? `${mismatched.map((digest) => digest.label).join(", ")} header disagrees with package body`
        : identityMissing || missing.length
          ? `${["Package identity", ...missing].filter((label) => identityMissing || label !== "Package identity").join(", ")} header missing from download response`
        : "Download response headers match package body digests.";

  return (
    <article className={`edge-proof-trace edge-proof-handoff edge-proof-trace-${tone}`} data-testid="mission-package-download-handoff">
      <div className="edge-proof-trace-header">
        <div>
          <span>Mission package handoff</span>
          <strong>{value}</strong>
          <small>{detail}</small>
        </div>
        <Badge value={handoff ? "downloaded" : "not downloaded"} />
      </div>
      <div className="edge-proof-trace-grid">
        <CapabilityMetric
          detail={handoff?.fileName || "package filename header pending"}
          label="Filename"
          tone={handoff?.fileName ? "good" : "neutral"}
          value={handoff?.fileName ? "retained" : "pending"}
        />
        <CapabilityMetric
          detail={handoff?.packageIdentitySha256 ? `sha256 ${shortProofDigest(handoff.packageIdentitySha256)}` : "identity hash header pending"}
          label="Package identity"
          tone={identityMatches ? "good" : handoff?.packageIdentitySha256 ? "warn" : "neutral"}
          value={identityMatches ? "matches body" : handoff?.packageIdentitySha256 ? "retained" : "pending"}
        />
        <CapabilityMetric
          detail={handoff?.payloadSha256 ? `sha256 ${shortProofDigest(handoff.payloadSha256)}` : "payload hash header pending"}
          label="Payload hash"
          tone={payloadMatches ? "good" : handoff?.payloadSha256 ? "warn" : "neutral"}
          value={payloadMatches ? "matches body" : handoff?.payloadSha256 ? "retained" : "pending"}
        />
        {headerDigests.map((digest) => {
          const matches =
            digest.value &&
            digest.body &&
            normalizeSha256Digest(digest.value) === normalizeSha256Digest(digest.body);
          return (
            <CapabilityMetric
              detail={digest.value ? `sha256 ${shortProofDigest(digest.value)}` : `${digest.label.toLowerCase()} header pending`}
              key={digest.key}
              label={`${digest.label} header`}
              tone={matches ? "good" : digest.value ? "warn" : handoff ? "warn" : "neutral"}
              value={matches ? "matches body" : digest.value ? "retained" : "pending"}
            />
          );
        })}
      </div>
    </article>
  );
}

function PackageBindingStep({
  label,
  value,
  detail,
  tone
}: {
  label: string;
  value: string;
  detail: string;
  tone: GateTone;
}): JSX.Element {
  return (
    <div className={`package-binding-step package-binding-step-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}
