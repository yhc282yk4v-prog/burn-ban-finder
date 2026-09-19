"use strict";

const STATES = "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split(" ");
const LABEL = { ban: "Burn ban", restricted: "Restrictions", lifted: "Ban lifted" };
const STALE_DAYS = 30;
const NEAR_MILES = 50;

const S = {
  bans: null, alerts: null, reports: [], states: {}, usa: null,
  me: null, focus: null, place: null,
  pick: false, pending: null,
  filters: { ban: true, restricted: true, alert: true, report: true, none: false, nodata: true },
};

const $ = (id) => document.getElementById(id);
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const where = (q) => (STATES.includes(q.state) ? `${q.name}, ${q.state}` : q.name);
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : null);

/* ---------- geometry ---------- */
function inRing(pt, ring) {
  let c = false;
  const [x, y] = pt;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c;
  }
  return c;
}
const inPoly = (pt, poly) => inRing(pt, poly[0]) && !poly.slice(1).some((h) => inRing(pt, h));
function contains(g, pt) {
  if (g.type === "Polygon") return inPoly(pt, g.coordinates);
  if (g.type === "MultiPolygon") return g.coordinates.some((p) => inPoly(pt, p));
  return false;
}
function* verts(g) {
  if (g.type === "Point") yield g.coordinates;
  else if (g.type === "Polygon") for (const r of g.coordinates) yield* r;
  else if (g.type === "MultiPolygon") for (const p of g.coordinates) for (const r of p) yield* r;
}
function miles([lng1, lat1], [lng2, lat2]) {
  const r = Math.PI / 180, dLat = (lat2 - lat1) * r, dLng = (lng2 - lng1) * r;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(dLng / 2) ** 2;
  return 3958.8 * 2 * Math.asin(Math.sqrt(a));
}
function distTo(g, pt) {
  if (g.type !== "Point" && contains(g, pt)) return 0;
  let best = Infinity;
  for (const v of verts(g)) best = Math.min(best, miles(pt, v));
  return best;
}

/* ---------- formatting ---------- */
function ago(ms) {
  if (!ms) return null;
  const d = (Date.now() - ms) / 864e5;
  if (d < 1) return "today";
  if (d < 2) return "yesterday";
  if (d < 45) return `${Math.floor(d)} days ago`;
  return new Date(ms).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}
const searchUrl = (place) => `https://www.google.com/search?q=${encodeURIComponent(`${place} burn ban`)}`;

/* ---------- map ---------- */
const map = L.map("map", { zoomControl: false, worldCopyJump: false }).setView([39.3, -97.5], 4);
L.control.zoom({ position: "bottomright" }).addTo(map);
map.createPane("nodata").style.zIndex = 405;
map.createPane("bans").style.zIndex = 410;
map.createPane("alerts").style.zIndex = 420;

const dark = matchMedia("(prefers-color-scheme: dark)");
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18, className: "base-tiles",
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);
dark.addEventListener("change", render);

let banLayer, alertLayer, reportLayer, nodataLayer, meMarker, spotMarker;

const liveStates = () => new Set((S.bans?.coverage || []).filter((c) => c.ok).flatMap((c) => c.covers));

function dotIcon(cls, color) {
  return L.divIcon({ className: "", html: `<div class="dot ${cls}" style="--c:${color}"></div>`, iconSize: [18, 18], iconAnchor: [9, 9] });
}

function render() {
  [banLayer, alertLayer, reportLayer, nodataLayer].forEach((l) => l && l.remove());
  const F = S.filters, col = { ban: css("--ban"), restricted: css("--restr"), none: css("--clear"), alert: css("--alert"), report: css("--report") };

  if (S.bans && S.usa && F.nodata) {
    // States without a live feed: shade them so "blank" reads as "unknown", never as "no ban".
    const live = liveStates(), codeOf = Object.fromEntries(Object.entries(S.states).map(([c, s]) => [s.name, c]));
    nodataLayer = L.geoJSON(S.usa.features.filter((f) => !live.has(codeOf[f.properties.name])), {
      pane: "nodata",
      style: () => ({ color: css("--muted"), weight: 1, opacity: 0.6, dashArray: "2 4", fillColor: css("--muted"), fillOpacity: 0.16 }),
      onEachFeature: (f, l) => l.bindTooltip(`${esc(f.properties.name)}: no live burn ban data`, { sticky: true }),
    }).addTo(map);
  }
  if (S.bans) {
    const feats = S.bans.features.filter((f) => F[f.properties.status]).sort((x, y) => !!y.properties.broad - !!x.properties.broad);
    banLayer = L.geoJSON(feats, {
      pane: "bans",
      style: (f) => f.properties.broad
        ? { color: col[f.properties.status], weight: 1.6, dashArray: "6 4", opacity: 0.85, fillColor: col[f.properties.status], fillOpacity: 0.22 }
        : f.properties.status === "none"
        ? { color: col.none, weight: 0.7, opacity: 0.55, fillColor: col.none, fillOpacity: 0.05 }
        : { color: col[f.properties.status], weight: 1.2, opacity: 0.9, fillColor: col[f.properties.status], fillOpacity: 0.5 },
      pointToLayer: (f, ll) => L.circleMarker(ll, { pane: "bans", radius: 9, color: "#fff", weight: 2, fillColor: col[f.properties.status], fillOpacity: 0.95 }),
      onEachFeature: (f, l) => l.bindTooltip(`${esc(where(f.properties))} · ${esc(f.properties.label)}`, { sticky: true }),
    }).addTo(map);
  }
  if (S.alerts && F.alert) {
    alertLayer = L.geoJSON(S.alerts.features, {
      pane: "alerts",
      style: () => ({ color: col.alert, weight: 1.6, dashArray: "6 5", fillColor: col.alert, fillOpacity: 0.12 }),
      onEachFeature: (f, l) => l.bindTooltip(esc(f.properties.event), { sticky: true }),
    }).addTo(map);
  }
  if (F.report) {
    reportLayer = L.layerGroup(S.reports.map((r) => {
      const old = Date.now() - r.reported > STALE_DAYS * 864e5;
      const m = L.marker([r.lat, r.lng], { icon: dotIcon(old ? "old" : "", col[r.status === "lifted" ? "none" : r.status] || col.report), bubblingMouseEvents: false });
      m.bindPopup(() => reportPopup(r));
      return m;
    })).addTo(map);
  }
  renderList();
}

function reportPopup(r) {
  const el = document.createElement("div");
  el.className = "pop";
  const src = safeUrl(r.url);
  el.innerHTML = `<h3>${esc(r.area)}${r.state ? ", " + esc(r.state) : ""}</h3>
    <div class="st ${esc(r.status)}">${esc(LABEL[r.status])}</div>
    ${r.note ? `<p>${esc(r.note)}</p>` : ""}
    <p class="fine">Community report · ${esc(ago(r.reported))}${Date.now() - r.reported > STALE_DAYS * 864e5 ? " · may be out of date" : ""}</p>
    ${src ? `<p><a href="${esc(src)}" target="_blank" rel="noopener">Source</a></p>` : ""}
    <button type="button">Remove this report</button>`;
  el.querySelector("button").onclick = async () => {
    await fetch(`api/reports/${r.id}`, { method: "DELETE" });
    map.closePopup();
    await loadReports();
    toast("Report removed");
    renderHere();
  };
  return el;
}

/* ---------- toast ---------- */
let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 3500);
}

/* ---------- place lookup (OpenStreetMap Nominatim) ---------- */
const geoCache = new Map();
async function reverse([lng, lat]) {
  const key = `${lat.toFixed(2)},${lng.toFixed(2)}`;
  if (geoCache.has(key)) return geoCache.get(key);
  let out = null;
  try {
    const r = await fetch(`https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=10&addressdetails=1&lat=${lat}&lon=${lng}`);
    const a = (await r.json()).address;
    if (a) {
      const code = (a["ISO3166-2-lvl4"] || "").replace("US-", "");
      out = { county: a.county || a.city || a.town || a.village || "", state: a.state || "", code: STATES.includes(code) ? code : "" };
    }
  } catch { /* offline or rate limited: the verdict still works from the feeds */ }
  geoCache.set(key, out);
  return out;
}

/* ---------- verdict for the chosen spot ---------- */
async function renderHere() {
  const el = $("here");
  const p = S.focus;
  if (!p) {
    el.innerHTML = `<p class="intro">Tap <b>Use my location</b>, search a place, or click anywhere on the map to see whether a burn ban is in effect there.</p>`;
    return;
  }
  const bans = (S.bans?.features || []).filter((f) => contains(f.geometry, p) || (f.geometry.type === "Point" && miles(p, f.geometry.coordinates) < 15));
  const active = bans.filter((f) => f.properties.status !== "none");
  const alerts = (S.alerts?.features || []).filter((f) => contains(f.geometry, p));
  const near = S.reports
    .map((r) => ({ r, d: miles(p, [r.lng, r.lat]) }))
    .filter((x) => x.d <= NEAR_MILES).sort((a, b) => a.d - b.d);

  const place = await reverse(p);
  if (S.focus !== p) return; // user moved on while we waited
  S.place = place;

  const code = place?.code || bans.map((f) => f.properties.state).find((s) => STATES.includes(s)) || "";
  const feedFor = (S.bans?.coverage || []).find((c) => c.ok && c.covers.includes(code));
  const stateInfo = S.states[code];
  const hasBan = active.some((f) => f.properties.status === "ban");
  let tone, verdict;
  if (hasBan) [tone, verdict] = ["ban", "Burn ban in effect"];
  else if (active.length) [tone, verdict] = ["restricted", "Fire restrictions in effect"];
  else if (alerts.length) [tone, verdict] = ["alert", "Fire weather alert"];
  else if (bans.length || feedFor) [tone, verdict] = ["clear", "No burn ban listed"];
  else [tone, verdict] = ["unknown", "No live data here"];

  const placeName = bans[0] ? where(bans[0].properties) : place ? [place.county, place.code || place.state].filter(Boolean).join(", ") : "Selected spot";
  const items = [];
  for (const f of bans) {
    const q = f.properties, src = safeUrl(q.url);
    items.push(`<div class="item"><b>${esc(where(q))} · ${esc(q.label)}</b>
      ${q.detail ? `<span>${esc(q.detail)}</span>` : ""}
      <span class="meta">${esc(q.source)}${q.updated ? ` · updated ${esc(ago(q.updated))}` : ""}${src ? ` · <a href="${esc(src)}" target="_blank" rel="noopener">order</a>` : ""}</span></div>`);
  }
  if (!active.length && feedFor && !bans.length && feedFor.kind !== "order") {
    items.push(`<div class="item"><b>No active order on record here</b><span class="meta">${esc(feedFor.source)}</span></div>`);
  }
  for (const n of (S.bans?.notes || []).filter((n) => n.state === code)) {
    items.push(`<div class="item"><b>${esc(S.states[code]?.name || code)} · ${n.manual ? "recent reports (not live data)" : "what the official sources say"}</b><span>${esc(n.text)}</span>
      <span class="meta">${esc(n.source)} · ${n.verified ? "re-checked against the official page just now" : esc(n.warn || (n.manual ? "checked by hand " : "last checked ") + n.checked_on)}${n.url ? ` · <a href="${esc(n.url)}" target="_blank" rel="noopener">page</a>` : ""}</span></div>`);
  }
  for (const f of alerts) {
    const q = f.properties;
    items.push(`<div class="item"><b>${esc(q.event)}</b><span>${esc(q.headline || q.area || "")}</span>
      <span class="meta">National Weather Service${q.ends ? ` · until ${esc(new Date(q.ends).toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" }))}` : ""}</span></div>`);
  }
  for (const { r, d } of near.slice(0, 3)) {
    items.push(`<div class="item"><b>${esc(r.area)}${r.state ? ", " + esc(r.state) : ""} · ${esc(LABEL[r.status])}</b>
      <span class="meta">Community report · ${Math.round(d)} mi away · ${esc(ago(r.reported))}</span></div>`);
  }

  const q = place ? `${place.county} ${place.state}`.trim() : "";
  const official = stateInfo ? `<a href="${esc(stateInfo.url)}" target="_blank" rel="noopener">${esc(stateInfo.agency)}</a>` : "";
  const search = q ? `<a href="${esc(searchUrl(q))}" target="_blank" rel="noopener">search ${esc(q)} burn ban</a>` : "";
  let caveat;
  if (tone === "unknown") {
    const who = stateInfo?.name || place?.state;
    caveat = `${who ? esc(who) + " doesn't" : "This area doesn't"} publish a county burn ban feed this app can read yet. Check ${official || "the county's fire department"}${search ? ` or ${search}` : ""}.`;
  } else {
    caveat = `Cities, fire districts and federal land can have their own rules. Confirm with your local fire department before you burn.${official ? ` Official source: ${official}.` : ""}${search && tone !== "ban" ? ` You can also ${search}.` : ""}`;
  }

  $("here").innerHTML = `<div class="sign ${tone}">
      <div class="kicker">${S.focus === S.me ? "Where you are" : "Selected spot"}</div>
      <div class="verdict">${verdict}</div><div class="where">${esc(placeName)}</div></div>
    <div class="detail">${items.join("")}<p class="caveat">${caveat}</p></div>`;
  renderList();
}

/* ---------- nearby list ---------- */
function renderList() {
  const col = { ban: "--ban", restricted: "--restr", lifted: "--clear", alert: "--alert" };
  const F = S.filters;
  const rows = [];
  for (const f of S.bans?.features || []) {
    const q = f.properties;
    if (q.status === "none" || !F[q.status]) continue;
    rows.push({ g: f.geometry, kind: q.status, name: where(q), sub: q.label, upd: q.updated });
  }
  if (F.alert) for (const f of S.alerts?.features || []) {
    rows.push({ g: f.geometry, kind: "alert", name: f.properties.event, sub: (f.properties.area || "").split(";")[0], upd: null });
  }
  if (F.report) for (const r of S.reports) {
    rows.push({ g: { type: "Point", coordinates: [r.lng, r.lat] }, kind: r.status, name: `${r.area}${r.state ? ", " + r.state : ""}`, sub: `${LABEL[r.status]} · community report`, upd: r.reported });
  }
  const ref = S.focus;
  for (const r of rows) r.d = ref ? distTo(r.g, ref) : null;
  rows.sort((a, b) => (ref ? a.d - b.d : a.name.localeCompare(b.name)));

  $("listTitle").textContent = ref ? "In effect near this spot" : "In effect now";
  const shown = rows.slice(0, 25);
  $("list").innerHTML = rows.length
    ? shown.map((r, i) => `<li><button type="button" data-i="${i}">
        <i style="--c:var(${col[r.kind] || "--report"})"></i><span class="n">${esc(r.name)}</span>
        <span class="d">${r.d == null ? "" : r.d === 0 ? "here" : Math.round(r.d) + " mi"}</span>
        <span class="s">${esc(r.sub)}${r.upd ? " · " + esc(ago(r.upd)) : ""}</span></button></li>`).join("") +
      (rows.length > shown.length ? `<li class="more">and ${rows.length - shown.length} more on the map</li>` : "")
    : `<li class="empty">${S.bans ? "Nothing active for the layers you have on." : "Loading…"}</li>`;
  $("list").onclick = (e) => {
    const b = e.target.closest("button[data-i]");
    if (!b) return;
    const g = shown[+b.dataset.i].g;
    const layer = L.geoJSON({ type: "Feature", geometry: g });
    const bounds = layer.getBounds();
    if (g.type === "Point") map.flyTo([g.coordinates[1], g.coordinates[0]], 10);
    else map.flyToBounds(bounds, { maxZoom: 10, padding: [30, 30] });
  };
}

const dayAgo = (ms) => {
  if (!ms) return "date unknown";
  const d = Math.floor((Date.now() - ms) / 864e5);
  return d < 1 ? "today" : d === 1 ? "yesterday" : d < 45 ? `${d} days ago` : new Date(ms).toLocaleDateString(undefined, { month: "short", year: "numeric" });
};

function renderFeeds() {
  const cov = S.bans?.coverage || [];
  $("feeds").innerHTML = cov.map((c) => {
    const tip = c.ok
      ? `${c.source}. Data ${c.kind === "order" ? "in effect since" : "last edited"} ${dayAgo(c.edited)}.${c.kind === "order" ? " Re-checked against the agency's page on every refresh." : ""}${c.warn ? " " + c.warn : ""}`
      : `${c.source}: ${c.error}`;
    return c.ok
      ? `<li class="${c.warn ? "warn" : ""}" title="${esc(tip)}"><a href="${esc(c.home)}" target="_blank" rel="noopener"><b>${esc(c.state)}</b></a> ${c.active ? `${c.active} active` : "none active"}${c.warn ? " ⚠" : ""}</li>`
      : `<li class="down" title="${esc(tip)}"><b>${esc(c.state)}</b> ${/^stale/.test(c.error || "") ? "stale, hidden" : "unavailable"}</li>`;
  }).join("");
  const live = liveStates();
  $("feedNote").textContent = cov.length
    ? `${live.size} states have live data. A source is only shown while it's fresh: layers that stop being updated are hidden, not trusted. Hover a chip for its data age. Fire weather alerts come from the National Weather Service. For the rest, use each state's official source below or report a ban you know about.`
    : "";
  renderStates(live);
}

function renderStates(live = new Set()) {
  $("dir").innerHTML = Object.entries(S.states).sort((x, y) => x[1].name.localeCompare(y[1].name)).map(([code, s]) =>
    `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${live.has(code) ? '<span class="live" title="Live data on the map"></span>' : ""}${esc(s.name)}</a><span class="agency">${esc(s.agency)}</span></li>`).join("");
}

/* ---------- focus (spot being checked) ---------- */
function setFocus(lngLat, { fly = false, zoom = 9, isMe = false } = {}) {
  S.focus = lngLat;
  if (isMe) S.me = lngLat;
  if (spotMarker) spotMarker.remove();
  if (!isMe) spotMarker = L.marker([lngLat[1], lngLat[0]], { icon: dotIcon("spot", css("--ink")), interactive: false }).addTo(map);
  if (fly) map.flyTo([lngLat[1], lngLat[0]], Math.max(map.getZoom(), zoom), { duration: 0.8 });
  renderHere();
  renderList();
}

/* ---------- geolocation ---------- */
function locate({ quiet = false } = {}) {
  return new Promise((resolve) => {
    if (!navigator.geolocation) { if (!quiet) toast("This browser can't share your location."); return resolve(null); }
    const btn = $("locate");
    btn.disabled = true;
    navigator.geolocation.getCurrentPosition((pos) => {
      btn.disabled = false;
      const ll = [pos.coords.longitude, pos.coords.latitude];
      if (meMarker) meMarker.remove();
      meMarker = L.marker([ll[1], ll[0]], { icon: dotIcon("me", "#1B6EF3"), interactive: false, keyboard: false }).addTo(map);
      if (spotMarker) { spotMarker.remove(); spotMarker = null; }
      setFocus(ll, { fly: true, zoom: 8, isMe: true });
      resolve(ll);
    }, (err) => {
      btn.disabled = false;
      if (!quiet) toast(err.code === 1 ? "Location is blocked. Allow it in your browser, or search a place." : "Couldn't get your location. Try searching a place.");
      resolve(null);
    }, { enableHighAccuracy: false, timeout: 12000, maximumAge: 300000 });
  });
}

/* ---------- search ---------- */
$("search").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("q").value.trim();
  if (!q) return;
  try {
    const r = await fetch(`https://nominatim.openstreetmap.org/search?format=jsonv2&countrycodes=us&limit=1&q=${encodeURIComponent(q)}`);
    const hit = (await r.json())[0];
    if (!hit) return toast(`Couldn't find "${q}" in the US.`);
    const ll = [parseFloat(hit.lon), parseFloat(hit.lat)];
    if (hit.boundingbox) {
      const [s, n, w, ea] = hit.boundingbox.map(Number);
      map.flyToBounds([[s, w], [n, ea]], { maxZoom: 10, duration: 0.8 });
    }
    setFocus(ll);
  } catch { toast("Search is unavailable right now."); }
});
$("locate").onclick = () => locate();

map.on("click", (e) => {
  const ll = [e.latlng.lng, e.latlng.lat];
  if (S.pick) return openReport(ll);
  setFocus(ll);
});

/* ---------- filters ---------- */
$("filters").addEventListener("change", (e) => {
  S.filters[e.target.dataset.f] = e.target.checked;
  render();
});

/* ---------- reports ---------- */
const stateSel = $("r-state");
stateSel.innerHTML = `<option value="" disabled selected>—</option>` + STATES.map((s) => `<option>${s}</option>`).join("");

function startPick() {
  S.pick = true;
  $("map").classList.add("picking");
  $("pickbar").hidden = false;
}
function endPick() {
  S.pick = false;
  $("map").classList.remove("picking");
  $("pickbar").hidden = true;
}
$("report").onclick = startPick;
$("pickCancel").onclick = endPick;
$("pickHere").onclick = async () => {
  const ll = S.me || (await locate());
  if (ll) openReport(ll);
};

async function openReport(ll) {
  endPick();
  S.pending = ll;
  $("reportForm").reset();
  $("r-err").hidden = true;
  $("r-loc").textContent = `Location: ${ll[1].toFixed(3)}, ${ll[0].toFixed(3)}`;
  $("reportDlg").showModal();
  const place = await reverse(ll);
  if (place) {
    if (!$("r-area").value) $("r-area").value = place.county;
    if (place.code) stateSel.value = place.code;
  }
}
$("r-cancel").onclick = () => $("reportDlg").close();
$("reportForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    lat: S.pending[1], lng: S.pending[0], area: $("r-area").value, state: stateSel.value,
    status: document.querySelector("input[name=r-status]:checked").value,
    note: $("r-note").value, url: $("r-url").value,
  };
  const btn = $("r-submit");
  btn.disabled = true;
  try {
    const res = await fetch("api/reports", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Couldn't save the report.");
    $("reportDlg").close();
    await loadReports();
    toast("Report saved");
    setFocus(S.pending);
  } catch (err) {
    const box = $("r-err");
    box.textContent = err.message; box.hidden = false;
  } finally { btn.disabled = false; }
});

/* ---------- data freshness ---------- */
function renderFresh() {
  const el = $("fresh");
  if (!S.bans) { el.textContent = "Loading live data…"; return; }
  const mins = Math.max(0, Math.round((Date.now() - S.bans.fetched) / 60000));
  const cov = S.bans.coverage, ok = cov.filter((c) => c.ok).length;
  const old = mins > 30;
  el.className = "fresh" + (old ? " old" : "");
  $("freshText").textContent = `${mins < 1 ? "Just checked" : `Checked ${mins} min ago`} · ${ok}/${cov.length} sources live · auto-refresh 5 min${S.alerts?.unavailable ? " · weather alerts unavailable" : ""}${old ? " (update is running late)" : ""}`;
}

// Tribal/other layers carry no state: infer it from where they sit.
function tagStates() {
  if (!S.usa || !S.bans) return;
  const byName = Object.fromEntries(Object.entries(S.states).map(([c, s]) => [s.name, c]));
  for (const f of S.bans.features) {
    if (STATES.includes(f.properties.state)) continue;
    const pt = verts(f.geometry).next().value;
    const hit = pt && S.usa.features.find((s) => contains(s.geometry, pt));
    if (hit && byName[hit.properties.name]) f.properties.state = byName[hit.properties.name];
  }
}

/* ---------- data loading ---------- */
async function getJson(url) {
  // Relative URLs so it works from a sub-path; the minute-stamp defeats CDN caching without hammering the origin.
  const r = await fetch(url.startsWith("api/") ? `${url}?_=${Math.floor(Date.now() / 60000)}` : url, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}
async function loadReports() {
  try { S.reports = await getJson("api/reports.json"); } catch { S.reports = []; }
  render();
}
let lastLoad = 0;
async function loadLive() {
  const [bans, alerts] = await Promise.allSettled([getJson("api/bans.json"), getJson("api/alerts.json")]);
  if (bans.status === "fulfilled") { S.bans = bans.value; lastLoad = Date.now(); tagStates(); }
  else toast("Couldn't reach the state burn ban feeds. Showing what's already loaded.");
  if (alerts.status === "fulfilled") S.alerts = alerts.value;
  renderFeeds();
  render();
  renderFresh();
  if (S.focus) renderHere();
}

async function applyConfig() {
  let cfg = { reports: true };
  try { cfg = await getJson("api/config.json"); } catch { /* older server: keep reports on */ }
  if (cfg.reports) return;
  S.filters.report = false;
  document.querySelector('[data-f="report"]').closest(".chip").hidden = true;
  document.querySelector(".panel-foot").hidden = true;
}

(async function init() {
  await applyConfig();
  try { S.states = await getJson("states.json"); } catch { S.states = {}; }
  getJson("us-states.json").then((j) => { S.usa = j; tagStates(); render(); }).catch(() => {});
  renderStates();
  renderHere();
  await Promise.all([loadReports(), loadLive()]);
  setInterval(loadLive, 5 * 60 * 1000);
  setInterval(renderFresh, 30 * 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && Date.now() - lastLoad > 90 * 1000) loadLive(); });
  $("refresh").onclick = async () => { $("refresh").disabled = true; await loadLive(); $("refresh").disabled = false; };
  locate({ quiet: true });
})();
