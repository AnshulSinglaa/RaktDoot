"use client";
// The top bar every staff page shares (desktop layout): brand, what this desk is, which organization, log out.
import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { LOGOUT_FAILED, api, logout, type Me, type Organization } from "@/lib/api";
import { Icon, Logo } from "@/lib/ui";

export function StaffBar({ me, org, desk, back }: { me: Me | null; org: Organization | null; desk: string; back?: string }) {
  const router = useRouter();
  const [failed, setFailed] = useState(false);
  return (
    <header className="topbar">
      {back && <Link className="iconbtn" href={back} aria-label="Back"><Icon name="back" /></Link>}
      <Link href="/" style={{ textDecoration: "none" }}><Logo /></Link>
      <span className="title muted" style={{ fontWeight: 600 }}>{desk}</span>
      {me && (
        <span className="who">
          <b>{org?.name ?? (me.role === "admin" ? "Administrator" : " ")}</b>
          <span>{me.email}</span>
        </span>
      )}
      {failed && <span className="small error-text" role="alert">{LOGOUT_FAILED}</span>}
      <button className="iconbtn" aria-label="Log out" title="Log out"
              onClick={async () => { setFailed(false); try { await logout(); router.push("/"); } catch { setFailed(true); } }}>
        <Icon name="logout" />
      </button>
    </header>
  );
}

/** The staff member's own organization (public read). `reload` after a change such as "stock is accurate". */
export function useOrg(me: Me | null): [Organization | null, (o: Organization) => void] {
  const [org, setOrg] = useState<Organization | null>(null);
  useEffect(() => {
    if (me?.organization_id) api<Organization>(`/v1/organizations/${me.organization_id}`).then(setOrg).catch(() => undefined);
  }, [me]);
  return [org, setOrg];
}
