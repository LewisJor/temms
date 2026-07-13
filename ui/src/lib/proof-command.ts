export function formatProofCommand(parts: string[]): string {
  if (parts.length <= 5) return parts.map(shellArg).join(" ");
  const firstLine = parts.slice(0, 5).map(shellArg).join(" ");
  const lines = [firstLine];
  for (let index = 5; index < parts.length;) {
    const token = parts[index];
    const flag = shellArg(token);
    const value = parts[index + 1];
    if (!token.startsWith("--")) {
      lines.push(`  ${flag}`);
      index += 1;
    } else if (value === undefined || value.startsWith("--")) {
      lines.push(`  ${flag}`);
      index += 1;
    } else {
      lines.push(`  ${flag} ${shellArg(value)}`);
      index += 2;
    }
  }
  return lines.join(" \\\n");
}

export function shellArg(value: string): string {
  if (/^[A-Za-z0-9_./:@%+=,-]+$/.test(value)) return value;
  return `"${value.replace(/(["\\$`])/g, "\\$1")}"`;
}
