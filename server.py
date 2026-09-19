#!/usr/bin/env python3
"""Burn Ban Finder: serves the app and proxies live public data (stdlib only).

  /api/bans     county burn bans / fire restrictions from state agency feeds
  /api/alerts   National Weather Service Red Flag Warnings & fire weather alerts
  /api/reports  community-submitted bans, stored in data/reports.json
"""
import datetime
import gzip
import html as htmlmod
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
REPORTS_FILE = os.path.join(ROOT, "data", "reports.json")
UA = "BurnBanFinder/1.0 (local app)"
PORT = int(os.environ.get("PORT", "8765"))
REFRESH_EVERY = 5 * 60   # background refresh: the map is never more than ~5 minutes behind its sources
BANS_TTL = 10 * 60
ALERTS_TTL = 10 * 60
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
HOST = os.environ.get("HOST", "127.0.0.1")
REPORTS_ON = os.environ.get("REPORTS", "on") != "off"  # hosted deployments turn this off: a public write endpoint invites spam

pool = ThreadPoolExecutor(max_workers=12)


def http_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/geo+json, application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def http_bytes(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def norm(name):  # "Deaf Smith County" / "DEAF SMITH" / "Saint Francis" / "St. Francis County" all compare equal
    s = re.sub(r"\s+(county|parish)$", "", str(name).lower().strip())
    return re.sub(r"[^a-z]", "", re.sub(r"\bsaint\b", "st", s))


def ms(v):
    return int(v) if isinstance(v, (int, float)) else None


# --- State feeds -------------------------------------------------------------
# Each parser turns one feature's attributes into
#   {name, status: ban|restricted|none, label, detail, updated, url?, point?}

def parse_tx(a):
    ban = a.get("BurnBan") == "Yes"
    detail = " ".join(x for x in [a.get("Comments"), "Fireworks ban also in effect." if a.get("Fireworks_Ban") == "Yes" else ""] if x)
    return dict(name=f"{a['NAME']} County", status="ban" if ban else "none",
                label="Burn ban" if ban else "No burn ban", detail=detail, updated=ms(a.get("Start_date")))


def parse_ok(a):
    v = a.get("Burn_Ban_Status")
    ban = v not in (None, "", "None")
    name = a.get("CountyName") or a.get("county_nam")
    return dict(name=f"{name} County", status="ban" if ban else "none",
                label="Burn ban" if ban else "No burn ban", detail="", updated=ms(a.get("EditDate")))


def parse_la(a):
    ban = str(a.get("Burn_Ban1") or "none").lower() not in ("none", "")
    return dict(name=f"{a['Parish1']} Parish", status="ban" if ban else "none",
                label="Burn ban" if ban else "No burn ban", detail="", updated=ms(a.get("EditDate")))


def parse_fl(a):
    v = str(a.get("Burn_Ban") or "")
    fw = "Fireworks ban also in effect." if str(a.get("Fireworks_Ban") or "").lower() in ("yes", "y") else ""
    if v.lower() in ("", "no burn ban", "none"):
        st, label = "none", "No burn ban"
    elif "yard" in v.lower():
        st, label = "restricted", "Yard debris burning ban"
    else:
        st, label = "ban", v
    return dict(name=f"{str(a['NAME']).title()} County", status=st, label=label, detail=fw, updated=ms(a.get("DateUpdated")))


def parse_tn(a):
    v = a.get("Ban_Status")
    ban = v not in (None, "", "None", "none", "No", "NO")
    return dict(name=f"{a['NAME']} County", status="ban" if ban else "none",
                label="Burn ban" if ban else "No burn ban", detail="" if not ban else str(v), updated=None)


def parse_ia(a):
    ban = bool(a.get("Active_Burn_Ban")) and str(a.get("Active_Burn_Ban")).lower() not in ("none", "no", "0")
    if not ban:
        return None
    pt = [a["longitude"], a["latitude"]] if a.get("longitude") is not None else None
    return dict(name=a["County"], status="ban", label="Burn ban", detail="", updated=None,
                url=a.get("proc_url") or a.get("request_url"), point=pt)


NV_STAGES = {
    "Stage_1": "Stage 1 fire restrictions",
    "Stage_2": "Stage 2 fire restrictions",
    "Prevention_Order": "Fire prevention order",
    "Shooting": "Target shooting restriction",
}


def parse_nv(a):
    area = a.get("FireRestrictionArea") or "Nevada area"
    zone = a.get("FireRestrictionZone")
    if len(area) <= 3 and zone:  # bare codes like "A" mean nothing on their own
        a = dict(a, FireRestrictionArea=f"{zone} area {area}")
    st = a.get("FireRestrictionStage")
    if st not in NV_STAGES:
        return dict(name=a.get("FireRestrictionArea") or "Nevada area", status="none", label="No restrictions",
                    detail="", updated=ms(a.get("FireRestrictionDate")))
    return dict(name=a.get("FireRestrictionArea") or "Nevada area", status="restricted", label=NV_STAGES[st],
                detail=a.get("Notes") or "", updated=ms(a.get("FireRestrictionDate")), url=a.get("FireRestrictionOrderLink"))


def epoch(day):  # "2026-09-04" -> ms
    try:
        return int(datetime.datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=datetime.timezone.utc).timestamp() * 1000)  # noon UTC: same calendar day everywhere in the US
    except (TypeError, ValueError):
        return None


def parse_ms(a):
    ban = a.get("hasCountyBan") == 1 or a.get("hasMFCBan") == 1
    notes = [t for k, t in (("hasRedFlag", "Red flag conditions."), ("hasAQWarning", "Air quality warning.")) if a.get(k) == 1]
    return dict(name=f"{a['NAME']} County", status="ban" if ban else "none",
                label=("State Forestry Commission burn ban" if a.get("hasMFCBan") == 1 else "County burn ban") if ban else "No burn ban",
                detail=" ".join(notes), updated=None)


def parse_wa(a):
    cd = a.get("BURN_BAN_LEVEL_CD")
    label, st = {1: ("Rule burns banned", "restricted"), 3: ("Rule, permit and recreation burns banned", "ban"),
                 4: ("Burn restrictions in effect", "restricted")}.get(cd, ("No burn ban", "none"))
    notes = (a.get("NOTES_TXT") or "").strip()
    detail = f"Fire danger: {a.get('FIRE_DANGER_LEVEL_NM')}. {notes[:300]}".strip() if st != "none" else ""
    return dict(name=a.get("FIREDANGER_AREA_NM") or "Washington DNR area", status=st, label=label,
                detail=(detail + " Applies to lands protected by DNR.").strip() if st != "none" else "", updated=None)


def parse_mt(a):
    rt = a.get("RestrictionType")
    if a.get("CLASS") == "WATER" or not rt:
        return None
    name = a.get("AreaSubUnit") or a.get("AreaName") or "Montana area"
    if rt.startswith("No Restrictions"):
        return dict(name=name, status="none", label="No restrictions", detail="", updated=ms(a.get("EditDate")))
    return dict(name=name, status="restricted", label=f"{rt} fire restrictions" if rt.startswith("Stage") else rt,
                detail="", updated=ms(a.get("EditDate")), url=a.get("Restriction_Document"))


UT_LABELS = {"Stage 1": "Stage 1 fire restrictions", "Stage 2": "Stage 2 fire restrictions", "Prevention Order": "Fire prevention order",
             "Closure": "Fire closure", "NPS Order": "Fire restriction order", "Special Order": "Special fire order"}


def parse_ut(a):
    rt = a.get("RestrictionType") or "Order"
    name = a.get("Short_AreaDescription") or (a.get("AreaDescription") or "Utah area")[:80]
    return dict(name=name, status="restricted", label=UT_LABELS.get(rt, rt),
                detail="Fireworks also restricted." if a.get("Fireworks") else "", updated=epoch(a.get("EffectiveDate")), url=a.get("Link"))


def parse_wy(a):
    st = a.get("restriction_status")
    name = f"{a.get('county_name')} ({str(a.get('LandownerC') or 'private').lower()} land)"
    if st in ("Stage 1", "Stage 2"):
        return dict(name=name, status="restricted", label=f"{st} fire restrictions", detail="", updated=None, url=a.get("restriction_order"))
    return dict(name=name, status="none", label="No restrictions", detail="", updated=None)


STATE_CODES = {"Colorado": "CO", "Nebraska": "NE", "Wyoming": "WY", "South Dakota": "SD", "Kansas": "KS", "Utah": "UT",
               "Montana": "MT", "North Dakota": "ND", "New Mexico": "NM", "Idaho": "ID"}


def parse_blm(a):  # BLM Rocky Mountain area; "Year Round" rows are standing rules, not seasonal restrictions
    st = a.get("restriction_status") or ""
    if not st.startswith("Stage"):
        return None
    return dict(name=f"BLM land, {a.get('county_name')}", state=STATE_CODES.get(a.get("state"), "US"), status="restricted",
                label=f"{st} fire restrictions", detail="Federal land managed by the Bureau of Land Management.",
                updated=ms(a.get("last_edited_date")), url=a.get("restriction_order"))


OR_DANGER = {1: "Low", 2: "Moderate", 3: "High", 4: "Extreme"}


def parse_or(a):
    danger = OR_DANGER.get(a.get("firedanger"), "unknown")
    name = f"ODF fire area {a.get('regusearea') or ''}".strip()
    if (a.get("_join") or {}).get("reguseineffect") == 1:
        return dict(name=name, status="restricted", label="Regulated use restrictions in effect", updated=None,
                    detail=f"Fire danger: {danger}. Industrial fire precaution level {a.get('ifplrestrictionlevel')}. "
                           "These rules cover ODF-protected land; ask your ODF district what they include.")
    return dict(name=name, status="none", label="No regulated-use restrictions", detail=f"Fire danger: {danger}.", updated=None)


R3_STATE = {"Cibola": "NM", "Santa Fe": "NM", "Carson": "NM", "Gila": "NM", "Lincoln": "NM", "Apache": "AZ", "Coconino": "AZ",
            "Coronado": "AZ", "Kaibab": "AZ", "Prescott": "AZ", "Tonto": "AZ"}


def parse_r3(a):  # Forest Service Southwestern Region: a row only counts while today is inside its start/end dates
    now = time.time() * 1000
    start, end, resc = ms(a.get("startdate")), ms(a.get("enddate")), ms(a.get("rescinddate"))
    if (resc and resc <= now) or (start and start > now) or (end and end + 864e5 < now):
        return None
    forest = a.get("forestname") or "National Forest"
    stage = str(a.get("ordertype") or "Fire restriction").replace("Fire Restriction - ", "")
    return dict(name=f"{forest} ({a.get('ordername') or 'fire order'})", state=next((v for k, v in R3_STATE.items() if k in forest), "US"),
                status="restricted", label=f"{stage} fire restrictions", detail=(a.get("description") or "")[:240].strip(),
                updated=ms(a.get("rev_date")), url=a.get("hyperlink"))


def parse_bia(a):
    st = a.get("Stage")
    if not st or str(st).strip().lower() == "none":
        return None
    return dict(name=f"{a.get('IND_NAME') or 'Tribal land'} ({a.get('AGENCY_NAM') or 'BIA'})", state="US", status="restricted",
                label=f"{st} fire restrictions", detail=(a.get("Notes") or "")[:240].strip(), updated=epoch(a.get("Start_Date")),
                url=a.get("Website") if str(a.get("Website") or "").startswith("http") else None)


TFS_URL = "https://tfsfrp.tamu.edu/WILDFIRES/BURNBAN.txt"


def apply_tfs(feats):
    """Texas: counties come from a map layer, but the ban list itself comes from Texas A&M Forest Service's own daily file."""
    text = http_bytes(TFS_URL).decode("utf-16")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    m = re.match(r"(\d+)\s+Texas Burn Bans\s*-\s*(\d+/\d+/\d+ \d+:\d+:\d+ [AP]M)", lines[0])
    if not m:
        raise RuntimeError("unexpected Texas A&M Forest Service file format")
    try:
        tz = ZoneInfo("America/Chicago")
    except Exception:  # no tz database on the host: the list is stamped in Central time, use its daylight offset
        tz = datetime.timezone(datetime.timedelta(hours=-5))
    stamp = datetime.datetime.strptime(m.group(2), "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=tz)
    edited = int(stamp.timestamp() * 1000)
    if time.time() * 1000 - edited > 3 * 864e5:
        raise RuntimeError(f"Texas A&M Forest Service list is stale ({m.group(2)})")
    banned = {norm(l) for l in lines[1:]}
    known = {norm(f["properties"]["name"]) for f in feats}
    missing = banned - known
    for f in feats:
        p = f["properties"]
        ban = norm(p["name"]) in banned
        p.update(status="ban" if ban else "none", label="Burn ban" if ban else "No burn ban", detail="", updated=edited,
                 source="Texas A&M Forest Service", home="https://tfsweb.tamu.edu/burnbans/")
    warn = f"{len(missing)} listed counties didn't match the map" if missing else None
    return feats, edited, warn


# Orders that no map layer carries. Each is re-checked against the agency's own page on every refresh;
# if the page stops saying what we recorded, the order drops off the map instead of being asserted blindly.
ORDERS = [
    dict(id="nm-statewide-2026", state="NM", full="New Mexico", source="New Mexico State Forester", geom="New Mexico",
         home="https://www.emnrd.nm.gov/sfd/find-current-fire-restrictions/", effective="2026-04-06", confirmed_on="2026-09-18",
         status="ban", label="Statewide fire restrictions", name="New Mexico (state, private and county land)",
         detail="Smoking, fireworks, campfires, and prescribed, open, agricultural and debris burning are prohibited on non-federal, "
                "non-Tribal, non-municipal land. Federal, Tribal and city land follow their own orders. In effect until the State Forester rescinds it.",
         verify=dict(url="https://www.emnrd.nm.gov/sfd/find-current-fire-restrictions/",
                     must=[r"Statewide Fire Restrictions", r"remain in place until rescinded"],
                     must_not=[r"(has|have) been rescinded", r"were rescinded", r"restrictions (were|are|have been) lifted"])),
    dict(id="wa-statewide-2026", state="WA", full="Washington", source="Washington Governor's proclamation", geom="Washington",
         home="https://governor.wa.gov/news/2026/governor-ferguson-declares-statewide-wildfire-emergency-issues-statewide-burn-ban",
         effective="2026-08-01", expires="2026-09-30", confirmed_on="2026-09-18", status="ban",
         label="Statewide burn ban (Governor's order)", name="Washington (statewide)",
         detail="Most outdoor and agricultural burning is prohibited statewide through September 30, 2026, including yard waste, trash, weeds and "
                "bonfires. Campfires are allowed only in contained fire pits. Rules for specific places can be stricter.",
         verify=dict(url="https://governor.wa.gov/news/2026/governor-ferguson-declares-statewide-wildfire-emergency-issues-statewide-burn-ban",
                     must=[r"statewide prohibition on most outdoor and agricultural burning through September 30, 2026"], must_not=[])),
    dict(id="az-blm-2026", state="AZ", full="Arizona", source="BLM Arizona", geom=None,
         home="https://www.blm.gov/programs/public-safety-and-fire/fire/regional-info/arizona/fire-restrictions", effective="2026-09-08",
         confirmed_on="2026-09-18", status=None,
         text="BLM lifted its seasonal fire restrictions in every Arizona district (the last, Arizona Strip, on Sept. 8, 2026). "
              "Year-round rules still ban fireworks, exploding targets, sky lanterns and tracer ammunition on BLM land. "
              "National forests are checked live from the Forest Service feed. Arizona state-land, county and city rules are not covered by any "
              "feed this app can read, so check the county before you burn.",
         verify=dict(url="https://www.blm.gov/programs/public-safety-and-fire/fire/regional-info/arizona/fire-restrictions",
                     must=[r"Seasonal fire restrictions were lifted on Sept\. 8, 2026"],
                     must_not=[r"Stage (1|2|I|II) fire restrictions (are|remain) in effect"])),
]


# Hand-checked news and agency reports for states with no feed this app can read. Each carries the date it was checked and is
# dropped automatically after valid_days, so an old report disappears instead of posing as current.
MANUAL_NOTES = [
    dict(state="CA", checked_on="2026-09-18", valid_days=7, source="News reports (CAL FIRE's site blocks automated checks)",
         url="https://burnpermit.fire.ca.gov/current-burn-status",
         text="CAL FIRE has suspended residential burn permits in several units. Reported: Humboldt-Del Norte (debris burning suspended, "
              "no end date as of Sept. 1) and Nevada, Yuba, Placer and Sierra counties (since June 15). It differs by unit and changes fast, "
              "so check your unit's status."),
    dict(state="CO", checked_on="2026-09-18", valid_days=7, source="County sheriff notices",
         url="https://csfs.colostate.edu/wildfire-mitigation/current-wildfire-information-fire-restrictions/",
         text="Colorado has no statewide burn ban; each county, forest and BLM district decides. Summit County went from Stage 2 to Stage 1 "
              "on Sept. 11. Unincorporated Jefferson County had no restrictions as of Sept. 18."),
    dict(state="ND", checked_on="2026-09-18", valid_days=7, source="KFYR-TV (Sept. 1) and ND Response",
         url="https://ndresponse.gov/burn-restrictions-fire-danger-maps",
         text="North Dakota counties set their own burn restrictions. Ward, Burleigh, Stark and Williams counties and the Turtle Mountain "
              "Reservation have had bans, and fire danger was rising as of Sept. 1. The state's county map shows the current list."),
    dict(state="MO", checked_on="2026-09-18", valid_days=7, source="Local news (Sept. 1-8)",
         url="https://dfs.dps.mo.gov/programs/resources/county-burn-bans.php",
         text="Missouri has no statewide list; counties order their own bans. Reported in early September: Jasper (through Sept. 30), "
              "Barton, Polk, McDonald, Cedar and Vernon counties, plus the city of Bolivar. Ask your county commission."),
    dict(state="KS", checked_on="2026-09-18", valid_days=7, source="KWCH / KSN (Sept. 2-15)",
         url="https://www.kansasforests.org",
         text="Kansas counties set their own bans, and several have expired or been renewed. Reported in September: Barton (from Sept. 15), "
              "Kiowa and Comanche (until further notice), plus Sedgwick County Fire District 1 and its member cities."),
    dict(state="ID", checked_on="2026-09-18", valid_days=7, source="BLM and Idaho Dept. of Lands releases",
         url="https://www.idl.idaho.gov/fire-management/fire-restrictions-finder/",
         text="Idaho restrictions vary by area. Stage 1 covered most of the state in late August with Stage 2 in the north; the Boise area lifted "
              "Stage 1 on Sept. 3 and BLM's Coeur d'Alene District rescinded its restrictions. Use the state's Fire Restrictions Finder."),
    dict(state="NC", checked_on="2026-09-18", valid_days=7, source="NC Forest Service / NC Agriculture",
         url="https://www.ncagr.gov/news/press-releases/2026/05/07/state-issued-burn-ban-lifted-all-north-carolina-counties-fire-danger-moderates-following-recent",
         text="North Carolina's statewide burn ban was fully lifted on May 8, 2026 and no new state ban was reported in September. "
              "Counties and cities can still restrict burning."),
]


def check_page(v):
    """-> (verdict, why): confirmed | unreachable | changed | contradicted."""
    try:
        raw = http_bytes(v["url"]).decode("utf-8", "ignore")
    except Exception as e:
        return "unreachable", f"couldn't reach the official page ({type(e).__name__})"
    text = re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", raw, flags=re.S)))
    if any(re.search(pat, text, re.I) for pat in v.get("must_not", [])):
        return "contradicted", "the official page now says this was lifted or changed"
    if all(re.search(pat, text, re.I) for pat in v["must"]):
        return "confirmed", None
    return "changed", "the official page no longer matches what was recorded"


def build_orders():
    feats, coverage, notes = [], [], []
    try:
        with open(os.path.join(STATIC, "us-states.json")) as f:
            usa = {x["properties"]["name"]: x["geometry"] for x in json.load(f)["features"]}
    except Exception:
        usa = {}
    now = int(time.time() * 1000)
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    for o in ORDERS:
        if o.get("expires") and today > o["expires"]:
            continue  # the order has run its course
        verdict, why = check_page(o["verify"])
        ok = verdict in ("confirmed", "unreachable")
        warn = None
        if verdict == "unreachable":
            warn = f"Couldn't re-check the official page just now; last confirmed {o['confirmed_on']}."
        active = 0
        if ok and o.get("status") and usa.get(o["geom"]):
            feats.append({"type": "Feature", "geometry": usa[o["geom"]], "properties": dict(
                id=o["id"], name=o["name"], state=o["state"], status=o["status"], label=o["label"], detail=o["detail"],
                updated=epoch(o["effective"]), url=o["home"], source=o["source"], home=o["home"], broad=True)})
            active = 1
        if ok and o.get("text"):
            notes.append(dict(state=o["state"], text=o["text"], source=o["source"], url=o["home"], verified=now if verdict == "confirmed" else None,
                              checked_on=o["confirmed_on"], warn=warn))
        coverage.append(dict(state=o["state"], full=o["full"], source=o["source"], home=o["home"], ok=ok,
                             error=None if ok else f"{o['source']}: {why}", active=active, total=1 if o.get("status") else 0,
                             covers=[o["state"]] if ok else [], edited=epoch(o["effective"]),
                             verified=now if verdict == "confirmed" else None, warn=warn, kind="order"))
    for n in MANUAL_NOTES:
        age = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(n["checked_on"])).days
        if age <= n["valid_days"]:
            notes.append(dict(state=n["state"], text=n["text"], source=n["source"], url=n["url"], verified=None,
                              checked_on=n["checked_on"], warn=None, manual=True))
    return feats, coverage, notes


# --- County lists scraped from state pages, drawn on nationwide county outlines ------------------------------
COUNTIES = "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_Counties_Generalized_Boundaries/FeatureServer/0"
_county_cache = {}


def county_geoms(state_full):
    hit = _county_cache.get(state_full)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    qs = urllib.parse.urlencode({"where": f"STATE_NAME='{state_full}'", "outFields": "NAME", "f": "geojson", "outSR": 4326,
                                 "maxAllowableOffset": 0.01, "geometryPrecision": 3, "resultRecordCount": 500})
    gj = http_json(f"{COUNTIES}/query?{qs}")
    out = {f["properties"]["NAME"]: f["geometry"] for f in gj.get("features", []) if f.get("geometry")}
    if not out:
        raise RuntimeError(f"no county outlines for {state_full}")
    _county_cache[state_full] = (time.time(), out)
    return out


def page_text(url):
    raw = http_bytes(url).decode("utf-8", "ignore")
    raw = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S)
    return re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", raw))).replace("​", " ")


def central(naive):
    try:
        tz = ZoneInfo("America/Chicago")
    except Exception:
        tz = datetime.timezone(datetime.timedelta(hours=-5))
    return int(naive.replace(tzinfo=tz).timestamp() * 1000)


def fetch_ar():
    """Arkansas Dept. of Agriculture lists every county with a judge-issued burn ban, stamped with the time it was compiled."""
    text = page_text("https://mip.agri.arkansas.gov/agtools/Forestry/Fire_Info/Burn_Bans?show_districts=False")
    stamp = re.search(r"Burn Bans as of (\d+/\d+/\d+ \d+:\d+ [AP]M)", text)
    lst = re.search(r"Burn Ban Counties \((\d+)\)\s*(.*?)\s*Privacy Policy", text)
    if not stamp or not lst:
        raise RuntimeError("unexpected Arkansas burn ban page format")
    names = [n.strip() for n in lst.group(2).split(",") if n.strip()]
    if len(names) != int(lst.group(1)):
        raise RuntimeError(f"Arkansas list says {lst.group(1)} counties but has {len(names)}")
    edited = central(datetime.datetime.strptime(stamp.group(1), "%m/%d/%Y %I:%M %p"))
    if time.time() * 1000 - edited > 2 * 864e5:
        raise RuntimeError(f"Arkansas list is stale ({stamp.group(1)})")
    return dict(banned={norm(n): "County judge's burn ban." for n in names}, edited=edited, status="ban", label="Burn ban")


def fetch_ga():
    """Georgia EPD's seasonal yard-debris burning ban: 54 metro counties, May 1 to September 30."""
    text = page_text("https://epd.georgia.gov/air-protection-branch/open-burning-rules-georgia/summer-open-burning-ban")
    if not re.search(r"May 1.{0,40}September 30", text):
        raise RuntimeError("Georgia EPD page no longer describes the May 1 - September 30 ban")
    names = [n.strip() for chunk in re.findall(r"Counties included:\s*([^.]*)\.", text) for n in chunk.split(",") if n.strip()]
    if len(names) != 54:
        raise RuntimeError(f"expected 54 Georgia counties, found {len(names)}")
    today = datetime.datetime.now(datetime.timezone.utc).date()
    in_season = datetime.date(today.year, 5, 1) <= today <= datetime.date(today.year, 9, 30)
    banned = {norm(n): "Yard and land-clearing debris burning is banned (ozone rule). Campfires and cooking fires are still allowed." for n in names} if in_season else {}
    return dict(banned=banned, edited=epoch(f"{today.year}-05-01"), status="restricted", label="Summer yard-debris burning ban")


_WI_STOP = re.compile(r"(burning (permits? )?(is|are) (suspended|prohibited|not allowed)|no burning|not (being )?issu)", re.I)


def fetch_wi():
    """Wisconsin DNR publishes fire danger and burn permit rules for all 72 counties as JSON."""
    rows = json.loads(http_bytes("https://apps.dnr.wi.gov/forestryapps/burnrestriction/json/").decode("utf-8"))
    if len(rows) < 60:
        raise RuntimeError("Wisconsin burn restriction data looks incomplete")
    stamps = [int(re.search(r"\d+", r["LAST_UPDATE_DATE"]).group()) for r in rows if r.get("LAST_UPDATE_DATE")]
    banned = {}
    for r in rows:
        txt = r.get("PERMIT_RESTRICTIONS") or ""
        if (r.get("DANGER_RATING_CODE") or 0) >= 4 or _WI_STOP.search(txt):
            banned[norm(r["COUNTY_NAME"])] = (txt[:240] or f"Fire danger: {r.get('DANGER_RATING_NAME')}")
    return dict(banned=banned, edited=max(stamps) if stamps else None, status="restricted", label="Burn permits suspended or fire danger very high")


def load_county_feed(cf):
    edited = None
    try:
        info = cf["county_fetch"]()
        edited = info["edited"]
        geoms = county_geoms(cf["full"])
        feats, matched = [], set()
        for name, g in geoms.items():
            key = norm(name)
            hit = key in info["banned"]
            matched.add(key) if hit else None
            feats.append({"type": "Feature", "geometry": g, "properties": dict(
                id=f"{cf['state']}-{key}", name=name, state=cf["state"], status=info["status"] if hit else "none",
                label=info["label"] if hit else "No burn ban", detail=info["banned"].get(key, "") if hit else "", updated=edited,
                source=cf["source"], home=cf["home"])})
        missing = set(info["banned"]) - matched
        warn = f"{len(missing)} listed counties didn't match the map ({', '.join(sorted(missing))[:80]})" if missing else None
        return cf, feats, None, edited, warn
    except Exception as e:
        return cf, [], f"{type(e).__name__}: {e}"[:160], edited, None


ARC = "https://services{n}.arcgis.com/{org}/arcgis/rest/services/{svc}/FeatureServer/{layer}"
FEEDS = [
    dict(state="TX", full="Texas", source="Texas A&M Forest Service", home="https://tfsweb.tamu.edu/burnbans/", parse=parse_tx, overlay=apply_tfs,
         url=ARC.format(n=7, org="hGTLI6IggobDDKZV", svc="Texas_Count_Burn_Bans", layer=0)),
    dict(state="OK", full="Oklahoma", source="Oklahoma Forestry Services", home="https://forestry.ok.gov", parse=parse_ok,
         url=ARC.format(n=3, org="yrIZ0Nv0mSGTWJsH", svc="OklahomaBurnBan__Data_Layer__v_9_0__View", layer=0)),
    dict(state="LA", full="Louisiana", source="Louisiana Dept. of Agriculture & Forestry", home="https://www.ldaf.la.gov", parse=parse_la,
         url=ARC.format(n=3, org="uymqJ6hE4wvB3E9t", svc="Burn_Ban1", layer=3)),
    dict(state="FL", full="Florida", source="Florida Forest Service", home="https://www.fdacs.gov/Divisions-Offices/Florida-Forest-Service", parse=parse_fl,
         url=ARC.format(n=3, org="XYg2eF8UuxZVuVmF", svc="Burn_Ban_Public_View_v2_4118cfe50dba49efa3d6fa7326d5a87f", layer=0)),
    dict(state="TN", full="Tennessee", source="Tennessee Division of Forestry", home="https://www.tn.gov/agriculture/forests.html", parse=parse_tn,
         url=ARC.format(n="", org="lvPBAGXeSupVUvx2", svc="Burn_Ban_WFL1", layer=0)),
    dict(state="IA", full="Iowa", source="Iowa Dept. of Public Safety", home="https://dps.iowa.gov", parse=parse_ia,
         url=ARC.format(n="", org="vPD5PVLI6sfkZ5E4", svc="Active_Burn_Bans_(View)", layer=0)),
    dict(state="NV", full="Nevada", source="Nevada Fire Info", home="https://www.nevadafireinfo.org", parse=parse_nv,
         url=ARC.format(n=3, org="T4QMspbfLg3qTGWY", svc="Nevada_Fire_Restrictions_Public_View", layer=0)),
    dict(state="WA", full="Washington", source="Washington DNR", home="https://www.dnr.wa.gov", parse=parse_wa,
         url="https://gis.dnr.wa.gov/site3/rest/services/Public_Wildfire/WADNR_PUBLIC_WD_WildfireDanger/MapServer/1"),
    dict(state="MT", simplify=0.02, full="Montana", source="Montana DNRC", home="https://dnrc.mt.gov", parse=parse_mt,
         url=ARC.format(n=2, org="DRQySz3VhPgOv7Bo", svc="Fire_Restrictions_by_Jurisdiction_Update_-_Read_Only_view", layer=3)),
    dict(state="UT", simplify=0.02, full="Utah", source="Utah Forestry, Fire & State Lands", home="https://ffsl.utah.gov", parse=parse_ut,
         where="Status IN ('Active','Future_Rescind') AND Campfire_etc=1",
         url=ARC.format(n="", org="ZzrwjTRez6FJiOq4", svc="Fire_Restrictions", layer=0)),
    dict(state="WY", simplify=0.02, full="Wyoming", source="Wyoming county fire restrictions (BLM / State Forestry)", home="https://sfd.wyo.gov", parse=parse_wy,
         url=ARC.format(n=3, org="T4QMspbfLg3qTGWY", svc="WY_County_Fire_Restrictions_(view)", layer=1)),
    dict(state="OR", full="Oregon", source="Oregon Dept. of Forestry", home="https://www.oregon.gov/odf/fire/", parse=parse_or, simplify=0.01,
         url="https://gis.odf.oregon.gov/odfags/rest/services/Hosted/Fire_Danger_Level_View/FeatureServer/0",
         join=dict(key="regusearea", url="https://gis.odf.oregon.gov/odfags/rest/services/Hosted/Fire_Regulated_Use_Area_View/FeatureServer/0")),
    dict(state="MS", full="Mississippi", source="Mississippi Forestry Commission", home="https://www.mfc.ms.gov", parse=parse_ms,
         url=ARC.format(n=5, org="hE6urTTXj32LRUqx", svc="MFC_Burn_Permits_County_Boundaries", layer=0)),
    # Federal land only: it can show a restriction but can't vouch for a spot being clear.
    dict(state="BLM", simplify=0.02, full="BLM Rocky Mountain Area", source="Bureau of Land Management", home="https://www.blm.gov", parse=parse_blm, covers=[],
         url=ARC.format(n=3, org="T4QMspbfLg3qTGWY", svc="Rocky_Mountain_Area_BLM_Fire_Restriction_Polygons_NEW_VIEW", layer=0)),
    dict(state="AR", full="Arkansas", source="Arkansas Dept. of Agriculture, Forestry Division", county_fetch=fetch_ar,
         home="https://mip.agri.arkansas.gov/agtools/Forestry/Fire_Info/Burn_Bans"),
    dict(state="GA", full="Georgia", source="Georgia Environmental Protection Division", county_fetch=fetch_ga,
         home="https://epd.georgia.gov/air-protection-branch/open-burning-rules-georgia/summer-open-burning-ban"),
    dict(state="WI", full="Wisconsin", source="Wisconsin DNR", county_fetch=fetch_wi,
         home="https://apps.dnr.wi.gov/forestryapps/burnrestriction"),
    # Seasonal restrictions change weekly, so these are only trusted while their layer has been edited recently.
    dict(state="USFS", full="Forest Service, Southwest (AZ/NM)", source="U.S. Forest Service, Southwestern Region", home="https://www.fs.usda.gov/r03",
         parse=parse_r3, covers=[], simplify=0.01, max_age_days=30,
         url="https://services1.arcgis.com/gGHDlz6USftL5Pau/arcgis/rest/services/r03_FireRestriction2/FeatureServer/0"),
    dict(state="BIA", full="BIA tribal land (Southwest/West)", source="Bureau of Indian Affairs", home="https://www.bia.gov/service/wildland-fire-management",
         parse=parse_bia, covers=[], simplify=0.01, max_age_days=30,
         url=ARC.format(n=3, org="T4QMspbfLg3qTGWY", svc="SW_BIA_Fire_Restrictions_Public", layer=0)),
]


def _rdp(pts, tol):
    """Douglas-Peucker on a list of [x, y] points (iterative, keeps endpoints)."""
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = pts[a][:2], pts[b][:2]
        dx, dy = x2 - x1, y2 - y1
        norm = (dx * dx + dy * dy) ** 0.5 or 1e-12
        far, idx = 0.0, None
        for i in range(a + 1, b):
            d = abs(dy * (pts[i][0] - x1) - dx * (pts[i][1] - y1)) / norm
            if d > far:
                far, idx = d, i
        if idx is not None and far > tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p[:2] for p, k in zip(pts, keep) if k]


def simplify_geom(g, tol):
    """Shrink polygons to ~tol degrees and drop specks; keeps at least the biggest part."""
    def ring(r):
        pts = [[round(p[0], 3), round(p[1], 3)] for p in r]
        # A closed ring has identical endpoints, so split it at the vertex farthest from the start first.
        far = max(range(len(pts)), key=lambda i: (pts[i][0] - pts[0][0]) ** 2 + (pts[i][1] - pts[0][1]) ** 2)
        if far == 0:
            return None
        out = _rdp(pts[: far + 1], tol)[:-1] + _rdp(pts[far:], tol)
        return out if len(out) >= 4 else None

    def poly(rings):
        outer = ring(rings[0])
        if not outer:
            return None
        return [outer] + [h for h in (ring(r) for r in rings[1:]) if h]

    def span(rings):
        xs, ys = [p[0] for p in rings[0]], [p[1] for p in rings[0]]
        return max(max(xs) - min(xs), max(ys) - min(ys))

    if g["type"] == "Polygon":
        r = poly(g["coordinates"])
        return {"type": "Polygon", "coordinates": r} if r else None
    if g["type"] == "MultiPolygon":
        parts = [r for r in (poly(p) for p in g["coordinates"]) if r]
        big = [r for r in parts if span(r) >= tol * 2]
        parts = big or sorted(parts, key=span, reverse=True)[:1]
        return {"type": "MultiPolygon", "coordinates": parts} if parts else None
    return g


def layer_edited(url):
    try:
        e = http_json(url + "?f=json").get("editingInfo") or {}
        return e.get("dataLastEditDate") or e.get("lastEditDate")
    except Exception:
        return None


def load_feed(feed):
    if feed.get("county_fetch"):
        return load_county_feed(feed)
    qs = urllib.parse.urlencode({
        "where": feed.get("where", "1=1"), "outFields": "*", "f": "geojson", "outSR": 4326,
        "geometryPrecision": 3, "maxAllowableOffset": 0.01, "resultRecordCount": 1000,
    })
    edited, warn = None, None
    try:
        gj = http_json(f"{feed['url']}/query?{qs}")
        if "error" in gj:
            raise RuntimeError(gj["error"].get("message", "service error"))
        joined = {}
        if feed.get("join"):  # second layer holding attributes for the same areas
            j = feed["join"]
            jq = urllib.parse.urlencode({"where": "1=1", "outFields": "*", "returnGeometry": "false", "f": "json", "resultRecordCount": 2000})
            joined = {f["attributes"][j["key"]]: f["attributes"] for f in http_json(f"{j['url']}/query?{jq}").get("features", [])}
        out = []
        for i, f in enumerate(gj.get("features", [])):
            a = f.get("properties") or {}
            if joined:
                a = dict(a, _join=joined.get(a.get(feed["join"]["key"]), {}))
            rec = feed["parse"](a)
            if not rec:
                continue
            geom = f.get("geometry")
            pt = rec.pop("point", None)
            if not geom and pt:
                geom = {"type": "Point", "coordinates": pt}
            if geom and feed.get("simplify"):
                geom = simplify_geom(geom, feed["simplify"])
            if not geom:
                continue
            rec.update(id=f"{feed['state']}-{i}", state=rec.get("state") or feed["state"], source=feed["source"], home=feed["home"])
            out.append({"type": "Feature", "geometry": geom, "properties": rec})
        seen = [layer_edited(feed["url"])] + [f["properties"].get("updated") for f in out]
        edited = max([x for x in seen if x] or [None]) if any(seen) else None
        if feed.get("overlay"):
            out, edited, warn = feed["overlay"](out)
        age_days = (time.time() * 1000 - edited) / 864e5 if edited else None
        limit = feed.get("max_age_days")
        if limit and (age_days is None or age_days > limit):
            when = time.strftime("%b %d, %Y", time.gmtime(edited / 1000)) if edited else "unknown"
            return feed, [], f"stale data (last edited {when}), so it isn't shown", edited, None
        active = sum(1 for f in out if f["properties"]["status"] != "none")
        if active and age_days is not None and age_days > 45:
            warn = f"Active bans listed, but this dataset was last edited {int(age_days)} days ago."
        return feed, out, None, edited, warn
    except Exception as e:  # one broken feed must not take the others down
        return feed, [], f"{type(e).__name__}: {e}"[:160], edited, None


_cache = {}
_cache_lock = threading.Lock()


def cached(key, ttl, loader):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    try:
        data = loader()
    except Exception:
        if hit:
            return hit[1]  # serve stale rather than fail
        raise
    with _cache_lock:
        _cache[key] = (time.time(), data)
    return data


def build_bans():
    features, coverage = [], []
    for feed, feats, err, edited, warn in pool.map(load_feed, FEEDS):
        active = sum(1 for f in feats if f["properties"]["status"] != "none")
        coverage.append(dict(state=feed["state"], full=feed["full"], source=feed["source"], home=feed["home"],
                             ok=err is None, error=err, active=active, total=len(feats), edited=edited, warn=warn,
                             covers=feed.get("covers", [feed["state"]]) if err is None else [], kind="feed"))
        features.extend(feats)
    o_feats, o_cov, notes = build_orders()
    features.extend(o_feats)
    coverage.extend(o_cov)
    if not features:
        raise RuntimeError("all feeds failed")
    return {"fetched": int(time.time() * 1000), "coverage": coverage, "notes": notes,
            "type": "FeatureCollection", "features": features}


def refresher():
    """Keep the cache warm so a page load never waits on the agencies, and data is never older than REFRESH_EVERY."""
    while True:
        for key, loader in (("bans", build_bans), ("alerts", build_alerts)):
            try:
                data = loader()
                with _cache_lock:
                    _cache[key] = (time.time(), data)
            except Exception as e:
                print(f"[refresh] {key} failed: {type(e).__name__}: {e}", flush=True)
        time.sleep(REFRESH_EVERY)


# --- NWS fire weather alerts -------------------------------------------------
NWS = "https://api.weather.gov"
ALERT_EVENTS = ["Red Flag Warning", "Fire Weather Watch", "Extreme Fire Danger", "Fire Warning"]
_zones = {}


def zone_geometry(url):
    if url in _zones:
        return _zones[url]
    try:
        g = http_json(url).get("geometry")
    except Exception:
        return None
    if g:
        _zones[url] = g
    return g


def build_alerts():
    qs = urllib.parse.urlencode({"event": ",".join(ALERT_EVENTS), "status": "actual"})
    data = http_json(f"{NWS}/alerts/active?{qs}")
    out = []
    for f in data.get("features", []):
        p = f["properties"]
        geom = f.get("geometry")
        if not geom:  # most fire alerts are zone-based: stitch the zone polygons together
            polys = []
            for g in pool.map(zone_geometry, p.get("affectedZones", [])):
                if g:
                    polys.extend(g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]])
            geom = {"type": "MultiPolygon", "coordinates": polys} if polys else None
        if not geom:
            continue
        out.append({"type": "Feature", "geometry": geom, "properties": {
            "id": p.get("id"), "event": p.get("event"), "headline": p.get("headline"),
            "area": p.get("areaDesc"), "starts": p.get("onset") or p.get("effective"), "ends": p.get("ends") or p.get("expires"),
            "office": p.get("senderName"), "description": (p.get("description") or "")[:1200],
            "instruction": (p.get("instruction") or "")[:600],
        }})
    return {"fetched": int(time.time() * 1000), "type": "FeatureCollection", "features": out}


# --- Community reports -------------------------------------------------------
_reports_lock = threading.Lock()
STATUSES = {"ban", "restricted", "lifted"}


def read_reports():
    try:
        with open(REPORTS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def write_reports(items):
    tmp = REPORTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(items, f, indent=1)
    os.replace(tmp, REPORTS_FILE)


def clean_report(b):
    lat, lng = float(b["lat"]), float(b["lng"])
    if not (17 <= lat <= 72 and -180 <= lng <= -64):
        raise ValueError("Location is outside the United States.")
    status = b.get("status")
    if status not in STATUSES:
        raise ValueError("Pick a status.")
    area = str(b.get("area", "")).strip()[:120]
    if not area:
        raise ValueError("Say which county or town this covers.")
    url = str(b.get("url", "")).strip()[:300]
    if url and not url.lower().startswith(("http://", "https://")):
        raise ValueError("Source link must start with http:// or https://")
    return dict(id=uuid.uuid4().hex[:10], lat=lat, lng=lng, status=status, area=area,
                state=str(b.get("state", ""))[:2].upper(), note=str(b.get("note", "")).strip()[:500],
                url=url, reported=int(time.time() * 1000))


# --- HTTP --------------------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=STATIC, **k)

    def log_message(self, fmt, *args):
        if self.path.startswith("/api/"):
            super().log_message(fmt, *args)

    def send_json(self, obj, code=200):
        body = json.dumps(obj, separators=(",", ":")).encode()
        gz = len(body) > 4096 and "gzip" in self.headers.get("Accept-Encoding", "")
        if gz:
            body = gzip.compress(body, 5)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/") and path.endswith(".json"):
            path = path[:-5]  # the hosted copy serves these as static files: /api/bans.json
        try:
            if path == "/api/bans":
                return self.send_json(cached("bans", BANS_TTL, build_bans))
            if path == "/api/alerts":
                return self.send_json(cached("alerts", ALERTS_TTL, build_alerts))
            if path == "/healthz":
                return self.send_json({"ok": True})
            if path == "/api/config":
                return self.send_json({"reports": REPORTS_ON})
            if path == "/api/reports":
                if not REPORTS_ON:
                    return self.send_json([])
                with _reports_lock:
                    return self.send_json(read_reports())
        except Exception as e:
            return self.send_json({"error": str(e)[:200]}, 502)
        super().do_GET()

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/reports":
            return self.send_json({"error": "not found"}, 404)
        if not REPORTS_ON:
            return self.send_json({"error": "Community reports are turned off on this site."}, 403)
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 10_000:
                raise ValueError("Report is too large.")
            item = clean_report(json.loads(self.rfile.read(n)))
        except (ValueError, KeyError, TypeError) as e:
            return self.send_json({"error": str(e) or "Invalid report."}, 400)
        with _reports_lock:
            items = read_reports()
            items.append(item)
            write_reports(items)
        self.send_json(item, 201)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/reports/"):
            return self.send_json({"error": "not found"}, 404)
        if not REPORTS_ON:
            return self.send_json({"error": "Community reports are turned off on this site."}, 403)
        rid = path.rsplit("/", 1)[1]
        with _reports_lock:
            items = read_reports()
            kept = [r for r in items if r["id"] != rid]
            write_reports(kept)
        self.send_json({"removed": len(items) - len(kept)})


def build_site(out):
    """Write a fully static copy of the site: the page plus the API responses as JSON files.
    Run on a schedule by GitHub Actions; the previous deploy stays up if this exits non-zero."""
    shutil.copytree(STATIC, out, dirs_exist_ok=True)
    api = os.path.join(out, "api")
    os.makedirs(api, exist_ok=True)
    bans = build_bans()  # raises if every feed failed
    try:
        alerts = build_alerts()
    except Exception as e:  # NWS being down shouldn't block ban updates; flag it so the page doesn't claim "no alerts"
        print(f"[build] alerts unavailable: {type(e).__name__}: {e}", flush=True)
        alerts = {"fetched": int(time.time() * 1000), "type": "FeatureCollection", "features": [], "unavailable": True}
    for name, data in (("bans", bans), ("alerts", alerts), ("config", {"reports": False}), ("reports", [])):
        with open(os.path.join(api, f"{name}.json"), "w") as f:
            json.dump(data, f, separators=(",", ":"))
    ok = sum(c["ok"] for c in bans["coverage"])
    print(f"[build] wrote {out}: {ok}/{len(bans['coverage'])} sources ok, {len(bans['features'])} areas, {len(alerts['features'])} alerts", flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--build":
        build_site(sys.argv[2])
        os._exit(0)
    os.makedirs(os.path.dirname(REPORTS_FILE), exist_ok=True)
    threading.Thread(target=refresher, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Burn Ban Finder running at http://localhost:{PORT}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
