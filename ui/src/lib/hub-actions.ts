import { loadSnapshot } from "../api";
import type { HubSnapshot } from "../types";
import { asRecord, numberOf, stringOf } from "./json";

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
