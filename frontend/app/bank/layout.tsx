import { RoleGate } from "@/lib/gate";

// Wrong role: a "this page is for blood bank accounts" notice instead of a broken page. Logged out: /login.
export default function Layout({ children }: { children: React.ReactNode }) {
  return <RoleGate roles={["blood_bank", "admin"]} page="blood bank" shell="desk">{children}</RoleGate>;
}
