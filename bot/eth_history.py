#!/usr/bin/env python3
"""Historical pattern engine for Eth Bot (KXETH15M).

Every row in the history file is fetched from real endpoints. Nothing here is
generated, simulated, or filled in. If a round cannot be read completely it is
skipped, not guessed.

  rounds   : GET /trade-api/v2/markets?series_ticker=KXETH15M&status=settled
  contract : GET /trade-api/v2/series/KXETH15M/markets/<T>/candlesticks
             (period_interval=1) -> per-minute yes_bid / yes_ask / volume
  ETH spot : GET api.exchange.coinbase.com/products/ETH-USD/candles
             (granularity=60) -> the same 1-minute closes the live bot reads

For each past settled round we record the state AT MINUTE 5 (3-min spot
momentum, RSI-14, contract volume so far, both sides' prices) and what actually
happened afterwards (the yes bid at minutes 10/11/12, and the settled result).

The lookup then answers one question: in the past rounds whose minute-5 state
looked like this one, how often did UP win?
"""
import json, os, time, random
import datetime as dt
import urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
HISTFILE = os.path.join(HERE, "record", "eth_history.json")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXETH15M"
UA = {"User-Agent": "Mozilla/5.0 (eth-bot; personal use)"}

MIN_MATCH = 12            # below this the matched set is too small to state a rate
FLAT_MOM = 0.02           # |3-min momentum| under this counts as flat (same as the bot)


def _get(url, params=None, timeout=25):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def rsi14(closes, n=14):
    if len(closes) < n + 1:
        return None
    g = l = 0.0
    for i in range(-n, 0):
        d = closes[i] - closes[i - 1]
        g += max(d, 0.0)
        l += max(-d, 0.0)
    if l == 0:
        return 100.0
    rs = (g / n) / (l / n)
    return round(100 - 100 / (1 + rs), 1)


# ------------------------------------------------------------------ buckets
def buckets(rsi, mom, vol5, vol_lo, vol_hi):
    ms = "flat" if mom is None or abs(mom) < FLAT_MOM else ("up" if mom > 0 else "down")
    rb = "na" if rsi is None else ("<40" if rsi < 40 else "40-50" if rsi < 50
                                   else "50-60" if rsi < 60 else ">60")
    vb = "na" if vol5 is None else ("low" if vol5 < vol_lo else "mid" if vol5 < vol_hi else "high")
    return ms, rb, vb


def wide_rsi_band(rsi):
    """Coarser RSI banding used by the widened match levels (Sep 15 2026).

    The four tight bands (<40/40-50/50-60/>60) crossed with 3 momentum states and
    3 volume states make 36 buckets over ~840 rounds - roughly 23 rounds each,
    which starved the >= 30 sample bar the history unlock requires.
    """
    if rsi is None:
        return "na"
    return "<45" if rsi < 45 else "45-55" if rsi < 55 else ">=55"


# ------------------------------------------------------------------ fetching
def _settled_markets(limit=800, stop_before=None):
    out, cur = [], None
    while len(out) < limit:
        p = dict(series_ticker=SERIES, status="settled", limit=200)
        if cur:
            p["cursor"] = cur
        j = _get(f"{KALSHI}/markets", p)
        b = j.get("markets", [])
        out += b
        cur = j.get("cursor")
        if not b or not cur:
            break
        if stop_before and min(_ts(m["open_time"]) for m in b) <= stop_before:
            break
    return out


def _candles(m, tries=4):
    o, cl = _ts(m["open_time"]), _ts(m["close_time"])
    for a in range(tries):
        try:
            j = _get(f"{KALSHI}/series/{SERIES}/markets/{m['ticker']}/candlesticks",
                     dict(start_ts=o - 60, end_ts=cl + 60, period_interval=1), timeout=20)
            return j.get("candlesticks", [])
        except Exception:
            time.sleep(0.6 * (a + 1) + random.random() * 0.4)
    return None


def _spot_range(start, end, tries=4):
    """1-minute ETH-USD closes, {unix_ts: close}. Coinbase caps a request at 300."""
    out = {}
    for s in range(start, end, 300 * 60):
        e = min(s + 299 * 60, end)
        for a in range(tries):
            try:
                rows = _get("https://api.exchange.coinbase.com/products/ETH-USD/candles",
                            dict(granularity=60,
                                 start=dt.datetime.utcfromtimestamp(s).isoformat() + "Z",
                                 end=dt.datetime.utcfromtimestamp(e).isoformat() + "Z"),
                            timeout=25)
                for t, lo, hi, op, c_, v in rows:
                    out[int(t)] = float(c_)
                break
            except Exception:
                time.sleep(0.8 * (a + 1))
    return out


def _row(m, cs, spot):
    """One past round's minute-5 state and what actually happened. None if incomplete."""
    if not cs or m.get("result") not in ("yes", "no"):
        return None
    o = _ts(m["open_time"])
    t5 = o + 300
    closes = []
    for k in range(20, -1, -1):
        v = spot.get(t5 - k * 60)
        if v is None:
            return None
        closes.append(v)
    rsi = rsi14(closes)
    mom = round((closes[-1] - closes[-4]) / closes[-4] * 100, 4)
    byt = {int(k["end_period_ts"]): k for k in cs}
    k5 = byt.get(t5)
    if not k5:
        return None
    ya = _f(k5["yes_ask"]["close_dollars"])
    yb = _f(k5["yes_bid"]["close_dollars"])
    vol5 = round(sum(float(k.get("volume_fp") or 0) for k in cs
                     if int(k["end_period_ts"]) <= t5), 1)
    ex = []
    for mm in (10, 11, 12):
        kk = byt.get(o + mm * 60)
        if kk:
            b = _f(kk["yes_bid"]["close_dollars"])
            if b is not None:
                ex.append(b)
    return dict(ticker=m["ticker"], open=o, rsi=rsi, mom=mom,
                yes_ask=ya, yes_bid=yb, no_ask=None if yb is None else round(1 - yb, 4),
                vol5=vol5, result=m["result"], yes_bid_exit=ex)


def build(limit=800, existing=None, log=print):
    """Full or incremental build. Returns the history dict that gets written."""
    have = {}
    if existing:
        have = {r["ticker"]: r for r in existing.get("rounds", [])}
    newest_have = max([r["open"] for r in have.values()], default=None)
    mkts = _settled_markets(limit, stop_before=newest_have)
    want = [m for m in mkts if m["ticker"] not in have]
    log(f"history: {len(mkts)} settled rounds listed, {len(want)} new to fetch")
    rows = list(have.values())
    if want:
        lo = min(_ts(m["open_time"]) for m in want) - 1800
        hi = max(_ts(m["close_time"]) for m in want) + 300
        spot = _spot_range(lo, hi)
        got = 0
        for m in want:
            cs = _candles(m)
            r = _row(m, cs, spot)
            if r:
                rows.append(r)
                got += 1
        log(f"history: {got} of {len(want)} new rounds fully readable and recorded")
    rows.sort(key=lambda r: r["open"])
    vs = sorted(r["vol5"] for r in rows)
    vol_lo = vs[len(vs) // 3] if vs else 0.0
    vol_hi = vs[2 * len(vs) // 3] if vs else 0.0
    return dict(built=time.time(), source=dict(
        rounds="GET /trade-api/v2/markets?series_ticker=KXETH15M&status=settled",
        contract="GET /trade-api/v2/series/KXETH15M/markets/<ticker>/candlesticks?period_interval=1",
        spot="GET api.exchange.coinbase.com/products/ETH-USD/candles?granularity=60"),
        vol_lo=vol_lo, vol_hi=vol_hi, n=len(rows), rounds=rows)


def load():
    try:
        with open(HISTFILE) as f:
            return json.load(f)
    except Exception:
        return None


def save(h):
    tmp = HISTFILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(h, f)
    os.replace(tmp, HISTFILE)


def refresh(log=print, limit=800):
    h = build(limit=limit, existing=load(), log=log)
    save(h)
    return h


# ------------------------------------------------------------------ lookup
def lookup(h, rsi, mom, vol5, min_n=None):
    """The core read: past rounds whose MINUTE-5 state resembles this one.

    Returns expected direction, the share of those rounds that direction won,
    the size of the matched set, and WHICH MATCH LEVEL produced it. Never a
    number without its sample, and never a sample without how it was gathered.

    Progressive matching (Sep 15 2026). The tight match is tried first; if its
    sample is smaller than ``min_n`` the match is widened ONE DIMENSION AT A
    TIME until a level reaches ``min_n``. The level that produced the numbers is
    named in ``level`` / ``basis`` and printed by the bot, so a hit rate is never
    quotable without knowing how wide a net gathered it.

    ``min_n`` is the caller's sample requirement (the bot passes its unlock bar,
    30). With min_n=None the old behaviour applies: the first level with at least
    MIN_MATCH rounds wins.
    """
    out = dict(expected=None, hit_rate=None, n=0, basis=None, level=None,
               up_rate=None, sample_from=None, text="no matched history yet")
    if not h or not h.get("rounds"):
        out["text"] = "no history file yet"
        return out
    rows = h["rounds"]
    ms, rb, vb = buckets(rsi, mom, vol5, h.get("vol_lo", 0), h.get("vol_hi", 0))
    wrb = wide_rsi_band(rsi)
    for r in rows:
        if "_ms" not in r:
            r["_ms"], r["_rb"], r["_vb"] = buckets(r["rsi"], r["mom"], r["vol5"],
                                                   h.get("vol_lo", 0), h.get("vol_hi", 0))
        if "_wrb" not in r:
            r["_wrb"] = wide_rsi_band(r["rsi"])

    levels = (
        ("exact", "momentum + RSI + volume",
         lambda r: (r["_ms"], r["_rb"], r["_vb"]) == (ms, rb, vb)),
        ("volume dropped", "momentum + RSI",
         lambda r: (r["_ms"], r["_rb"]) == (ms, rb)),
        ("rsi-band widened", "momentum + wide RSI",
         lambda r: (r["_ms"], r["_wrb"]) == (ms, wrb)),
        ("momentum only", "momentum",
         lambda r: r["_ms"] == ms),
    )

    floor_n = MIN_MATCH if min_n is None else max(int(min_n), MIN_MATCH)
    best = None   # deepest level that cleared MIN_MATCH, used if nothing clears floor_n
    for level, name, key in levels:
        sel = [r for r in rows if key(r)]
        if len(sel) >= MIN_MATCH and best is None:
            best = (level, name, sel)
        if len(sel) >= floor_n:
            best = (level, name, sel)
            break
    if not best:
        out["text"] = "too few past rounds match this setup - no rate stated"
        return out
    level, name, sel = best
    up = sum(1 for s in sel if s["result"] == "yes") / len(sel)
    exp = "UP" if up >= 0.5 else "DOWN"
    rate = up if exp == "UP" else 1 - up
    out.update(expected=exp, hit_rate=round(rate * 100, 1), n=len(sel),
               basis=name, level=level, up_rate=round(up * 100, 1),
               sample_from=dt.datetime.utcfromtimestamp(
                   min(s["open"] for s in sel)).strftime("%b %d"),
               text=f"{exp}, {rate*100:.0f}% over {len(sel)} similar past rounds "
                    f"(match: {level}; matched on {name})")
    return out


if __name__ == "__main__":
    h = refresh()
    print(json.dumps(dict(n=h["n"], vol_lo=h["vol_lo"], vol_hi=h["vol_hi"]), indent=1))
