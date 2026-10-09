import i18n from "../i18n";

export function money(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `$${v.toLocaleString(i18n.language, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

export function dateTime(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const d = typeof v === "number" ? new Date(v * 1000) : new Date(v);
  return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString(i18n.language);
}

export function duration(fromIso: string | null | undefined, toIso?: string | null): string {
  if (!fromIso) return "—";
  const ms = (toIso ? new Date(toIso).getTime() : Date.now()) - new Date(fromIso).getTime();
  if (ms < 0) return "—";
  const m = Math.floor(ms / 60000);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function num(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toLocaleString(i18n.language, { maximumFractionDigits: digits });
}

/** Span-style duration: `850 ms`, `1.25 s`, `12.4 s`, `3m 05s`. */
export function millis(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "—";
  if (ms < 1) return ms <= 0 ? "0 ms" : "<1 ms";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
  const s = Math.round(ms / 1000);
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}