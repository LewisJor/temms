export function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function stringsOf(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => String(item)).filter(Boolean);
  if (typeof value === "string") return csvValue(value);
  const record = asRecord(value);
  const profiles = record.device_profiles;
  return Array.isArray(profiles) ? profiles.map((item) => String(item)).filter(Boolean) : [];
}

export function stringOf(value: unknown, fallback: string): string {
  return value === undefined || value === null || value === "" ? fallback : String(value);
}

export function numberOf(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

export function booleanOf(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

export function latestByTime<T extends { updated_at?: string; created_at?: string }>(items: T[]): T | undefined {
  return [...items].sort((a, b) => timeOf(b) - timeOf(a))[0];
}

function timeOf(value: { updated_at?: string; created_at?: string }): number {
  const date = new Date(value.updated_at ?? value.created_at ?? 0);
  return Number.isNaN(date.valueOf()) ? 0 : date.valueOf();
}

function csvValue(value: string): string[] {
  return value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}
