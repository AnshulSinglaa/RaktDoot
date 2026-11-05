import { RoleGate } from "@/lib/gate";

// Wrong role: a "this page is for hospital accounts" notice instead of a broken page. Logged out: /login.
export default function Layout({ children }: { children: React.ReactNode }) {
  return <RoleGate roles={["hospital", "admin"]} page="hospital" shell="desk">{children}</RoleGate>;
}
