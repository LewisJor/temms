import { Clipboard, PlayCircle, RefreshCw } from "lucide-react";
import { displayGateState } from "../lib/hub-format";
import {
  edgeReadinessCommandReason,
  readinessActionContext,
  readinessActionTitle,
  readinessApiCommandText,
  readinessCommand
} from "../lib/mission-workflow";
import type { ReadinessGateAction, ReadinessVerdict } from "../lib/workbench-types";
import { Badge, Button } from "./ui";

export function ReadinessVerdictPanel({
  verdict,
  onAction
}: {
  verdict: ReadinessVerdict;
  onAction: (action: ReadinessGateAction) => void;
}): JSX.Element {
  return (
    <section
      className={`readiness-verdict readiness-verdict-${verdict.tone}`}
      aria-labelledby="readiness-verdict-heading"
    >
      <div className="verdict-copy">
        <span className="section-kicker">Operational verdict</span>
        <h2 id="readiness-verdict-heading">{verdict.headline}</h2>
        <p>{verdict.detail}</p>
      </div>
      <div className="verdict-action">
        <Badge value={verdict.label} />
        <strong>{verdict.nextAction}</strong>
      </div>
      <div className="verdict-gates" aria-label="Deployment readiness gates">
        {verdict.gates.map((gate) => {
          const visibleActions = (gate.actions ?? []).filter(shouldRenderVerdictAction);
          return (
            <div className={`verdict-gate verdict-gate-${gate.tone}`} key={gate.label}>
              <span>{gate.label}</span>
              <strong>{displayGateState(gate.state)}</strong>
              <small>{gate.detail}</small>
              {visibleActions.length ? (
                <div className="verdict-gate-actions" aria-label={`${gate.label} actions`}>
                  {visibleActions.map((action) => (
                    <button
                      key={action.id || action.label}
                      type="button"
                      onClick={() => onAction(action)}
                      title={readinessActionTitle(action)}
                    >
                      {action.label}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function shouldRenderVerdictAction(action: ReadinessGateAction): boolean {
  return ![
    "acknowledge_dead_letters",
    "quarantine_blocked",
    "restore_online",
    "sync_pending"
  ].includes(action.kind);
}

export function ReadinessCommandPanel({
  action,
  disabled,
  onCopy,
  onClose,
  onRun
}: {
  action: ReadinessGateAction;
  disabled: boolean;
  onCopy: (label: string, command: string) => void;
  onClose: () => void;
  onRun: () => void;
}): JSX.Element {
  const command = readinessCommand(action);
  const context = readinessActionContext(action);
  const body = command?.body && Object.keys(command.body).length ? JSON.stringify(command.body, null, 2) : "";
  const edgeCommand = command?.edge_command_text || (command?.edge_command ?? []).join(" ");
  if (!command) return <></>;
  const commandTitle = command.requires_edge_execution ? "Edge execution command" : "API command";
  const commandDetail = command.requires_edge_execution
    ? edgeReadinessCommandReason(action, command)
    : "This command can be executed by the Hub operator from the browser.";
  const apiCommand = readinessApiCommandText(command);

  return (
    <section className="remediation-review" aria-labelledby="remediation-heading">
      <div className="remediation-copy">
        <span className="section-kicker">Readiness remediation</span>
        <h2 id="remediation-heading">{action.label}</h2>
        <p>{context || "Operator-confirmed command from the selected readiness gate."}</p>
        <small>{commandDetail}</small>
      </div>
      <div className="remediation-command" aria-label="Readiness command">
        <div className="remediation-command-topline">
          <Badge value={command.requires_edge_execution ? "edge-run" : command.method ?? "command"} />
          <strong>{commandTitle}</strong>
          <button
            className="button-mini"
            type="button"
            onClick={() =>
              onCopy(
                command.requires_edge_execution ? `${action.label} edge command` : `${action.label} API command`,
                command.requires_edge_execution && edgeCommand ? edgeCommand : apiCommand
              )
            }
          >
            <Clipboard size={14} />
            Copy
          </button>
        </div>
        <code>{command.path}</code>
        <details className="payload-details" open={Boolean(body)}>
          <summary>Request body</summary>
          <pre>{body || "No request body"}</pre>
        </details>
        {edgeCommand ? (
          <details className="payload-details" open>
            <summary>Edge command</summary>
            <pre>{edgeCommand}</pre>
            {command.edge_command_note ? <small>{command.edge_command_note}</small> : null}
          </details>
        ) : null}
      </div>
      <div className="remediation-actions">
        <Button
          icon={<PlayCircle size={16} />}
          disabled={disabled || command.requires_edge_execution}
          onClick={onRun}
        >
          {command.requires_edge_execution ? "Run on edge" : "Run command"}
        </Button>
        <Button icon={<RefreshCw size={16} />} variant="secondary" disabled={disabled} onClick={onClose}>
          Close
        </Button>
      </div>
    </section>
  );
}
