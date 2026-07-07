import { ArrowRight, Clipboard, PackageCheck, UploadCloud } from "lucide-react";
import { useRef } from "react";
import { deviceId, runtimeTargetId } from "../lib/hub-format";
import { asRecord, stringOf } from "../lib/json";
import type { MissionDraft } from "../lib/mission-spec";
import type { ModelRecord } from "../lib/workbench-types";
import type { Device, JsonObject, RuntimeTarget } from "../types";
import { Badge, CapabilityMetric } from "./ui";

export function MissionDesignPanel({
  draft,
  manifest,
  selectedDevice,
  selectedModel,
  selectedRuntime,
  onChange,
  onCopyManifest,
  onImportYaml,
  onImportYamlError,
  onPlanPackage
}: {
  draft: MissionDraft;
  manifest: JsonObject;
  selectedDevice: Device | undefined;
  selectedModel: ModelRecord | undefined;
  selectedRuntime: RuntimeTarget | undefined;
  onChange: (draft: MissionDraft) => void;
  onCopyManifest: () => void;
  onImportYaml: (yaml: string, fileName: string) => void;
  onImportYamlError: (fileName: string) => void;
  onPlanPackage: () => void;
}): JSX.Element {
  const yamlFileInputRef = useRef<HTMLInputElement>(null);
  const update = (key: keyof MissionDraft, value: string): void => {
    onChange({ ...draft, [key]: value });
  };
  const importMissionYamlFile = (file: File | undefined): void => {
    if (!file) return;
    void file
      .text()
      .then((yaml) => onImportYaml(yaml, file.name))
      .catch(() => onImportYamlError(file.name));
  };
  const selectedRuntimeId = selectedRuntime ? runtimeTargetId(selectedRuntime) : "runtime pending";
  const selectedDeviceId = selectedDevice ? deviceId(selectedDevice) : "edge pending";
  const manifestMission = asRecord(manifest.mission);
  const manifestHandling = asRecord(manifest.model_handling);

  return (
    <section className="mission-builder" aria-labelledby="mission-builder-heading">
      <div className="mission-builder-header">
        <div>
          <span className="section-kicker">Mission spec</span>
          <h2 id="mission-builder-heading">Define what the edge system must accomplish</h2>
        </div>
        <div className="mission-builder-actions">
          <input
            ref={yamlFileInputRef}
            className="visually-hidden"
            type="file"
            accept=".yaml,.yml,text/yaml,application/x-yaml,text/plain"
            onChange={(event) => {
              importMissionYamlFile(event.currentTarget.files?.[0]);
              event.currentTarget.value = "";
            }}
          />
          <Badge value="temms-edge-mission-package/v1" />
          <button
            className="button button-secondary"
            type="button"
            onClick={() => yamlFileInputRef.current?.click()}
          >
            <UploadCloud size={16} />
            <span>Import YAML</span>
          </button>
          <button className="button" type="button" onClick={onPlanPackage}>
            <PackageCheck size={16} />
            <span>Plan package</span>
          </button>
          <button className="button button-secondary" type="button" onClick={onCopyManifest}>
            <Clipboard size={16} />
            <span>Copy package manifest</span>
          </button>
        </div>
      </div>

      <div className="mission-builder-grid">
        <div className="mission-builder-form">
          <label className="field">
            <span>Mission goal</span>
            <textarea
              rows={4}
              value={draft.goal}
              onChange={(event) => update("goal", event.target.value)}
            />
          </label>
          <label className="field">
            <span>Mission YAML</span>
            <textarea
              rows={8}
              placeholder="schema_version: temms-edge-mission/v1"
              value={draft.yaml}
              onChange={(event) => update("yaml", event.target.value)}
            />
          </label>
        </div>

        <div className="mission-package-preview">
          <div className="mission-preview-header">
            <span className="section-kicker">Package plan</span>
            <strong>{selectedModel?.name ?? "model pending"}</strong>
            <small>
              {selectedRuntimeId} on {selectedDeviceId}
            </small>
          </div>
          <div className="mission-preview-grid">
            <CapabilityMetric
              label="Goal"
              value={draft.goal ? "defined" : draft.yaml ? "yaml loaded" : "pending"}
              detail={String(manifestMission.goal || "mission goal pending")}
              tone={draft.goal || draft.yaml ? "good" : "warn"}
            />
            <CapabilityMetric
              label="Sensor"
              value={draft.sensor}
              detail={`slot ${draft.slot || "vision"}`}
              tone={draft.sensor ? "good" : "warn"}
            />
            <CapabilityMetric
              label="Switching"
              value={String(manifestHandling.switch_policy || "pending").replace(/_/g, " ")}
              detail={`threshold ${draft.confidenceThreshold}; fallback ${draft.fallbackModelId || "auto"}`}
              tone="good"
            />
            <CapabilityMetric
              label="Edge package"
              value={selectedModel && selectedRuntime && selectedDevice ? "ready to prove" : "needs context"}
              detail={`${selectedModel?.packageId ?? "package"} -> ${selectedRuntimeId} -> ${selectedDeviceId}`}
              tone={selectedModel && selectedRuntime && selectedDevice ? "good" : "warn"}
            />
          </div>
        </div>
      </div>
    </section>
  );
}

export function HandlingPolicyPanel({
  draft,
  manifest,
  models,
  selectedDevice,
  selectedModel,
  selectedRuntime,
  onChange,
  onGoPackage,
  onPlanPackage
}: {
  draft: MissionDraft;
  manifest: JsonObject;
  models: ModelRecord[];
  selectedDevice: Device | undefined;
  selectedModel: ModelRecord | undefined;
  selectedRuntime: RuntimeTarget | undefined;
  onChange: (draft: MissionDraft) => void;
  onGoPackage: () => void;
  onPlanPackage: () => void;
}): JSX.Element {
  const update = (key: keyof MissionDraft, value: string): void => {
    onChange({ ...draft, [key]: value });
  };
  const selectedRuntimeId = selectedRuntime ? runtimeTargetId(selectedRuntime) : "runtime pending";
  const selectedDeviceId = selectedDevice ? deviceId(selectedDevice) : "edge pending";
  const manifestHandling = asRecord(manifest.model_handling);
  const manifestSlo = asRecord(manifest.slo);
  const manifestDdil = asRecord(manifest.ddil);
  const packageReady = Boolean(selectedModel && selectedRuntime && selectedDevice && draft.sensor && draft.slot);

  return (
    <section className="mission-builder handling-policy" aria-labelledby="handling-policy-heading">
      <div className="mission-builder-header">
        <div>
          <span className="section-kicker">Sensor and model handling</span>
          <h2 id="handling-policy-heading">Define how the edge should switch, fallback, and operate through DDIL</h2>
        </div>
        <div className="mission-builder-actions">
          <Badge value={draft.slot || "slot pending"} />
          <button className="button" type="button" onClick={onPlanPackage}>
            <PackageCheck size={16} />
            <span>Plan package</span>
          </button>
          <button
            className="button button-secondary"
            data-testid="handling-go-package"
            disabled={!packageReady}
            type="button"
            onClick={onGoPackage}
          >
            <span>Continue to Package Handoff</span>
            <ArrowRight size={16} />
          </button>
        </div>
      </div>

      <div className="handling-policy-grid">
        <div className="mission-params">
          <label className="field">
            <span>Sensor input</span>
            <select value={draft.sensor} onChange={(event) => update("sensor", event.target.value)}>
              <option value="camera.rgb">camera.rgb</option>
              <option value="camera.lowlight">camera.lowlight</option>
              <option value="thermal.stream">thermal.stream</option>
              <option value="audio.array">audio.array</option>
              <option value="multisensor.fusion">multisensor.fusion</option>
            </select>
          </label>
          <label className="field">
            <span>Slot</span>
            <input value={draft.slot} onChange={(event) => update("slot", event.target.value)} />
          </label>
          <label className="field">
            <span>Latency budget p95 ms</span>
            <input value={draft.latencyBudgetMs} onChange={(event) => update("latencyBudgetMs", event.target.value)} />
          </label>
          <label className="field">
            <span>Min throughput ips</span>
            <input value={draft.throughputMinIps} onChange={(event) => update("throughputMinIps", event.target.value)} />
          </label>
          <label className="field">
            <span>Model switch policy</span>
            <select value={draft.switchPolicy} onChange={(event) => update("switchPolicy", event.target.value)}>
              <option value="condition_and_confidence">condition and confidence</option>
              <option value="sensor_degradation">sensor degradation</option>
              <option value="runtime_health">runtime health</option>
              <option value="operator_approval">operator approval</option>
            </select>
          </label>
          <label className="field">
            <span>Switch confidence threshold</span>
            <input value={draft.confidenceThreshold} onChange={(event) => update("confidenceThreshold", event.target.value)} />
          </label>
          <label className="field">
            <span>Fallback model</span>
            <select value={draft.fallbackModelId} onChange={(event) => update("fallbackModelId", event.target.value)}>
              <option value="">auto-select compatible fallback</option>
              {models.map((model) => (
                <option key={`${model.packageId}-${model.id}`} value={model.id}>
                  {model.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>DDIL behavior</span>
            <select value={draft.ddilMode} onChange={(event) => update("ddilMode", event.target.value)}>
              <option value="queue_signed_intents">queue signed intents</option>
              <option value="require_local_proof">require local proof</option>
              <option value="fail_closed">fail closed</option>
              <option value="operator_repair">operator repair</option>
            </select>
          </label>
        </div>

        <div className="mission-package-preview">
          <div className="mission-preview-header">
            <span className="section-kicker">Handling plan</span>
            <strong>{selectedModel?.name ?? "model pending"}</strong>
            <small>
              {selectedRuntimeId} on {selectedDeviceId}
            </small>
          </div>
          <div className="mission-preview-grid">
            <CapabilityMetric
              label="Sensor"
              value={draft.sensor}
              detail={`slot ${draft.slot || "vision"}`}
              tone={draft.sensor && draft.slot ? "good" : "warn"}
            />
            <CapabilityMetric
              label="SLO"
              value={`${stringOf(manifestSlo.latency_budget_ms, draft.latencyBudgetMs)} ms`}
              detail={`${stringOf(manifestSlo.min_throughput_ips, draft.throughputMinIps)} ips minimum`}
              tone={draft.latencyBudgetMs && draft.throughputMinIps ? "good" : "warn"}
            />
            <CapabilityMetric
              label="Switching"
              value={String(manifestHandling.switch_policy || draft.switchPolicy).replace(/_/g, " ")}
              detail={`threshold ${draft.confidenceThreshold}; fallback ${draft.fallbackModelId || "auto"}`}
              tone="good"
            />
            <CapabilityMetric
              label="DDIL"
              value={String(manifestDdil.mode || draft.ddilMode).replace(/_/g, " ")}
              detail="policy is embedded into the mission package handoff"
              tone="good"
            />
          </div>
        </div>
      </div>
    </section>
  );
}
