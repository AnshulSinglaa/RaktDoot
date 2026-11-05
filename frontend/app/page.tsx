"use client";
// Home: the patient's family first (the stressed user), then one tap to every other role.
// No business rules here: the group list and city list are display data; /find and the API do the searching.
import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { LOGOUT_FAILED, authed, homeFor, logout, qs, BLOOD_GROUPS, type BloodGroup, type Me } from "@/lib/api";
import { MP_CITIES, placeFor } from "@/lib/places";
import { Icon, Logo, type IconName } from "@/lib/ui";

const DESK: Record<string, string> = {
  donor: "your donor profile", hospital: "your hospital desk", blood_bank: "your blood bank desk", admin: "your desk",
};

// Staff cards say plainly that staff accounts come from the organisation: signup only ever creates a donor (API).
const STAFF_NOTE = "Log in with the account your organisation was given. New hospital or blood bank? Ask Rakt Doot to verify you.";
const ROLES: { as: string; dest: string; owner: string[]; icon: IconName; title: string; line: string; note?: string }[] = [
  { as: "donor", dest: "/donor", owner: ["donor"], icon: "heart", title: "I want to donate",
    line: "Get alerts when a hospital near you needs your group, and switch availability on or off." },
  { as: "hospital", dest: "/hospital", owner: ["hospital", "admin"], icon: "building", title: "Hospital staff",
    line: "Post an emergency request, hold units at nearby banks and follow it live.", note: STAFF_NOTE },
  { as: "bank", dest: "/bank", owner: ["blood_bank"], icon: "box", title: "Blood bank staff",
    line: "Confirm held units, keep your stock list current and register new units.", note: STAFF_NOTE },
];

export default function Home() {
  const router = useRouter();
  const [me, setMe] = useState<Me | null | undefined>(undefined);      // undefined = still checking
  const [group, setGroup] = useState<BloodGroup | null>(null);
  const [city, setCity] = useState("");
  const [error, setError] = useState("");
  const [logoutFailed, setLogoutFailed] = useState(false);

  // Not useMe(): that hook sends logged-out visitors to /login, and this page is for everyone.
  useEffect(() => { authed<Me>("/v1/auth/me").then(setMe).catch(() => setMe(null)); }, []);

  function go(where: Record<string, string>) {
    if (!group) return setError("Choose the patient's blood group first.");
    router.push("/find" + qs({ group, ...where }));    // qs() encodes "+" so "O+" survives the URL
  }

  function searchCity(e: React.FormEvent) {
    e.preventDefault();
    const p = placeFor(city);
    if (!p) return setError("We could not find that city. Pick one from the list (Madhya Pradesh only for now).");
    go({ city: p.label });
  }

  return (
    <main className="home">
      <header className="topbar home-bar">
        <Link href="/" className="grow" style={{ textDecoration: "none" }}><Logo /></Link>
        {me === null && <Link className="btn ghost sm" href="/login"><Icon name="user" />Log in</Link>}
      </header>

      {me && (
        <div className="signed-in" role="region" aria-label="Your account">
          <div className="wrap spread">
            <span className="row"><Icon name="user" /><span>Logged in as <b>{me.email}</b></span></span>
            <span className="row">
              <Link className="btn sm" href={homeFor(me.role)}>Continue to {DESK[me.role] ?? "your desk"}<span aria-hidden="true">→</span></Link>
              <button className="linkbtn small"
                      onClick={async () => { setLogoutFailed(false); try { await logout(); setMe(null); } catch { setLogoutFailed(true); } }}>
                Not you? Log out
              </button>
              {logoutFailed && <span className="small error-text" role="alert">{LOGOUT_FAILED}</span>}
            </span>
          </div>
        </div>
      )}

      <section className="home-hero" aria-labelledby="hero-title">
        <div className="wrap hero-grid">
          <div className="hero-copy">
            <p className="tagline"><span lang="hi">रक्त दूत</span> · blood messenger</p>
            <h1 id="hero-title">Need blood for a patient?</h1>
            <p className="hero-lede">See which blood banks near the patient have compatible blood, and call them before you travel.</p>
            <p className="hero-facts">
              <span><Icon name="checkCircle" />Free</span>
              <span><Icon name="checkCircle" />No login</span>
              <span><Icon name="checkCircle" />Location not saved</span>
            </p>
            <a className="skip-roles" href="#roles">Donor, hospital or blood bank? Go to your page<Icon name="back" className="icon down" /></a>
          </div>

          <div className="quick" id="find">
            <h2>Find blood near the patient</h2>
            <fieldset className="quick-step">
              <legend><span className="n">1</span>Patient&apos;s blood group</legend>
              <div className="groups compact">
                {BLOOD_GROUPS.map((g) => (
                  <button key={g} type="button" className="group" aria-pressed={group === g}
                          onClick={() => { setGroup(g); setError(""); }}>
                    <b>{g}</b>
                  </button>
                ))}
              </div>
              <Link className="small muted-link" href="/find">Not sure? Go step by step</Link>
            </fieldset>

            <div className="quick-step">
              <p className="legend"><span className="n">2</span>Where is the patient?</p>
              <button type="button" className="btn block" onClick={() => go({ near: "me" })}>
                <Icon name="navigation" />Use my current location
              </button>
              <form className="city-row" onSubmit={searchCity}>
                <label className="sr-only" htmlFor="home-city">Or type the city</label>
                <input id="home-city" className="input" list="home-cities" placeholder="Or type the city"
                       autoComplete="address-level2" value={city} onChange={(e) => { setCity(e.target.value); setError(""); }} />
                <datalist id="home-cities">{Object.keys(MP_CITIES).sort().map((c) => <option key={c} value={c} />)}</datalist>
                <button className="btn ghost" disabled={!city.trim()}>Search</button>
              </form>
            </div>
            {error && <p className="notice bad" role="alert"><Icon name="alert" /><span>{error}</span></p>}
          </div>
        </div>
      </section>

      <section className="wrap home-section" id="roles" aria-labelledby="roles-title">
        <h2 id="roles-title">Choose who you are</h2>
        <div className="roles">
          <Link className="role role-main" href="/find">
            <span className="ico"><Icon name="drop" /></span>
            <span className="role-text"><b>I need blood for a patient</b>
              <span>Find banks with compatible stock near the patient. No login.</span></span>
            <span className="go" aria-hidden="true">→</span>
          </Link>
          {ROLES.map((r) => (
            <Link key={r.as} className="role" href={me && r.owner.includes(me.role) ? r.dest : `/login?as=${r.as}`}>
              <span className="ico"><Icon name={r.icon} /></span>
              <span className="role-text"><b>{r.title}</b><span>{r.line}</span>{r.note && <small>{r.note}</small>}</span>
              <span className="go" aria-hidden="true">→</span>
            </Link>
          ))}
        </div>
      </section>

      <section className="wrap home-section" aria-labelledby="how-title">
        <h2 id="how-title">When a hospital asks for blood</h2>
        <ol className="flow">
          <li><span className="n">1</span><div><h3>The hospital posts a request</h3>
            <p className="muted">Rakt Doot holds compatible units at the nearest blood banks.</p></div></li>
          <li><span className="n">2</span><div><h3>Banks confirm, donors can be alerted</h3>
            <p className="muted">If stock is short, the hospital alerts verified donors nearby.</p></div></li>
          <li><span className="n">3</span><div><h3>The hospital follows it live</h3>
            <p className="muted">The request updates as banks and donors answer, until the units are received.</p></div></li>
        </ol>
      </section>

      <footer className="wrap home-foot">
        <p className="row"><Icon name="shield" />Rakt Doot helps you find blood. Doctors and blood banks always check compatibility before a transfusion.</p>
      </footer>
    </main>
  );
}
