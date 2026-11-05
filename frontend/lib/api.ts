// The one place the browser talks to the FastAPI backend.
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export const BLOOD_GROUPS = ["O+", "O-", "A+", "A-", "B+", "B-", "AB+", "AB-"] as const;
export type BloodGroup = (typeof BLOOD_GROUPS)[number];
export const RARE: BloodGroup[] = ["B-", "AB+", "AB-"]; // ponytail: display hint only, matches the reference design

export type Organization = {
  id: string; kind: string; name: string; city: string; phone: string | null;
  stock_confirmed_at: string | null;      // a blood bank's "our stock list is right" moment; null = never
  auto_confirm_holds: boolean;
};

/** An RFC 9457 problem from the API. Screens switch on `type`; `message` is the API's human text (detail, else title). */
export class ApiError extends Error {
  /** `fields`: for a validation problem, the API names of the fields it rejected (e.g. "hospital_ref"). */
  constructor(public status: number, public type: string, title: string, public detail?: string, public fields: string[] = []) {
    super(detail || title);
  }
  /** True when this is the problem `urn:lifelink:problem:<slug>`. */
  is(slug: string): boolean {
    return this.type.endsWith(":" + slug);
  }
}

/** The API's slug check for any thrown value. */
export const isProblem = (e: unknown, slug: string) => e instanceof ApiError && e.is(slug);

/** Turns a failed response into an ApiError. A validation problem lists its fields, so screens can show the API's
 *  own words ("units needed: Input should be less than or equal to 20") instead of restating its rules. */
async function problemFrom(res: Response): Promise<ApiError> {
  const p = await res.json().catch(() => ({}));
  const errors: { field: string; message: string }[] = Array.isArray(p.errors) ? p.errors : [];
  const fields = errors.map((x) => String(x.field).replace(/^(body|query|header|path)\./, ""));
  const why = errors.map((x, i) => `${fields[i].replaceAll("_", " ")}: ${x.message}`).join("; ");
  return new ApiError(res.status, p.type ?? "about:blank", p.title ?? res.statusText,
                      [p.detail, why].filter(Boolean).join(" ") || undefined, fields);
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(API_URL + path, {
    ...init,
    credentials: "include", // sends the refresh-token cookie to /v1/auth (Stage 2)
    // Content-Type only with a body: a bodyless POST must not claim to carry JSON.
    headers: { ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers },
  });
  if (!res.ok) throw await problemFrom(res);
  return res.status === 204 ? (undefined as T) : res.json();
}

/** An Idempotency-Key. crypto.randomUUID exists only on https or localhost; a hospital opening the app over plain
 *  http on its LAN would crash on it. getRandomValues works everywhere, so it is the fallback (32 hex chars). */
export const newIdempotencyKey = (): string => typeof crypto.randomUUID === "function"
  ? crypto.randomUUID()
  : Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");

/** URLSearchParams encodes "+" as %2B. A hand-built "?recipient=AB+" would arrive as "AB " and fail. */
export const qs = (params: Record<string, string>) => "?" + new URLSearchParams(params).toString();

// ---------- Stage 2: auth ----------
// Access token: in memory only (gone on reload, never in localStorage where any XSS could read it).
// Refresh token: an HttpOnly cookie the API sets; JavaScript cannot see it, the browser just sends it to /v1/auth.
let accessToken: string | null = null;
type Tokens = { access_token: string };

// Single-flight: every caller that needs a refresh at the same moment shares ONE request. Two parallel refreshes
// would present the same rotating token twice, and the API treats that as theft and ends the session (ADR 013).
let refreshing: Promise<boolean> | null = null;

export function refreshSession(): Promise<boolean> {
  refreshing ??= api<Tokens>("/v1/auth/refresh", { method: "POST" })
    .then((t) => { accessToken = t.access_token; return true; })
    // false = the API says the session is gone (a 4xx). An outage (503, network error) is NOT "logged out": rethrow
    // it, so a page can say "briefly unavailable" instead of sending a nurse to the login screen mid-emergency.
    .catch((e) => { if (e instanceof ApiError && e.status < 500) { accessToken = null; return false; } throw e; })
    .finally(() => { refreshing = null; });
  return refreshing;
}

/** A call that needs a logged-in user: restores the session from the cookie, retries once after a 401. */
export async function authed<T>(path: string, init: RequestInit = {}): Promise<T> {
  // No session at all: stop here. Calling anyway would 401 and trigger a second, pointless refresh.
  if (!accessToken && !(await refreshSession())) throw new ApiError(401, "urn:lifelink:problem:unauthorized", "Unauthorized");
  const call = () => api<T>(path, { ...init, headers: { ...init.headers, Authorization: `Bearer ${accessToken}` } });
  try {
    return await call();
  } catch (e) {
    if (e instanceof ApiError && e.status === 401 && (await refreshSession())) return call();
    throw e;
  }
}

export async function login(email: string, password: string) {
  accessToken = (await api<Tokens>("/v1/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }))
    .access_token;
}

export async function register(email: string, password: string) {
  await api("/v1/auth/register", { method: "POST", body: JSON.stringify({ email, password }) });
  await login(email, password);
}

/** Forgets the token in memory either way, but rethrows a failure: the refresh cookie may still be valid, and on a
 *  shared ward PC "you are logged out" must be true. Callers show LOGOUT_FAILED and stay on the page. */
export async function logout() {
  try {
    await api("/v1/auth/logout", { method: "POST" });
  } finally {
    accessToken = null;
  }
}

export const LOGOUT_FAILED = "Could not log out, try again.";

export type Me = { id: string; email: string; role: string; organization_id: string | null };
export type DonorProfile = {
  id: string; name: string; blood_group: BloodGroup; is_available: boolean; eligible_now: boolean;
  phone_verified: boolean;      // false = never matched or alerted until staff verify the number in person (ADR 020)
};

// ---------- roles: where each kind of user lands after login ----------
export function homeFor(role: string): string {
  return role === "donor" ? "/donor" : role === "blood_bank" ? "/bank" : "/hospital";   // hospital and admin
}

// ---------- emergencies (mirror the API schemas by hand; CLAUDE.md 5A) ----------
export type Component = "red_cells" | "plasma";
export type Allocation = { blood_unit_id: string; blood_bank_id: string; blood_group: BloodGroup; allocated_at: string };
// OVERDUE: confirmed, pickup deadline passed, still held (never auto-released). ISSUED: collected (by the ward or
// handed over by the bank); it can appear while the request is still OPEN or ALLOCATED. RELEASED: the bank took back an OVERDUE hold.
export type HoldStatus = "PENDING" | "CONFIRMED" | "OVERDUE" | "ISSUED" | "REJECTED" | "EXPIRED" | "CANCELLED" | "RELEASED";
/** One blood bank's part of a request (ADR 021). Released holds stay in the list so the hospital sees why. */
export type Hold = {
  blood_bank_id: string; bank_name: string; bank_phone: string | null; units: number; status: HoldStatus;
  bank_is_demo: boolean;  // synthetic request at a real (OSM) facility: show the number, never offer to call it
  expires_at: string;     // live: the deadline; released: the deadline it had
};
export type Emergency = {
  id: string; code: string;          // the short code the ward writes on the paper requisition
  hospital_ref: string | null;       // bed / IP number, if the ward gave one; never the patient's name
  hospital_id: string; blood_group: BloodGroup; component: Component; units_needed: number;
  units_allocated: number;   // live holds, collected ones included
  units_collected: number;   // of those, units that left the bank. Held, not yet collected = allocated - collected
  urgency: "CRITICAL" | "HIGH" | "NORMAL"; status: "OPEN" | "ALLOCATED" | "FULFILLED" | "CANCELLED";
  created_at: string; allocations: Allocation[]; holds: Hold[];
  cap_reached: boolean;   // only on create / allocate: fewer units held because of the per-hospital cap
};
/** What a blood bank sees of a request: its needs and the bank's own hold. No patient data exists to show.
 *  GET /holds lists PENDING | CONFIRMED | OVERDUE; confirm / reject / release / issued return the one they acted on. */
export type BankHold = {
  request_id: string; code: string; hospital_ref: string | null; hospital_name: string; blood_group: BloodGroup; component: Component; units: number;
  hospital_phone: string | null; hospital_is_demo: boolean;   // demo: show the number, never offer to call it
  urgency: Emergency["urgency"]; status: HoldStatus; expires_at: string;
};
/** fulfil and cancel must say which banks' units were physically collected (ADR 021 rev. 2). Empty is allowed, missing is 422. */
export type CloseRequest = { collected_from: string[] };
export const closeRequest = (id: string, how: "fulfil" | "cancel", body: CloseRequest) =>
  authed<Emergency>(`/v1/emergencies/${id}/${how}`, { method: "POST", body: JSON.stringify(body) });
/** The bank handed its held units over at the counter (bank staff only, own hold). A repeat returns 200. */
export const markIssued = (id: string) => authed<BankHold>(`/v1/emergencies/${id}/holds/issued`, { method: "POST" });

/** Shared wording for problems every screen can hit. */
export const UNAVAILABLE = "Rakt Doot is briefly unavailable. Wait a few seconds and try again.";

// ---------- public search (no login) ----------
export type Ownership = "government" | "private" | "charitable" | "unknown";
/** The `ownership=` filter of nearest-banks. Charitable and unknown banks only appear under "all". */
export type OwnershipFilter = "all" | "government" | "private";
export type NearbyBank = {
  blood_bank_id: string; name: string; city: string; distance_km: number; units: number; blood_groups: BloodGroup[];
  units_same_group: number;               // of `units`, exactly the patient's own group
  phone: string | null; stock_confirmed_at: string | null;
  latitude: number; longitude: number;    // the facility's own location (public); donors never get coordinates
  ownership: Ownership;
  ownership_inferred: boolean;            // true = guessed from the name ("Govt ..."), not tagged in OpenStreetMap
  stock_is_demo: boolean;                 // true = the units are synthetic demo data, even when the bank is real (ADR 022)
};
export type DonorsNearby = {
  total: number | null;                                   // null = fewer than 5: too few to show without singling anyone out
  bands: { band: string; donors: number | null }[];
  how_to_reach_them: string;
};
export type DonorAnswer = { donor_id: string; first_name: string; blood_group: BloodGroup; status: "NOTIFIED" | "ACCEPTED" | "DECLINED"; responded_at: string | null };
export type InboxItem = {
  request_id: string; hospital_name: string; hospital_city: string; blood_group: BloodGroup; component: Component;
  urgency: string; request_status: Emergency["status"]; my_status: DonorAnswer["status"]; notified_at: string;
};
export type BloodUnit = { id: string; blood_bank_id: string; blood_group: BloodGroup; component: string; collected_at: string; expires_at: string; status: string };
/** Usable units of one group + component at one bank (GET /v1/blood-units/summary): the stock count starts from it. */
export type StockLine = { blood_group: BloodGroup; component: Component | "platelets"; units: number };
export type StockTakeResult = { summary: StockLine[]; stock_confirmed_at: string };
export const stockSummary = (bankId: string) => authed<StockLine[]>("/v1/blood-units/summary" + qs({ blood_bank_id: bankId }));
/** The bank counted its shelf (ADR 023). Every listed pair must be counted; lower counts take the surplus off the list. */
export const stockTake = (bankId: string, counts: StockLine[]) =>
  authed<StockTakeResult>(`/v1/organizations/${bankId}/stock-take`, { method: "POST", body: JSON.stringify({ counts }) });
export type Snapshot = { request: Emergency; responses: DonorAnswer[] };

// The server sends a heartbeat comment every 15 s on an idle stream (Backend/config.py sse_heartbeat_seconds).
// No bytes for 2.5x that = a half-open connection (Wi-Fi switched, a proxy dropped it), which never errors by itself.
const STREAM_SILENCE_MS = 2.5 * 15000;

/**
 * Live request status over Server-Sent Events. Read with fetch(), not EventSource: EventSource cannot send an
 * Authorization header, and a token in the URL would end up in server and proxy logs.
 * Reconnects with backoff when the connection drops or goes silent; every (re)connect starts with a full snapshot,
 * so nothing is missed. Stops for good and calls onError when retrying cannot help: 403/404 (another hospital's
 * request, or a wrong link) and 401 after a failed refresh (logged out). Returns a function that stops watching.
 */
export function watchEmergency(id: string, onSnapshot: (s: Snapshot) => void, onLive: (live: boolean) => void,
                               onError: (e: ApiError) => void): () => void {
  const stop = new AbortController();
  const open = (signal: AbortSignal) => fetch(`${API_URL}/v1/emergencies/${id}/events`, {
    headers: { Authorization: `Bearer ${accessToken}` }, credentials: "include", signal,
  });
  (async () => {
    for (let attempt = 0; !stop.signal.aborted; attempt++) {
      // One connection per attempt. The watchdog aborts it when no bytes arrive in time; the loop then reconnects.
      const conn = new AbortController();
      const kill = () => conn.abort();
      stop.signal.addEventListener("abort", kill);
      let watchdog: ReturnType<typeof setTimeout> | undefined;
      const pet = () => { clearTimeout(watchdog); watchdog = setTimeout(kill, STREAM_SILENCE_MS); };
      let closed = false;
      try {
        if (!accessToken && !(await refreshSession())) throw new ApiError(401, "urn:lifelink:problem:unauthorized", "Unauthorized");
        pet();
        let res = await open(conn.signal);
        if (res.status === 401 && (await refreshSession())) res = await open(conn.signal);   // refresh once, then give up
        if (!res.ok || !res.body) throw await problemFrom(res);
        onLive(true);
        attempt = 0;
        const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
        let buffer = "";
        for (;;) {
          pet();
          const { value, done } = await reader.read();
          if (done) break;
          buffer += value;
          let cut;
          while ((cut = buffer.indexOf("\n\n")) >= 0) {           // one SSE frame ends with a blank line
            const frame = buffer.slice(0, cut);
            buffer = buffer.slice(cut + 2);
            const data = frame.split("\n").find((l) => l.startsWith("data: "));
            if (frame.includes("event: snapshot") && data) {
              const snap: Snapshot = JSON.parse(data.slice(6));
              onSnapshot(snap);
              closed = snap.request.status === "FULFILLED" || snap.request.status === "CANCELLED";
            }
          }
        }
      } catch (e) {
        if (stop.signal.aborted) return;
        if (e instanceof ApiError && [401, 403, 404].includes(e.status)) {
          onLive(false);
          onError(e);
          return;
        }
      } finally {
        clearTimeout(watchdog);
        stop.signal.removeEventListener("abort", kill);
      }
      onLive(false);
      if (closed) return;                                         // the server ended it: nothing more will happen
      const wait = Math.min(1000 * 2 ** attempt, 15000);          // 1, 2, 4 ... 15 s, cut short if the page stops watching
      await new Promise<void>((r) => {
        const t = setTimeout(r, wait);
        stop.signal.addEventListener("abort", () => { clearTimeout(t); r(); }, { once: true });
      });
    }
  })();
  return () => stop.abort();
}
