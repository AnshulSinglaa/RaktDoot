// One click-through of the three main journeys: family finds blood, hospital posts a request, bank counts stock.
// Uses the seeded demo accounts (`python manage.py seed-demo`). The password is read from the gitignored
// Backend/demo-credentials.txt (or E2E_PASSWORD) and never printed.
import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const SHOTS = path.join(__dirname, "..", "..", "docs", "screenshots");

function demoPassword(): string {
  if (process.env.E2E_PASSWORD) return process.env.E2E_PASSWORD;
  const txt = readFileSync(path.join(__dirname, "..", "..", "Backend", "demo-credentials.txt"), "utf8");
  const m = txt.match(/Password for ALL accounts:\s*(\S+)/);
  if (!m) throw new Error("No demo password found: run `python manage.py seed-demo` first");
  return m[1];
}

async function shot(page: Page, name: string) {
  if (process.env.SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: false });
}

async function noSidewaysScroll(page: Page) {
  const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(over, "page scrolls sideways").toBeLessThanOrEqual(0);
}

async function logIn(page: Page, email: string) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(demoPassword());
  await page.getByRole("button", { name: "Log in" }).click();
}

test("home, then a family finds compatible blood in Bhopal", async ({ page }, info) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Need blood for a patient?" })).toBeVisible();
  await noSidewaysScroll(page);
  if (info.project.name === "desktop") await shot(page, "home");

  await page.goto("/find");
  await page.getByRole("button", { name: /^O-/ }).click();
  await page.getByRole("button", { name: "Continue with O-" }).click();
  await page.getByLabel("City").fill("Bhopal");
  await page.getByRole("button", { name: "Show blood banks" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(/blood banks? with O- or compatible blood/);
  // Real facilities, synthetic stock: a call button may only ask what the bank really has, never claim the demo stock.
  for (const call of await page.locator('a[href^="tel:"]').all()) await expect(call).toContainText("Call to ask what they really have");
  await noSidewaysScroll(page);
  // The desktop map loads its tiles from OpenStreetMap: wait for them, or the screenshot shows an empty grey box.
  if (info.project.name === "desktop" && process.env.SHOTS) {
    await page.locator(".leaflet-tile-loaded").first().waitFor({ timeout: 20_000 });
    await page.waitForTimeout(1_500); // let the remaining tiles and markers settle
  }
  await shot(page, info.project.name === "desktop" ? "find-desktop" : "find-mobile");
});

test("hospital posts an emergency request and sees its holds", async ({ page }, info) => {
  test.skip(info.project.name === "phone", "staff desks are desktop screens");
  await logIn(page, "hospital1@demo.raktdoot.in");
  await expect(page.getByRole("heading", { name: "New emergency request" })).toBeVisible();
  await page.getByLabel("Patient blood group").selectOption("O+");
  await page.getByLabel("Units needed").fill("2");
  await page.getByLabel("Bed / IP number (optional)").fill("ICU-4");
  await page.getByRole("button", { name: "Post emergency request" }).click();
  await expect(page).toHaveURL(/\/hospital\/requests\//);
  await expect(page.locator("#req-title")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Blood banks" })).toBeVisible();
  // A demo request must never offer to call a real bank (safety rule 5).
  await expect(page.locator('a[href^="tel:"]')).toHaveCount(0);
  await shot(page, "hospital-request");
});

test("bank counts its shelf: an unchanged count changes nothing", async ({ page }, info) => {
  test.skip(info.project.name === "phone", "staff desks are desktop screens");
  await logIn(page, "bank2@demo.raktdoot.in");
  await expect(page.getByRole("heading", { name: "Count your stock" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Holds on your units" })).toBeVisible();
  await shot(page, "bank-desk");
  await page.getByRole("button", { name: "Save shelf count" }).click();
  await expect(page.getByRole("status")).toContainText("Your list matched the shelf, so nothing changed.");
});
