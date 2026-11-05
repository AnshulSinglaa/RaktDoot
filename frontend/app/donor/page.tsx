"use client";
// The donor's own profile: create it (with consent), see eligibility, toggle availability, answer requests.
// Every rule (who may see what, the donation interval) is decided by the API; this page only shows the answer.
import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, LOGOUT_FAILED, UNAVAILABLE, authed, BLOOD_GROUPS, homeFor, logout, type BloodGroup, type DonorProfile, type InboxItem, type Me } from "@/lib/api";
import { Badge, Icon, Skeleton, URGENCY_LOOK, ago, useNow } from "@/lib/ui";

export default function Donor() {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [profile, setProfile] = useState<DonorProfile | null | undefined>(undefined); // undefined = loading, null = none yet
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [down, setDown] = useState(false);      // API unreachable: say so, never send the donor to /login for it
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    authed<Me>("/v1/auth/me")
      .then(async (user) => {
        setMe(user);
        if (user.role !== "donor") return router.replace(homeFor(user.role));      // staff have their own screens
        try {
          setProfile(await authed<DonorProfile>("/v1/donors/me"));
        } catch (e) {
          if (e instanceof ApiError && e.status === 404) setProfile(null);
          else throw e;
        }
      })
      // Only 401 (no session, or it expired) means "log in again". A 503 or a network blip is an outage.
      .catch((e) => (e instanceof ApiError && e.status === 401 ? router.replace("/login") : setDown(true)));
  }, [router, attempt]);

  async function signOut() {
    setError("");
    try {
      await logout();
      router.push("/");
    } catch {
      setError(LOGOUT_FAILED);
    }
  }

  async function toggle() {
    if (!profile) return;
    setError("");
    setSaving(true);
    try {
      setProfile(await authed<DonorProfile>(`/v1/donors/${profile.id}`, {
        method: "PATCH", body: JSON.stringify({ is_available: !profile.is_available }),
      }));
    } catch {
      setError("Could not update your availability. Check your connection and try again.");
    } finally {
      setSaving(false);
    }
  }

  const header = (
    <header className="topbar">
      <Link className="iconbtn" href="/" aria-label="Home"><Icon name="back" /></Link>
      <span className="title">{profile === null ? "Become a donor" : "My donor profile"}</span>
      <button className="iconbtn" onClick={signOut} aria-label="Log out" title="Log out"><Icon name="logout" /></button>
    </header>
  );

  if (down) return (
    <main className="phone">{header}<div className="body">
      <div className="notice warn" role="alert"><Icon name="info" /><span>{UNAVAILABLE}</span></div>
      <button className="btn block" onClick={() => { setDown(false); setAttempt((n) => n + 1); }}>Try again</button>
    </div></main>
  );
  if (!me || profile === undefined) return (
    <main className="phone">{header}<div className="body">
      {error && <p className="notice bad" role="alert"><Icon name="alert" /><span>{error}</span></p>}
      <Skeleton />
    </div></main>
  );
  if (profile === null) return (
    <main className="phone">{header}
      {error === LOGOUT_FAILED && <div className="body"><p className="notice bad" role="alert"><Icon name="alert" /><span>{error}</span></p></div>}
      <CreateProfile email={me.email} onCreated={setProfile} />
    </main>
  );

  const status = profile.eligible_now
    ? { cls: "ok", icon: "checkCircle" as const, text: "You can donate now" }
    : profile.is_available
      ? { cls: "warn", icon: "clock" as const, text: "Resting after your last donation" }
      : { cls: "", icon: "minus" as const, text: "Paused: you will not be asked" };

  return (
    <main className="phone">
      {header}
      <div className="body">
        <article className="card stack">
          <div className="row" style={{ gap: "var(--s4)", flexWrap: "nowrap" }}>
            <span className="bg-pill" aria-label={`Blood group ${profile.blood_group}`}>{profile.blood_group}</span>
            <div style={{ minWidth: 0 }}>
              <h2>{profile.name}</h2>
              <p className="muted" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{me.email}</p>
            </div>
          </div>
          <span className={`badge ${status.cls}`} style={{ justifySelf: "start" }}><Icon name={status.icon} />{status.text}</span>
        </article>

        {/* plain text, not a colour: the donor must understand why no request reaches them */}
        {!profile.phone_verified && (
          <div className="notice warn" role="status"><Icon name="phone" /><span>
            <b>Your phone number is not verified yet</b>
            Until it is, hospitals cannot ask you to donate. A blood bank or donation camp verifies your mobile number
            in person on your next visit.
          </span></div>
        )}

        <div className="card">
          <label className="switch">
            <span className="grow">
              <b style={{ display: "block" }}>Available for emergencies</b>
              <span className="muted">{profile.is_available ? "On: nearby hospitals can ask you." : "Off: nobody will ask you until you turn this on."}</span>
            </span>
            <input type="checkbox" checked={profile.is_available} onChange={toggle} disabled={saving} />
            <span className="track" aria-hidden="true" />
          </label>
        </div>
        {error && <p className="notice bad" role="alert"><Icon name="alert" /><span>{error}</span></p>}
        <Inbox />
      </div>
    </main>
  );
}

function CreateProfile({ email, onCreated }: { email: string; onCreated: (p: DonorProfile) => void }) {
  const [name, setName] = useState("");
  const [age, setAge] = useState("");
  const [group, setGroup] = useState<BloodGroup | "">("");
  const [sex, setSex] = useState("");
  const [phone, setPhone] = useState("");
  const [city, setCity] = useState("");
  const [lastDonated, setLastDonated] = useState("");
  const [consent, setConsent] = useState(false);
  const [where, setWhere] = useState<{ latitude: number; longitude: number } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      onCreated(await authed<DonorProfile>("/v1/donors", {
        method: "POST",
        body: JSON.stringify({
          name, email, age: Number(age), blood_group: group, sex, consent,
          phone: phone.trim() || null,      // the API normalises it to +91...; it stays unverified until staff see you
          city: city.trim() || null,
          ...(where ?? {}),         // rounded to ~1 km by the API before it is stored
          // the API wants a timezone-aware timestamp: a date input gives "2026-08-01", so pin it to UTC midnight
          last_donated_at: lastDonated ? `${lastDonated}T00:00:00Z` : null,
        }),
      }));
    } catch (err) {
      // the API's own words (it lists each field it rejected), never a copy of its rules that could drift
      setError(err instanceof ApiError ? err.message : "Could not reach Rakt Doot. Check your internet connection.");
      setBusy(false);
    }
  }

  return (
    <form className="body" onSubmit={submit}>
      <div className="stack">
        <h1>Become a donor</h1>
        <p className="lede">Hospitals see only your first name, blood group and a rough distance, and only for an emergency near you.</p>
      </div>
      <label className="field"><span>Full name</span>
        <input className="input" required maxLength={100} value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
      </label>
      <div className="two">
        <label className="field"><span>Age</span>
          <input className="input" type="number" inputMode="numeric" required min={18} max={65} value={age} onChange={(e) => setAge(e.target.value)} />
        </label>
        {/* sex sets the gap between donations: 90 days for men, 120 for women and others (the API decides) */}
        <label className="field"><span>Sex</span>
          <select className="input" required value={sex} onChange={(e) => setSex(e.target.value)}>
            <option value="" disabled>Choose</option>
            <option value="male">Male</option>
            <option value="female">Female</option>
            <option value="other">Other</option>
          </select>
        </label>
      </div>
      <label className="field"><span>Blood group</span>
        <select className="input" required value={group} onChange={(e) => setGroup(e.target.value as BloodGroup)}>
          <option value="" disabled>Choose your blood group</option>
          {BLOOD_GROUPS.map((g) => <option key={g} value={g}>{g}</option>)}
        </select>
      </label>
      <label className="field"><span>Mobile number (optional)</span>
        <input className="input" type="tel" inputMode="tel" maxLength={20} value={phone} onChange={(e) => setPhone(e.target.value)}
               autoComplete="tel" aria-describedby="phone-hint" placeholder="98765 43210" />
        <small id="phone-hint">A blood bank or camp verifies it in person before you get any alert.</small>
      </label>
      <label className="field"><span>City (optional)</span>
        <input className="input" maxLength={100} value={city} onChange={(e) => setCity(e.target.value)} autoComplete="address-level2" />
      </label>
      <div className="field">
        <span>Location, so nearby hospitals can find you</span>
        <button type="button" className="btn ghost block" onClick={() => navigator.geolocation?.getCurrentPosition(
          (p) => setWhere({ latitude: p.coords.latitude, longitude: p.coords.longitude }),
          () => setError("Location was not shared. You can still save; you will not be matched until you add it."))}>
          <Icon name={where ? "check" : "navigation"} />
          {where ? "Approximate location added (about 1 km)" : "Use my approximate location"}
        </button>
        <small>Stored rounded to about 1 km. Your exact address is never stored.</small>
      </div>
      <label className="field"><span>Last donation date (optional)</span>
        <input className="input" type="date" value={lastDonated} onChange={(e) => setLastDonated(e.target.value)} />
      </label>
      {/* DPDP Act 2023: health data needs explicit, informed consent. The API rejects anything but true. */}
      <label className="check card flat">
        <input type="checkbox" required checked={consent} onChange={(e) => setConsent(e.target.checked)} />
        <span>I consent to Rakt Doot storing my blood group and contact details to match me with emergency requests.</span>
      </label>
      {error && <p className="notice bad" role="alert"><Icon name="alert" /><span>{error}</span></p>}
      <button className="btn block" disabled={busy}>{busy ? "Saving…" : "Save my profile"}</button>
    </form>
  );
}

/** Emergencies this donor was asked about (delivered by the outbox worker), with yes/no buttons. */
function Inbox() {
  const now = useNow(60000);
  const [items, setItems] = useState<InboxItem[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [note, setNote] = useState("");
  const [sending, setSending] = useState(false);
  const load = () => authed<InboxItem[]>("/v1/emergencies/donor-inbox")
    .then((r) => { setItems(r); setLoadError(false); }).catch(() => setLoadError(true));
  useEffect(() => { load(); }, []);

  async function answer(i: InboxItem, accept: boolean) {
    setNote("");
    setSending(true);
    try {
      await authed(`/v1/emergencies/${i.request_id}/responses`, { method: "POST", body: JSON.stringify({ accept }) });
      setNote(accept ? `Thank you. ${i.hospital_name} can see your yes and will call you. Please wait for their call before you travel.`
        : "Thanks for answering. You will not be asked again for this request.");
    } catch (e) {
      setNote(e instanceof ApiError ? e.message : "Your answer was not sent. Check your connection and try again.");
    } finally {
      setSending(false);
    }
    load();
  }

  return (
    <section className="stack" aria-labelledby="inbox-title" style={{ marginTop: "var(--s2)" }}>
      <h2 id="inbox-title">Requests for your help</h2>
      <p role="status" className={note ? "notice ok" : "sr-only"}>{note && <><Icon name="info" /><span>{note}</span></>}</p>
      {loadError && (
        <div className="notice bad" role="alert"><Icon name="alert" /><span>
          <b>Could not load your requests</b>{items ? "The list below may be out of date." : "Check your connection."}
        </span></div>
      )}
      {loadError && <button className="btn ghost block" onClick={load}><Icon name="refresh" />Try again</button>}
      {items === null && !loadError && <Skeleton lines={1} />}
      {items?.length === 0 && (
        <div className="empty card flat">
          <Icon name="bell" />
          <h3>No requests right now</h3>
          <p className="small">When a hospital near you needs your blood group, it appears here.</p>
        </div>
      )}
      {items?.map((i) => {
        const live = i.request_status === "OPEN" || i.request_status === "ALLOCATED";
        return (
          <article className="card stack" key={i.request_id}>
            <div className="spread" style={{ alignItems: "flex-start" }}>
              <div>
                <h3>{i.hospital_name}</h3>
                <p className="muted">{i.hospital_city} · asked {ago(i.notified_at, now)}</p>
              </div>
              <span className="bg-pill sm">{i.blood_group}</span>
            </div>
            <div className="row">
              <Badge look={URGENCY_LOOK[i.urgency] ?? URGENCY_LOOK.NORMAL} />
              <span className="badge">{i.component === "plasma" ? "Plasma" : "Blood"}</span>
              <span className={`badge ${i.my_status === "ACCEPTED" ? "ok" : ""}`}>
                <Icon name={i.my_status === "NOTIFIED" ? "clock" : i.my_status === "ACCEPTED" ? "check" : "minus"} />
                {i.my_status === "NOTIFIED" ? "Waiting for your answer" : i.my_status === "ACCEPTED" ? "You said yes" : "You said no"}
              </span>
              {!live && <span className="badge"><Icon name="minus" />Request closed</span>}
            </div>
            {live && (
              <div className="two">
                <button className="btn" disabled={sending} onClick={() => answer(i, true)}>
                  I can donate<span className="sr-only"> for {i.hospital_name}</span>
                </button>
                <button className="btn ghost" disabled={sending} onClick={() => answer(i, false)}>
                  Not this time<span className="sr-only"> for {i.hospital_name}</span>
                </button>
              </div>
            )}
          </article>
        );
      })}
    </section>
  );
}
