import { ApiError } from "../api";
import type { Device, HubPackage, Rollout, RolloutPlan, RuntimeTarget, Toast } from "../types";

const tokenKey = "temms.hub.token";

export function storedToken(): string {
  try {
    return localStorage.getItem(tokenKey) ?? "";
  } catch {
    return "";
  }
}

export function saveToken(token: string): void {
  try {
    if (token) localStorage.setItem(tokenKey, token);
    else localStorage.removeItem(tokenKey);
  } catch {
    // Browser storage can be unavailable in locked-down operator contexts.
  }
}

function idOf(item: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    const value = item[key];
    if (value !== undefined && value !== null && value !== "") return String(value);
  }
  return "";
}

export const deviceId = (device: Device): string => idOf(device as Record<string, unknown>, "device_id", "id");
export const packageId = (pkg: HubPackage): string => idOf(pkg as Record<string, unknown>, "package_id", "id");
export const runtimeTargetId = (target: RuntimeTarget): string =>
  idOf(target as Record<string, unknown>, "runtime_target_id", "id");
export const rolloutId = (rollout: Rollout): string =>
  idOf(rollout as Record<string, unknown>, "rollout_id", "id");
export const planId = (plan: RolloutPlan): string => idOf(plan as Record<string, unknown>, "plan_id");

export function csv(value: string): string[] {
  return value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

export function fieldValue(form: HTMLFormElement, name: string): string {
  const field = form.elements.namedItem(name);
  if (
    field instanceof HTMLInputElement ||
    field instanceof HTMLTextAreaElement ||
    field instanceof HTMLSelectElement
  ) {
    return field.value.trim();
  }
  return "";
}

export function isChecked(form: HTMLFormElement, name: string): boolean {
  const field = form.elements.namedItem(name);
  return field instanceof HTMLInputElement ? field.checked : false;
}

export function compactDate(value?: string): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

export function currentHubUrl(): string {
  if (typeof window === "undefined" || !window.location?.origin) return "http://127.0.0.1:8080";
  return window.location.origin;
}

export function localizeHubCommandText(command: string): string {
  return command.split("${TEMMS_HUB_URL}").join(currentHubUrl());
}

export function localizeHubCommandPart(part: string): string {
  return part.split("${TEMMS_HUB_URL}").join(currentHubUrl());
}

export function toneFor(value: string): "good" | "warn" | "bad" | "neutral" {
  const normalized = value.toLowerCase();
  if (
    ["released", "approved", "activated", "complete", "completed", "healthy", "signed", "rolled_back"].includes(
      normalized
    )
  ) {
    return "good";
  }
  if (
    ["pending", "candidate", "assigned", "advancing", "ready", "paused", "validated", "preview", "preview_only"].includes(
      normalized
    )
  ) {
    return "warn";
  }
  if (["failed", "error", "retired", "blocked", "missing"].includes(normalized)) {
    return "bad";
  }
  return "neutral";
}

export function toneForPath(state: string): "good" | "warn" | "bad" | "neutral" {
  const normalized = state.toLowerCase();
  if (["ready", "released", "approved", "activated", "rolled_back", "complete"].includes(normalized)) return "good";
  if (["blocked", "failed", "error", "missing"].includes(normalized)) return "bad";
  if (["pending", "assigned", "advancing", "downloading", "imported", "preview", "preview_only"].includes(normalized)) return "warn";
  return "neutral";
}

export function toneForReadinessStatus(status: string): "good" | "warn" | "bad" | "neutral" {
  const normalized = status.toLowerCase();
  if (normalized === "go") return "good";
  if (normalized === "attention") return "warn";
  if (normalized === "blocked") return "bad";
  return toneForPath(normalized);
}

export function actionLabel(action: string): string {
  return action
    .split("-")
    .map((part) => `${part.slice(0, 1).toUpperCase()}${part.slice(1)}`)
    .join(" ");
}

export function displayGateState(value: string): string {
  const normalized = value.replace(/_/g, " ").trim();
  if (!normalized) return value;
  const acronyms: Record<string, string> = {
    cpu: "CPU",
    ddil: "DDIL",
    gpu: "GPU",
    onnx: "ONNX",
    slo: "SLO"
  };
  return normalized
    .split(/\s+/)
    .map((word, index) =>
      acronyms[word.toLowerCase()] ??
      (index === 0 ? `${word.charAt(0).toUpperCase()}${word.slice(1)}` : word)
    )
    .join(" ");
}

export function errorToast(title: string, error: unknown): Toast {
  if (error instanceof ApiError) {
    return { tone: "error", title, detail: `${error.status}: ${error.message}` };
  }
  return { tone: "error", title, detail: error instanceof Error ? error.message : String(error) };
}

export function nextPromotion(state: string): string {
  const states = ["candidate", "validated", "approved", "released"];
  const index = states.indexOf(state);
  return index >= 0 && index < states.length - 1 ? states[index + 1] ?? "" : "";
}
