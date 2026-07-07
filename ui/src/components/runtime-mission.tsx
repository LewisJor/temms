import type { EdgeRuntimeMission } from "../lib/workbench-types";

export function EdgeRuntimeMissionPanel({ mission }: { mission: EdgeRuntimeMission }): JSX.Element {
  return (
    <section className={`edge-mission edge-mission-${mission.tone}`} aria-labelledby="edge-mission-heading">
      <div className="edge-mission-header">
        <div>
          <span className="section-kicker">Edge runtime mission</span>
          <h2 id="edge-mission-heading">{mission.headline}</h2>
          <p>{mission.detail}</p>
        </div>
        <code>{mission.path}</code>
      </div>
      <div className="edge-mission-grid" aria-label="Selected on-device runtime proof">
        {mission.metrics.map((metric) => (
          <div className={`edge-mission-metric edge-mission-metric-${metric.tone}`} key={metric.label}>
            <span>{metric.label}</span>
            <strong>{metric.value}</strong>
            <small>{metric.detail}</small>
          </div>
        ))}
      </div>
      <div className="edge-mission-focus" aria-label="Operator focus">
        {mission.focus.map((item) => (
          <span key={item}>{item}</span>
        ))}
      </div>
    </section>
  );
}
