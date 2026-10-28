"""
Real geography: any Indian city (OpenStreetMap Nominatim) and real road distance (OSRM).
Both are free public services; results are cached in data/geo_cache.json so a city or a lane is looked up once.
If a service is down, distance falls back to straight-line x road factor and says so.
"""
import json, math, threading, time
from pathlib import Path
import requests

CACHE_FILE = Path(__file__).parent / "data" / "geo_cache.json"
UA = {"User-Agent": "bhaada-freight-desk/1.0 (github.com/ymcankit/bhaada-freight-desk)"}
NOMINATIM = "https://nominatim.openstreetmap.org/search"
PHOTON = "https://photon.komoot.io/api/"      # second OpenStreetMap geocoder: Nominatim often throttles cloud hosts
OSRM = "https://router.project-osrm.org/route/v1/driving/{},{};{},{}?overview=false"
ROAD_FACTOR = 1.3
_lock = threading.Lock()                       # the cache
_net = threading.Lock()                        # geocoder requests, one at a time
_last_geocode = [0.0]
_missed = {}                                   # lookups that just failed -> when
_down = {"geocode": 0.0, "route": 0.0}         # service hanging (timeouts) -> skip it until then
DOWN_SECS = 60                                 # each hung try costs 10-20 s, often on the desk's one worker

def _load():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"cities": {}, "routes": {}}

_cache = _load()

def _save():
    try:
        CACHE_FILE.write_text(json.dumps(_cache, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass                                       # read-only disk on some hosts: the cache just lives in memory

def seed_cities(known):
    """Pre-load coordinates we already have ({name: [lat, lon]}) so common cities need no lookup."""
    for name, (lat, lon) in known.items():
        _cache["cities"].setdefault(name.lower(), {"name": name, "lat": lat, "lon": lon, "src": "seed"})

def resolve_city(text):
    """'bhiwandi', 'Bhiwandi (Mumbai)', 'Surat' -> {'name','lat','lon'} or None. India only."""
    if not text:
        return None
    key = str(text).strip().lower()
    with _lock:
        if key in _cache["cities"]:
            return _cache["cities"][key]
        for k, v in list(_cache["cities"].items()):    # 'Bhiwandi (Mumbai)' contains a known name
            if len(k) > 3 and k in key:
                return v
        if time.time() - _missed.get(key, 0) < 600 or time.time() < _down["geocode"]:   # just failed: don't spend another 20 s on it
            return None
    with _net:                                         # only lookups wait on lookups; cached cities never do
        with _lock:
            if key in _cache["cities"] or time.time() - _missed.get(key, 0) < 600 or time.time() < _down["geocode"]:
                return _cache["cities"].get(key)
        wait = 1.05 - (time.time() - _last_geocode[0])  # Nominatim policy: at most 1 request a second
        if wait > 0:
            time.sleep(wait)
        _last_geocode[0] = time.time()
        answered = False                               # "no such place" is remembered; "service down" is not
        try:
            r = requests.get(NOMINATIM, params={"q": text, "countrycodes": "in", "format": "json", "limit": 1},
                             headers=UA, timeout=10)
            hits = r.json() if r.ok else []
            answered = r.ok
        except Exception:
            hits = []
        hit = None
        if hits:
            h = hits[0]
            hit = {"name": (h.get("name") or str(text)).strip(), "lat": float(h["lat"]), "lon": float(h["lon"]), "src": "nominatim"}
        else:
            try:
                r = requests.get(PHOTON, params={"q": f"{text}, India", "limit": 5, "lang": "en"}, headers=UA, timeout=10)
                feats = [f for f in (r.json().get("features", []) if r.ok else []) if f["properties"].get("country") == "India"]
                answered = answered or r.ok
            except Exception:
                feats = []
            if feats:
                f = feats[0]
                lon, lat = f["geometry"]["coordinates"]
                hit = {"name": (f["properties"].get("name") or str(text)).strip(), "lat": lat, "lon": lon, "src": "photon"}
    if not hit:
        print("geocode failed for", repr(text)[:60], "" if answered else "(services unreachable)")
        if answered:
            if len(_missed) > 500:
                _missed.clear()
            _missed[key] = time.time()
        elif time.time() - _last_geocode[0] > 5:       # it hung (timeouts), not refused: skip it for a minute
            _down["geocode"] = time.time() + DOWN_SECS
        return None
    with _lock:
        _cache["cities"][key] = hit
        _cache["cities"].setdefault(hit["name"].lower(), hit)
        _save()
    return hit

def _haversine_km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))

def road_km(a, b, precise=True):
    """Road distance in km between two resolved cities. precise=True asks OSRM (cached);
    precise=False is the quick estimate used to rank many trucks by distance to pickup."""
    if a["name"] == b["name"]:
        return {"km": 0, "src": "same city"}
    if not precise:
        return {"km": round(_haversine_km(a, b) * ROAD_FACTOR), "src": "estimate"}
    key = "|".join(sorted([a["name"], b["name"]]))
    with _lock:
        if key in _cache["routes"]:
            return _cache["routes"][key]
    if time.time() < _down["route"]:
        return {"km": round(_haversine_km(a, b) * ROAD_FACTOR), "src": "estimate (routing service unavailable)"}
    started = time.time()
    try:
        r = requests.get(OSRM.format(a["lon"], a["lat"], b["lon"], b["lat"]), headers=UA, timeout=12)
        data = r.json()
        if data.get("code") == "Ok":
            res = {"km": round(data["routes"][0]["distance"] / 1000), "hours": round(data["routes"][0]["duration"] / 3600, 1),
                   "src": "OSRM road route"}
            with _lock:
                _cache["routes"][key] = res
                _save()
            return res
    except Exception:
        if time.time() - started > 5:                  # it hung, not refused: skip it for a minute
            _down["route"] = time.time() + DOWN_SECS
    return {"km": round(_haversine_km(a, b) * ROAD_FACTOR), "src": "estimate (routing service unavailable)"}
