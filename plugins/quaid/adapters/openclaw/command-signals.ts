export type QuaidCommandSignalLabel = "CompactionSignal" | "ResetSignal" | "NewSignal";

export function normalizeCommandName(raw: unknown): string {
  const s = String(raw || "").trim().toLowerCase();
  if (!s) return "";
  return s.startsWith("/") ? s.slice(1) : s;
}

export function extractCommandName(event: any): string {
  const direct = normalizeCommandName(event?.command || event?.name);
  if (direct) return direct;
  const text = String(event?.text || event?.input || event?.raw || "").trim().toLowerCase();
  const m = text.match(/^\/([a-z0-9_-]+)/i);
  return m ? m[1].toLowerCase() : "";
}

export function signalLabelForCommand(commandRaw: unknown): QuaidCommandSignalLabel | null {
  const command = normalizeCommandName(commandRaw);
  if (!command) return null;
  if (command === "compact" || command === "compaction") return "CompactionSignal";
  if (command === "new") return "NewSignal";
  if (command === "reset" || command === "restart") return "ResetSignal";
  return null;
}
