import { RoleGate } from "@/lib/gate";

// Wrong role: a "this page is for donor accounts" notice instead of a broken page. Logged out: /login.
export default function Layout({ children }: { children: React.ReactNode }) {
  return <RoleGate roles={["donor"]} page="donor" shell="phone">{children}</RoleGate>;
}
