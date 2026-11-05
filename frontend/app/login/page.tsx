"use client";
// Log in, or create a donor account. Staff accounts are created by an admin (ADR 013), never here.
// ?as=donor|hospital|bank only changes the heading. The role always comes from the account (homeFor(me.role)).
import { Suspense, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ApiError, authed, homeFor, login, register, type Me } from "@/lib/api";
import { Icon, Logo } from "@/lib/ui";

const HINT: Record<string, { title: string; lede: string }> = {
  donor: { title: "Log in to donate", lede: "See alerts from hospitals near you and set when you are available. New here? Create an account below." },
  hospital: { title: "Hospital desk", lede: "Log in with the account your organisation was given. Staff accounts are never created here." },
  bank: { title: "Blood bank desk", lede: "Log in with the account your organisation was given. Staff accounts are never created here." },
};

export default function Page() {
  return <Suspense fallback={<main className="phone" />}><Login /></Suspense>;
}

function Login() {
  const router = useRouter();
  const as = useSearchParams().get("as") ?? "";
  const hint = HINT[as];
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await (mode === "login" ? login(email, password) : register(email, password));
      // Always the page for the account's real role (from the API), never the card that was clicked. The ?as= lets
      // that page say "this is a donor account" when the two differ (lib/gate.tsx).
      router.push(homeFor((await authed<Me>("/v1/auth/me")).role) + (hint ? `?as=${as}` : ""));
    } catch (err) {
      // Switch on the stable problem `type`, never on the human title (CLAUDE.md 5A).
      const p = err instanceof ApiError ? err : null;
      setError(
        p?.is("unauthorized") ? "That email and password do not match. Check both and try again."
        : p?.is("email-taken") ? "This email already has an account. Log in instead."
        : p?.is("too-many-requests") ? "Too many attempts. Wait a minute, then try again."
        : p ? p.message
        : "Could not reach Rakt Doot. Check your internet connection.",
      );
    } finally {
      setBusy(false);
    }
  }

  const registering = mode === "register";
  return (
    <main className="phone">
      <header className="topbar">
        <Link className="iconbtn" href="/" aria-label="Back to home"><Icon name="back" /></Link>
        <span className="title" />
      </header>
      <form className="body" onSubmit={submit}>
        <div className="stack">
          <Logo />
          <h1>{registering ? "Create a donor account" : hint?.title ?? "Welcome back"}</h1>
          <p className="lede">
            {registering ? "Two minutes now can save a life later. You add your blood group on the next screen."
              : hint?.lede ?? "Donors, hospital staff and blood bank staff all log in here."}
          </p>
        </div>
        <label className="field">
          <span>Email</span>
          <input className="input" type="email" required autoComplete="email" inputMode="email" value={email}
                 onChange={(e) => setEmail(e.target.value)} />
        </label>
        <div className="field">
          <label htmlFor="password" style={{ fontSize: "var(--t-sm)", fontWeight: 600, color: "var(--ink-2)" }}>Password</label>
          <div style={{ position: "relative" }}>
            <input id="password" className="input" type={show ? "text" : "password"} required
                   minLength={registering ? 12 : 1} maxLength={128} aria-describedby={registering ? "pw-hint" : undefined}
                   autoComplete={registering ? "new-password" : "current-password"} value={password}
                   onChange={(e) => setPassword(e.target.value)} style={{ paddingRight: 72 }} />
            <button type="button" className="linkbtn" onClick={() => setShow(!show)} aria-pressed={show}
                    style={{ position: "absolute", right: 14, top: 13, fontSize: "var(--t-sm)" }}>
              {show ? "Hide" : "Show"}<span className="sr-only"> password</span>
            </button>
          </div>
          {registering && <small id="pw-hint">At least 12 characters. A short sentence is easy to remember.</small>}
        </div>
        {error && <p className="notice bad" role="alert"><Icon name="alert" /><span>{error}</span></p>}
        <button className="btn block" disabled={busy}>
          {busy ? "Please wait…" : registering ? "Create account" : "Log in"}
        </button>
        <p className="muted" style={{ textAlign: "center" }}>
          {registering ? "Already have an account? " : "Want to donate blood? "}
          <button type="button" className="linkbtn" onClick={() => { setMode(registering ? "login" : "register"); setError(""); }}>
            {registering ? "Log in" : "Create a donor account"}
          </button>
        </p>
        <p className="muted" style={{ textAlign: "center" }}>
          Hospital and blood bank accounts are created and verified by Rakt Doot. There is no staff sign-up.
        </p>
      </form>
    </main>
  );
}
