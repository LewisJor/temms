import type {
  EdgeProofDownloadHandoff,
  MissionPackageDownloadHandoff
} from "../api";
import type {
  DeploymentReadiness,
  Device,
  JsonObject,
  RuntimeTarget,
  RuntimeValidation
} from "../types";
import type {
  EdgeProofComponentDigestStatus,
  EdgeProofTraceStatus,
  EdgeProofWorkflow,
  EdgeRuntimeFit,
  EdgeRuntimeMission,
  ModelRecord,
  ReadinessGateAction,
  ReadinessVerdict,
  RuntimeFitDisplay
} from "../lib/workbench-types";
import { EdgeProofPanel } from "./edge-proof";
import {
  EdgePackagePlanPanel,
  MissionPackageDownloadHandoffCard
} from "./package-handoff";
import { ReadinessVerdictPanel } from "./readiness-panels";
import { EdgeRuntimeMissionPanel } from "./runtime-mission";
import { EdgeExecutionContractPanel } from "./runtime-execution-contract";
import { Badge } from "./ui";

export interface PackageHandoffStageProps {
  canStageMissionPackage: boolean;
  componentDigests: EdgeProofComponentDigestStatus;
  disabled: boolean;
  edgeExecutionContract: JsonObject;
  edgeProofHandoff: EdgeProofDownloadHandoff | undefined;
  edgeRuntimeFit: EdgeRuntimeFit;
  manifest: JsonObject;
  missionPackageHandoff: MissionPackageDownloadHandoff | undefined;
  proof: JsonObject | undefined;
  readiness: DeploymentReadiness | undefined;
  readinessVerdict: ReadinessVerdict;
  resourceEnvelopeFit: EdgeRuntimeFit;
  runtimeDecision: JsonObject;
  runtimeFitDisplay: RuntimeFitDisplay;
  runtimeMission: EdgeRuntimeMission;
  selectedDevice: Device | undefined;
  selectedModel: ModelRecord | undefined;
  selectedRuntime: RuntimeTarget | undefined;
  selectedRuntimeValidation: RuntimeValidation | undefined;
  trace: EdgeProofTraceStatus;
  workflow: EdgeProofWorkflow;
  onCopyCommand: (label: string, command: string) => void;
  onCopyManifest: () => void;
  onDownloadEdgeProof: () => void;
  onDownloadPackage: () => void;
  onGenerateEdgeProof: () => void;
  onGoDeploy: () => void;
  onPlanPackage: () => void;
  onReadinessAction: (action: ReadinessGateAction) => void;
  onSelectRuntimeTarget: (runtimeTargetIdValue: string) => void;
  onStageDeploy: () => void;
}

export function PackageHandoffStage({
  canStageMissionPackage,
  componentDigests,
  disabled,
  edgeExecutionContract,
  edgeProofHandoff,
  edgeRuntimeFit,
  manifest,
  missionPackageHandoff,
  proof,
  readiness,
  readinessVerdict,
  resourceEnvelopeFit,
  runtimeDecision,
  runtimeFitDisplay,
  runtimeMission,
  selectedDevice,
  selectedModel,
  selectedRuntime,
  selectedRuntimeValidation,
  trace,
  workflow,
  onCopyCommand,
  onCopyManifest,
  onDownloadEdgeProof,
  onDownloadPackage,
  onGenerateEdgeProof,
  onGoDeploy,
  onPlanPackage,
  onReadinessAction,
  onSelectRuntimeTarget,
  onStageDeploy
}: PackageHandoffStageProps): JSX.Element {
  return (
    <div className="stage-stack" data-testid="hub-stage-package">
      <EdgePackagePlanPanel
        canStageDeploy={canStageMissionPackage}
        manifest={manifest}
        readinessVerdict={readinessVerdict}
        workflow={workflow}
        onCopyManifest={onCopyManifest}
        onDownloadPackage={onDownloadPackage}
        onGoDeploy={onGoDeploy}
        onPlanPackage={onPlanPackage}
        onStageDeploy={onStageDeploy}
      />
      <MissionPackageDownloadHandoffCard
        handoff={missionPackageHandoff}
        manifest={manifest}
      />

      <details className="package-verification-drawer" data-testid="package-advanced-verification">
        <summary>
          <span className="package-verification-summary-copy">
            <span className="section-kicker">Advanced verification</span>
            <strong>Proof, readiness, and execution contract</strong>
            <small>Open when an operator needs to inspect why this package can or cannot deploy.</small>
          </span>
          <Badge value={readinessVerdict.label} />
        </summary>

        <div className="package-verification-stack">
          <ReadinessVerdictPanel verdict={readinessVerdict} onAction={onReadinessAction} />

          <EdgeRuntimeMissionPanel mission={runtimeMission} />

          <EdgeProofPanel
            componentDigests={componentDigests}
            disabled={disabled}
            handoff={edgeProofHandoff}
            proof={proof}
            trace={trace}
            workflow={workflow}
            onGenerate={onGenerateEdgeProof}
            onDownload={onDownloadEdgeProof}
            onCopy={onCopyCommand}
          />

          <EdgeExecutionContractPanel
            device={selectedDevice}
            edgeRuntimeFit={edgeRuntimeFit}
            edgeExecutionContract={edgeExecutionContract}
            model={selectedModel}
            readiness={readiness}
            readinessVerdict={readinessVerdict}
            resourceEnvelopeFit={resourceEnvelopeFit}
            runtime={selectedRuntime}
            runtimeDecision={runtimeDecision}
            runtimeFitDisplay={runtimeFitDisplay}
            runtimeValidation={selectedRuntimeValidation}
            onCopyRemediation={onCopyCommand}
            onSelectRuntimeTarget={onSelectRuntimeTarget}
          />
        </div>
      </details>
    </div>
  );
}
