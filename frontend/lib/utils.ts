import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Parse an ISO datetime string from the API.
 * If the string lacks a timezone offset, it is assumed to be UTC (since the backend stores UTC),
 * ensuring the resulting Date object correctly converts to the client's system local timezone.
 */
export function parseUtcDate(iso: string | Date | undefined | null): Date {
  if (!iso) return new Date();
  if (iso instanceof Date) return iso;
  const s = String(iso).trim();
  const hasTz = /[Zz]$|[+-]\d{2}:?\d{2}$/.test(s);
  return new Date(hasTz ? s : `${s}Z`);
}

/**
 * Format date and time in system local time.
 */
export function formatLocalDateTime(iso: string | Date | undefined | null): string {
  const d = parseUtcDate(iso);
  if (isNaN(d.getTime())) return String(iso ?? "");
  return d.toLocaleString();
}

/**
 * Split into localized date and 24h time parts (for Overview recent activity).
 */
export function formatLocalDateParts(iso: string | Date | undefined | null) {
  const d = parseUtcDate(iso);
  if (isNaN(d.getTime())) return { date: String(iso ?? ""), time: "" };
  return {
    date: d.toLocaleDateString(undefined, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }),
    time: d.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }),
  };
}

