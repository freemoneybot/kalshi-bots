"""Crypto Fear & Greed index (alternative.me, free, no key).

Daily value. Cached on disk so a round never hammers the API, and a fetch
failure simply omits the input instead of crashing a bot.
"""
import json, os, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "logs", "fng_cache.json")
TTL = 3600.0          # it only updates once a day; one fetch an hour is plenty
URL = "https://api.alternative.me/fng/?limit=2"

_mem = {"at": 0.0, "data": None}


def _load_disk():
    try:
        d = json.load(open(CACHE))
        if time.time() - d.get("at", 0) < TTL:
            return d
    except Exception:
        pass
    return None


def _save_disk(d):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        json.dump(d, open(CACHE + ".tmp", "w"))
        os.replace(CACHE + ".tmp", CACHE)
    except Exception:
        pass


def _fetch():
    req = urllib.request.Request(URL, headers={"User-Agent": "kalshi-bots/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        j = json.loads(r.read().decode())
    rows = j.get("data") or []
    if not rows:
        return None
    v = int(rows[0]["value"])
    cls = rows[0].get("value_classification") or ""
    prev = int(rows[1]["value"]) if len(rows) > 1 else None
    return dict(value=v, classification=cls, previous=prev,
                delta=(v - prev if prev is not None else None))


def read():
    """{'available':True,'value':..,'classification':..,'tilt':'up'|'down'|None,'text':..}
    or {'available': False, 'why': ...} — never raises."""
    now = time.time()
    if _mem["data"] is not None and now - _mem["at"] < TTL:
        d = _mem["data"]
    else:
        d = _load_disk()
        if d is not None:
            d = d.get("data")
        if d is None:
            try:
                d = _fetch()
            except Exception as ex:
                d = None
                why = f"{type(ex).__name__}: {ex}"
                disk = None
                try:
                    disk = json.load(open(CACHE)).get("data")
                except Exception:
                    pass
                if disk:
                    d = disk          # stale is better than nothing
                else:
                    return dict(available=False, why=f"fear & greed fetch failed ({why})")
            else:
                _save_disk({"at": now, "data": d})
        _mem["at"] = now
        _mem["data"] = d
    if not d:
        return dict(available=False, why="fear & greed unavailable")
    v = d["value"]
    # Contrarian tilt ONLY at the extremes; the middle is market context, not a vote.
    if v <= 25:
        tilt, strength = "up", (2 if v <= 15 else 1)
    elif v >= 75:
        tilt, strength = "down", (2 if v >= 85 else 1)
    else:
        tilt, strength = None, 0
    text = f"Fear & Greed {v} ({d['classification']})"
    if d.get("delta") is not None:
        text += f", {d['delta']:+d} vs yesterday"
    if tilt:
        text += f" \u2014 an extreme reading, contrarian tilt {tilt.upper()}"
    else:
        text += " \u2014 mid range, no tilt"
    return dict(available=True, value=v, classification=d["classification"],
                previous=d.get("previous"), delta=d.get("delta"),
                tilt=tilt, strength=strength, text=text)
