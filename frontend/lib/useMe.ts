"use client";
// The guard every staff page uses: who is logged in, and are they the right kind of user for this screen?
// UX only. The API enforces every permission on every call (the frontend is not a security boundary).
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, authed, homeFor, type Me } from "@/lib/api";

export function useMe(roles: string[]): Me | null {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const allowed = roles.join(",");
  useEffect(() => {
    authed<Me>("/v1/auth/me")
      .then((u) => (allowed.split(",").includes(u.role) ? setMe(u) : router.replace(homeFor(u.role))))
      // Only "not logged in" sends you to /login: an outage (503) must not log a nurse out mid-emergency.
      // RoleGate (the layout around every staff page) shows the "briefly unavailable" message and the retry.
      .catch((e) => { if (e instanceof ApiError && e.status === 401) router.replace("/login"); });
  }, [router, allowed]);
  return me;
}
