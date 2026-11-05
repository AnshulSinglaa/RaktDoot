// Approximate centres of Madhya Pradesh district headquarters, for "where is the patient?" without a geocoding service.
// ponytail: a static list (no Nominatim calls: its public instance forbids this use, CLAUDE.md 4 point 5). The API rounds
// any location to ~11 km anyway. Upgrade: a pincode -> centroid table, or a self-hosted geocoder, when other states join.
export const MP_CITIES: Record<string, [number, number]> = {
  Bhopal: [23.26, 77.41], Indore: [22.72, 75.86], Jabalpur: [23.18, 79.99], Gwalior: [26.22, 78.18],
  Ujjain: [23.18, 75.79], Sagar: [23.84, 78.74], Dewas: [22.97, 76.05], Sehore: [23.2, 77.08],
  Vidisha: [23.53, 77.81], Raisen: [23.33, 77.78], Narmadapuram: [22.75, 77.72], Rewa: [24.54, 81.3],
  Satna: [24.6, 80.83], Ratlam: [23.33, 75.04], Khandwa: [21.83, 76.35], Chhindwara: [22.06, 78.94],
  Katni: [23.83, 80.39], Shivpuri: [25.44, 77.67], Guna: [24.65, 77.31], Mandsaur: [24.07, 75.07],
  Burhanpur: [21.31, 76.23], Damoh: [23.83, 79.44], Morena: [26.49, 77.99], Bhind: [26.56, 78.79],
  Chhatarpur: [24.92, 79.59], Betul: [21.9, 77.9], Shajapur: [23.43, 76.27], Rajgarh: [24.01, 76.73],
  Dhar: [22.6, 75.3], Khargone: [21.82, 75.61], Seoni: [22.09, 79.54], Mandla: [22.6, 80.37],
  Balaghat: [21.81, 80.18], Shahdol: [23.3, 81.36], Tikamgarh: [24.74, 78.83], Neemuch: [24.47, 74.87],
};

export type Place = { label: string; latitude: number; longitude: number };

export function placeFor(city: string): Place | null {
  const key = Object.keys(MP_CITIES).find((c) => c.toLowerCase() === city.trim().toLowerCase());
  return key ? { label: key, latitude: MP_CITIES[key][0], longitude: MP_CITIES[key][1] } : null;
}
