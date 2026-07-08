import { GitBranch, Rocket, ShieldCheck } from "lucide-react";
import type { FormEvent, RefObject } from "react";
import {
  deviceId,
  planId,
  rolloutId,
  runtimeTargetId
} from "../lib/hub-format";
import {
  benchmarkFreshness,
  formatBenchmark,
  formatBenchmarkFreshness,
  performanceSloDetail,
  performanceSloLabel,
  performanceSloTone,
  runtimeInventoryDetail,
  runtimeInventoryLabel,
  runtimeInventoryTone,
  runtimeTargetCapabilityDetail
} from "../lib/runtime-fit";
import type {
  EdgeRuntimeFit,
  GateTone,
  MissionPackageStageStatus,
  ModelRecord,
  ReadinessVerdict,
  RuntimeFitDisplay
} from "../lib/workbench-types";
import type {
  DeploymentReadiness,
  Device,
  EdgeRecommendation,
  JsonObject,
  Rollout,
  RolloutPlan,
  RuntimeTarget,
  RuntimeValidation
} from "../types";
import { CapabilityDossier } from "./capability-dossier";
import {
  EmptyState,
  RolloutPlanRow,
  RolloutRow,
  TargetRow
} from "./deploy-lists";
import { EdgeRecommendationPanel } from "./runtime-optimizer";
import {
  Badge,
  Button,
  CapabilityMetric,
  PathStep,
  ReadinessCard,
  Submit
} from "./ui";

export interface EdgeDeployStageProps {
  canStageMissionPackage: boolean;
  deploymentSectionClassName: string;
  deploymentRef: RefObject<HTMLElement>;
  devices: Device[];
  edgeRecommendations: EdgeRecommendation[];
  edgeRuntimeFit: EdgeRuntimeFit;
  evidenceBundleCount: number;
  evidenceValue: number;
  hasMissionPackageDeploymentIntent: boolean;
  latestRollout: Rollout | undefined;
  missionSlot: string;
  missionPackageDeploymentCommand: JsonObject;
  missionPackageDeploymentIntent: JsonObject;
  missionPackageStageStatus: MissionPackageStageStatus;
  missionRolloutPlans: RolloutPlan[];
  missionRollouts: Rollout[];
  plansSectionClassName: string;
  plansRef: RefObject<HTMLElement>;
  proofEvents: number;
  readiness: DeploymentReadiness | undefined;
  readinessVerdict: ReadinessVerdict;
  resourceEnvelopeFit: EdgeRuntimeFit;
  rolloutsSectionClassName: string;
  rolloutsRef: RefObject<HTMLElement>;
  runtimeFitDisplay: RuntimeFitDisplay;
  runtimeTargets: RuntimeTarget[];
  selectedDevice: Device | undefined;
  selectedModel: ModelRecord | undefined;
  selectedRuntime: RuntimeTarget | undefined;
  selectedRuntimeValidation: RuntimeValidation | undefined;
  onAdvanceRolloutPlan: (id: string) => void;
  onApplyEdgeRecommendation: (recommendation: EdgeRecommendation) => void;
  onApplyRollout: (id: string) => void;
  onApproveRollout: (id: string) => void;
  onPauseRolloutPlan: (id: string) => void;
  onResumeRolloutPlan: (id: string) => void;
  onRollbackRollout: (id: string) => void;
  onSelectDevice: (deviceIdValue: string) => void;
  onSelectRuntime: (runtimeTargetIdValue: string) => void;
  onStageMissionPackageRollout: () => void;
  onSubmitForm: (name: string, event: FormEvent<HTMLFormElement>) => void;
}

export function EdgeDeployStage({
  canStageMissionPackage,
  deploymentSectionClassName,
  deploymentRef,
  devices,
  edgeRecommendations,
  edgeRuntimeFit,
  evidenceBundleCount,
  evidenceValue,
  hasMissionPackageDeploymentIntent,
  latestRollout,
  missionSlot,
  missionPackageDeploymentCommand,
  missionPackageDeploymentIntent,
  missionPackageStageStatus,
  missionRolloutPlans,
  missionRollouts,
  plansSectionClassName,
  plansRef,
  proofEvents,
  readiness,
  readinessVerdict,
  resourceEnvelopeFit,
  rolloutsSectionClassName,
  rolloutsRef,
  runtimeFitDisplay,
  runtimeTargets,
  selectedDevice,
  selectedModel,
  selectedRuntime,
  selectedRuntimeValidation,
  onAdvanceRolloutPlan,
  onApplyEdgeRecommendation,
  onApplyRollout,
  onApproveRollout,
  onPauseRolloutPlan,
  onResumeRolloutPlan,
  onRollbackRollout,
  onSelectDevice,
  onSelectRuntime,
  onStageMissionPackageRollout,
  onSubmitForm
}: EdgeDeployStageProps): JSX.Element {
  const selectedDeviceId = selectedDevice ? deviceId(selectedDevice) : "";
  const selectedRuntimeId = selectedRuntime ? runtimeTargetId(selectedRuntime) : "";
  const selectedModelId = selectedModel?.id ?? "";
  const missionSlotValue = missionSlot || "vision";
  const runtimeEvidenceState: GateTone =
    selectedRuntimeValidation ||
    (selectedModel?.benchmarkDeviceId && benchmarkFreshness(selectedModel).state === "fresh")
      ? "good"
      : "warn";

  return (
    <>
      <section
        className={deploymentSectionClassName}
        aria-labelledby="deploy-heading"
        ref={deploymentRef}
        tabIndex={-1}
      >
        <div className="section-header">
          <div>
            <span className="section-kicker">Edge deploy</span>
            <h2 id="deploy-heading">Stage the planned mission package</h2>
          </div>
          <Badge value={latestRollout?.state ?? "not assigned"} />
        </div>

        <div className="deploy-primary-lane" aria-label="Mission package deploy path">
          <CapabilityMetric
            label="Package handoff"
            value={missionPackageStageStatus.value}
            detail={missionPackageStageStatus.detail}
            tone={missionPackageStageStatus.tone}
          />
          <CapabilityMetric
            label="Deploy intent"
            value={
              hasMissionPackageDeploymentIntent
                ? String(missionPackageDeploymentIntent.rollout_id)
                : "plan package first"
            }
            detail={
              hasMissionPackageDeploymentIntent
                ? String(missionPackageDeploymentCommand.path || "/v1/hub/rollouts")
                : "Deploy is bound only after the mission package is hashed."
            }
            tone={canStageMissionPackage ? "good" : "warn"}
          />
          <CapabilityMetric
            label="Edge target"
            value={selectedDevice ? deviceId(selectedDevice) : "select edge"}
            detail={`${selectedRuntimeId || "runtime pending"}; ${selectedModel?.name ?? "model pending"}`}
            tone={selectedDevice && selectedRuntime && selectedModel ? "good" : "warn"}
          />
          <Button
            icon={<Rocket size={16} />}
            disabled={!canStageMissionPackage}
            onClick={onStageMissionPackageRollout}
          >
            Stage package rollout
          </Button>
        </div>

        <EdgeRecommendationPanel
          recommendations={edgeRecommendations}
          selectedDeviceId={selectedDeviceId}
          selectedModelId={selectedModelId}
          selectedRuntimeId={selectedRuntimeId}
          onSelect={onApplyEdgeRecommendation}
        />

        <CapabilityDossier
          edgeRuntimeFit={edgeRuntimeFit}
          model={selectedModel}
          readiness={readiness}
          readinessVerdict={readinessVerdict}
          resourceEnvelopeFit={resourceEnvelopeFit}
          runtime={selectedRuntime}
          runtimeValidation={selectedRuntimeValidation}
          device={selectedDevice}
        />

        <div className="path-line" aria-label="Deployment readiness">
          <PathStep title="Model" value={selectedModel?.name ?? "Missing"} state={selectedModel ? "ready" : "blocked"} />
          <PathStep title="Runtime" value={selectedRuntimeId || "Missing"} state={selectedRuntime ? "ready" : "blocked"} />
          <PathStep title="Edge" value={selectedDeviceId || "Missing"} state={selectedDevice ? "ready" : "blocked"} />
          <PathStep title="Runtime fit" value={runtimeFitDisplay.label} state={runtimeFitDisplay.tone} />
          <PathStep title="Resources" value={resourceEnvelopeFit.label} state={resourceEnvelopeFit.tone} />
          <PathStep title="Rollout" value={latestRollout?.state ?? "Not assigned"} state={latestRollout ? latestRollout.state ?? "pending" : "pending"} />
          <PathStep
            title="Evidence"
            value={proofEvents ? `${proofEvents} proof events` : `${evidenceBundleCount} bundles`}
            state={evidenceValue ? "ready" : "pending"}
          />
        </div>

        <div className="readiness-grid edge-fit-grid" aria-label="On-device runtime capability fit">
          <ReadinessCard
            title="On-device runtime fit"
            value={runtimeFitDisplay.label}
            detail={runtimeFitDisplay.detail}
            state={runtimeFitDisplay.tone}
          />
          <ReadinessCard
            title="Runtime inventory"
            value={runtimeInventoryLabel(selectedDevice)}
            detail={runtimeInventoryDetail(selectedDevice)}
            state={edgeRuntimeFit.failures.length ? "bad" : runtimeInventoryTone(selectedDevice)}
          />
          <ReadinessCard
            title="Runtime target"
            value={selectedRuntimeId || "missing"}
            detail={runtimeTargetCapabilityDetail(selectedRuntime)}
            state={selectedRuntime ? edgeRuntimeFit.tone : "bad"}
          />
          <ReadinessCard
            title="Performance SLO"
            value={performanceSloLabel(selectedModel)}
            detail={selectedModel ? performanceSloDetail(selectedModel) : "select a model"}
            state={performanceSloTone(selectedModel)}
          />
          <ReadinessCard
            title="Resource envelope"
            value={resourceEnvelopeFit.label}
            detail={resourceEnvelopeFit.detail}
            state={resourceEnvelopeFit.tone}
          />
          <ReadinessCard
            title="Field proof"
            value={
              selectedRuntimeValidation
                ? "validated"
                : selectedModel?.benchmarkDeviceId && benchmarkFreshness(selectedModel).state === "fresh"
                  ? "benchmarked"
                  : selectedModel?.benchmarkDeviceId
                    ? "stale proof"
                    : "pending"
            }
            detail={
              selectedRuntimeValidation
                ? "package passed selected runtime target validation"
                : selectedModel
                  ? `${formatBenchmark(selectedModel)}; ${formatBenchmarkFreshness(selectedModel)}`
                  : "no benchmark"
            }
            state={runtimeEvidenceState}
          />
        </div>

        <details className="stage-inline-drawer">
          <summary>
            <span>
              <span className="section-kicker">Manual controls</span>
              <strong>Direct rollout and compatibility tools</strong>
            </span>
            <Badge value="advanced" />
          </summary>
          <div className="stage-inline-drawer-body">
            <form className="deploy-form" onSubmit={(event) => onSubmitForm("assign-rollout", event)}>
              <input name="package_id" type="hidden" value={selectedModel?.packageId ?? ""} />
              <input name="model_id" type="hidden" value={selectedModelId} />
              <label className="field">
                <span>Edge node</span>
                <select name="device_id" value={selectedDeviceId} onChange={(event) => onSelectDevice(event.target.value)} required>
                  {devices.length ? null : <option value="">No edge nodes</option>}
                  {devices.map((device) => (
                    <option key={deviceId(device)} value={deviceId(device)}>
                      {deviceId(device)} - {device.profile ?? "unknown profile"}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>Runtime target</span>
                <select name="runtime_target_id" value={selectedRuntimeId} onChange={(event) => onSelectRuntime(event.target.value)}>
                  {runtimeTargets.length ? null : <option value="">No runtime targets</option>}
                  {runtimeTargets.map((target) => (
                    <option key={runtimeTargetId(target)} value={runtimeTargetId(target)}>
                      {runtimeTargetId(target)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>Mission slot</span>
                <input name="slot" value={missionSlotValue} readOnly />
              </label>
              <label className="field">
                <span>Rollout ID</span>
                <input name="rollout_id" placeholder="auto-generated" />
              </label>
              <input name="actor" type="hidden" value="operator:mission-package-workbench" />
              <label className="check deploy-check">
                <input name="require_approval" type="checkbox" defaultChecked />
                <span>Require approval</span>
              </label>
              <Submit icon={<Rocket size={16} />} disabled={!selectedModel || !selectedDevice}>
                Create rollout
              </Submit>
            </form>

            <form className="preview-form" onSubmit={(event) => onSubmitForm("compatibility-preview", event)}>
              <input name="device_id" type="hidden" value={selectedDeviceId} />
              <input name="package_id" type="hidden" value={selectedModel?.packageId ?? ""} />
              <input name="model_id" type="hidden" value={selectedModelId} />
              <input name="runtime_target_id" type="hidden" value={selectedRuntimeId} />
              <Submit icon={<ShieldCheck size={16} />} variant="secondary" disabled={!selectedModel || !selectedDevice}>
                Preview compatibility
              </Submit>
            </form>
          </div>
        </details>
      </section>

      <section
        className={plansSectionClassName}
        aria-labelledby="plans-heading"
        ref={plansRef}
        tabIndex={-1}
      >
        <div className="section-header">
          <div>
            <span className="section-kicker">Rollout coordination</span>
            <h2 id="plans-heading">Stage selected model across the fleet</h2>
          </div>
          <span className="section-count">{missionRolloutPlans.length}</span>
        </div>

        <form className="rollout-plan-form" onSubmit={(event) => onSubmitForm("create-rollout-plan", event)}>
          <input name="package_id" type="hidden" value={selectedModel?.packageId ?? ""} />
          <input name="model_id" type="hidden" value={selectedModelId} />
          <input name="runtime_target_id" type="hidden" value={selectedRuntimeId} />
          <input name="actor" type="hidden" value="operator:mission-package-workbench" />
          <label className="field">
            <span>Plan ID</span>
            <input name="plan_id" placeholder="auto-generated" />
          </label>
          <label className="field">
            <span>Device IDs</span>
            <input name="device_ids" defaultValue={devices.map(deviceId).join(",")} required />
          </label>
          <label className="field">
            <span>Mission slot</span>
            <input name="slot" value={missionSlotValue} readOnly />
          </label>
          <label className="field">
            <span>Batch size</span>
            <input name="batch_size" type="number" min="1" defaultValue="1" />
          </label>
          <label className="check deploy-check">
            <input name="require_approval" type="checkbox" defaultChecked />
            <span>Require approval</span>
          </label>
          <label className="check deploy-check">
            <input name="require_runtime_validation" type="checkbox" />
            <span>Require validation</span>
          </label>
          <Submit icon={<GitBranch size={16} />} disabled={!selectedModel || !selectedDevice || !devices.length}>
            Create plan
          </Submit>
        </form>

        <div className="rollout-list rollout-plan-list">
          {missionRolloutPlans.length ? (
            missionRolloutPlans.slice(0, 4).map((plan) => (
              <RolloutPlanRow
                key={planId(plan)}
                plan={plan}
                onAdvance={onAdvanceRolloutPlan}
                onPause={onPauseRolloutPlan}
                onResume={onResumeRolloutPlan}
              />
            ))
          ) : (
            <EmptyState title="No coordinated rollout plans" detail="Create a plan to stage selected models through approval and batch assignment." />
          )}
        </div>
      </section>

      <section
        className={rolloutsSectionClassName}
        aria-labelledby="rollouts-heading"
        ref={rolloutsRef}
        tabIndex={-1}
      >
        <div className="section-header">
          <div>
            <span className="section-kicker">Rollouts</span>
            <h2 id="rollouts-heading">Approval and activation</h2>
          </div>
          <span className="section-count">{missionRollouts.length}</span>
        </div>
        <div className="rollout-list">
          {missionRollouts.length ? (
            missionRollouts.slice(0, 6).map((rollout) => (
              <RolloutRow
                key={rolloutId(rollout)}
                rollout={rollout}
                onApprove={onApproveRollout}
                onApply={onApplyRollout}
                onRollback={onRollbackRollout}
              />
            ))
          ) : (
            <EmptyState title="No rollouts assigned" detail="Create a rollout from the selected model to start activation." />
          )}
        </div>
      </section>

      <section className="section fleet-section deploy-secondary-section" aria-labelledby="fleet-heading">
        <div className="section-header">
          <div>
            <span className="section-kicker">Fleet and runtimes</span>
            <h2 id="fleet-heading">Deployment targets</h2>
          </div>
          <span className="section-count">{devices.length}</span>
        </div>
        <div className="compact-list">
          {devices.map((device) => (
            <TargetRow key={deviceId(device)} label={deviceId(device)} detail={device.profile ?? "unknown profile"} status={device.status ?? "registered"} />
          ))}
          {runtimeTargets.slice(0, 4).map((target) => (
            <TargetRow
              key={runtimeTargetId(target)}
              label={runtimeTargetId(target)}
              detail={`${target.arch ?? "arch unknown"} - ${target.device_profiles?.join(", ") || "any profile"}`}
              status="runtime"
            />
          ))}
        </div>
      </section>
    </>
  );
}
