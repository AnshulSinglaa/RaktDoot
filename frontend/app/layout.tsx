import "./globals.css";
import type { Metadata, Viewport } from "next";

export const metadata: Metadata = {
  title: "Rakt Doot",
  description: "Find blood banks with compatible stock near a patient, and reach donors through the hospital.",
};
export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#c8102e" };

// Each page picks its shell: <main className="phone"> for public and donor pages (mobile-first column),
// <main className="desk"> for staff pages, which are used on desktop computers.
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
