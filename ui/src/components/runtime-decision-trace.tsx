import { Clipboard } from "lucide-react";
import { stringOf } from "../lib/json";
import type {
  RuntimeRemediationCommand,
  RuntimeRemediationContext,
  RuntimeWorkbenchRow
} from "../lib/workbench-types";
import { Badge } from "./ui";

export function RuntimeDecisionTrace({
  commandForRow,
  context,
  onCopyCommand,
  rows
}: {
  commandForRow: (
    row: RuntimeWorkbenchRow,
    context: RuntimeRemediationContext
  ) => RuntimeRemediationCommand | undefined;
  context: RuntimeRemediationContext;
  onCopyCommand: (label: string, command: string) => void;
  rows: RuntimeWorkbenchRow[];
}): JSX.Element {
  return (
    <details className="runtime-decision-trace" data-testid="runtime-decision-trace">
      <summary className="runtime-decision-trace-header">
        <div>
          <span className="section-kicker">Runtime decision trace</span>
          <strong>Ranked on-device capability proof</strong>
        </div>
        <Badge value={`${rows.filter((row) => row.compatible).length}/${rows.length} eligible`} />
      </summary>
      <div className="runtime-decision-trace-grid">
        {rows.map((row) => (
          <RuntimeDecisionTraceItem
            commandForRow={commandForRow}
            context={context}
            key={row.targetId}
            onCopyCommand={onCopyCommand}
            row={row}
          />
        ))}
      </div>
    </details>
  );
}

function RuntimeDecisionTraceItem({
  commandForRow,
  context,
  onCopyCommand,
  row
}: {
  commandForRow: (
    row: RuntimeWorkbenchRow,
    context: RuntimeRemediationContext
  ) => RuntimeRemediationCommand | undefined;
  context: RuntimeRemediationContext;
  onCopyCommand: (label: string, command: string) => void;
  row: RuntimeWorkbenchRow;
}): JSX.Element {
  const command = commandForRow(row, context);
  const reason = runtimeWorkbenchTraceReason(row);
  return (
    <article className={`runtime-decision-trace-item runtime-decision-trace-item-${row.tone}`}>
      <div className="runtime-decision-trace-topline">
        <div>
          <span>{runtimeWorkbenchTraceRank(row)}</span>
          <strong>{row.targetId}</strong>
        </div>
        <Badge value={runtimeWorkbenchTraceBadge(row)} />
      </div>
      <p>{reason}</p>
      <div className="runtime-decision-trace-metrics">
        {row.traceMetrics.map((metric) => (
          <div className={`runtime-decision-trace-metric runtime-decision-trace-metric-${metric.tone}`} key={`${row.targetId}-${metric.label}`}>
            <span>{metric.label}</span>
            <strong>{metric.value}</strong>
            <small>{metric.detail}</small>
          </div>
        ))}
      </div>
      <div className="runtime-decision-trace-command">
        <div>
          <span>{row.actionRequiresEdge ? "edge-run action" : "operator action"}</span>
          <strong>{row.actionLabel || row.actionKind || "Review runtime path"}</strong>
          <small>{command?.note || runtimeWorkbenchTraceActionDetail(row)}</small>
        </div>
        {command ? (
          <button className="button-mini" type="button" onClick={() => onCopyCommand(command.label, command.command)}>
            <Clipboard size={13} />
            Copy
          </button>
        ) : null}
      </div>
    </article>
  );
}

function runtimeWorkbenchTraceRank(row: RuntimeWorkbenchRow): string {
  if (row.rank !== undefined) return `rank ${row.rank}`;
  if (row.selected && row.best) return "selected best";
  if (row.selected) return "selected";
  if (row.best) return "best";
  return row.status;
}

function runtimeWorkbenchTraceBadge(row: RuntimeWorkbenchRow): string {
  const labels = [];
  if (row.selected) labels.push("selected");
  if (row.best) labels.push("best");
  if (!labels.length) labels.push(row.compatible ? "eligible" : "blocked");
  if (row.score !== undefined) labels.push(`${row.score}/100`);
  return labels.join(" / ");
}

function runtimeWorkbenchTraceReason(row: RuntimeWorkbenchRow): string {
  return (
    row.reasons[0] ||
    row.penalties[0] ||
    stringOf(row.remediation.detail, "") ||
    row.detail ||
    "runtime target assessed"
  );
}

function runtimeWorkbenchTraceActionDetail(row: RuntimeWorkbenchRow): string {
  return (
    stringOf(row.remediation.operator_command_note, "") ||
    stringOf(row.remediation.edge_command_note, "") ||
    stringOf(row.remediation.detail, "") ||
    row.detail ||
    "review this runtime path"
  );
}
