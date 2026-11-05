# ADR 024: Leaflet for the find-blood map

Date: 2026-10-20. Status: accepted. Item 2r (frontend). The first runtime dependency added to the frontend.

## Context
`/find` lists the nearest blood banks with a distance in km. A distance does not show what a family needs to judge a
trip: which side of the river or highway a bank is on, whether three banks sit together in one hospital district, how
far "48 km" really is from the patient. Item 2r-BE gave every `NearbyBank` its real coordinates (OpenStreetMap, ADR
022), so a map is now possible. The users are scared people on cheap phones and slow networks; the map must never be
the only way to a result, and it must not cost the other pages anything.

## Options
1. **No map.** Zero cost, but distance alone hides geography (the problem above).
2. **Static map image** (a rendered PNG per search). No JS, but needs a tile renderer or a paid static-maps API, cannot
   be clicked to link marker and card, and changes with every chip.
3. **Leaflet 1.9.x.** ~40 KB gzip JS + ~4 KB CSS, no dependencies of its own, raster OSM tiles, BSD-2 licence, the
   most used open web map library; mature (1.9.4 is the current stable).
4. **MapLibre GL.** Vector tiles and WebGL: ~200 KB+ gzip, needs a vector tile source and a style, heavy on cheap
   phones. Its strengths (rotation, 3D, smooth vector zoom) are not needed to show 20 points.
5. **Google Maps JS API.** API key and billing account, a third party that sees every search location, terms that
   restrict caching and mixing with other data. Not acceptable for a health-data app's public page.

## Decision
Option 3: `leaflet` pinned to exactly `1.9.4`, `@types/leaflet` `1.9.20` (dev). Nothing else.
- **No React wrapper.** `react-leaflet` is not added: one client component (`app/find/BankMap.tsx`) owns the map in
  one `useEffect` (create/remove), a second effect redraws markers when the results change, a third re-styles the
  selected marker. Three effects do not justify a second dependency.
- **Client only, /find only.** `page.tsx` loads it with `next/dynamic(() => import("./BankMap"), { ssr: false })`
  (Leaflet touches `window` on import). The Leaflet chunk is referenced only by the `/find` page; the server bundle
  does not contain it (checked in `.next` after `npm run build`).
- **Tiles:** OSM standard tiles (`tile.openstreetmap.org`) with the attribution control "© OpenStreetMap contributors"
  linking to openstreetmap.org/copyright (ODbL, ADR 022). The page also carries the attribution as text under the
  results, and staff desks in their footer.
- **Markers:** `L.divIcon` with the bank's list number (1..20), no image icons (bundlers break Leaflet's default icon
  URLs, and a number links marker and card without relying on colour). The selected marker is larger and filled.
  The patient is a dashed ~5.5 km circle around the already-rounded search point, labelled "Search area
  (approximate)", never a pin.
- **Accessibility:** the map is `aria-hidden` and nothing inside it is focusable (keyboard handler off, zoom and
  attribution links set to `tabindex=-1`). The numbered list is the accessible primary view and carries every fact
  and action (call, directions, "Show on map"). Popup content is built with `textContent`: OSM names come from anyone.

## Consequences
- Cost: ~44 KB gzip JS for the `/find` map chunk (Leaflet + BankMap, measured in `.next/static/chunks`), loaded after
  the list renders; other routes unchanged.
- **Tile usage policy:** OSM's public tile servers allow light use with attribution and a valid referer, and forbid
  heavy or bulk use. A demo and interviews are within it. **A production pilot needs its own tile server or a paid
  tile provider** (e.g. a self-hosted OpenMapTiles / tileserver on the same OSM extract, or a commercial provider):
  swapping is one URL in `BankMap.tsx`.
- **Failure mode:** tiles blocked or offline -> grey map plus a notice "The map could not load. Every bank is in the
  list."; no uncaught error (verified with the tile host blocked). The JS chunk failing to load leaves the list intact.
- Privacy: tile requests reveal the viewed area to the tile host (coarse: the map shows the rounded search area and the
  banks around it, never the user's precise position).
- Not done: marker clustering (20 results at most), routes or ETA (Stage 5, OSRM), keyboard-operable markers (the list
  mirrors them).
