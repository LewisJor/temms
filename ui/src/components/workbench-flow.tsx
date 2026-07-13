import {
  Activity,
  ArrowLeft,
  ArrowRight,
  Cpu,
  Download,
  PackageCheck,
  RefreshCw,
  Rocket,
  ShieldCheck
} from "lucide-react";
import type { ReactNode } from "react";
import type {
  GateTone,
  HubStage,
  HubStageItem,
  HubStageRunbook,
  HubStageRunbookAction,
  MissionWorkflowSignal
} from "../lib/workbench-types";
import { Badge } from "./ui";

export function MissionWorkflowCockpit({
  activeStage,
  children,
  contextState,
  runbook,
  signals,
  stages,
  onSelect
}: {
  activeStage: HubStage;
  children: ReactNode;
  contextState: string;
  runbook: HubStageRunbook;
  signals: MissionWorkflowSignal[];
  stages: HubStageItem[];
  onSelect: (stage: HubStage) => void;
}): JSX.Element {
  const activeIndex = Math.max(0, stages.findIndex((stage) => stage.id === activeStage));
  const current = stages[activeIndex] ?? stages[0];
  const previous = activeIndex > 0 ? stages[activeIndex - 1] : undefined;
  const next = activeIndex < stages.length - 1 ? stages[activeIndex + 1] : undefined;
  const primaryAction = runbook.actions.find((action) => action.variant !== "secondary") ?? runbook.actions[0];
  const secondaryActions = runbook.actions.filter((action) => action !== primaryAction);

  return (
    <section
      className={`mission-workflow-cockpit mission-workflow-cockpit-${current.tone}`}
      aria-label="Mission package workflow cockpit"
      data-testid="mission-workflow-cockpit"
    >
      <nav className="operator-path-rail" aria-label="Mission package operator path">
        {stages.map((stage, index) => (
          <button
            aria-label={`${index + 1}. ${stage.label}. ${stage.value}. ${stage.detail}`}
            aria-current={activeStage === stage.id ? "step" : undefined}
            className={`operator-path-step operator-path-step-${stage.tone}${
              activeStage === stage.id ? " operator-path-step-active" : ""
            }`}
            data-stage-id={stage.id}
            data-testid={`operator-flow-${stage.id}`}
            key={stage.id}
            type="button"
            onClick={() => onSelect(stage.id)}
          >
            <span className="operator-path-index">{index + 1}</span>
            <span className="operator-path-copy">
              <strong>{stage.label}</strong>
              <small>{stage.value}</small>
            </span>
          </button>
        ))}
      </nav>

      <div className="stage-focus-panel" data-testid="stage-focus-panel">
        <div className="stage-focus-copy">
          <span className="section-kicker">Current stage</span>
          <h2>{current.label}</h2>
          <strong>{current.value}</strong>
          <p>{current.decision}</p>
        </div>

        <div className="stage-focus-outcome">
          <StageRunbookFact label="Ready when" value={runbook.ready} tone={runbook.tone} />
          <StageRunbookFact label="Risk" value={runbook.risk} tone={runbook.tone === "good" ? "neutral" : runbook.tone} />
        </div>

        <div className="stage-focus-actions" aria-label="Primary stage actions">
          <button
            className="button button-secondary"
            disabled={!previous}
            type="button"
            onClick={() => previous && onSelect(previous.id)}
          >
            <ArrowLeft size={16} />
            <span>{previous ? previous.label : "Start"}</span>
          </button>
          {primaryAction ? (
            <button
              className={`button${primaryAction.variant === "secondary" ? " button-secondary" : ""}`}
              disabled={primaryAction.disabled}
              type="button"
              onClick={primaryAction.onClick}
              title={primaryAction.detail}
            >
              {runbookActionIcon(primaryAction.icon)}
              <span>{primaryAction.label}</span>
            </button>
          ) : null}
          <button
            className="button"
            disabled={!next}
            type="button"
            onClick={() => next && onSelect(next.id)}
          >
            <span>{next ? `Next: ${next.label}` : "Complete"}</span>
            <ArrowRight size={16} />
          </button>
        </div>

        {secondaryActions.length ? (
          <div className="stage-secondary-actions" aria-label="Secondary stage actions">
            {secondaryActions.map((action) => (
              <button
                className="button-mini"
                disabled={action.disabled}
                key={action.label}
                type="button"
                onClick={action.onClick}
                title={action.detail}
              >
                {runbookActionIcon(action.icon)}
                <span>{action.label}</span>
              </button>
            ))}
          </div>
        ) : null}
      </div>

      <aside className="mission-signal-panel" aria-label="Mission package signals">
        <div className="mission-signal-header">
          <span className="section-kicker">Package path</span>
          <Badge value={contextState} />
        </div>
        <div className="mission-signal-grid">
          {signals.map((signal) => (
            <div className={`mission-signal mission-signal-${signal.tone}`} key={signal.label}>
              <span>{signal.label}</span>
              <strong>{signal.value}</strong>
              <small>{signal.detail}</small>
            </div>
          ))}
        </div>
        <details className="mission-context-drawer">
          <summary>
            <span>Live context</span>
            <Badge value="inventory" />
          </summary>
          {children}
        </details>
      </aside>
    </section>
  );
}

export function StatusTile({
  label,
  value,
  detail,
  icon
}: {
  label: string;
  value: number | string;
  detail: string;
  icon: ReactNode;
}): JSX.Element {
  return (
    <div className="status-tile">
      <span className="status-icon">{icon}</span>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function StageRunbookFact({
  label,
  tone,
  value
}: {
  label: string;
  tone: GateTone;
  value: string;
}): JSX.Element {
  return (
    <div className={`stage-fact stage-fact-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function runbookActionIcon(icon: HubStageRunbookAction["icon"]): JSX.Element {
  const size = 16;
  switch (icon) {
    case "activity":
      return <Activity size={size} />;
    case "cpu":
      return <Cpu size={size} />;
    case "download":
      return <Download size={size} />;
    case "package":
      return <PackageCheck size={size} />;
    case "refresh":
      return <RefreshCw size={size} />;
    case "rocket":
      return <Rocket size={size} />;
    case "shield":
      return <ShieldCheck size={size} />;
    case "arrow":
    default:
      return <ArrowRight size={size} />;
  }
}
