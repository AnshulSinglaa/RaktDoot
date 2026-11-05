"use client";
// The find-blood map (ADR 024). Loaded only on /find, only in the browser (next/dynamic with ssr: false).
// One effect owns the Leaflet map; no React wrapper. The map is aria-hidden and keeps nothing focusable: the numbered
// list next to it carries every fact and every action, so keyboard and screen-reader users never need the map.
import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import type { NearbyBank } from "@/lib/api";

export type Area = { latitude: number; longitude: number };

const pin = (n: number, on: boolean) => L.divIcon({
  className: "bank-pin-wrap",                   // no default image icon: bundlers break its URL, and a number is clearer
  html: `<span class="bank-pin${on ? " on" : ""}">${n}</span>`,
  iconSize: on ? [40, 40] : [30, 30], iconAnchor: on ? [20, 20] : [15, 15], popupAnchor: [0, -14],
});

// Inside an aria-hidden region nothing may take keyboard focus (zoom buttons, attribution and popup links).
const unfocus = (el: HTMLElement) => el.querySelectorAll("a, button").forEach((a) => a.setAttribute("tabindex", "-1"));

function popup(b: NearbyBank): HTMLElement {
  // textContent, never innerHTML: names and phones come from OpenStreetMap, i.e. from anyone.
  const box = document.createElement("div");
  const add = (tag: string, text: string) => { const e = document.createElement(tag); e.textContent = text; box.append(e); return e; };
  add("b", b.name);
  add("div", `${b.distance_km} km away · ${b.units} unit${b.units === 1 ? "" : "s"}${b.stock_is_demo ? " (demo stock, not real)" : ""}`);
  if (b.phone) (add("a", "Call " + b.phone) as HTMLAnchorElement).href = "tel:" + b.phone;
  return box;
}

export default function BankMap({ banks, area, selected, onSelect, onTiles }: {
  banks: NearbyBank[]; area: Area; selected: string | null;
  onSelect: (id: string) => void; onTiles: (ok: boolean) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const markers = useRef(new Map<string, { m: L.Marker; n: number }>());
  const cb = useRef({ onSelect, onTiles });
  cb.current = { onSelect, onTiles };
  const fit = useRef<() => void>(() => undefined);

  useEffect(() => {
    const m = L.map(box.current!, { keyboard: false, scrollWheelZoom: true });
    // One failed tile is often a blip: say "could not load" only after 3 failures in a row, and take it back as soon
    // as a tile loads.
    let failures = 0;
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    })
      .on("tileerror", () => { if (++failures >= 3) cb.current.onTiles(false); })
      .on("tileload", () => { failures = 0; cb.current.onTiles(true); })
      .addTo(m);
    // Zoom buttons exist now; attribution links appear on layeradd (once the view is set), popup links on popupopen.
    const quiet = () => unfocus(box.current!);
    quiet();
    m.on("layeradd popupopen", quiet);
    // On phones the map sits behind the List/Map toggle (display: none, 0 px wide). Fitting bounds into 0 px zooms out
    // to the whole world, so re-measure on every size change and fit again when the map becomes visible.
    let wide = 0;
    const ro = new ResizeObserver(() => {
      const was = wide;
      wide = box.current?.clientWidth ?? 0;
      m.invalidateSize();
      if (!was && wide) fit.current();
    });
    ro.observe(box.current!);
    map.current = m;
    return () => { ro.disconnect(); m.remove(); map.current = null; };
  }, []);

  useEffect(() => {
    const m = map.current!;
    const layer = L.layerGroup().addTo(m);
    markers.current.clear();
    // The search point is already rounded to ~11 km by the page and the API: draw an area, never a precise pin.
    const centre: L.LatLngTuple = [area.latitude, area.longitude];
    L.circle(centre, { radius: 5500, color: "#1f4f8f", weight: 2, dashArray: "6 6", fillOpacity: 0.08, interactive: false })
      // beside the centre, on the side facing the middle of the map ("auto"), so a 360 px phone never clips it
      .bindTooltip("Search area (approximate)", { permanent: true, direction: "auto", className: "area-tip" })
      .addTo(layer);
    banks.forEach((b, i) => {
      // Nearest on top: in a dense city the markers overlap, and the nearest bank is the one most likely to matter.
      const mk = L.marker([b.latitude, b.longitude], { icon: pin(i + 1, false), keyboard: false, title: b.name, zIndexOffset: -i * 10 })
        .bindPopup(() => popup(b))
        .on("click", () => cb.current.onSelect(b.blood_bank_id))
        .addTo(layer);
      markers.current.set(b.blood_bank_id, { m: mk, n: i + 1 });
    });
    const bounds = L.latLngBounds([centre, ...banks.map((b): L.LatLngTuple => [b.latitude, b.longitude])]);
    fit.current = () => { if (box.current?.clientWidth) m.fitBounds(bounds.pad(0.15), { maxZoom: 13 }); };
    if (!box.current?.clientWidth) m.setView(centre, 10);   // hidden: any view, so Leaflet is "ready"; fit on show
    fit.current();
    return () => { layer.remove(); };
  }, [banks, area]);

  useEffect(() => {
    markers.current.forEach(({ m, n }, id) => {
      const on = id === selected;
      m.setIcon(pin(n, on)).setZIndexOffset(on ? 1000 : -(n - 1) * 10);
      if (on && map.current && !map.current.getBounds().contains(m.getLatLng())) map.current.panTo(m.getLatLng());
    });
  }, [selected, banks]);

  return <div ref={box} className="bank-map" aria-hidden="true" />;
}
