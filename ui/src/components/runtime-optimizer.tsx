import type { EdgeRecommendation } from "../types";
import { asRecord, numberOf, stringOf } from "../lib/json";
import { artifactLaneValue, formatMetricNumber, formatThroughput } from "../lib/runtime-fit";
import { Badge } from "./ui";

export function EdgeRecommendationPanel({
  recommendations,
  selectedModelId,
  selectedDeviceId,
  selectedRuntimeId,
  onSelect
}: {
  recommendations: EdgeRecommendation[];
  selectedModelId: string;
  selectedDeviceId: string;
  selectedRuntimeId: string;
  onSelect: (recommendation: EdgeRecommendation) => void;
}): JSX.Element {
  const visible = recommendations.slice(0, 3);
  if (!visible.length) {
    return (
      <div className="edge-recommendations edge-recommendations-empty">
        <div>
          <span className="section-kicker">Runtime optimizer</span>
          <strong>Recommendation score pending</strong>
        </div>
        <p>Register packages, edge inventory, and runtime targets to rank deployment paths.</p>
      </div>
    );
  }
  return (
    <div className="edge-recommendations" aria-label="Ranked edge runtime recommendations">
      <div className="edge-recommendations-header">
        <div>
          <span className="section-kicker">Runtime optimizer</span>
          <strong>Best edge paths</strong>
        </div>
        <span>{visible.length} ranked</span>
      </div>
      <div className="edge-recommendation-grid">
        {visible.map((recommendation) => {
          const runtimeId = recommendation.runtime_target_id
            ? String(recommendation.runtime_target_id)
            : "device inventory";
          const modelId = recommendation.model_id ? String(recommendation.model_id) : "package";
          const device = recommendation.device_id ? String(recommendation.device_id) : "edge";
          const selected =
            modelId === selectedModelId &&
            device === selectedDeviceId &&
            (recommendation.runtime_target_id ? runtimeId === selectedRuntimeId : !selectedRuntimeId);
          const optimization = asRecord(recommendation.optimization);
          const runtimeFit = asRecord(recommendation.runtime_fit);
          const artifactLane = asRecord(recommendation.artifact_lane ?? runtimeFit.artifact_lane);
          const runtimeFitScore = numberOf(runtimeFit.score);
          const runtimeFitTier = stringOf(runtimeFit.tier, "fit").replace(/_/g, " ");
          const latency = metricText(optimization.latency_ms_p95);
          const throughput = throughputText(optimization.throughput_ips);
          const action = (recommendation.required_actions ?? [])[0];
          return (
            <article
              className={`edge-recommendation edge-recommendation-${recommendationTone(recommendation)}${
                selected ? " edge-recommendation-selected" : ""
              }`}
              key={`${recommendation.rank}-${modelId}-${device}-${runtimeId}`}
            >
              <div className="edge-recommendation-topline">
                <span>#{recommendation.rank ?? "-"}</span>
                <strong>{recommendation.score ?? 0}</strong>
              </div>
              <div>
                <Badge value={formatRecommendationDecision(recommendation.decision)} />
                <h3>{modelId}</h3>
                <p>{device} / {runtimeId}</p>
              </div>
              <p className="edge-recommendation-reason">
                {recommendation.primary_reason || action || "Review this target"}
              </p>
              <div className="edge-recommendation-metrics">
                <span>
                  {runtimeFitScore !== undefined
                    ? `${runtimeFitScore}/100 ${runtimeFitTier}`
                    : `${recommendation.confidence || "low"} confidence`}
                </span>
                {latency ? <span>{latency} ms p95</span> : null}
                {throughput ? <span>{throughput} ips</span> : null}
                {Object.keys(artifactLane).length ? <span>{artifactLaneValue(artifactLane)}</span> : null}
              </div>
              <button
                className="button button-ghost"
                type="button"
                onClick={() => onSelect(recommendation)}
                disabled={selected}
              >
                <span>{selected ? "Selected" : "Use path"}</span>
              </button>
            </article>
          );
        })}
      </div>
    </div>
  );
}

function formatRecommendationDecision(value?: string): string {
  if (!value) return "review";
  return value.replace(/_/g, " ");
}

function metricText(value: unknown): string {
  if (value === undefined || value === null || value === "") return "";
  const numeric = numberOf(value);
  if (numeric !== undefined) return formatMetricNumber(numeric);
  return String(value);
}

function throughputText(value: unknown): string {
  if (value === undefined || value === null || value === "") return "";
  const numeric = numberOf(value);
  if (numeric !== undefined) return formatThroughput(numeric);
  return String(value);
}

function recommendationTone(recommendation: EdgeRecommendation): "good" | "warn" | "bad" {
  if (recommendation.decision === "deploy") return "good";
  if (recommendation.decision === "blocked") return "bad";
  return "warn";
}
