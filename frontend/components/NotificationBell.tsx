"use client";

import { useEffect, useRef, useState } from "react";
import { Bell, CheckCheck } from "lucide-react";
import { toast } from "sonner";

import { API_BASE, EventFeedItem } from "@/lib/api";
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { cn, parseUtcDate } from "@/lib/utils";

const KIND_LABEL: Record<string, string> = {
  working: "Order working",
  open: "Filled — position open",
  rolled: "Rolled",
  managed: "Managed",
  closed: "Closed",
  canceled: "Canceled",
  rejected: "Rejected",
  planned: "Planned",
};

// Dot color per lifecycle kind (mirrors the position timeline).
const KIND_DOT: Record<string, string> = {
  planned: "bg-text-faint",
  working: "bg-info",
  open: "bg-gain",
  managed: "bg-warn",
  rolled: "bg-warn",
  closed: "bg-muted-foreground",
  canceled: "bg-loss",
  rejected: "bg-loss",
};

// Which lifecycle events are worth surfacing to the user.
const NOTIFY = new Set(["working", "open", "rolled", "managed", "closed", "canceled", "rejected"]);

const MAX_HISTORY = 60;

function fireToast(e: EventFeedItem) {
  const label = KIND_LABEL[e.kind] ?? e.kind;
  const title = `${e.symbol} · ${label}`;
  const opts = { description: e.detail, duration: 6000 };

  if (e.kind === "open") toast.success(title, opts);
  else if (e.kind === "rejected" || e.kind === "canceled") toast.error(title, opts);
  else if (e.kind === "rolled" || e.kind === "managed") toast.warning(title, opts);
  else if (e.kind === "closed") {
    const win = /realized \+/.test(e.detail);
    (win ? toast.success : toast.error)(title, opts);
  } else toast.info(title, opts);

  if (typeof window !== "undefined" && "Notification" in window && Notification.permission === "granted") {
    try {
      new Notification(title, { body: e.detail, tag: `tastyagent-${e.id}` });
    } catch {
      /* ignore */
    }
  }
}

function relTime(iso: string): string {
  const t = parseUtcDate(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 45) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  return `${d}d ago`;
}

/**
 * Status-bar notification bell: polls `/api/events`, fires toast + desktop
 * notifications for new lifecycle events, and keeps a scrollable history so the
 * user can review anything they missed. The unread badge clears when opened.
 */
export default function NotificationBell() {
  const [events, setEvents] = useState<EventFeedItem[]>([]);
  const [unread, setUnread] = useState(0);
  const [open, setOpen] = useState(false);

  const lastId = useRef(0);
  const seeded = useRef(false);
  const openRef = useRef(false);

  useEffect(() => {
    if (typeof window !== "undefined" && "Notification" in window && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }

    let alive = true;
    const poll = async () => {
      try {
        const r = await fetch(`${API_BASE}/api/events?after=${lastId.current}&limit=50`);
        if (!r.ok) return;
        const items: EventFeedItem[] = await r.json();
        if (!items.length) return;
        const maxId = items[items.length - 1].id;
        const notable = items.filter((e) => NOTIFY.has(e.kind));

        if (!seeded.current) {
          // First run: seed recent history (so the bell isn't empty) but don't
          // replay toasts or count these as unread — they already happened.
          seeded.current = true;
          lastId.current = maxId;
          setEvents(notable.slice(-MAX_HISTORY).reverse());
          return;
        }

        if (notable.length) {
          for (const e of notable) fireToast(e);
          setEvents((prev) => [...notable.slice().reverse(), ...prev].slice(0, MAX_HISTORY));
          // Don't accrue unread while the panel is open — the user is reading it.
          if (!openRef.current) setUnread((n) => n + notable.length);
        }
        lastId.current = maxId;
      } catch {
        /* network blip — try again next tick */
      }
    };

    poll();
    const id = setInterval(() => {
      if (alive) poll();
    }, 4000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const onOpenChange = (next: boolean) => {
    setOpen(next);
    openRef.current = next;
    if (next) setUnread(0);
  };

  return (
    <DropdownMenu open={open} onOpenChange={onOpenChange}>
      <DropdownMenuTrigger asChild>
        <button
          aria-label={`Notifications${unread > 0 ? ` (${unread} unread)` : ""}`}
          className="relative inline-flex size-9 items-center justify-center rounded-lg border border-border bg-surface-2 text-muted-foreground transition-colors hover:border-border-strong hover:text-foreground"
        >
          <Bell className="size-[17px]" />
          {unread > 0 && (
            <span className="absolute -right-1.5 -top-1.5 inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-brand px-1 text-[10px] font-bold leading-none text-white">
              {unread > 99 ? "99+" : unread}
            </span>
          )}
        </button>
      </DropdownMenuTrigger>

      <DropdownMenuContent align="end" className="w-[340px] p-0">
        <div className="flex items-center justify-between border-b border-border px-3.5 py-2.5">
          <span className="text-[13px] font-semibold">Notifications</span>
          {events.length > 0 && (
            <span className="inline-flex items-center gap-1 text-[11px] text-text-faint">
              <CheckCheck className="size-3.5" />
              Caught up
            </span>
          )}
        </div>

        <div className="max-h-[400px] overflow-y-auto">
          {events.length === 0 ? (
            <div className="px-3.5 py-10 text-center text-[13px] text-text-faint">
              No notifications yet.
            </div>
          ) : (
            events.map((e) => (
              <div
                key={e.id}
                className="flex gap-3 border-b border-border/50 px-3.5 py-2.5 last:border-b-0"
              >
                <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", KIND_DOT[e.kind] || "bg-text-faint")} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="truncate text-[13px] font-medium">
                      {e.symbol} — {KIND_LABEL[e.kind] ?? e.kind}
                    </span>
                    <span className="shrink-0 font-mono text-[10px] tabular-nums text-text-faint">
                      {relTime(e.ts)}
                    </span>
                  </div>
                  {e.detail && (
                    <div className="mt-0.5 truncate text-[12px] text-muted-foreground">{e.detail}</div>
                  )}
                </div>
              </div>
            ))
          )}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
