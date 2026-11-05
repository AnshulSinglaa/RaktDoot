"use client";
// Hospital desk (desktop layout): post an emergency, see this hospital's requests and where each one stands.
import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, UNAVAILABLE, authed, BLOOD_GROUPS, newIdempotencyKey, type BloodGroup, type Component, type Emergency } from "@/lib/api";
import { useMe } from "@/lib/useMe";
import { StaffBar, useOrg } from "@/lib/staff";
import { Badge, Icon, Skeleton, URGENCY_LOOK, ago, requestLook, unitsLine, useNow } from "@/lib/ui";

export default function Hospital() {
  const router = useRouter();
  const me = useMe(["hospital", "admin"]);
  const [org] = useOrg(me);
  const now = useNow(30000);
  const [requests, setRequests] = useState<Emergency[] | null>(null);
  const [listError, setListError] = useState(false);
  // No default group: a rushed nurse must choose, never inherit "O+" by not looking.
  const [group, setGroup] = useState<BloodGroup | "">("");
  const [component, setComponent] = useState<Component>("red_cells");
  const [units, setUnits] = useState(1);
  const [urgency, setUrgency] = useState<Emergency["urgency"]>("HIGH");
  const [bed, setBed] = useState("");
  const [error, setError] = useState("");
  const [capped, setCapped] = useState<Emergency | null>(null);
  const [busy, setBusy] = useState(false);
  // ONE Idempotency-Key per form fill, kept across retries: if the network drops after the API saved the request,
  // pressing Post again replays the first answer instead of holding a second set of units (ADR 016).
  // A new key only after a create succeeded.
  const idemKey = useRef<string | null>(null);

  const load = useCallback(() => authed<Emergency[]>("/v1/emergencies")
    .then((r) => { setRequests(r); setListError(false); }).catch(() => setListError(true)), []);
  useEffect(() => { if (me) load(); }, [me, load]);

  async function post(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setCapped(null);
    setBusy(true);
    try {
      idemKey.current ??= newIdempotencyKey();
      const created = await authed<Emergency>("/v1/emergencies", {
        method: "POST",
        headers: { "Idempotency-Key": idemKey.current },
        body: JSON.stringify({ blood_group: group, component, units_needed: units, urgency, hospital_ref: bed.trim() || null }),
      });
      idemKey.current = null;                    // saved: the next form fill is a new request
      if (created.cap_reached) {                 // stay here: the explanation matters more than the redirect
        setCapped(created);
        setBusy(false);
        load();
        return;
      }
      router.push(`/hospital/requests/${created.id}`);
    } catch (err) {
      const p = err instanceof ApiError ? err : null;
      if (p?.is("idempotency-key-reuse")) {      // the first try WAS saved, then the form changed: do not post twice by accident
        idemKey.current = null;
        load();
      }
      setError(p?.is("hospital-has-no-location") ? "Your hospital has no location on file yet, so nearby banks cannot be found. Ask a Rakt Doot admin to add it."
        : p?.is("idempotency-key-reuse") ? "Your earlier try was already posted. Check Your requests before posting again."
        : p?.is("validation-error") && p.fields.includes("hospital_ref")
          ? "Bed / IP number: letters, digits, spaces, / # and - only. Never the patient's name."
        : p?.is("database-unavailable") ? UNAVAILABLE
        : p ? p.message : "Could not reach Rakt Doot. Check the connection and try again.");
      setBusy(false);
    }
  }

  if (!me) return <main className="desk"><div className="desk-main"><Skeleton /></div></main>;
  const live = (requests ?? []).filter((r) => r.status === "OPEN" || r.status === "ALLOCATED");
  const waitingOnBanks = live.reduce((n, r) => n + r.holds.filter((h) => h.status === "PENDING").length, 0);

  return (
    <main className="desk">
      <StaffBar me={me} org={org} desk="Emergency desk" />
      <div className="desk-main">
        <h1 className="sr-only">Emergency desk</h1>
        <div className="kpis">
          <div className="kpi"><b>{requests ? live.length : "–"}</b><span>Open requests</span></div>
          <div className="kpi"><b>{requests ? waitingOnBanks : "–"}</b><span>Bank holds awaiting confirmation</span></div>
          <div className="kpi"><b>{requests ? requests.filter((r) => r.status === "FULFILLED").length : "–"}</b><span>Fulfilled (recent)</span></div>
        </div>

        <div className="desk-cols">
          <form className="panel" onSubmit={post} aria-labelledby="new-title">
            <div className="panel-head"><h2 id="new-title">New emergency request</h2></div>
            <div className="panel-body form-grid">
              <p className="muted">Compatible units at the nearest blood banks are held the moment you post. Each bank then confirms its hold.</p>
              <div className="two">
                <label className="field"><span>Patient blood group</span>
                  <select className="input" required value={group} onChange={(e) => setGroup(e.target.value as BloodGroup)}>
                    <option value="" disabled>Choose the group</option>
                    {BLOOD_GROUPS.map((g) => <option key={g} value={g}>{g}</option>)}
                  </select>
                </label>
                <label className="field"><span>Units needed</span>
                  <input className="input" type="number" inputMode="numeric" min={1} max={20} required value={units}
                         onChange={(e) => setUnits(Number(e.target.value))} />
                </label>
              </div>
              <label className="field"><span>Component</span>
                <select className="input" value={component} onChange={(e) => setComponent(e.target.value as Component)}>
                  <option value="red_cells">Red cells</option>
                  <option value="plasma">Plasma</option>
                </select>
              </label>
              <label className="field"><span>Bed / IP number (optional)</span>
                <input className="input" maxLength={30} value={bed} onChange={(e) => setBed(e.target.value)}
                       autoComplete="off" aria-describedby="ref-hint" />
                <small id="ref-hint">Helps the bank match the paper requisition. Never the patient&apos;s name.</small>
              </label>
              <fieldset className="field" style={{ border: 0, padding: 0, margin: 0 }}>
                <legend style={{ fontSize: "var(--t-sm)", fontWeight: 600, color: "var(--ink-2)", marginBottom: 6 }}>Urgency</legend>
                <div className="row">
                  {(["CRITICAL", "HIGH", "NORMAL"] as const).map((u) => (
                    <label key={u} className={`badge ${urgency === u ? URGENCY_LOOK[u].cls || "info" : ""}`} style={{ cursor: "pointer", padding: "8px 14px" }}>
                      <input type="radio" name="urgency" value={u} checked={urgency === u} onChange={() => setUrgency(u)}
                             style={{ accentColor: "var(--brand)", margin: 0 }} />
                      {URGENCY_LOOK[u].text}
                    </label>
                  ))}
                </div>
              </fieldset>
              {error && <p className="notice bad" role="alert"><Icon name="alert" /><span>{error}</span></p>}
              <button className="btn block" disabled={busy}>{busy ? "Posting…" : "Post emergency request"}</button>
            </div>
          </form>

          <section className="panel" aria-labelledby="list-title">
            <div className="panel-head">
              <h2 id="list-title">Your requests</h2>
              <button className="btn ghost sm" onClick={load}><Icon name="refresh" />Refresh</button>
            </div>
            <div className="panel-body stack">
              {capped && (
                <div className="notice warn" role="alert"><Icon name="alert" /><span>
                  <b>Only part of this request could be held</b>
                  Your hospital already holds the most units allowed at one time across its open requests. This limit stops
                  any one hospital from tying up a region&apos;s stock. Close requests whose blood was collected, or cancel requests you no longer
                  need, then press &quot;Check blood banks again&quot; on the request.{" "}
                  <Link href={`/hospital/requests/${capped.id}`} style={{ fontWeight: 650, color: "var(--brand)" }}>Open the request</Link>
                </span></div>
              )}
              {listError && (
                <div className="notice bad" role="alert"><Icon name="alert" /><span>
                  <b>Could not load your requests</b>{requests ? "The list below may be out of date." : "Check the connection."}
                </span></div>
              )}
              {listError && <button className="btn ghost" onClick={load}><Icon name="refresh" />Try again</button>}
              {requests === null && !listError && <Skeleton lines={2} />}
              {requests?.length === 0 && (
                <div className="empty"><Icon name="box" /><h3>No requests yet</h3><p className="small">Requests you post appear here with their live status.</p></div>
              )}
              {requests?.map((r) => {
                const pending = r.holds.filter((h) => h.status === "PENDING").length;
                const confirmed = r.holds.filter((h) => h.status === "CONFIRMED" || h.status === "OVERDUE").length;
                return (
                  <Link key={r.id} href={`/hospital/requests/${r.id}`} className="card card-link">
                    <div className="spread">
                      <div className="row" style={{ gap: "var(--s3)" }}>
                        <span className="bg-pill">{r.blood_group}</span>
                        <div>
                          <h3>{r.units_needed} unit{r.units_needed === 1 ? "" : "s"} of {r.component === "plasma" ? "plasma" : "red cells"}</h3>
                          <p className="muted">Request {r.code}{r.hospital_ref ? ` · bed ${r.hospital_ref}` : ""} · posted {ago(r.created_at, now)} · {unitsLine(r)}</p>
                        </div>
                      </div>
                      <div className="row">
                        <Badge look={URGENCY_LOOK[r.urgency] ?? URGENCY_LOOK.NORMAL} />
                        <Badge look={requestLook(r)} />
                      </div>
                    </div>
                    {(pending > 0 || confirmed > 0) && (r.status === "OPEN" || r.status === "ALLOCATED") && (
                      <p className="muted" style={{ marginTop: "var(--s2)" }}>
                        {confirmed} bank{confirmed === 1 ? "" : "s"} confirmed · {pending} waiting to confirm
                      </p>
                    )}
                  </Link>
                );
              })}
            </div>
          </section>
        </div>
      </div>
    </main>
  );
}
