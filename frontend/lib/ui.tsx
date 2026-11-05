"use client";
// Small shared UI pieces: inline SVG icons (emoji render differently on every cheap Android, SVG does not),
// a ticking clock for countdowns, time and phone formatting. No dependency: a dozen paths are not worth a package.
import { useEffect, useRef, useState } from "react";

const PATHS = {
  drop: "M12 2.7s-6.5 7.3-6.5 11.8a6.5 6.5 0 0 0 13 0C18.5 10 12 2.7 12 2.7z",
  back: "M15 18l-6-6 6-6",
  close: "M18 6L6 18M6 6l12 12",
  user: "M20 21a8 8 0 0 0-16 0M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10z",
  logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
  phone: "M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z",
  pin: "M12 22s7-6.1 7-12a7 7 0 0 0-14 0c0 5.9 7 12 7 12zM12 12.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z",
  check: "M20 6L9 17l-5-5",
  checkCircle: "M22 11.1V12a10 10 0 1 1-5.9-9.1M22 4L12 14l-3-3",
  clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2",
  alert: "M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01",
  info: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-4M12 8h.01",
  xCircle: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM15 9l-6 6M9 9l6 6",
  minus: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM8 12h8",
  refresh: "M21 12a9 9 0 1 1-2.6-6.4M21 3v6h-6",
  heart: "M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21.2l8.8-8.8a5.5 5.5 0 0 0 0-7.8z",
  building: "M3 21h18M5 21V7l7-4 7 4v14M9 9h1M14 9h1M9 13h1M14 13h1M10 21v-4h4v4",
  box: "M21 8l-9-5-9 5 9 5 9-5zM3 8v8l9 5 9-5V8M12 13v8",
  bell: "M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0",
  navigation: "M3 11l19-9-9 19-2-8-8-2z",
  shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  users: "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3",
  zap: "M13 2L3 14h9l-1 8 10-12h-9l1-8z",
} as const;
export type IconName = keyof typeof PATHS;

/** Decorative by default (aria-hidden). Pass `label` only when the icon is the sole content of its control. */
export function Icon({ name, label, className = "icon" }: { name: IconName; label?: string; className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
         strokeLinecap="round" strokeLinejoin="round" aria-hidden={label ? undefined : true} role={label ? "img" : undefined}
         aria-label={label}>
      <path d={PATHS[name]} />
    </svg>
  );
}

export function Logo() {
  return (
    <span className="logo">
      <span className="logo-mark"><Icon name="drop" /></span>
      <span>Rakt <b>Doot</b></span>
    </span>
  );
}

/** The current time, re-rendered every `ms`. For countdowns; the API owns the real deadline. */
export function useNow(ms = 15000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

function span(minutes: number): string {
  if (minutes < 1) return "less than a minute";
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60), m = minutes % 60;
  if (h < 24) return m ? `${h} h ${m} min` : `${h} h`;
  const d = Math.floor(h / 24);
  return `${d} day${d === 1 ? "" : "s"}`;
}

/** "5 min ago", "3 h ago", "2 days ago". */
export function ago(iso: string, now: number): string {
  const min = Math.max(0, Math.floor((now - new Date(iso).getTime()) / 60000));
  return min < 1 ? "just now" : `${span(min)} ago`;
}

/** Minutes left until `iso`, or null when the moment has passed. */
export function left(iso: string, now: number): string | null {
  const ms = new Date(iso).getTime() - now;
  return ms <= 0 ? null : span(Math.floor(ms / 60000));
}

/** "+919876543210" -> "+91 98765 43210". Anything unexpected is shown as is. */
export function formatPhone(p: string): string {
  const m = /^\+91(\d{5})(\d{5})$/.exec(p);
  return m ? `+91 ${m[1]} ${m[2]}` : p;
}

/** A call button plus the number as selectable text: tel: links do nothing on many desktops.
 *  `demo`: the request is synthetic but the facility (and its number) is real, so no call button, just a warning. */
export function PhoneLine({ phone, label = "Call", who, demo = false }: { phone: string; label?: string; who?: string; demo?: boolean }) {
  return (
    <div className="phone-line">
      {!demo && <a className="btn ghost sm" href={`tel:${phone}`}><Icon name="phone" />{label}{who && <span className="sr-only"> {who}</span>}</a>}
      <span className="digits">{formatPhone(phone)}</span>
      {demo && <small className="warn-text"><Icon name="alert" /> Demo request: do not call</small>}
    </div>
  );
}

/** ref for a success message: moves keyboard and screen-reader focus to it when it appears (needs tabIndex={-1}). */
export const focusOnMount = (el: HTMLElement | null) => el?.focus();

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="stack" aria-busy="true" aria-label="Loading">
      {Array.from({ length: lines }, (_, i) => <div key={i} className="skeleton" style={{ height: i === 0 ? 28 : 72 }} />)}
    </div>
  );
}

// ---------- status presentation: icon + words, colour only reinforces (never the only signal) ----------
type Look = { cls: "" | "ok" | "warn" | "bad" | "info"; icon: IconName; text: string };

export const REQUEST_LOOK: Record<"OPEN" | "ALLOCATED" | "FULFILLED" | "CANCELLED", Look> = {
  OPEN: { cls: "warn", icon: "search", text: "Searching for units" },
  ALLOCATED: { cls: "info", icon: "box", text: "Units held at banks" },
  FULFILLED: { cls: "ok", icon: "checkCircle", text: "Units received" },
  CANCELLED: { cls: "", icon: "minus", text: "Cancelled" },
};

/** ALLOCATED with every held unit already collected: nothing waits at a bank, the ward only has to close (N5). */
export function requestLook(r: { status: keyof typeof REQUEST_LOOK; units_allocated: number; units_collected: number }): Look {
  return r.status === "ALLOCATED" && r.units_collected > 0 && r.units_collected === r.units_allocated
    ? { cls: "info", icon: "check", text: "Units collected, close the request" }
    : REQUEST_LOOK[r.status];
}

/** Focus goes back to the button that opened a step when the step is left with Back (WCAG 2.4.3).
 *  `back(key)` before closing the step; `ref={opener(key)}` on the opening button focuses it when it mounts again. */
export function useReturnFocus() {
  const want = useRef<string | null>(null);
  return {
    back: (key: string) => { want.current = key; },
    opener: (key: string) => (el: HTMLButtonElement | null) => {
      if (el && want.current === key) { want.current = null; el.focus(); }
    },
  };
}

export const HOLD_LOOK: Record<"PENDING" | "CONFIRMED" | "OVERDUE" | "ISSUED" | "REJECTED" | "EXPIRED" | "CANCELLED" | "RELEASED", Look> = {
  PENDING: { cls: "warn", icon: "clock", text: "Waiting for the bank to confirm" },
  CONFIRMED: { cls: "ok", icon: "checkCircle", text: "Confirmed: ready for pickup" },
  OVERDUE: { cls: "bad", icon: "alert", text: "Pickup overdue: collect or tell the bank" },
  ISSUED: { cls: "ok", icon: "check", text: "Collected" },
  RELEASED: { cls: "", icon: "minus", text: "Released by the bank after pickup was overdue" },
  REJECTED: { cls: "bad", icon: "xCircle", text: "Bank could not supply" },
  EXPIRED: { cls: "bad", icon: "alert", text: "Bank did not confirm in time" },
  CANCELLED: { cls: "", icon: "minus", text: "Released" },
};

/** "2 held, 1 collected of 3 units": held = still at the bank, collected = in the hospital's hands.
 *  A closed request holds nothing any more, so it only says what was collected. */
export function unitsLine(r: { status: string; units_needed: number; units_allocated: number; units_collected: number }): string {
  const of = `of ${r.units_needed} unit${r.units_needed === 1 ? "" : "s"}`;
  return r.status === "FULFILLED" || r.status === "CANCELLED" ? `${r.units_collected} ${of} collected`
    : `${r.units_allocated - r.units_collected} held, ${r.units_collected} collected ${of}`;
}

export function Badge({ look }: { look: Look }) {
  return <span className={`badge ${look.cls}`}><Icon name={look.icon} />{look.text}</span>;
}

/** Who runs a blood bank (from OpenStreetMap, ADR 022). "(from name)" = inferred from words like "Govt", not tagged. */
export function ownershipLook(o: string, inferred: boolean): Look {
  return o === "government" ? { cls: "info", icon: "shield", text: inferred ? "Government (from name)" : "Government" }
    : o === "private" ? { cls: "", icon: "building", text: "Private" }
    : o === "charitable" ? { cls: "", icon: "heart", text: "Charitable" }
    : { cls: "", icon: "info", text: "Ownership not known" };
}

export const URGENCY_LOOK: Record<string, Look> = {
  CRITICAL: { cls: "bad", icon: "alert", text: "Critical" },
  HIGH: { cls: "warn", icon: "zap", text: "High" },
  NORMAL: { cls: "", icon: "info", text: "Normal" },
};
