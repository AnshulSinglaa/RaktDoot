"use client";
// The patient's family, no login: blood group -> where the patient is -> nearest banks with compatible stock.
// The API rounds the location to ~11 km before searching and only ever COUNTS donors (threat model #2, ADR 020).
// The home page hands off with ?group=O%2B and either &city=Bhopal or &near=me (coordinates never go in the URL).
import { Suspense, useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { useRouter, useSearchParams } from "next/navigation";
import { api, qs, BLOOD_GROUPS, RARE, isProblem, type BloodGroup, type DonorsNearby, type NearbyBank, type OwnershipFilter } from "@/lib/api";
import { MP_CITIES, placeFor, type Place } from "@/lib/places";
import { Badge, Icon, PhoneLine, Skeleton, ago, ownershipLook, useNow } from "@/lib/ui";

// Leaflet touches `window` on import: load it only in the browser, and only on this page (ADR 024).
const BankMap = dynamic(() => import("./BankMap"), {
  ssr: false, loading: () => <div className="bank-map map-loading" aria-hidden="true" />,
});

const STEPS = 3;
const STALE_HOURS = 24;                     // older than a day: still shown, but the family is told to call first
const OWNERSHIP: { value: OwnershipFilter; label: string }[] = [
  { value: "all", label: "All" }, { value: "government", label: "Government" }, { value: "private", label: "Private" },
];
const RADII = [10, 25, 50, 100, 1000];      // km; 1000 is the "100+" chip (MP is ~900 km across; the API caps at 20 banks)
const DEFAULT_RADIUS = 50;
const radiusLabel = (km: number) => (km === 1000 ? "100+ km" : `${km} km`);
const isFresh = (b: NearbyBank, now: number) =>
  !!b.stock_confirmed_at && now - new Date(b.stock_confirmed_at).getTime() < STALE_HOURS * 3600000;
// OpenStreetMap cities are often in capitals ("BHOPAL"): show them in title case, leave mixed case alone.
const nice = (s: string) =>
  s === s.toUpperCase() ? s.toLowerCase().replace(/(^|[\s(/-])(\p{L})/gu, (_, a: string, c: string) => a + c.toUpperCase()) : s;

// useSearchParams needs a Suspense boundary so the rest of the route can still be prerendered (Next 16 docs).
export default function Page() {
  return <Suspense fallback={<main className="phone" />}><FindBlood /></Suspense>;
}

function FindBlood() {
  const router = useRouter();
  const params = useSearchParams();
  const preset = BLOOD_GROUPS.find((g) => g === params.get("group")) ?? null;   // anything else is ignored
  const [step, setStep] = useState(preset ? 2 : 1);
  const [group, setGroup] = useState<BloodGroup | null>(preset);
  const [city, setCity] = useState(params.get("city") ?? "");
  const [place, setPlace] = useState<Place | null>(null);
  const [locating, setLocating] = useState(false);
  const [banks, setBanks] = useState<NearbyBank[] | null>(null);
  const [donors, setDonors] = useState<DonorsNearby | null>(null);
  const [error, setError] = useState("");

  const [ownership, setOwnership] = useState<OwnershipFilter>("all");
  const [radius, setRadius] = useState(DEFAULT_RADIUS);

  // Only the latest search may write the results: starting one aborts the one before it (and any pending chip
  // re-query), so a slow Government response can never land under the Private chip.
  const bankReq = useRef<AbortController | null>(null);
  const donorReq = useRef<AbortController | null>(null);
  const chipTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  function newBankSearch() {
    clearTimeout(chipTimer.current);
    bankReq.current?.abort();
    bankReq.current = new AbortController();
    setError("");
    setBanks(null);
    return bankReq.current;
  }

  // The chips re-query only the banks; the donor count is a 50 km bonus and does not depend on them.
  async function loadBanks(where: Place, own: OwnershipFilter, km: number, req = newBankSearch()) {
    const at = { recipient: group!, latitude: String(where.latitude), longitude: String(where.longitude) };
    try {
      const rows = await api<NearbyBank[]>("/v1/public/nearest-banks" + qs({ ...at, ownership: own, radius_km: String(km) }),
        { signal: req.signal });
      if (req === bankReq.current) setBanks(rows);
    } catch (e) {
      if (req !== bankReq.current || req.signal.aborted) return;   // superseded: not an error the family should see
      setError(isProblem(e, "too-many-requests") ? "Too many searches from this connection. Wait a minute and try again."
        : isProblem(e, "validation-error") ? "That search could not be run. Reset the filters with Show all and try again."
        : "Could not reach Rakt Doot. Check your internet connection and try again.");
    }
  }

  function search(where: Place) {
    // Round here too (the API rounds to 1 decimal, ~11 km): the map then shows the same coarse point the API used.
    const p = { ...where, latitude: Math.round(where.latitude * 10) / 10, longitude: Math.round(where.longitude * 10) / 10 };
    setPlace(p);
    setDonors(null);
    setStep(3);
    loadBanks(p, ownership, radius);
    const at = { recipient: group!, latitude: String(p.latitude), longitude: String(p.longitude) };
    donorReq.current?.abort();
    const req = (donorReq.current = new AbortController());
    api<DonorsNearby>("/v1/public/donors-nearby" + qs(at), { signal: req.signal })
      .then((d) => req === donorReq.current && setDonors(d)).catch(() => null);   // a bonus: never block the banks
  }

  // Chip taps re-query after 300 ms of quiet, so rapid taps cost one request. The chip shows pressed at once and the
  // in-flight search is aborted at once; the first search of a place is not delayed.
  function filter(own: OwnershipFilter, km: number) {
    setOwnership(own);
    setRadius(km);
    if (!place) return;
    const req = newBankSearch();
    chipTimer.current = setTimeout(() => loadBanks(place, own, km, req), 300);
  }

  function searchCity(name: string) {
    const p = placeFor(name);
    if (p) search(p);
    else setError("We could not find that city. Pick one from the list (Madhya Pradesh only for now), or use your location.");
  }

  function submitCity(e: React.FormEvent) {
    e.preventDefault();
    searchCity(city);
  }

  function locate() {
    setError("");
    if (!navigator.geolocation) return setError("This browser cannot share a location. Type the city instead.");
    setLocating(true);
    navigator.geolocation.getCurrentPosition(
      (p) => { setLocating(false); search({ label: "your location", latitude: p.coords.latitude, longitude: p.coords.longitude }); },
      () => { setLocating(false); setError("Location was not shared. Type the city instead."); },
      { timeout: 15000, maximumAge: 600000 },
    );
  }

  // Prefilled from the home page: run the search on arrival. Leaving the page cancels the pending chip timer and any
  // search in flight, so no late answer lands on an unmounted screen. (In dev, StrictMode mounts twice: the first
  // search is cancelled by this cleanup and the second one runs, so the result is the same.)
  useEffect(() => {
    if (preset) {
      if (params.get("near") === "me") locate();
      else if (city.trim()) searchCity(city);
    }
    return () => { clearTimeout(chipTimer.current); bankReq.current?.abort(); donorReq.current?.abort(); };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const back = () => (step === 1 ? router.push("/") : (setError(""), setStep(step - 1)));

  return (
    <main className={step === 3 ? "phone wide" : "phone"}>
      <header className="topbar">
        <button className="iconbtn" onClick={back} aria-label={step === 1 ? "Back to home" : "Previous step"}><Icon name="back" /></button>
        <span className="title">{step === 3 ? "Blood banks near the patient" : "Find blood"}</span>
        <button className="iconbtn" onClick={() => router.push("/")} aria-label="Close and go home"><Icon name="close" /></button>
      </header>
      <div className="steps">
        <span className="muted">Step {step} of {STEPS}</span>
        <div className="progress" role="progressbar" aria-label="Progress" aria-valuemin={1} aria-valuemax={STEPS} aria-valuenow={step}>
          <span style={{ width: `${(step / STEPS) * 100}%` }} />
        </div>
      </div>

      {step === 1 && (
        <>
          <div className="body">
            <div className="stack">
              <h1>What is the patient&apos;s blood group?</h1>
              <p className="lede">We also look for groups that are compatible with it.</p>
            </div>
            <div className="groups" role="group" aria-label="Blood group">
              {BLOOD_GROUPS.map((g) => (
                <button key={g} className="group" aria-pressed={group === g} onClick={() => setGroup(g)}>
                  <b>{g}</b>
                  <small>{RARE.includes(g) ? "Rare" : "Common"}</small>
                </button>
              ))}
            </div>
            <p className="muted">Not sure? The patient&apos;s doctor or the hospital lab can tell you.</p>
          </div>
          <div className="footer">
            <button className="btn block" disabled={!group} onClick={() => setStep(2)}>
              {group ? `Continue with ${group}` : "Choose a blood group"}
            </button>
          </div>
        </>
      )}

      {step === 2 && (
        <form className="grow" style={{ display: "flex", flexDirection: "column" }} onSubmit={submitCity}>
          <div className="body">
            <div className="stack">
              <h1>Where is the patient?</h1>
              <p className="lede">Used only for this search. Nothing is saved.</p>
            </div>
            <button type="button" className="btn ghost block" onClick={locate} disabled={locating}>
              <Icon name="navigation" />{locating ? "Finding your location…" : "Use my current location"}
            </button>
            <div className="or">or type the city</div>
            <label className="field">
              <span>City</span>
              <input className="input" list="cities" value={city} onChange={(e) => { setCity(e.target.value); setError(""); }}
                     placeholder="e.g. Bhopal" autoComplete="address-level2" />
              <datalist id="cities">{Object.keys(MP_CITIES).sort().map((c) => <option key={c} value={c} />)}</datalist>
              <small>Madhya Pradesh districts for now.</small>
            </label>
            {error && <p className="notice bad" role="alert"><Icon name="alert" /><span>{error}</span></p>}
          </div>
          <div className="footer">
            <button className="btn block" disabled={!city.trim()}>Show blood banks</button>
          </div>
        </form>
      )}

      {step === 3 && (
        <>
          <div className="body">
            <Results group={group!} place={place} banks={banks} donors={donors} error={error}
                     ownership={ownership} radius={radius} filter={filter}
                     retry={() => place && loadBanks(place, ownership, radius)} />
          </div>
          <div className="footer">
            <button className="btn ghost block" onClick={() => { setStep(2); setError(""); }}>
              <Icon name="pin" />Search a different place
            </button>
          </div>
        </>
      )}
    </main>
  );
}

function Results({ group, place, banks, donors, error, ownership, radius, filter, retry }: {
  group: BloodGroup; place: Place | null; banks: NearbyBank[] | null; donors: DonorsNearby | null; error: string;
  ownership: OwnershipFilter; radius: number; filter: (own: OwnershipFilter, km: number) => void; retry: () => void;
}) {
  const now = useNow(60000);
  const [freshOnly, setFreshOnly] = useState(false);        // client-side: the API already sent each bank's timestamp
  const [view, setView] = useState<"list" | "map">("list");  // phones only; from 900 px both are shown
  const [selected, setSelected] = useState<string | null>(null);
  const [tilesFailed, setTilesFailed] = useState(false);
  const cards = useRef(new Map<string, HTMLElement>());
  // On phones "Show on map" hides the list, so the focused button vanishes: move focus to the card under the map,
  // and back to the same bank's card when the user returns (WCAG 2.4.3). A no-op on desktop, where nothing hides.
  const refocus = useRef<"map" | "list" | null>(null);
  useEffect(() => {
    const want = refocus.current;
    refocus.current = null;
    if (want === "map") document.querySelector<HTMLElement>(".map-chosen button")?.focus();
    if (want === "list" && selected) cards.current.get(selected)?.querySelector<HTMLElement>(".show-on-map")?.focus();
  }, [view, selected]);

  const shown = banks && (freshOnly ? banks.filter((b) => isFresh(b, now)) : banks);
  const chosen = shown?.find((b) => b.blood_bank_id === selected) ?? null;
  const widest = radius === 1000 && ownership === "all" && !freshOnly;

  function pickFromMap(id: string) {
    setSelected(id);
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    cards.current.get(id)?.scrollIntoView({ block: "nearest", behavior: reduce ? "auto" : "smooth" });
  }
  function showOnMap(id: string) {
    setSelected(id);
    if (getComputedStyle(document.querySelector(".view-toggle")!).display !== "none") {
      refocus.current = "map";
      setView("map");                                       // phones: the map replaces the list
    }
  }

  // What narrowed an empty result, in words, so the family knows which chip to change.
  const narrowed = [
    ownership !== "all" && (ownership === "government" ? "Government only" : "Private only"),
    radius === 1000 ? "any distance" : `within ${radius} km`,
    freshOnly && `stock confirmed in the last ${STALE_HOURS} hours`,
  ].filter(Boolean).join(", ");
  const showAll = () => { setFreshOnly(false); filter("all", 1000); };
  const where = radius === 1000 ? `Nearest to ${place?.label}, any distance` : `Within ${radius} km of ${place?.label}`;

  return (
    <>
      <div className="stack">
        <div aria-live="polite" className="stack">
          <h1 style={{ fontSize: "var(--t-xl)" }}>
            {error ? "The search did not finish"
              : !shown ? "Searching…"
              : shown.length === 0 ? "No blood bank matches this search"
              : `${shown.length} blood bank${shown.length === 1 ? "" : "s"} with ${group} or compatible blood`}
          </h1>
          <p className="muted">{where}, nearest first{banks?.length === 20 ? ". The 20 nearest are shown" : ""}.</p>
        </div>

        <div className="filters">
          <div className="chips" role="group" aria-labelledby="f-own">
            <span id="f-own" className="chips-label">Run by</span>
            {OWNERSHIP.map((o) => (
              <button key={o.value} className="chip" aria-pressed={ownership === o.value}
                      onClick={() => ownership !== o.value && filter(o.value, radius)}>
                {ownership === o.value && <Icon name="check" />}{o.label}
              </button>
            ))}
          </div>
          <div className="chips" role="group" aria-labelledby="f-km">
            <span id="f-km" className="chips-label">Distance</span>
            {RADII.map((km) => (
              <button key={km} className="chip" aria-pressed={radius === km}
                      onClick={() => radius !== km && filter(ownership, km)}>
                {radius === km && <Icon name="check" />}{radiusLabel(km)}
              </button>
            ))}
          </div>
          <div className="chips">
            <button className="chip" aria-pressed={freshOnly} onClick={() => setFreshOnly(!freshOnly)}>
              <Icon name={freshOnly ? "check" : "clock"} />Stock confirmed in the last {STALE_HOURS} hours
            </button>
          </div>
        </div>
      </div>

      {error ? (
        <div className="stack">
          <p className="notice bad" role="alert"><Icon name="alert" /><span>{error}</span></p>
          <button className="btn ghost" onClick={retry}><Icon name="refresh" />Try again</button>
        </div>
      ) : !shown || !place ? (
        <><p className="sr-only" role="status">Searching…</p><Skeleton lines={3} /></>
      ) : (
        <>
          {shown.length > 0 && (
            <div className="view-toggle" role="group" aria-label="Show results as">
              <button className="chip" aria-pressed={view === "list"} onClick={() => setView("list")}>
                {view === "list" && <Icon name="check" />}List
              </button>
              <button className="chip" aria-pressed={view === "map"} onClick={() => setView("map")}>
                {view === "map" && <Icon name="check" />}Map
              </button>
            </div>
          )}

          <div className="results" data-view={shown.length ? view : "list"}>
            <div className="results-list stack">
              {shown.length === 0 && (
                <div className="notice warn"><Icon name="info" /><span>
                  <b>Nothing found: {narrowed}</b>
                  {widest
                    ? "No bank in reach lists compatible stock. Ask the hospital treating the patient to post an emergency request on Rakt Doot: it holds units at nearby banks and can alert matching donors."
                    : "Widen the search with Show all, or ask the hospital treating the patient to post an emergency request on Rakt Doot."}
                </span></div>
              )}
              {shown.length === 0 && !widest && <button className="btn" onClick={showAll}>Show all</button>}

              {shown.length > 0 && (
                <ol className="bank-list" aria-label="Blood banks, nearest first">
                  {shown.map((b, i) => (
                    <li key={b.blood_bank_id}
                        ref={(el) => { if (el) cards.current.set(b.blood_bank_id, el); else cards.current.delete(b.blood_bank_id); }}>
                      <BankCard b={b} group={group} n={i + 1} now={now} on={selected === b.blood_bank_id}
                                onSelect={() => showOnMap(b.blood_bank_id)} />
                    </li>
                  ))}
                </ol>
              )}

              {donors && (
                <section className="card flat stack" aria-labelledby="donors-title">
                  <div className="row"><Icon name="users" /><h2 id="donors-title">Donors near the patient</h2></div>
                  <p>
                    {donors.total === null ? "A few eligible donors live within 50 km."
                      : donors.total === 0 ? "No eligible donor is registered within 50 km yet."
                      : <><b className="num">{donors.total}</b> eligible donors live within 50 km.</>}
                  </p>
                  <p className="muted">{donors.how_to_reach_them}</p>
                </section>
              )}
            </div>

            {shown.length > 0 && (
              <div className="results-map stack">
                <BankMap banks={shown} area={place} selected={selected} onSelect={pickFromMap}
                         onTiles={(ok) => setTilesFailed(!ok)} />
                {tilesFailed && (
                  <p className="notice warn"><Icon name="info" /><span>The map could not load. Every bank is in the list.</span></p>
                )}
                <p className="muted">Numbers on the map match the list. The dashed circle is the approximate search area, not an exact address.</p>
                {/* phones, map view: the tapped bank's full card under the map, so the call button is one tap away */}
                {chosen && view === "map" && (
                  <div className="map-chosen">
                    <BankCard b={chosen} group={group} n={shown.indexOf(chosen) + 1} now={now} on onSelect={() => { refocus.current = "list"; setView("list"); }} inMap />
                  </div>
                )}
              </div>
            )}
          </div>

          <p className="muted">
            Facilities: &copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">
              OpenStreetMap contributors<span className="sr-only"> (opens in a new tab)</span></a> (ODbL).
            {shown.some((b) => b.stock_is_demo) && " Stock shown is demo data."}
          </p>
        </>
      )}
    </>
  );
}

function BankCard({ b, group, n, now, on, onSelect, inMap = false }: {
  b: NearbyBank; group: BloodGroup; n: number; now: number; on: boolean; onSelect: () => void; inMap?: boolean;
}) {
  const fresh = isFresh(b, now);
  const id = `bank-${b.blood_bank_id}${inMap ? "-map" : ""}`;
  return (
    <article className={on ? "card stack bank-card on" : "card stack bank-card"} aria-labelledby={id}>
      <div className="bank-head">
        <span className="bank-n" aria-hidden="true">{n}</span>
        <div className="grow">
          <h2 id={id}>{b.name}</h2>
          <p className="muted">{nice(b.city)} · {b.distance_km} km away</p>
        </div>
      </div>
      <div className="row">
        <Badge look={ownershipLook(b.ownership, b.ownership_inferred)} />
        {on && !inMap && <span className="badge brand"><Icon name="pin" />Selected on the map</span>}
      </div>
      <div className="facts">
        <div>
          <Icon name="drop" />
          <span><b className="num">{b.units_same_group} {group} unit{b.units_same_group === 1 ? "" : "s"}</b>
            {" "}({b.units} compatible in all: {b.blood_groups.join(", ")})
            {b.stock_is_demo && <> <span className="badge warn demo-tag"><Icon name="alert" />Demo stock, not real</span></>}
          </span>
        </div>
        {b.stock_confirmed_at ? (
          <div className={fresh ? "" : "warn-text"}>
            <Icon name={fresh ? "checkCircle" : "alert"} />
            <span>Stock confirmed {ago(b.stock_confirmed_at, now)}{fresh ? "" : ": call before you travel"}</span>
          </div>
        ) : (
          <div className="warn-text"><Icon name="alert" /><span>Stock never confirmed: call before you travel</span></div>
        )}
      </div>
      {b.phone
        ? <PhoneLine phone={b.phone} label={b.stock_is_demo ? "Call to ask what they really have" : "Call bank"} who={b.name} />
        : <p className="muted">No phone number listed.</p>}
      <div className="row">
        <a className="btn ghost sm" target="_blank" rel="noopener noreferrer"
           href={"https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(`${b.latitude},${b.longitude}`)}>
          <Icon name="navigation" />Directions<span className="sr-only"> to {b.name} (opens Google Maps)</span>
        </a>
        <button className={inMap ? "btn ghost sm" : "btn ghost sm show-on-map"} onClick={onSelect}>
          <Icon name={inMap ? "back" : "pin"} />{inMap ? "Back to the list" : "Show on map"}
          {!inMap && <span className="sr-only"> {b.name}</span>}
        </button>
      </div>
    </article>
  );
}
