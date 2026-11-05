"use client";
// One emergency, live: status, each blood bank's hold and donor answers update the moment they change (SSE, ADR 018).
// Closing always says what physically happened: which banks' units the ward collected (ADR 021 revision 2).
import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { ApiError, UNAVAILABLE, authed, closeRequest, watchEmergency, type Emergency, type Hold, type Snapshot } from "@/lib/api";
import { useMe } from "@/lib/useMe";
import { StaffBar, useOrg } from "@/lib/staff";
import { Badge, HOLD_LOOK, Icon, PhoneLine, focusOnMount, Skeleton, URGENCY_LOOK, ago, left, requestLook, unitsLine, useNow, useReturnFocus } from "@/lib/ui";

const LIVE_HOLD = new Set<Hold["status"]>(["PENDING", "CONFIRMED", "OVERDUE"]);   // held at the bank, not yet collected
const SET_ASIDE = new Set<Hold["status"]>(["CONFIRMED", "OVERDUE"]);     // the bank checked the shelf (overdue = still held)
const plural = (n: number, one: string, many = one + "s") => `${n} ${n === 1 ? one : many}`;

/** The API's problem, in words a nurse can act on. Switches on the problem type, never on the title. */
function problemText(e: unknown): string {
  const p = e instanceof ApiError ? e : null;
  return p?.is("nothing-collected") ? "Tick at least one bank you collected units from. If nothing was collected, cancel the request instead."
    : p?.is("units-collected") ? "Units were already collected for this request, so it cannot be cancelled. Close it as received instead."
    : p?.is("unknown-bank") ? "One of the ticked banks no longer holds units for this request. The list is up to date: check it and try again."
    : p?.is("hold-not-live") ? "This bank's hold was already released and its units went back to its stock. Call the bank before collecting."
    : p?.is("invalid-transition") ? "This request is already closed."
    : p?.is("database-unavailable") ? UNAVAILABLE
    : p ? p.message : "Could not reach Rakt Doot. Check the connection and try again.";
}

type Note = { kind: "ok" | "bad" | "warn"; text: string };

export default function RequestLive() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const me = useMe(["hospital", "admin"]);
  const [org] = useOrg(me);
  const now = useNow(15000);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [live, setLive] = useState(false);
  const [failed, setFailed] = useState(false);     // 403/404: another hospital's request, or a wrong link. Retrying cannot help.
  const [note, setNote] = useState<Note | null>(null);
  const [busy, setBusy] = useState(false);
  const [closing, setClosing] = useState(false);
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const focus = useReturnFocus();

  useEffect(() => (me ? watchEmergency(id, setSnap, setLive, (e) => {
    if (e.status === 401) router.replace("/login");      // logged out (refresh failed): the only reason to go to /login
    else setFailed(true);
  }) : undefined), [me, id, router]);   // returns the unsubscribe

  /** Runs one API call and shows its answer. No manual refresh afterwards: the change arrives through the live stream. */
  async function act<T>(call: () => Promise<T>, done: (body: T) => Note): Promise<boolean> {
    setBusy(true);
    setNote(null);
    try {
      setNote(done(await call()));
      return true;
    } catch (e) {
      setNote({ kind: "bad", text: problemText(e) });
      return false;
    } finally {
      setBusy(false);
    }
  }
  const post = <T,>(path: string) => () => authed<T>(`/v1/emergencies/${id}/${path}`, { method: "POST" });

  const bar = <StaffBar me={me} org={org} desk="Emergency request" back="/hospital" />;
  if (failed) return (
    <main className="desk">{bar}<div className="desk-main stack">
      <h1 className="sr-only">Emergency request</h1>
      <p className="notice bad" role="alert"><Icon name="alert" />
        <span>We could not open this request. It may belong to another hospital or the link is wrong.</span></p>
      <div><Link className="btn" href="/hospital">Back to your requests</Link></div>
    </div></main>
  );
  if (!me || !snap) return (
    <main className="desk">{bar}<div className="desk-main"><p className="sr-only" role="status">Connecting…</p><Skeleton /></div></main>
  );

  const r = snap.request;
  const open = r.status === "OPEN" || r.status === "ALLOCATED";
  const liveHolds = r.holds.filter((h) => LIVE_HOLD.has(h.status));
  const collectedHolds = r.holds.filter((h) => h.status === "ISSUED");
  const confirmed = liveHolds.filter((h) => SET_ASIDE.has(h.status));
  const confirmedUnits = confirmed.reduce((n, h) => n + h.units, 0);
  const held = r.units_allocated - r.units_collected;
  const tickedLive = liveHolds.filter((h) => ticked.has(h.blood_bank_id));     // a ticked bank may have handed over meanwhile
  const receiving = r.units_collected + tickedLive.reduce((n, h) => n + h.units, 0);
  const canCollect = me.role === "hospital";        // the API takes "collected" from the ward's own staff only
  const accepted = snap.responses.filter((a) => a.status === "ACCEPTED").length;
  const pct = Math.min(100, Math.round((100 * r.units_allocated) / r.units_needed));
  const toggle = (bank: string) => setTicked((t) => {
    const n = new Set(t);
    if (n.has(bank)) n.delete(bank); else n.add(bank);
    return n;
  });
  const startClose = () => { setNote(null); setTicked(new Set()); setClosing(true); };

  return (
    <main className="desk">
      {bar}
      <div className="desk-main">
        <section className="panel" aria-labelledby="req-title">
          <div className="panel-head">
            <div className="row" style={{ gap: "var(--s3)" }}>
              <span className="bg-pill">{r.blood_group}</span>
              <div>
                <h1 id="req-title" style={{ fontSize: "var(--t-xl)" }}>
                  {plural(r.units_needed, "unit")} of {r.component === "plasma" ? "plasma" : "red cells"}
                </h1>
                <p className="muted">Request <b>{r.code}</b>{r.hospital_ref ? <> · bed {r.hospital_ref}</> : null} · posted {ago(r.created_at, now)}</p>
              </div>
            </div>
            <div className="row">
              <Badge look={URGENCY_LOOK[r.urgency] ?? URGENCY_LOOK.NORMAL} />
              <Badge look={requestLook(r)} />
              <span className={`badge ${live ? "ok" : open ? "warn" : ""}`} aria-live="polite">
                {live ? <><span className="live-dot" aria-hidden="true" />Live updates on</>
                  : open ? <><Icon name="refresh" />Reconnecting…</> : <><Icon name="minus" />Closed</>}
              </span>
            </div>
          </div>
          <div className="panel-body stack">
            <div className="spread" aria-live="polite">
              <b className="num">{unitsLine(r)}</b>
              {open && held > 0 && <span className="muted num">{confirmedUnits} of the {held} held confirmed by the bank</span>}
            </div>
            <div className={`progress ${r.units_collected >= r.units_needed || r.status === "FULFILLED" ? "ok" : ""}`} role="progressbar"
                 aria-label="Units held or collected" aria-valuemin={0} aria-valuemax={r.units_needed} aria-valuenow={r.units_allocated}
                 aria-valuetext={unitsLine(r)}>
              <span style={{ width: `${pct}%` }} />
            </div>
          </div>
        </section>

        <div className="desk-cols wide-left">
          <section className="panel" aria-labelledby="holds-title">
            <div className="panel-head">
              <h2 id="holds-title">Blood banks</h2>
              {liveHolds.length > 0 && <span className="muted">{confirmed.length} of {plural(liveHolds.length, "bank")} confirmed</span>}
            </div>
            <div className="panel-body" aria-live="polite">
              {r.holds.length === 0 && (
                <div className="empty"><Icon name="building" />
                  <h3>{open ? "No bank nearby has compatible units right now" : "No bank held units for this request"}</h3>
                  {open && <p className="small">Ask nearby donors, or check the banks again after they register new stock.</p>}
                </div>
              )}
              {r.holds.map((h, i) => (
                <HoldRow key={`${h.blood_bank_id}-${h.status}-${i}`} hold={h} now={now} requestOpen={open} busy={busy}
                         onCollect={canCollect ? () => act(post<Emergency>(`holds/${h.blood_bank_id}/collected`),
                           () => ({ kind: "ok", text: `Marked ${plural(h.units, "unit")} from ${h.bank_name} as collected.` })) : undefined} />
              ))}
            </div>
          </section>

          <div className="stack-lg">
            <section className="panel" aria-labelledby="next-title">
              <div className="panel-head"><h2 id="next-title">{open ? (closing ? "Close this request" : "Next step") : "Request closed"}</h2></div>
              <div className="panel-body stack">
                {note && <p className={`notice ${note.kind}`} role="status" tabIndex={-1} ref={note.kind === "ok" ? focusOnMount : undefined}><Icon name={note.kind === "ok" ? "checkCircle" : "alert"} /><span>{note.text}</span></p>}
                {r.status === "FULFILLED" && <p className="muted">{plural(r.units_collected, "unit")} collected. Units that were not collected went back to their banks. Thank you.</p>}
                {r.status === "CANCELLED" && <p className="muted">This request was cancelled. Held units went back to the banks.</p>}
                {r.status === "ALLOCATED" && !closing && (
                  <p>Send someone to collect from the banks on the left. Press Collected from this bank as units arrive.
                    Close the request when the blood is with the patient.</p>
                )}
                {open && !closing && (
                  <button className="btn block" disabled={busy} aria-describedby={r.status === "ALLOCATED" ? "donors-help" : undefined}
                          onClick={() => act(post<{ queued: number }>("notify-donors"), (b) => ({ kind: "ok", text: `${b.queued} nearby donor${b.queued === 1 ? " is" : "s are"} being alerted. Their answers appear below.` }))}>
                    <Icon name="bell" />Ask nearby donors
                  </button>
                )}
                {r.status === "ALLOCATED" && !closing && (
                  <p className="muted small" id="donors-help">All units are held. Alert donors anyway if a bank may not confirm.</p>
                )}
                {r.status === "OPEN" && !closing && (
                  <>
                    <button className="btn ghost block" disabled={busy} aria-describedby="again-help"
                            onClick={() => act(post<Emergency>("allocate"), (b) => b.cap_reached
                              ? { kind: "warn", text: "Some units could not be held: your hospital already holds the most units allowed at once. Close requests you no longer need, then try again." }
                              : { kind: "ok", text: "Checked the blood banks again." })}>
                      <Icon name="refresh" />Check blood banks again
                    </button>
                    <p className="muted small" id="again-help">A bank that missed its time to confirm can be asked again. Call it first.</p>
                  </>
                )}
                {open && !closing && (
                  // secondary until blood has actually left a bank: closing early is the mistake to avoid
                  <button className={`btn ${r.units_collected > 0 ? "okay" : "ghost"} block`} disabled={busy} onClick={startClose} ref={focus.opener("close")}>
                    <Icon name="check" />Close request
                  </button>
                )}
                {open && closing && (
                  <ClosePanel liveHolds={liveHolds} collectedHolds={collectedHolds} ticked={ticked} toggle={toggle}
                              receiving={receiving} collected={r.units_collected} busy={busy} onBack={() => { focus.back("close"); setClosing(false); }}
                              onFulfil={async () => {
                                if (await act(() => closeRequest(id, "fulfil", { collected_from: tickedLive.map((h) => h.blood_bank_id) }),
                                  (b) => ({ kind: "ok", text: `Closed: ${plural(b.units_collected, "unit")} received.` })))
                                  setClosing(false);
                              }}
                              onCancel={async () => {
                                if (await act(() => closeRequest(id, "cancel", { collected_from: [] }),
                                  () => ({ kind: "ok", text: "Request cancelled." })))
                                  setClosing(false);
                              }} />
                )}
              </div>
            </section>

            <section className="panel" aria-labelledby="donors-title">
              <div className="panel-head">
                <h2 id="donors-title">Donors</h2>
                <span className="muted num">{snap.responses.length} asked · {accepted} said yes</span>
              </div>
              <div className="panel-body stack" aria-live="polite">
                {snap.responses.length === 0 && <p className="muted">No donors asked yet.</p>}
                {snap.responses.map((a) => (
                  <div className="spread" key={a.donor_id}>
                    <span className="row"><span className="bg-pill sm">{a.blood_group}</span>{a.first_name}</span>
                    <span className={`badge ${a.status === "ACCEPTED" ? "ok" : a.status === "DECLINED" ? "" : "warn"}`}>
                      <Icon name={a.status === "ACCEPTED" ? "check" : a.status === "DECLINED" ? "minus" : "clock"} />
                      {a.status === "ACCEPTED" ? "Said yes" : a.status === "DECLINED" ? "Said no" : "No answer yet"}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          </div>
        </div>
      </div>
    </main>
  );
}

/** "Which banks did you collect from?" The API releases every hold that is neither ticked nor already collected. */
function ClosePanel({ liveHolds, collectedHolds, ticked, toggle, receiving, collected, busy, onBack, onFulfil, onCancel }: {
  liveHolds: Hold[]; collectedHolds: Hold[]; ticked: Set<string>; toggle: (bank: string) => void; receiving: number;
  collected: number; busy: boolean; onBack: () => void; onFulfil: () => void; onCancel: () => void;
}) {
  const tickedAny = liveHolds.some((h) => ticked.has(h.blood_bank_id));
  const cancelBlocked = collected > 0
    ? `You cannot cancel: ${plural(collected, "unit")} ${collected === 1 ? "was" : "were"} already collected. Close the request as received.`
    : tickedAny ? "Untick every bank to cancel: cancelling means nothing was collected." : null;
  return (
    <div className="stack">
      <fieldset className="stack" style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
        <legend style={{ fontWeight: 650, marginBottom: "var(--s2)" }}>Which banks did you collect units from?</legend>
        {collectedHolds.map((h) => (
          <label className="check" key={`c-${h.blood_bank_id}`}>
            <input type="checkbox" checked disabled />
            <span>{plural(h.units, "unit")} from {h.bank_name}: <b>already collected</b></span>
          </label>
        ))}
        {liveHolds.map((h, i) => (
          <label className="check" key={h.blood_bank_id}>
            <input type="checkbox" checked={ticked.has(h.blood_bank_id)} disabled={busy} onChange={() => toggle(h.blood_bank_id)}
                   autoFocus={i === 0} />
            <span>We collected {plural(h.units, "unit")} from {h.bank_name}</span>
          </label>
        ))}
        {liveHolds.length + collectedHolds.length === 0 && <p className="muted">No bank holds units for this request.</p>}
      </fieldset>
      {liveHolds.length > 0 && <p className="muted small">Units from banks you leave unticked go back to that bank&apos;s stock.</p>}
      <button className="btn okay block" disabled={busy || receiving === 0} onClick={onFulfil} autoFocus={liveHolds.length === 0}>
        <Icon name="check" />Blood received, close{receiving > 0 ? `: ${plural(receiving, "unit")}` : ""}
      </button>
      <button className="btn danger block" disabled={busy || !!cancelBlocked} onClick={onCancel}
              aria-describedby={cancelBlocked ? "cancel-why" : undefined}>
        Cancel request, nothing was collected
      </button>
      {cancelBlocked && <p className="small" id="cancel-why"><Icon name="info" /> {cancelBlocked}</p>}
      <button className="btn ghost block" disabled={busy} onClick={onBack}>Back</button>
    </div>
  );
}

function HoldRow({ hold: h, now, requestOpen, busy, onCollect }: {
  hold: Hold; now: number; requestOpen: boolean; busy: boolean; onCollect?: () => Promise<boolean>;
}) {
  const [asking, setAsking] = useState(false);
  const focus = useReturnFocus();
  const isLive = requestOpen && LIVE_HOLD.has(h.status);     // a closed request's holds have no deadline left
  const remaining = left(h.expires_at, now);
  return (
    <article className={`hold ${isLive || h.status === "ISSUED" ? "" : "done"}`}>
      <div className="spread">
        <div>
          <h3>{h.bank_name}</h3>
          <p className="muted num">{plural(h.units, "unit")}{isLive ? " held" : h.status === "ISSUED" ? " collected" : ""}</p>
        </div>
        <Badge look={HOLD_LOOK[h.status]} />
      </div>
      {isLive && (
        <p className={`small num ${remaining ? "" : "error-text"}`}>
          <Icon name="clock" />{" "}
          {h.status === "PENDING"
            ? remaining ? `The bank must confirm within ${remaining}, or the hold is released.` : "Confirmation time is up. Updating…"
            : h.status === "OVERDUE" ? `Pickup was due ${ago(h.expires_at, now)}. Collect the units, or call the bank: it may put them back in stock.`
            : remaining ? `Collect within ${remaining}.` : "The pickup deadline has passed. Collect the units, or call the bank."}
        </p>
      )}
      {isLive && (h.bank_phone ? <PhoneLine phone={h.bank_phone} label="Call bank" who={h.bank_name} demo={h.bank_is_demo} /> : <p className="muted">No phone number listed for this bank.</p>)}
      {isLive && onCollect && !asking && (
        <div><button className="btn ghost sm" disabled={busy} onClick={() => setAsking(true)} ref={focus.opener("collect")}>
          <Icon name="box" />Collected from this bank<span className="sr-only"> {h.bank_name}</span>
        </button></div>
      )}
      {isLive && onCollect && asking && (
        <div className="stack" role="group" aria-label={`Mark units from ${h.bank_name} as collected`} style={{ gap: "var(--s2)" }}>
          <p className="small">Mark {plural(h.units, "unit")} from {h.bank_name} as collected? {h.units === 1 ? "It leaves" : "They leave"} the bank&apos;s stock for good.</p>
          <div className="row">
            <button className="btn okay sm" disabled={busy} autoFocus
                    onClick={async () => { if (await onCollect()) setAsking(false); }}>
              <Icon name="check" />Yes, collected
            </button>
            <button className="btn ghost sm" disabled={busy} onClick={() => { focus.back("collect"); setAsking(false); }}>Back</button>
          </div>
        </div>
      )}
    </article>
  );
}
