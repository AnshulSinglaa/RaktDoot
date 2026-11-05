// Calibration: how fast is the stack when the app does nothing? (k6 run load/health.js)
import http from "k6/http";
export const options = { vus: 40, duration: "15s" };
export default function () { http.get(`${__ENV.BASE}/health/live`); }
