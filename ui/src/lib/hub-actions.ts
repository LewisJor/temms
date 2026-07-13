import { controlApi, loadSnapshot } from "../api";
import type { HubSnapshot, JsonObject, Preview, Toast } from "../types";
import { asRecord, numberOf, stringOf } from "./json";

export interface CopyOperatorCommandResult {
  preview?: Preview;
  toast: Toast;
}

export interface SyncPendingOperationsResult {
  payload: JsonObject;
  snapshot: HubSnapshot;
}

export interface SyncPendingOperationsAction {
  title: string;
}

export async function copyOperatorCommand({
  command,
  label,
  writeText = (text: string) => navigator.clipboard.writeText(text)
}: {
  command: string;
  label: string;
  writeText?: (text: string) => Promise<unknown>;
}): Promise<CopyOperatorCommandResult> {
  try {
    await writeText(command);
    return { toast: { tone: "success", title: `${label} copied` } };
  } catch {
    return {
      preview: { title: label, payload: { command } },
      toast: {
        tone: "info",
        title: `${label} ready`,
        detail: "Command opened in the payload panel."
      }
    };
  }
}

export async function syncPendingOperationsWithReconciliation(
  token: string,
  options: {
    loadReconciledSnapshot?: (token: string) => Promise<HubSnapshot>;
    syncPending?: (token: string) => Promise<JsonObject>;
  } = {}
): Promise<SyncPendingOperationsResult> {
  const syncPending = options.syncPending ?? controlApi.syncPending;
  const loadReconciledSnapshot = options.loadReconciledSnapshot ?? loadSnapshotAfterReconciliation;
  const payload = await syncPending(token);
  const snapshot = await loadReconciledSnapshot(token);
  return { payload, snapshot };
}

export function syncPendingOperationsAction(): SyncPendingOperationsAction {
  return {
    title: "Sync pending DDIL operations"
  };
}

export async function loadSnapshotAfterReconciliation(token: string): Promise<HubSnapshot> {
  let next = await loadSnapshot(token);
  for (let attempt = 0; attempt < 5 && isAwaitingReconciliation(next); attempt += 1) {
    await delay(350);
    next = await loadSnapshot(token);
  }
  return next;
}

function isAwaitingReconciliation(snapshot: HubSnapshot): boolean {
  const runtime = asRecord(snapshot.evidenceSummary?.runtime);
  const deployment = asRecord(runtime.deployment_state);
  const pendingOperations = numberOf(runtime.pending_operations_count) ?? 0;
  return runtime.offline_mode === false && pendingOperations === 0 && stringOf(deployment.state, "") === "PENDING";
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

export function actionTitle(action: string): string {
  return action
    .split("-")
    .map((part) => `${part.slice(0, 1).toUpperCase()}${part.slice(1)}`)
    .join(" ");
}
