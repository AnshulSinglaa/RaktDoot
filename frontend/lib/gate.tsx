"use client";
// The page-level role check used by the /donor, /hospital and /bank layouts. UX only: the API answers 403 to the
// wrong role on every call. This just makes it obvious instead of showing a broken desk.
import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, LOGOUT_FAILED, UNAVAILABLE, authed, homeFor, logout, type Me } from "@/lib/api";
import { Icon, Logo, Skeleton } from "@/lib/ui";

/** Which account roles each entry card (login ?as=) is meant for. Headings only: the server decides the role. */
export const DESK_ROLES: Record<string, string[]> = {
  donor: ["donor"], hospital: ["hospital", "admin"], bank: ["blood_bank", "admin"],
};
export const ROLE_NAME: Record<string, string> = {
  donor: "donor", hospital: "hospital", blood_bank: "blood bank", admin: "administrator",
};
const CARD_NAME: Record<string, string> = { donor: "I want to donate", hospital: "Hospital staff", bank: "Blood bank staff" };

export function RoleGate({ roles, page, shell, children }: {
  roles: string[]; page: string; shell: "phone" | "desk"; children: React.ReactNode;
}) {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [came, setCame] = useState("");        // the card the user clicked before logging in, if it does not fit
  const [down, setDown] = useState(false);     // API or database unreachable: say so, never log the user out
  const [attempt, setAttempt] = useState(0);
  const [logoutFailed, setLogoutFailed] = useState(false);

  useEffect(() => {
    authed<Me>("/v1/auth/me")
      .then((u) => {
        setMe(u);
        const as = new URLSearchParams(window.location.search).get("as") ?? "";
        if (DESK_ROLES[as] && !DESK_ROLES[as].includes(u.role)) setCame(as);
      })
      // 401 = logged out or the session expired. Anything else (a 503 outage, a network blip) is NOT a reason to
      // throw a nurse out mid-emergency: show the outage and let them retry.
      .catch((e) => (e instanceof ApiError && e.status === 401 ? router.replace("/login") : setDown(true)));
  }, [router, attempt]);

  if (down) return (
    <main className={shell}><div className={shell === "desk" ? "desk-main" : "body"}>
      <div className="notice warn" role="alert"><Icon name="info" /><span>{UNAVAILABLE}</span></div>
      <button className="btn block" onClick={() => { setDown(false); setAttempt((n) => n + 1); }}>Try again</button>
    </div></main>
  );

  if (!me) return <main className={shell}><div className={shell === "desk" ? "desk-main" : "body"}><Skeleton /></div></main>;

  if (!roles.includes(me.role)) return (
    <main className="phone">
      <header className="topbar"><Link href="/" className="grow" style={{ textDecoration: "none" }}><Logo /></Link></header>
      <div className="body">
        <div className="notice warn" role="alert"><Icon name="info" /><span>
          <b>This page is for {page} accounts</b>
          You are logged in as {me.email}, a {ROLE_NAME[me.role] ?? me.role} account. Hospital and blood bank accounts are
          set up and verified by Rakt Doot; they cannot be created here.
        </span></div>
        <Link className="btn block" href={homeFor(me.role)}>Go to your {ROLE_NAME[me.role] ?? ""} page</Link>
        {logoutFailed && <p className="notice bad" role="alert"><Icon name="alert" /><span>{LOGOUT_FAILED}</span></p>}
        <button className="btn ghost block"
                onClick={async () => { setLogoutFailed(false); try { await logout(); router.push("/login"); } catch { setLogoutFailed(true); } }}>
          Log in with a different account
        </button>
      </div>
    </main>
  );

  return (
    <>
      {came && (
        <p className="notice warn role-mismatch" role="status"><Icon name="info" /><span>
          <b>This is a {ROLE_NAME[me.role] ?? me.role} account</b>
          You came in through &ldquo;{CARD_NAME[came]}&rdquo;, so we opened your own page instead. Staff desks need the
          account your organisation was given.
        </span></p>
      )}
      {children}
      {shell === "desk" && (
        <footer className="desk-foot">
          Facility names and locations: &copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a> (ODbL).
        </footer>
      )}
    </>
  );
}
