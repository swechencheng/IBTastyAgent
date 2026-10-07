"use client";

import { useEffect, useRef, useState } from "react";
import { Bell, CheckCheck } from "lucide-react";
import { toast } from "sonner";

import { API_BASE, EventFeedItem } from "@/lib/api";
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { cn, parseUtcDate } from "@/lib/utils";

const KIND_LABEL: Record<string, string> = {
  working: "Order working",
  open: "Filled",
  rolled: "Rolled",
  managed: "Managed",
  closed: "Closed",
  canceled: "Canceled",
  rejected: "Rejected",
  planned: "Planned",
};

const KIND_BADGE: Record<string, string> = {
  planned: "bg-surface-3 text-muted-foreground border-border",
  working: "bg-info/10 text-info border-info/20",
  open: "bg-gain/10 text-gain border-gain/20",
  managed: "bg-warn/10 text-warn border-warn/20",
  rolled: "bg-warn/10 text-warn border-warn/20",
  closed: "bg-surface-3 text-muted-foreground border-border",
  canceled: "bg-loss/10 text-loss/80 border-loss/20",
  rejected: "bg-loss/10 text-loss/80 border-loss/20",
};

// Which lifecycle events are worth surfacing to the user.
const NOTIFY = new Set(["working", "open", "rolled", "managed", "closed", "canceled", "rejected"]);

const MAX_HISTORY = 60;
const STORAGE_KEY = "tastyagent_last_read_event_id";

function fireToast(e: EventFeedItem) {
  const label = KIND_LABEL[e.kind] ?? e.kind;
  const title = `${e.symbol} · ${label}`;
  const opts = { description: e.detail, duration: 3500 };

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
 * notifications for new live lifecycle events, and keeps a scrollable history.
 * Tracks last-read event ID in localStorage so unread states and badges clear permanently once read.
 */
export default function NotificationBell() {
  const [events, setEvents] = useState<EventFeedItem[]>([]);
  const [lastReadId, setLastReadId] = useState<number>(() => {
    if (typeof window === "undefined") return 0;
    const v = localStorage.getItem(STORAGE_KEY);
    return v !== null ? Number(v) : -1; // -1 indicates fresh browser session
  });
  const [open, setOpen] = useState(false);

  const lastId = useRef(0);
  const seeded = useRef(false);
  const openRef = useRef(false);
  const lastReadIdRef = useRef(lastReadId);
  lastReadIdRef.current = lastReadId;

  // Unread count: items with id > lastReadId (if fresh browser session, 0 unread on first load)
  const unreadCount =
    lastReadId === -1
      ? 0
      : events.filter((e) => e.id > lastReadId).length;

  const markAllAsRead = () => {
    if (!events.length) return;
    const maxId = Math.max(...events.map((e) => e.id), lastReadId);
    setLastReadId(maxId);
    if (typeof window !== "undefined") {
      localStorage.setItem(STORAGE_KEY, String(maxId));
    }
  };

  useEffect(() => {
    const handleReset = () => {
      setEvents([]);
      setLastReadId(0);
      lastId.current = 0;
      seeded.current = false;
      if (typeof window !== "undefined") {
        localStorage.removeItem(STORAGE_KEY);
      }
    };
    window.addEventListener("tastyagent:sandbox_reset", handleReset);
    return () => window.removeEventListener("tastyagent:sandbox_reset", handleReset);
  }, []);

  useEffect(() => {
    if (typeof window !== "undefined" && "Notification" in window && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }

    let alive = true;
    const poll = async () => {
      try {
        const afterParam = seeded.current ? lastId.current : 0;
        const r = await fetch(`${API_BASE}/api/events?after=${afterParam}&limit=50`);
        if (!r.ok) return;
        const items: EventFeedItem[] = await r.json();
        if (!items.length) {
          if (afterParam === 0) {
            setEvents([]);
            lastId.current = 0;
            seeded.current = true;
          }
          return;
        }
        const maxId = items[items.length - 1].id;
        const notable = items.filter((e) => NOTIFY.has(e.kind));

        // Detect backend database reset if IDs rolled back
        if (lastId.current > 0 && maxId < lastId.current) {
          lastId.current = maxId;
          setEvents(notable.slice(-MAX_HISTORY).reverse());
          setLastReadId(maxId);
          if (typeof window !== "undefined") {
            localStorage.setItem(STORAGE_KEY, String(maxId));
          }
          return;
        }

        if (!seeded.current) {
          // First run: seed recent history into dropdown, but NEVER replay historical toasts.
          seeded.current = true;
          lastId.current = maxId;
          setEvents(notable.slice(-MAX_HISTORY).reverse());

          // If fresh session (no prior lastReadId in localStorage), mark existing history as read
          if (lastReadIdRef.current === -1) {
            setLastReadId(maxId);
            if (typeof window !== "undefined") {
              localStorage.setItem(STORAGE_KEY, String(maxId));
            }
          }
          return;
        }

        if (notable.length) {
          // Only fire toasts for fresh live events (occurred within the last 60s)
          const now = Date.now();
          const freshEvents = notable.filter((e) => {
            const t = parseUtcDate(e.ts).getTime();
            return !isNaN(t) && now - t <= 60000;
          });

          // Rate-limit toasts: max 2 individual + 1 summary
          if (freshEvents.length > 0) {
            const toToast = freshEvents.slice(0, 2);
            for (const e of toToast) fireToast(e);
            if (freshEvents.length > 2) {
              toast.info(`+${freshEvents.length - 2} more trade events`, { duration: 3500 });
            }
          }

          setEvents((prev) => {
            const next = [...notable.slice().reverse(), ...prev];
            const seen = new Set<number>();
            const deduped: EventFeedItem[] = [];
            for (const ev of next) {
              if (!seen.has(ev.id)) {
                seen.add(ev.id);
                deduped.push(ev);
              }
            }
            return deduped.slice(0, MAX_HISTORY);
          });

          // If dropdown is currently open, automatically advance read pointer
          if (openRef.current) {
            setLastReadId(maxId);
            if (typeof window !== "undefined") {
              localStorage.setItem(STORAGE_KEY, String(maxId));
            }
          }
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
    if (next) {
      markAllAsRead();
    }
  };

  return (
    <DropdownMenu open={open} onOpenChange={onOpenChange}>
      <DropdownMenuTrigger asChild>
        <button
          aria-label={`Notifications${unreadCount > 0 ? ` (${unreadCount} unread)` : ""}`}
          className="relative inline-flex size-9 items-center justify-center rounded-lg border border-border bg-surface-2 text-muted-foreground transition-colors hover:border-border-strong hover:text-foreground"
        >
          <Bell className="size-[17px]" />
          {unreadCount > 0 && (
            <span className="absolute -right-1.5 -top-1.5 inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-brand px-1 text-[10px] font-bold leading-none text-white shadow-sm animate-in fade-in zoom-in">
              {unreadCount > 99 ? "99+" : unreadCount}
            </span>
          )}
        </button>
      </DropdownMenuTrigger>

      <DropdownMenuContent align="end" className="w-[350px] p-0 shadow-xl">
        <div className="flex items-center justify-between border-b border-border px-3.5 py-2.5">
          <div className="flex items-center gap-2">
            <span className="text-[13px] font-semibold">Notifications</span>
            {unreadCount > 0 && (
              <span className="rounded-full bg-brand/10 text-brand px-2 py-0.5 text-[10px] font-bold">
                {unreadCount} new
              </span>
            )}
          </div>
          {unreadCount > 0 ? (
            <button
              onClick={markAllAsRead}
              className="inline-flex items-center gap-1 text-[11px] font-medium text-brand hover:underline"
            >
              <CheckCheck className="size-3.5" />
              Mark all read
            </button>
          ) : events.length > 0 ? (
            <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground/80">
              <CheckCheck className="size-3.5 text-gain" />
              Caught up
            </span>
          ) : null}
        </div>

        <div className="max-h-[400px] overflow-y-auto">
          {events.length === 0 ? (
            <div className="px-3.5 py-10 text-center text-[13px] text-text-faint">
              No notifications yet.
            </div>
          ) : (
            events.map((e) => {
              const isUnread = lastReadId !== -1 && e.id > lastReadId;
              return (
                <div
                  key={e.id}
                  className={cn(
                    "flex items-start gap-2.5 border-b border-border/40 px-3.5 py-2.5 transition-colors last:border-b-0",
                    isUnread ? "bg-surface-2/70" : "hover:bg-surface-2/30"
                  )}
                >
                  <div className="mt-1.5 flex w-2 shrink-0 justify-center">
                    {isUnread ? (
                      <span className="size-2 rounded-full bg-brand" title="Unread" />
                    ) : (
                      <span className="size-1 rounded-full bg-transparent" />
                    )}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-1.5 truncate">
                        <span className="truncate text-[13px] font-semibold text-foreground">
                          {e.symbol}
                        </span>
                        <span className="text-muted-foreground/40 text-xs">·</span>
                        <span
                          className={cn(
                            "rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider",
                            KIND_BADGE[e.kind] || "bg-surface-3 text-muted-foreground border-border"
                          )}
                        >
                          {KIND_LABEL[e.kind] ?? e.kind}
                        </span>
                      </div>
                      <span className="shrink-0 font-mono text-[10px] tabular-nums text-text-faint">
                        {relTime(e.ts)}
                      </span>
                    </div>
                    {e.detail && (
                      <div className="mt-1 text-[12px] leading-relaxed text-muted-foreground line-clamp-2">
                        {e.detail}
                      </div>
                    )}
                  </div>
                </div>
              );
            })
          )}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
