// End-to-end smoke test against the running Docker stack (`docker compose up`): web :3000, api :8000.
// Run: npx playwright test   (SHOTS=1 also writes the README screenshots to docs/screenshots/)
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  workers: 1, // ponytail: serial, the tests share one dev DB; parallelise when it is a separate test stack
  use: { baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000", trace: "retain-on-failure" },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } } },
    { name: "phone", use: { ...devices["Pixel 7"], viewport: { width: 360, height: 780 } } },
  ],
});
