export interface MissionDraft {
  confidenceThreshold: string;
  ddilMode: string;
  fallbackModelId: string;
  goal: string;
  latencyBudgetMs: string;
  sensor: string;
  slot: string;
  switchPolicy: string;
  throughputMinIps: string;
  yaml: string;
}

export interface MissionYamlSelection {
  deviceId: string;
  modelId: string;
  packageId: string;
  runtimeTargetId: string;
}

export const defaultMissionDraft: MissionDraft = {
  confidenceThreshold: "0.65",
  ddilMode: "queue_signed_intents",
  fallbackModelId: "",
  goal: "Detect vehicles in changing light, keep inference local during link loss, and preserve proof for every model/runtime switch.",
  latencyBudgetMs: "95",
  sensor: "camera.rgb",
  slot: "vision",
  switchPolicy: "condition_and_confidence",
  throughputMinIps: "25",
  yaml: ""
};

export function missionDraftFromYaml(current: MissionDraft, yaml: string): MissionDraft {
  const scalars = extractMissionYamlScalars(yaml);
  const read = (...keys: string[]): string => missionYamlScalar(scalars, ...keys);

  return {
    ...current,
    confidenceThreshold:
      read(
        "model_handling.confidence_threshold",
        "switching.confidence_threshold",
        "switch_confidence_threshold",
        "confidence_threshold"
      ) || current.confidenceThreshold,
    ddilMode:
      read("ddil.mode", "ddil_mode", "ddil.behavior", "ddil_behavior", "offline_behavior") ||
      current.ddilMode,
    fallbackModelId:
      read("model_handling.fallback_model_id", "fallback_model_id", "fallback_model", "fallback") ||
      current.fallbackModelId,
    goal: read("mission.goal", "mission_goal", "goal", "objective", "description") || current.goal,
    latencyBudgetMs:
      read(
        "slo.latency_budget_ms",
        "latency_budget_ms",
        "max_latency_ms_p95",
        "latency_ms_p95",
        "latency_ms"
      ) || current.latencyBudgetMs,
    sensor:
      read("mission.sensor", "input.sensor", "sensor_input", "sensor_id", "sensor") || current.sensor,
    slot: read("mission.slot", "capability_slot", "slot") || current.slot,
    switchPolicy:
      read("model_handling.switch_policy", "switching.policy", "model_switch_policy", "switch_policy") ||
      current.switchPolicy,
    throughputMinIps:
      read(
        "slo.min_throughput_ips",
        "min_throughput_ips",
        "throughput_ips",
        "min_inferences_per_second"
      ) || current.throughputMinIps,
    yaml
  };
}

export function missionSelectionFromYaml(yaml: string): MissionYamlSelection {
  const scalars = extractMissionYamlScalars(yaml);
  const read = (...keys: string[]): string => missionYamlScalar(scalars, ...keys);

  return {
    deviceId:
      read(
        "selection.device_id",
        "edge.device_id",
        "edge_device_id",
        "target_device_id",
        "device_id"
      ),
    modelId:
      read(
        "selection.model_id",
        "model.id",
        "selected_model_id",
        "primary_model_id",
        "model_id"
      ),
    packageId:
      read(
        "selection.package_id",
        "model.package_id",
        "artifact.package_id",
        "package.id",
        "package_id"
      ),
    runtimeTargetId:
      read(
        "selection.runtime_target_id",
        "runtime.runtime_target_id",
        "target_runtime_id",
        "runtime.id",
        "runtime_target_id"
      )
  };
}

function extractMissionYamlScalars(yaml: string): Map<string, string> {
  const values = new Map<string, string>();
  const stack: Array<{ indent: number; key: string }> = [];
  const lines = yaml.replace(/\r\n/g, "\n").split("\n");

  for (let index = 0; index < lines.length; index += 1) {
    const rawLine = lines[index] ?? "";
    const trimmedLine = rawLine.trim();
    if (!trimmedLine || trimmedLine.startsWith("#")) continue;

    const match = rawLine.match(/^(\s*)([A-Za-z0-9_.-]+)\s*:\s*(.*?)\s*$/);
    if (!match) continue;

    const indent = match[1].length;
    const key = match[2];
    const rawValue = match[3].trim();

    while (stack.length && indent <= stack[stack.length - 1].indent) {
      stack.pop();
    }

    let value = missionYamlScalarValue(rawValue);
    if (rawValue === "|" || rawValue === ">") {
      const block = collectMissionYamlBlock(lines, index + 1, indent, rawValue === ">");
      value = block.value;
      index = block.nextIndex;
    }

    if (value) {
      const path = [...stack.map((entry) => entry.key), key].join(".");
      values.set(normalizeMissionYamlKey(path), value);
      values.set(normalizeMissionYamlKey(key), value);
    } else {
      stack.push({ indent, key });
    }
  }

  return values;
}

function collectMissionYamlBlock(
  lines: string[],
  startIndex: number,
  baseIndent: number,
  folded: boolean
): { nextIndex: number; value: string } {
  const blockLines: string[] = [];
  let index = startIndex;

  for (; index < lines.length; index += 1) {
    const rawLine = lines[index] ?? "";
    if (!rawLine.trim()) {
      blockLines.push("");
      continue;
    }
    const indent = rawLine.match(/^\s*/)?.[0].length ?? 0;
    if (indent <= baseIndent) break;
    blockLines.push(rawLine.slice(Math.min(rawLine.length, baseIndent + 2)).trimEnd());
  }

  const value = folded ? blockLines.map((line) => line.trim()).join(" ").trim() : blockLines.join("\n").trim();
  return { nextIndex: Math.max(startIndex - 1, index - 1), value };
}

function missionYamlScalarValue(rawValue: string): string {
  const value = stripMissionYamlComment(rawValue).trim();
  if (!value || ["{}", "[]", "null", "~"].includes(value.toLowerCase())) return "";
  if (
    (value.startsWith('"') && value.endsWith('"')) ||
    (value.startsWith("'") && value.endsWith("'"))
  ) {
    return value.slice(1, -1).trim();
  }
  return value;
}

function stripMissionYamlComment(value: string): string {
  const trimmed = value.trim();
  if (trimmed.startsWith('"') || trimmed.startsWith("'")) return trimmed;
  const commentIndex = trimmed.indexOf(" #");
  return commentIndex >= 0 ? trimmed.slice(0, commentIndex) : trimmed;
}

function missionYamlScalar(values: Map<string, string>, ...keys: string[]): string {
  for (const key of keys) {
    const value = values.get(normalizeMissionYamlKey(key));
    if (value) return value;
  }
  return "";
}

function normalizeMissionYamlKey(key: string): string {
  return key.toLowerCase().replace(/[^a-z0-9]/g, "");
}
