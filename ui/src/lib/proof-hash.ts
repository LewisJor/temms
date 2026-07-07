export const EDGE_PROOF_COMPONENT_DIGEST_TARGETS = [
  { key: "runtime_workbench_sha256", label: "Workbench", component: "runtime_workbench" },
  { key: "runtime_decision_trace_sha256", label: "Trace", component: "runtime_decision_trace" },
  { key: "edge_execution_manifest_sha256", label: "Manifest", component: "edge_execution_manifest" }
] as const;

export function canonicalJsonStringify(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return `[${value.map(canonicalJsonStringify).join(",")}]`;
  if (typeof value === "object") {
    const record = asRecord(value);
    return `{${Object.keys(record)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJsonStringify(record[key])}`)
      .join(",")}}`;
  }
  if (typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") return Number.isFinite(value) ? JSON.stringify(value) : "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  return JSON.stringify(String(value));
}

export async function sha256Hex(value: string): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function isSha256Digest(value: string): boolean {
  return /^[a-fA-F0-9]{64}$/.test(value.replace(/^sha256:/, ""));
}

export function shortProofDigest(value: string): string {
  return value.replace(/^sha256:/, "").slice(0, 12) || "pending";
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}
