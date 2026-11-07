"use client";
// Blood bank desk (desktop layout): confirm or reject holds that hospitals' requests placed on this bank's units
// (ADR 021), count the shelf so the list matches it (stock-take, ADR 023), register collected units, and see stock soonest-to-expire first (FEFO).
import { useCallback, useEffect, useState } from "react";
import { ApiError, UNAVAILABLE, authed, BLOOD_GROUPS, markIssued, qs, stockSummary, stockTake, type BankHold, type BloodGroup, type BloodUnit, type Component, type Organization, type StockLine } from "@/lib/api";
import { useMe } from "@/lib/useMe";
import { StaffBar, useOrg } from "@/lib/staff";
import { Badge, Icon, PhoneLine, Skeleton, URGENCY_LOOK, ago, focusOnMount, left, useNow, useReturnFocus } from "@/lib/ui";

const today = () => new Date().toLocaleDateString("en-CA");       // YYYY-MM-DD in the user's own time zone
const HOLDS_POLL_MS = 20000;       // Trade-off: polling; banks have no live stream yet. Upgrade: SSE like the hospital page.
const COMPONENT_TEXT: Record<string, string> = { red_cells: "red cells", plasma: "plasma", platelets: "platelets" };
const pairText = (l: StockLine) => `${l.blood_group} ${COMPONENT_TEXT[l.component] ?? l.component}`;
const REASONS = { not_in_stock: "Not in stock", cannot_supply: "Cannot supply" } as const;
type Reason = keyof typeof REASONS;

export default function Bank() {
  const me = useMe(["blood_bank", "admin"]);
  const [org, setOrg] = useOrg(me);
  const bar = <><StaffBar me={me} org={org} desk="Blood bank desk" /><h1 className="sr-only">Blood bank desk</h1></>;

  if (!me) return <main className="desk">{bar}<div className="desk-main"><Skeleton /></div></main>;
  if (me.role !== "blood_bank" || !me.organization_id) return (
    <main className="desk">{bar}<div className="desk-main">
      <p className="notice"><Icon name="info" /><span><b>This desk belongs to a blood bank</b>
        Holds, stock counts and stock entry are done by the bank&apos;s own staff. Log in with a blood bank account.</span></p>
    </div></main>
  );
  return <BankDesk bankId={me.organization_id} org={org} setOrg={setOrg} bar={bar} />;
}

function BankDesk({ bankId, org, setOrg, bar }: { bankId: string; org: Organization | null; setOrg: (o: Organization) => void; bar: React.ReactNode }) {
  const now = useNow(15000);
  const [holds, setHolds] = useState<BankHold[] | null>(null);
  const [holdsError, setHoldsError] = useState(false);
  const [units, setUnits] = useState<BloodUnit[] | null>(null);
  const [unitsError, setUnitsError] = useState(false);
  const [summary, setSummary] = useState<StockLine[] | null>(null);
  const [summaryError, setSummaryError] = useState(false);
  const [note, setNote] = useState<{ kind: "ok" | "bad"; text: string } | null>(null);

  const loadHolds = useCallback(() => authed<BankHold[]>("/v1/emergencies/holds")
    .then((h) => { setHolds(h); setHoldsError(false); }).catch(() => setHoldsError(true)), []);
  // The unit list and the count summary change on the same actions, so they load together. The summary is a GROUP BY
  // on the server: the paginated list (limit 100) would undercount a big bank.
  const loadUnits = useCallback(() => {
    stockSummary(bankId).then((l) => { setSummary(l); setSummaryError(false); }).catch(() => setSummaryError(true));
    return authed<{ items: BloodUnit[] }>("/v1/blood-units" + qs({ blood_bank_id: bankId, limit: "100" }))
      .then((p) => { setUnits(p.items); setUnitsError(false); }).catch(() => setUnitsError(true));
  }, [bankId]);

  useEffect(() => {
    loadHolds();
    loadUnits();
    const t = setInterval(loadHolds, HOLDS_POLL_MS);
    const onFocus = () => document.visibilityState === "visible" && loadHolds();
    document.addEventListener("visibilitychange", onFocus);
    return () => { clearInterval(t); document.removeEventListener("visibilitychange", onFocus); };
  }, [loadHolds, loadUnits]);

  async function run(call: () => Promise<unknown>, ok: string) {
    setNote(null);
    try {
      await call();
      setNote({ kind: "ok", text: ok });
    } catch (e) {
      const p = e instanceof ApiError ? e : null;
      setNote({ kind: "bad", text:
        p?.is("hold-not-pending") ? "This hold has expired or was already released, so it cannot be changed. The list is refreshed."
        : p?.is("hold-not-overdue") ? "This hold is not overdue any more (the hospital may have collected or cancelled), so it was not released. The list is refreshed."
        : p?.is("hold-not-found") ? "This request no longer has a hold at your bank. The list is refreshed."
        : p?.is("hold-not-live") ? "This hold was already released and its units are back in your available stock, so it cannot be handed over. Call the hospital."
        : p?.is("database-unavailable") ? UNAVAILABLE
        : p ? p.message : "Could not reach Rakt Doot. Check the connection and try again." });
    }
    loadHolds();
    loadUnits();
  }

  const pending = holds?.filter((h) => h.status === "PENDING") ?? [];
  // Tab-title signal: a desk left in a background tab still shows that holds are waiting.
  useEffect(() => {
    document.title = pending.length ? `(${pending.length}) Blood bank desk` : "Blood bank desk";
  }, [pending.length]);
  useEffect(() => () => { document.title = "Rakt Doot"; }, []);   // the layout's title, when leaving the desk
  const confirmed = holds?.filter((h) => h.status === "CONFIRMED" || h.status === "OVERDUE") ?? [];

  return (
    <main className="desk">
      {bar}
      <div className="desk-main">
        <div className="kpis">
          <div className="kpi"><b style={pending.length ? { color: "var(--warn)" } : undefined}>{holds ? pending.length : "–"}</b><span>Holds waiting for you</span></div>
          <div className="kpi"><b>{holds ? confirmed.length : "–"}</b><span>Confirmed, awaiting pickup</span></div>
          <div className="kpi"><b>{summary ? summary.reduce((n, l) => n + l.units, 0) : "–"}</b><span>Units available</span></div>
          <div className="kpi"><b style={{ fontSize: "var(--t-lg)", paddingTop: 8 }}>{org ? (org.stock_confirmed_at ? ago(org.stock_confirmed_at, now) : "Never") : "–"}</b><span>Stock last counted</span></div>
        </div>

        {note && <p className={`notice ${note.kind}`} role="status" tabIndex={-1} ref={note.kind === "ok" ? focusOnMount : undefined}><Icon name={note.kind === "ok" ? "checkCircle" : "alert"} /><span>{note.text}</span></p>}

        <section className="panel" aria-labelledby="holds-title">
          <div className="panel-head">
            <div>
              <h2 id="holds-title">Holds on your units</h2>
              <ol className="muted" style={{ margin: "var(--s1) 0 0", paddingLeft: "1.2em" }}>
                <li>Check the shelf, then press Confirm before the time runs out.</li>
                <li>Keep confirmed units aside until the hospital collects them.</li>
                <li>Press Handed over when the units leave your counter.</li>
              </ol>
            </div>
            <button className="btn ghost sm" onClick={loadHolds}><Icon name="refresh" />Refresh</button>
          </div>
          <div className="panel-body">
            <p className="sr-only" aria-live="polite">{holds ? `${pending.length} hold${pending.length === 1 ? "" : "s"} waiting for confirmation.` : ""}</p>
            {holdsError && <p className="notice bad" role="alert"><Icon name="alert" /><span>Could not load holds. Retrying every 20 seconds.</span></p>}
            {holds === null && !holdsError && <Skeleton lines={2} />}
            {holds?.length === 0 && !holdsError && (
              <div className="empty"><Icon name="checkCircle" /><h3>No holds right now</h3>
                <p className="small">When a hospital&apos;s emergency request holds units here, it appears in this list.</p></div>
            )}
            {holds && holds.length > 0 && (
              <div className="table-wrap">
                <table className="data">
                  <thead><tr><th scope="col">Hospital</th><th scope="col">Needs</th><th scope="col">Urgency</th><th scope="col">Status</th><th scope="col">Time left</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
                  <tbody>
                    {holds.map((h) => <HoldRow key={h.request_id} hold={h} now={now} run={run} onStart={() => setNote(null)} />)}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </section>

        <div className="desk-cols">
          <div className="stack-lg">
            <StockPanel org={org} setOrg={setOrg} bankId={bankId} now={now} run={run}
                        summary={summary} summaryError={summaryError} reload={loadUnits} />
            <AddUnits bankId={bankId} onAdded={loadUnits} />
          </div>
          <StockList units={units} now={now} error={unitsError} onRetry={loadUnits} />
        </div>
      </div>
    </main>
  );
}

function HoldRow({ hold: h, now, run, onStart }: {
  hold: BankHold; now: number; run: (c: () => Promise<unknown>, ok: string) => Promise<void>; onStart: () => void;
}) {
  // One open step at a time; opening a step clears the previous action's message (N3).
  const [step, setStep] = useState<"reject" | "release" | "handover" | null>(null);
  const open = (s: typeof step) => { onStart(); setStep(s); };
  const rejecting = step === "reject", releasing = step === "release", handing = step === "handover";
  const [reason, setReason] = useState<Reason | "">("");
  const [busy, setBusy] = useState(false);
  const focus = useReturnFocus();
  const back = (from: NonNullable<typeof step>) => { focus.back(from); setStep(null); };
  const code = h.code;
  const remaining = left(h.expires_at, now);
  const go = async (call: () => Promise<unknown>, ok: string) => { setBusy(true); await run(call, ok); setBusy(false); };
  const what = `${h.units} unit${h.units === 1 ? "" : "s"} of ${h.blood_group} ${h.component === "plasma" ? "plasma" : "red cells"}`;

  return (
    <tr>
      <td style={{ minWidth: 160 }}><b>{h.hospital_name}</b><div className="muted">Request {code}{h.hospital_ref ? ` · bed ${h.hospital_ref}` : ""}</div>
        {h.hospital_phone && <PhoneLine phone={h.hospital_phone} label="Call" who={h.hospital_name} demo={h.hospital_is_demo} />}</td>
      <td className="nowrap"><span className="row" style={{ flexWrap: "nowrap" }}><span className="bg-pill sm">{h.blood_group}</span>
        <span className="num">{h.units} × {h.component === "plasma" ? "plasma" : "red cells"}</span></span></td>
      <td><Badge look={URGENCY_LOOK[h.urgency] ?? URGENCY_LOOK.NORMAL} /></td>
      <td>{h.status === "PENDING" ? <span className="badge warn"><Icon name="clock" />Needs your check</span>
        : h.status === "OVERDUE" ? <span className="badge bad"><Icon name="alert" />Pickup overdue</span>
        : <span className="badge ok"><Icon name="checkCircle" />Confirmed</span>}</td>
      <td className="num" style={{ whiteSpace: "nowrap" }}>
        {h.status === "OVERDUE" ? <>Pickup was due<br /><b className="error-text">{ago(h.expires_at, now)}</b></>
          : remaining ? <>{h.status === "PENDING" ? "Confirm within" : "Pickup within"}<br /><b>{remaining}</b></>
          : <span className="error-text">{h.status === "PENDING" ? "Time is up" : "Pickup deadline passed"}</span>}
      </td>
      <td style={{ minWidth: 220 }}>
        {handing && (
          <div className="stack" style={{ gap: "var(--s2)" }} role="group" aria-label={`Hand over units held for ${h.hospital_name}`}>
            <small>Hand {what} to {h.hospital_name}? {h.units === 1 ? "It leaves" : "They leave"} your stock for good and the hospital sees {h.units === 1 ? "it" : "them"} as collected.</small>
            <div className="row" style={{ flexWrap: "nowrap" }}>
              <button className="btn okay sm" disabled={busy} autoFocus style={{ whiteSpace: "nowrap" }}
                      onClick={() => go(() => markIssued(h.request_id), `Handed over ${what} to ${h.hospital_name}. They are off your stock list.`)}>
                <Icon name="check" />Yes, handed over
              </button>
              <button className="btn ghost sm" disabled={busy} onClick={() => back("handover")}>Back</button>
            </div>
          </div>
        )}
        {h.status === "PENDING" && !step && (
          <div className="row" style={{ flexWrap: "nowrap" }}>
            <button className="btn okay sm" disabled={busy || !remaining}
                    onClick={() => go(() => authed(`/v1/emergencies/${h.request_id}/holds/confirm`, { method: "POST" }),
                                      `Confirmed ${what} for ${h.hospital_name}. Keep them aside for pickup.`)}>
              <Icon name="check" />Confirm<span className="sr-only"> hold for {h.hospital_name}, request {code}</span>
            </button>
            <button className="btn danger sm" disabled={busy} onClick={() => open("reject")} ref={focus.opener("reject")}>
              Reject<span className="sr-only"> hold for {h.hospital_name}, request {code}</span>
            </button>
          </div>
        )}
        {h.status === "PENDING" && rejecting && (
          <div className="stack" style={{ gap: "var(--s2)" }}>
            <label className="field"><span>Why can you not supply?</span>
              <select className="input" value={reason} onChange={(e) => setReason(e.target.value as Reason)} autoFocus>
                <option value="" disabled>Choose a reason</option>
                {Object.entries(REASONS).map(([v, t]) => <option key={v} value={v}>{t}</option>)}
              </select>
            </label>
            {reason === "not_in_stock" && <small className="muted">These units will be removed from your stock list.</small>}
            {reason === "cannot_supply" && <small className="muted">These units go back to available stock.</small>}
            <div className="row" style={{ flexWrap: "nowrap" }}>
              <button className="btn danger sm" disabled={busy || !reason}
                      onClick={() => go(() => authed(`/v1/emergencies/${h.request_id}/holds/reject`, { method: "POST", body: JSON.stringify({ reason }) }),
                                        `Rejected the hold for ${h.hospital_name}. They have been told, and your bank will not be asked again for this request.`)}>
                Reject hold
              </button>
              <button className="btn ghost sm" disabled={busy} onClick={() => { back("reject"); setReason(""); }}>Back</button>
            </div>
          </div>
        )}
        {h.status === "CONFIRMED" && !step && <span className="muted">Waiting for the hospital to collect.</span>}
        {h.status === "OVERDUE" && !step && (
          <div className="stack" style={{ gap: "var(--s2)" }}>
            <span className="muted">The units are still held. Call {h.hospital_name} first.</span>
            <button className="btn danger sm" disabled={busy} onClick={() => open("release")} ref={focus.opener("release")}>
              Release units<span className="sr-only"> held for {h.hospital_name}, request {code}</span>
            </button>
          </div>
        )}
        {h.status === "OVERDUE" && releasing && (
          <div className="stack" style={{ gap: "var(--s2)" }} role="group" aria-label={`Release units held for ${h.hospital_name}`}>
            <small>{h.units === 1 ? "This unit goes" : `These ${h.units} units go`} back to your available stock, and {h.hospital_name} sees the hold as released. Only do this if the units were not collected.</small>
            <div className="row" style={{ flexWrap: "nowrap" }}>
              <button className="btn danger sm" disabled={busy} autoFocus
                      onClick={() => go(() => authed(`/v1/emergencies/${h.request_id}/holds/release`, { method: "POST" }),
                                        `Released ${what}. Back in your available stock; ${h.hospital_name} sees the hold as released.`)}>
                Yes, release
              </button>
              <button className="btn ghost sm" disabled={busy} onClick={() => back("release")}>Keep holding</button>
            </div>
          </div>
        )}
        {/* only a hold the bank checked can be handed over: a PENDING one is confirmed first */}
        {!step && (h.status === "CONFIRMED" || h.status === "OVERDUE") && (
          <div style={{ marginTop: "var(--s2)" }}>
            <button className="btn ghost sm" disabled={busy} onClick={() => open("handover")} ref={focus.opener("handover")}>
              <Icon name="box" />Handed over<span className="sr-only"> to {h.hospital_name}, request {code}</span>
            </button>
          </div>
        )}
      </td>
    </tr>
  );
}

type Msg = { kind: "ok" | "bad"; text: string };

function StockPanel({ org, setOrg, bankId, now, run, summary, summaryError, reload }: {
  org: Organization | null; setOrg: (o: Organization) => void; bankId: string; now: number;
  run: (c: () => Promise<unknown>, ok: string) => Promise<void>;
  summary: StockLine[] | null; summaryError: boolean; reload: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<Msg | null>(null);     // here, not in the form: the form re-mounts when the list changes
  const stale = !org?.stock_confirmed_at || now - new Date(org.stock_confirmed_at).getTime() > 24 * 3600000;

  async function settings(on: boolean) {
    // Turning it on means no person checks the shelf before a hospital is told "set aside": ask first.
    if (on && !window.confirm("Confirm new holds automatically?\n\nHospitals will be told units are set aside without anyone checking the shelf. Only turn this on if you count your stock often and the list is exact.")) return;
    setBusy(true);
    await run(async () => setOrg(await authed<Organization>(`/v1/organizations/${bankId}/bank-settings`,
                                                          { method: "PATCH", body: JSON.stringify({ auto_confirm_holds: on }) })),
              on ? "New holds will be confirmed automatically." : "You will confirm each new hold yourself.");
    setBusy(false);
  }

  return (
    <section className="panel" aria-labelledby="stock-title">
      <div className="panel-head"><h2 id="stock-title">Count your stock</h2></div>
      <div className="panel-body stack">
        {/* nothing until the bank has loaded: "Never counted" before the data arrives would be false */}
        {org ? (
          <p className="small">
            {org.stock_confirmed_at
              ? <><Icon name={stale ? "alert" : "checkCircle"} /> Last counted <b>{ago(org.stock_confirmed_at, now)}</b>.</>
              : <><Icon name="alert" /> <b>Never counted.</b></>}
            {" "}Families searching for blood see this before they travel.
          </p>
        ) : <Skeleton lines={1} />}
        {summaryError && (
          <div className="notice bad" role="alert"><Icon name="alert" /><span>Could not load your stock counts.{" "}
            <button type="button" className="btn ghost sm" onClick={reload}><Icon name="refresh" />Try again</button></span></div>
        )}
        {summary === null && !summaryError && <Skeleton lines={1} />}
        {summary && org && (
          <StockTakeForm key={JSON.stringify(summary)} summary={summary} bankId={bankId} setMsg={setMsg}
                         onCounted={(at) => { setOrg({ ...org, stock_confirmed_at: at }); reload(); }} onStale={reload} />
        )}
        {msg && <p className={`notice ${msg.kind}`} role="status" tabIndex={-1} ref={msg.kind === "ok" ? focusOnMount : undefined}>
          <Icon name={msg.kind === "ok" ? "checkCircle" : "alert"} /><span>{msg.text}</span></p>}
        <hr className="sep" />
        <label className="switch">
          <span className="grow">
            <b style={{ display: "block" }}>Confirm new holds automatically</b>
            <span className="muted">
              {org?.auto_confirm_holds ? "On: new holds start as confirmed. Keep your stock list exact."
                : "Off: you confirm each hold after checking the shelf."}
            </span>
          </span>
          <input type="checkbox" checked={!!org?.auto_confirm_holds} disabled={busy || !org}
                 onChange={(e) => settings(e.target.checked)} />
          <span className="track" aria-hidden="true" />
        </label>
      </div>
    </section>
  );
}

/** Staff type what is really on the shelf (ADR 023). The API takes the surplus off the list (earliest expiry first,
 *  marked issued) and refuses a count above the list: new bags are registered with their label expiry. Keyed on the
 *  list, so it re-mounts and starts from the current numbers whenever the list changes. */
function StockTakeForm({ summary, bankId, setMsg, onCounted, onStale }: {
  summary: StockLine[]; bankId: string; setMsg: (m: Msg | null) => void; onCounted: (at: string) => void; onStale: () => void;
}) {
  const [counts, setCounts] = useState(() => summary.map((l) => String(l.units)));
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    setBusy(true);
    const sent = summary.map((l, i) => ({ ...l, units: Number(counts[i]) }));
    try {
      const res = await stockTake(bankId, sent);
      const removed = summary.map((l, i) => ({ l, n: l.units - sent[i].units })).filter((x) => x.n > 0);
      const total = removed.reduce((a, x) => a + x.n, 0);
      const at = new Date(res.stock_confirmed_at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
      setMsg({ kind: "ok", text: (total
        ? `Marked ${total} unit${total === 1 ? "" : "s"} as issued (${removed.map((x) => `${x.n} × ${pairText(x.l)}`).join(", ")}), earliest expiry first. `
        : "Your list matched the shelf, so nothing changed. ") + `Stock confirmed at ${at}.` });
      onCounted(res.stock_confirmed_at);
    } catch (err) {
      const p = err instanceof ApiError ? err : null;
      setMsg({ kind: "bad", text:
        p?.is("stock-take-incomplete") ? `Your stock list changed while you were counting. ${p.message} The lines are updated: count again.`
        : p?.is("stock-take-increase") ? `${p.message} A count can only lower the list. Nothing was changed.`
        : p?.is("forbidden") ? "Only this bank's own staff can count its stock."
        : p?.is("database-unavailable") ? UNAVAILABLE
        : p ? p.message : "Could not reach Rakt Doot. Check the connection and try again." });
      if (p?.is("stock-take-incomplete")) onStale();
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="stack" onSubmit={submit} aria-describedby="count-hint">
      <p className="muted" id="count-hint">Count the bags on the shelf, not the ones set aside for Rakt Doot holds. Type the real
        number on every line, 0 included. More bags than listed? Register them below with their label expiry first.</p>
      {summary.length === 0
        ? <p className="small">No usable units on your list. Save to confirm the shelf is empty too.</p>
        : (
          <div className="count-grid">
            {summary.map((l, i) => (
              <label key={`${l.blood_group}/${l.component}`} className="field">
                <span>{pairText(l)} <small className="muted">(listed {l.units})</small></span>
                <input className="input" type="number" inputMode="numeric" required min={0} max={l.units} step={1}
                       value={counts[i]} onChange={(e) => setCounts((c) => c.map((v, j) => (j === i ? e.target.value : v)))} />
              </label>
            ))}
          </div>
        )}
      <button className="btn block" disabled={busy}><Icon name="check" />{busy ? "Saving the count…" : "Save shelf count"}</button>
    </form>
  );
}

function AddUnits({ bankId, onAdded }: { bankId: string; onAdded: () => void }) {
  const [group, setGroup] = useState<BloodGroup | "">("");
  const [component, setComponent] = useState<Component>("red_cells");
  const [count, setCount] = useState(1);
  const [collected, setCollected] = useState(today());
  const [expires, setExpires] = useState("");     // read off the bag label: the bank knows its storage, the client does not
  const [msg, setMsg] = useState<{ kind: "ok" | "bad"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    setBusy(true);
    // The API wants timezone-aware timestamps. A date without a time is read in local time: collected at the start
    // of that day, usable until the end of the expiry day. toISOString() sends them as UTC.
    const collectedAt = new Date(`${collected}T00:00:00`);
    const expiresAt = new Date(`${expires}T23:59:59`);
    let added = 0;
    try {
      for (; added < count; added++) {
        await authed("/v1/blood-units", {
          method: "POST",
          body: JSON.stringify({ blood_bank_id: bankId, blood_group: group, component,
                                 collected_at: collectedAt.toISOString(), expires_at: expiresAt.toISOString() }),
        });
      }
      setMsg({ kind: "ok", text: `Added ${count} unit${count === 1 ? "" : "s"} of ${group}.` });
    } catch (err) {
      const why = err instanceof ApiError ? (err.is("database-unavailable") ? UNAVAILABLE : err.message) : "Could not reach Rakt Doot.";
      setMsg({ kind: "bad", text: `${added ? `Added ${added} of ${count}, then stopped: ` : ""}${why}` });
    } finally {
      setBusy(false);
      onAdded();
    }
  }

  return (
    <form className="panel" onSubmit={add} aria-labelledby="add-title">
      <div className="panel-head"><h2 id="add-title">Register collected units</h2></div>
      <div className="panel-body form-grid">
        <div className="two">
          <label className="field"><span>Blood group</span>
            <select className="input" required value={group} onChange={(e) => setGroup(e.target.value as BloodGroup)}>
              <option value="" disabled>Choose the group</option>
              {BLOOD_GROUPS.map((g) => <option key={g} value={g}>{g}</option>)}
            </select>
          </label>
          <label className="field"><span>Number of units</span>
            <input className="input" type="number" inputMode="numeric" min={1} max={20} required value={count} onChange={(e) => setCount(Number(e.target.value))} />
          </label>
        </div>
        <label className="field"><span>Component</span>
          <select className="input" value={component} onChange={(e) => setComponent(e.target.value as Component)}>
            <option value="red_cells">Red cells</option>
            <option value="plasma">Plasma (frozen)</option>
          </select>
        </label>
        <div className="two">
          <label className="field"><span>Collected on</span>
            <input className="input" type="date" max={today()} required value={collected} onChange={(e) => setCollected(e.target.value)} />
          </label>
          <label className="field"><span>Expires on</span>
            <input className="input" type="date" min={collected} required value={expires} onChange={(e) => setExpires(e.target.value)}
                   aria-describedby="expires-hint" />
            <small id="expires-hint">As printed on the bag label.</small>
          </label>
        </div>
        {msg && <p className={`notice ${msg.kind}`} role="status" tabIndex={-1} ref={msg.kind === "ok" ? focusOnMount : undefined}><Icon name={msg.kind === "ok" ? "checkCircle" : "alert"} /><span>{msg.text}</span></p>}
        <button className="btn block" disabled={busy}>{busy ? "Adding…" : "Add to stock"}</button>
      </div>
    </form>
  );
}

const STATUS_TEXT: Record<string, string> = { AVAILABLE: "Available", RESERVED: "Held for a request", ISSUED: "Issued", EXPIRED: "Expired", DISCARDED: "Removed" };

function StockList({ units, now, error, onRetry }: { units: BloodUnit[] | null; now: number; error: boolean; onRetry: () => void }) {
  // AVAILABLE past its expiry date is not usable (the expiry job may not have run yet): show it as expired, never count it
  const expired = (u: BloodUnit) => u.status === "AVAILABLE" && new Date(u.expires_at).getTime() <= now;
  const status = (u: BloodUnit) => (expired(u) ? "EXPIRED" : u.status);
  const available = units?.filter((u) => status(u) === "AVAILABLE") ?? [];
  const byGroup = BLOOD_GROUPS.map((g) => ({ g, n: available.filter((u) => u.blood_group === g).length }));
  return (
    <section className="panel" aria-labelledby="list-title">
      <div className="panel-head">
        <h2 id="list-title">Current stock</h2>
        <span className="muted">Soonest to expire first</span>
      </div>
      <div className="panel-body stack">
        {error && (
          <div className="notice bad" role="alert"><Icon name="alert" /><span>
            <b>Could not load your stock</b>{units ? "The list below may be out of date." : "Check the connection."}
          </span></div>
        )}
        {error && <div><button className="btn ghost sm" onClick={onRetry}><Icon name="refresh" />Try again</button></div>}
        {units === null && !error && <Skeleton lines={2} />}
        {units && (
          <div className="row" aria-label="Available units by blood group">
            {byGroup.map(({ g, n }) => (
              <span key={g} className={`badge ${n ? "ok" : ""}`}><b>{g}</b><span className="num">{n}</span></span>
            ))}
          </div>
        )}
        {units?.length === 0 && <div className="empty"><Icon name="box" /><h3>No units registered</h3><p className="small">Units you register appear here.</p></div>}
        {units && units.length > 0 && (
          <div className="table-wrap">
            <table className="data">
              <thead><tr><th scope="col">Group</th><th scope="col">Component</th><th scope="col">Status</th><th scope="col">Collected</th><th scope="col">Expires</th></tr></thead>
              <tbody>
                {units.map((u) => (
                  <tr key={u.id}>
                    <td><span className="bg-pill sm">{u.blood_group}</span></td>
                    <td>{u.component === "plasma" ? "Plasma" : "Red cells"}</td>
                    <td>
                      <span className={`badge ${status(u) === "AVAILABLE" ? "ok" : status(u) === "RESERVED" ? "warn" : ""}`}>
                        <Icon name={status(u) === "AVAILABLE" ? "check" : status(u) === "RESERVED" ? "clock" : "minus"} />{STATUS_TEXT[status(u)] ?? status(u)}
                      </span>
                    </td>
                    <td className="num">{u.collected_at.slice(0, 10)}</td>
                    <td className="num">{u.expires_at.slice(0, 10)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}
