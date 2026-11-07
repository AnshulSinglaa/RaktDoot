// The real workload (k6). Run from INSIDE the compose network, so Docker Desktop's port forwarding is not measured:
//   docker run --rm --network raktdoot_default -e BASE=http://api:8000 \
//     -v "<repo>/Backend/load:/scripts:ro" -v "<repo>/Backend/demo-credentials.txt:/creds.txt:ro" \
//     grafana/k6:0.57.0 run /scripts/emergency.js
// Each iteration is one full emergency lifecycle, so the test is self-cleaning (every reserved unit is released):
//   POST /v1/emergencies (geo search + FOR UPDATE SKIP LOCKED) -> 3 x GET -> donor-matches (PostGIS) -> cancel
import http from "k6/http";
import { check } from "k6";

const CREDS = open("/creds.txt").split("\n");
const PASSWORD = CREDS[0].split("ALL accounts: ")[1].trim();
const HOSPITALS = CREDS.filter((l) => l.includes(" hospital ")).map((l) => l.split(/\s+/)[0]).slice(0, 8);
const GROUPS = ["O+", "B+", "A+", "AB+", "O-", "B-", "A-", "AB-"];

export const options = {
  scenarios: { hospitals: { executor: "constant-vus", vus: Number(__ENV.VUS || 40), duration: __ENV.DURATION || "30s" } },
  // The SLO, decided BEFORE measuring. The run fails if it is broken.
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{name:create}": ["p(95)<300"],
    "http_req_duration{name:read}": ["p(95)<150"],
  },
};

export function setup() {
  // 8 logins: the login limiter allows 10 per minute per IP (ADR 013)
  return HOSPITALS.map((email) => {
    const r = http.post(`${__ENV.BASE}/v1/auth/login`, JSON.stringify({ email, password: PASSWORD }),
                        { headers: { "Content-Type": "application/json" } });
    return r.json("access_token");
  });
}

export default function (tokens) {
  const headers = { Authorization: `Bearer ${tokens[__VU % tokens.length]}`, "Content-Type": "application/json" };
  const body = JSON.stringify({ blood_group: GROUPS[Math.floor(Math.random() * 8)], units_needed: 1 + Math.floor(Math.random() * 3) });
  const created = http.post(`${__ENV.BASE}/v1/emergencies`, body, { headers, tags: { name: "create" } });
  if (!check(created, { "created 201": (r) => r.status === 201 })) return;
  const id = created.json("id");
  for (let i = 0; i < 3; i++) http.get(`${__ENV.BASE}/v1/emergencies/${id}`, { headers, tags: { name: "read" } });
  http.get(`${__ENV.BASE}/v1/emergencies/${id}/donor-matches`, { headers, tags: { name: "donor_matches" } });
  check(http.post(`${__ENV.BASE}/v1/emergencies/${id}/cancel`, JSON.stringify({ collected_from: [] }), { headers, tags: { name: "cancel" } }),
        { "cancelled 200": (r) => r.status === 200 });
}
