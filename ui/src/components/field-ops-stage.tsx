import {
  Activity,
  CheckCircle2,
  Database,
  Download,
  FileCheck2,
  GitBranch,
  RefreshCw,
  Rocket,
  ShieldCheck,
  UploadCloud
} from "lucide-react";
import type { RefObject } from "react";
import { compactDate } from "../lib/hub-format";
import { stringOf } from "../lib/json";
import type { ModelRecord, RuntimeRepairProof } from "../lib/workbench-types";
import type {
  Device,
  EvidenceBundleRecord,
  EvidenceExportMode,
  EvidenceSummary,
  MissionReplayPhase
} from "../types";
import {
  EmptyState,
  EvidenceSummaryRow,
  MissionPhaseRow,
  TargetRow
} from "./deploy-lists";
import {
  DeadLetteredOperationRow,
  EvidenceEventRow,
  PendingOperationRow,
  RuntimeRepairProofPanel
} from "./field-ops";
import { Badge, Button, ReadinessCard } from "./ui";

export interface FieldOpsStageProps {
  activeSlot: Record<string, unknown> | undefined;
  completedMissionPhases: number;
  connectivityState: string;
  ddilSectionClassName: string;
  ddilRef: RefObject<HTMLElement>;
  deadLetteredOperationLedger: Record<string, unknown>[];
  deadLetteredOperations: number;
  deploymentDetail: string;
  deploymentStateName: string;
  evidenceBundles: EvidenceBundleRecord[];
  evidenceSectionClassName: string;
  evidenceRef: RefObject<HTMLElement>;
  evidenceSummary: EvidenceSummary | undefined;
  incompleteMissionPhases: string[];
  latestEvents: Record<string, unknown>[];
  missionPhaseTotal: number;
  missionPhases: MissionReplayPhase[];
  missionProofComplete: boolean;
  missionReplayHeadline: string | undefined;
  offlineMode: boolean;
  pendingOperationLedger: Record<string, unknown>[];
  pendingOperations: number;
  proofEvents: number;
  replayBlockedOperations: number;
  runtimeRepairProof: RuntimeRepairProof | undefined;
  selectedDevice: Device | undefined;
  selectedModel: ModelRecord | undefined;
  signedEvidenceImports: number;
  onAcknowledgeDeadLetteredOperations: () => void;
  onCopyCommand: (label: string, command: string) => void;
  onEnterOfflineMode: () => void;
  onExportAirgap: (includePackages: boolean) => void;
  onExportEvidence: (mode: EvidenceExportMode) => void;
  onQuarantineBlockedOperations: () => void;
  onQueueDeploymentIntent: () => void;
  onRequeueDeadLetteredOperation: (operation: Record<string, unknown>) => void;
  onRequeueDeadLetteredOperations: () => void;
  onRestoreOnlineMode: () => void;
  onRetargetRuntime: (operation: Record<string, unknown>) => void;
  onSyncPendingOperations: () => void;
}

export function FieldOpsStage({
  activeSlot,
  completedMissionPhases,
  connectivityState,
  ddilSectionClassName,
  ddilRef,
  deadLetteredOperationLedger,
  deadLetteredOperations,
  deploymentDetail,
  deploymentStateName,
  evidenceBundles,
  evidenceSectionClassName,
  evidenceRef,
  evidenceSummary,
  incompleteMissionPhases,
  latestEvents,
  missionPhaseTotal,
  missionPhases,
  missionProofComplete,
  missionReplayHeadline,
  offlineMode,
  pendingOperationLedger,
  pendingOperations,
  proofEvents,
  replayBlockedOperations,
  runtimeRepairProof,
  selectedDevice,
  selectedModel,
  signedEvidenceImports,
  onAcknowledgeDeadLetteredOperations,
  onCopyCommand,
  onEnterOfflineMode,
  onExportAirgap,
  onExportEvidence,
  onQuarantineBlockedOperations,
  onQueueDeploymentIntent,
  onRequeueDeadLetteredOperation,
  onRequeueDeadLetteredOperations,
  onRestoreOnlineMode,
  onRetargetRuntime,
  onSyncPendingOperations
}: FieldOpsStageProps): JSX.Element {
  return (
    <>
      <section
        className={ddilSectionClassName}
        aria-labelledby="readiness-heading"
        ref={ddilRef}
        tabIndex={-1}
      >
        <div className="section-header">
          <div>
            <span className="section-kicker">DDIL readiness</span>
            <h2 id="readiness-heading">Field operating picture</h2>
          </div>
          <Badge value={offlineMode ? "offline" : pendingOperations ? "pending" : "ready"} />
        </div>

        <div className="readiness-grid">
          <ReadinessCard
            title="Connectivity"
            value={connectivityState}
            detail={
              offlineMode
                ? "link unavailable"
                : pendingOperations
                  ? `${pendingOperations} pending operations`
                  : "network available"
            }
            state={offlineMode ? "warn" : "good"}
          />
          <ReadinessCard
            title="Deployment"
            value={deploymentStateName}
            detail={deploymentDetail}
            state={deploymentStateName === "READY" ? "good" : "warn"}
          />
          <ReadinessCard
            title="Active slot"
            value={stringOf(activeSlot?.slot, "vision")}
            detail={stringOf(activeSlot?.active_model, selectedModel?.id ?? "no active model")}
            state={activeSlot?.active_model ? "good" : "warn"}
          />
          <ReadinessCard
            title="Evidence chain"
            value={`${proofEvents} events`}
            detail={
              deadLetteredOperations
                ? `${deadLetteredOperations} quarantined intent${deadLetteredOperations === 1 ? "" : "s"}`
                : missionPhaseTotal
                  ? `${completedMissionPhases}/${missionPhaseTotal} replay phases`
                  : `${signedEvidenceImports} signed package${signedEvidenceImports === 1 ? "" : "s"}`
            }
            state={
              missionProofComplete || (proofEvents && signedEvidenceImports && !missionPhaseTotal)
                ? "good"
                : "warn"
            }
          />
        </div>

        {runtimeRepairProof ? (
          <RuntimeRepairProofPanel
            proof={runtimeRepairProof}
            onRetargetRuntime={
              runtimeRepairProof.operation ? onRetargetRuntime : undefined
            }
          />
        ) : null}

        {latestEvents.length ? (
          <div className="readiness-timeline" aria-label="Latest evidence events">
            {latestEvents.map((event, index) => (
              <EvidenceEventRow key={`${stringOf(event.timestamp, "event")}-${index}`} event={event} />
            ))}
          </div>
        ) : null}

        {pendingOperationLedger.length ? (
          <div className="pending-operation-ledger" aria-label="Pending DDIL operations">
            {pendingOperationLedger.map((operation, index) => (
              <PendingOperationRow
                key={`${stringOf(operation.payload_sha256, "pending")}-${index}`}
                operation={operation}
                onCopyCommand={onCopyCommand}
                onRetargetRuntime={onRetargetRuntime}
              />
            ))}
          </div>
        ) : null}

        {deadLetteredOperationLedger.length ? (
          <div className="dead-letter-operation-ledger" aria-label="Quarantined DDIL operations">
            <div className="ddil-ledger-heading">
              <span>Quarantined DDIL intents</span>
              <strong>{deadLetteredOperations}</strong>
            </div>
            {deadLetteredOperationLedger.map((operation, index) => (
              <DeadLetteredOperationRow
                key={`${stringOf(operation.payload_sha256, "dead-letter")}-${index}`}
                operation={operation}
                onCopyCommand={onCopyCommand}
                onRequeue={onRequeueDeadLetteredOperation}
              />
            ))}
          </div>
        ) : null}

        <div className="ddil-controls" aria-label="DDIL drill controls">
          <Button icon={<Activity size={16} />} variant="secondary" disabled={offlineMode} onClick={onEnterOfflineMode}>
            Link loss
          </Button>
          <Button icon={<RefreshCw size={16} />} variant="secondary" disabled={!offlineMode} onClick={onRestoreOnlineMode}>
            Restore link
          </Button>
          <Button
            icon={<Rocket size={16} />}
            variant="secondary"
            disabled={!selectedModel || !selectedDevice}
            onClick={onQueueDeploymentIntent}
          >
            Queue intent
          </Button>
          <Button
            icon={<UploadCloud size={16} />}
            variant="secondary"
            disabled={!pendingOperations || Boolean(replayBlockedOperations)}
            onClick={onSyncPendingOperations}
          >
            Sync pending
          </Button>
          <Button
            icon={<Database size={16} />}
            variant="secondary"
            disabled={!replayBlockedOperations}
            onClick={onQuarantineBlockedOperations}
          >
            Quarantine blocked
          </Button>
          <Button
            icon={<FileCheck2 size={16} />}
            variant="secondary"
            disabled={!deadLetteredOperations}
            onClick={onRequeueDeadLetteredOperations}
          >
            Requeue quarantined
          </Button>
          <Button
            icon={<CheckCircle2 size={16} />}
            variant="secondary"
            disabled={!deadLetteredOperations}
            onClick={onAcknowledgeDeadLetteredOperations}
          >
            Acknowledge quarantine
          </Button>
        </div>
      </section>

      <section
        className={evidenceSectionClassName}
        aria-labelledby="evidence-heading"
        ref={evidenceRef}
        tabIndex={-1}
      >
        <div className="section-header">
          <div>
            <span className="section-kicker">Evidence</span>
            <h2 id="evidence-heading">Mission proof</h2>
          </div>
          <span className="section-count">
            {missionPhaseTotal ? `${completedMissionPhases}/${missionPhaseTotal}` : evidenceBundles.length}
          </span>
        </div>
        <div className="button-row">
          <Button icon={<ShieldCheck size={16} />} onClick={() => onExportEvidence("summary")}>
            Summary
          </Button>
          <Button icon={<GitBranch size={16} />} variant="secondary" onClick={() => onExportEvidence("replay")}>
            Replay
          </Button>
          <Button icon={<Download size={16} />} variant="secondary" onClick={() => onExportEvidence("full")}>
            Full bundle
          </Button>
          <Button icon={<Database size={16} />} variant="secondary" onClick={() => onExportAirgap(true)}>
            Air-gap bundle
          </Button>
        </div>
        {missionPhases.length ? (
          <div className="mission-phase-list" aria-label="Mission replay phases">
            <div className="mission-phase-heading">
              <span>{missionReplayHeadline ?? "mission replay"}</span>
              <strong>
                {incompleteMissionPhases.length
                  ? `${incompleteMissionPhases.length} remaining`
                  : "complete"}
              </strong>
            </div>
            {missionPhases.map((phase) => (
              <MissionPhaseRow key={phase.phase ?? phase.label ?? phase.summary} phase={phase} />
            ))}
          </div>
        ) : null}
        {runtimeRepairProof ? <RuntimeRepairProofPanel compact proof={runtimeRepairProof} /> : null}
        <div className="compact-list evidence-list">
          {evidenceBundles.length ? (
            evidenceBundles.slice(0, 4).map((record) => (
              <TargetRow
                key={record.evidence_id ?? `${record.device_id}-${record.created_at}`}
                label={record.evidence_id ?? "evidence"}
                detail={`${record.device_id ?? "unknown device"} - ${compactDate(record.created_at)}`}
                status={record.schema_version ?? "evidence"}
              />
            ))
          ) : proofEvents ? (
            <EvidenceSummaryRow
              headline={evidenceSummary?.headline ?? "mission proof ready"}
              events={proofEvents}
              signedImports={signedEvidenceImports}
            />
          ) : (
            <EmptyState title="No evidence yet" detail="Export or ingest evidence after rollout activity." />
          )}
        </div>
      </section>
    </>
  );
}
