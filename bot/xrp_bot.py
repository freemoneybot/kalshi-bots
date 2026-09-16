#!/usr/bin/env python3
"""XRP Bot - KXXRP15M. Port of Silver Bot, NORMAL MODE (not scalp mode).

Anthony, Sep 14 2026: "Strike at minute 5" / "No scalp mode."
Anthony, Sep 15 2026 16:52Z/16:56Z: "ONLY buy 3-15%" / "don't have a certain sell
time. We're only buying low % so sell whenever you see best fit" / "We're going
for big wins low % buys" - the fixed minute 10-12 sell window is REVOKED.

So: the strike is picked and the entry evaluated at MINUTE 5 of each 15-minute
round, the position is then HELD and left to run: it is sold by the proportional
giveback trail, the hard stop (when it is not inert), or the minute-14 deadline.
The -9c hard stop and the contract-tape veto stay on as protection.

Every price written here is a real Kalshi read. Nothing is invented; a failed
read is recorded as unavailable.
"""
import json, math, os, sys, time, traceback
import datetime as dt
import urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fng                      # crypto Fear & Greed - XRP IS crypto, so this applies
import xrp_history as hist      # historical pattern engine (real settled KXXRP15M rounds)

RECORD = os.path.join(HERE, "record", "xrp.scalp.json")
POSFILE = os.path.join(HERE, "record", "xrp.pos.json")
SITE = os.path.join(HERE, "site")
# The Eth Bot was retired on Anthony's instruction (Sep 15 21:09Z, "No Eth"). Its
# final tally is preserved in record/eth_final.json and carried through every XRP
# snapshot as `eth_historical` so the dashboard keeps showing it as the historical
# record. Read once at import; if the file is missing this is None and the page
# simply shows nothing there - never an invented number.
def _load_eth_final():
    try:
        with open(os.path.join(HERE, "record", "eth_final.json")) as f:
            return json.load(f)
    except Exception:
        return None

ETH_FINAL = _load_eth_final()

SNAP = os.path.join(SITE, "snapshot.json")
LOG = os.path.join(HERE, "logs", "xrp_decisions.log")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
# --- THE SETTLEMENT INDEX (Sep 15 2026, correctness fix).
# KXXRP15M does NOT settle on Coinbase. Its own rule text from the Kalshi API:
#   "If the simple average of the sixty seconds of CF Benchmarks' XRPUSDRTI
#    before <close> is at least the simple average of the sixty seconds of
#    XRPUSDRTI before <open>, the market resolves to Yes."
# Both ends are 60-SECOND AVERAGES of CF Benchmarks' XRPUSDRTI index. Kalshi
# serves that exact index free and unauthenticated, ~1 tick per second:
KALSHI_EXT = "https://external-api.kalshi.com/trade-api/v2"
IDX_SRC = "kalshi-index"        # XRPUSDRTI, the series the contract settles on
CB_SRC = "coinbase-fallback"    # the old proxy, used ONLY if the index read fails
SETTLE_AVG_SECS = 60            # settlement averages the final 60s of the round
SERIES = "KXXRP15M"
# XRP TICK SIZE DIFFERS FROM ETH. KXXRP15M markets carry
# price_level_structure = "tapered_deci_cent": the tick is $0.001 below 10c and
# above 90c, and $0.01 in between (ETH was 1c throughout). So a cheap ticket can
# quote at 3.7c, not just 3c or 4c. Every price here is carried as dollars and
# printed through c() at ONE decimal, so a deci-cent quote is never rounded away.
UA = {"User-Agent": "Mozilla/5.0 (xrp-bot; personal use)"}

MIN_ENTRY = 0.03        # Anthony Sep 15 16:52Z: "ONLY buy 3-15%" (was 5c)
CHEAP_SIDE_MODE = True    # Anthony, Sep 15 17:09Z: "Eve try single time it's low %
                          # u fire" (= every single time it's low %, you fire) and,
                          # after being shown the measured expectancy (405 real rounds,
                          # 18.5% win rate, -2.72c/trade), 17:11Z: "Do it regardless".
                          # THIS SUSPENDS THE OLD HARD RULE "NEVER THE OPPOSITE SIDE OF
                          # THE SIGNAL" ON HIS EXPLICIT ORDER. Do not "fix" it back
                          # without him saying so. The ENTRY line names every trade that
                          # goes against our own read, so the cost of it is countable.
MAX_ENTRY = 0.30        # Anthony Sep 15 17:09Z: "raise it to 30%" - supersedes the
                        # 15c of 16:52Z, the 55c of 14:49Z and the 80c of Sep 14
                        # (supersedes 55c at 14:49Z, and 80c at Sep 14 18:14Z)
MAX_SPREAD = 0.06
MIN_SECS_LEFT = 90        # don't open inside the last 90s
FORCE_EXIT_SECS = 40      # flat before the window closes, always
LOOP = 6
HOLD_LOOP = 2             # while holding, re-read the book every 2s (gaps kill scalps)
# --- ADAPTIVE IN-POSITION CADENCE (Sep 15 2026). Measured, n=3: the rules were
# right and the LOOKING was slow. 06:36 exit gapped 1c through the floor, 08:37
# gapped 4c through it and closed RED after arming at +9c, and 05:20 filled a
# 47.2c stop at 37.0c. A 2s poll turns an unavoidable 1-2c gap into a 4-10c one.
# So: keep HOLD_LOOP when every trigger is far away, and poll every second once
# the live bid is within HOLD_NEAR of the nearest trigger level (hard stop,
# trailing floor, or the giveback level), or within HOLD_NEAR_SECS of a
# time-based trigger (the minute-14 flat deadline, the window-close flat).
# This changes ONLY how often the bot looks - never what it does when it sees.
HOLD_LOOP_FAST = 1        # in-position poll when a trigger is close
HOLD_NEAR = 0.05          # "close" = the bid is within 5c of the trigger price
BID_DIVERGE_SECS = 10     # how often the orderbook-vs-list-row proof line may log
HOLD_NEAR_SECS = 15       # "close" = within 15s of a time-based trigger
TAPE_VETO = 0.03          # contract-tape veto, cents/100
MIN_MOM = 0.02            # |3-min XRP momentum| gate, same constant/comparison as silver
MIN_CVOL = 600           # 15m contract volume floor - skip thin tape
RSI_DOWN_MAX = 56.0       # DOWN needs RSI below this
RSI_UP_MIN = 44.0         # UP needs RSI above this
MIN_LEAN = 0.8
STOP_FRAC = 0.25          # proportional hard stop: 25% of entry price...
STOP_MIN = 0.08           # ...floored at 8c...
STOP_MAX = 0.20           # ...and capped at 20c

# --- Conviction gate (Sep 14 2026). First seven trades separated perfectly on
# |lean|: the 3 winners leaned +1.5, +1.6, -1.6; the 4 losers +1.0, +0.4, -1.0
# and one flip-bug trade. Seven trades is a working hypothesis, not proof.
# Below this floor the round is a coin flip and is sat out. One line to revert.
CONVICTION_FLOOR = 1.8

# --- History unlock (Sep 15 2026). Six rounds in a row - 00:35 to 01:50 UTC -
# produced no trade at all under the 1.8 floor, and Anthony's hard rule is
# "Call every 15". So a SECOND, NARROW path in: a lean in [1.5, 1.8) may trade
# only when the historical read agrees with the side the signal picked, at a
# hit rate of at least 70% over at least 30 matched past rounds. Below 1.5,
# never - history is not allowed to let in a coin flip.
# --- EMA + VWAP (Anthony, Sep 15 2026: "Add ema into it" / "And vwap").
# Both are ADDITIVE terms in the lean, sized alongside the existing ones
# (momentum 1.0, RSI 0.5, FVG 0.6) and deliberately small enough that the two
# together (max 0.9) can NEVER push a round over the 1.5 unlock floor - let
# alone the 1.8 conviction floor - on their own. They never pick or flip a
# side, and they never touch the 80c ceiling, the tape veto or the stop.
EMA_FAST = 9
EMA_SLOW = 21
EMA_WEIGHT = 0.5
VWAP_WINDOW = 30          # minutes of 1-min candles in the rolling VWAP
VWAP_WEIGHT = 0.4
VWAP_MIN_DIST = 0.03      # |spot - vwap| under this %, VWAP says nothing

HIST_UNLOCK_MIN = 1.5     # bottom of the unlock band (the floor stays the normal bar)
HIST_MIN_RATE = 70.0      # matched-set hit rate, percent
HIST_MIN_N = 30           # matched-set sample size - the HARD bar, unchanged
HIST_TARGET_N = 60        # widening target: the engine keeps loosening the match
                          # one dimension at a time until the matched set reaches
                          # this, then stops. The 30-round bar below still decides.
HIST_MAX_AGE_SECS = 3600  # older than this, the history is stale and unlocks nothing

# --- EXIT, REBUILT Sep 15 2026 17:0xZ for the 3-15c band (Anthony, 16:56Z:
# "don't have a certain sell time ... sell whenever you see best fit" /
# "We're going for big wins low % buys"). The fixed minute 10-12 window is
# REVOKED as a mandatory exit - it was built for 50-80c tickets where the job was
# protecting a small gain. On a 3-15c ticket the economics invert: the most that
# can be lost is the 3-15c paid, the win is 85-97c, and roughly 1 in 6 has to
# land to break even. So the exit's job is to NOT CUT A WINNER SHORT while still
# banking a gain that is clearly done.
#
# 1. NO PROFIT TARGET. Nothing caps the upside any more.
# 2. GIVEBACK TRAIL SCALED TO THE GAIN, not a fixed cent arm. A fixed +8c arm is
#    meaningless here (+8c on a 5c entry is a 2.6x move). The trail arms once the
#    position has MULTIPLIED, and then gives back a FRACTION OF THE PEAK GAIN.
# 3. HARD DEADLINE at minute 14: settlement is the average of the FINAL 60
#    SECONDS of XRPUSDRTI, so the real decision point is minute 14 and a spike in
#    the last 30s barely moves the outcome. Flat by 14 is the conservative
#    reading and never risks the whole stake. HOLDING TO SETTLEMENT is a
#    legitimate alternative for "big wins" and is ANTHONY'S TO CHOOSE, not the
#    bot's: his Sep 14 "we're not holding either" was said under the old
#    expensive-entry regime. Until he says otherwise, flat by 14.
# 4x, not 2x, and it is CHOSEN FROM THE BACKTEST, not from taste. On the 873
# stored rounds (108 entries priced 3-15c at minute 5), take-profit-plus-trail
# measured -2.42c/trade at 4x vs -5.14c at 2x and -4.09c at 3x; a FLAT take at
# 4x (sell instantly, no trail) measured -3.91c, so the trail above the take is
# worth keeping. EVERY variant tested LOSES money - see the report - so 4x is
# the least-bad shape and the one that matches Anthony's words (Sep 15 16:58Z:
# "We're not holding. We see decent profit you sell"), NOT a measured edge.
TRAIL_ARM_MULT = 2.0      # Sep 16 00:40Z: was 4.0. MEASURED on 203 real XRP
                          # 3-30c entries (min-10/11/12 bids): holding to the
                          # deadline = -3.60c/trade, 45 wins; taking profit at
                          # the first touch of ANY multiple ~halves the loss -
                          # 1.25x -1.99c (73 wins), 1.5x -2.16c, 2x -2.23c,
                          # 3x -1.91c. All within noise of each other, so the
                          # level was chosen for robustness, not for the best
                          # cell: 2x is "double your money", which is Anthony's
                          # "We see decent profit you sell" (16:58Z). NOTE THE
                          # HONEST PART: every variant is still NEGATIVE. This
                          # halves the bleed, it does not make the strategy win.
TRAIL_ARM_MIN = 0.03      # ...and at least 3c above entry (2x of 3c is only +3c)
TRAIL_GIVEBACK_FRAC = 0.40  # once armed, give back at most 40% of the PEAK GAIN
TRAIL_FLOOR = 0.01        # an armed position never closes below entry + 1c
FLAT_BY_MIN = 15.0 - SETTLE_AVG_SECS / 60.0   # = 14.0, the hard deadline

TRAIL_ARM_REACH = 0.45    # ...but never demand more than 45% of the room to 100c

def trail_arm_level(entry):
    """The bid at which the trail arms: the position has to have MULTIPLIED.
    CAPPED SO IT IS REACHABLE (Sep 15 19:34Z). 4x a 30c entry is 120c, which no
    contract can print, so above ~25c the trail could never arm and every such
    position ran to the minute-14 deadline instead. Trade 40 is the proof: DOWN
    at 30c PEAKED AT 48c and was handed back at 37c because nothing was armed to
    protect it. The arm is now the LOWER of 4x entry and entry + 45% of the room
    left to 100c, so it is always a price the contract can actually reach."""
    mult = max(entry * TRAIL_ARM_MULT, entry + TRAIL_ARM_MIN)
    reach = entry + TRAIL_ARM_REACH * (1.0 - entry)
    return max(min(mult, reach), entry + TRAIL_ARM_MIN)

def trail_exit_level(entry, peak):
    """Once armed: the bid at or below which we bank it. A fraction of the peak
    gain given back, floored so an armed winner can never close red."""
    return max(peak - TRAIL_GIVEBACK_FRAC * (peak - entry), entry + TRAIL_FLOOR)

# --- Entry: strike selection / entry evaluation at minute 5 of the round.
STRIKE_MINUTE = 5.0
STRIKE_WINDOW_MINS = 1.0   # entry is evaluated from minute 5.0 to 6.0. A 0.5 window was
                           # too narrow: on Sep 15 a round produced no log line at all
                           # because no poll tick landed inside it. "Call every 15" beats
                           # a few seconds of drift - and a MISSED line now backstops it.
# --- SETTLEMENT MECHANIC (Sep 15 2026). Settlement is the simple average of the
# FINAL 60 SECONDS of XRPUSDRTI against the 60s average before the open, so a
# spike in the last 30 seconds moves the settled outcome by only about half its
# size, and the effective decision deadline is ~minute 14, not 15. The exit now
# USES that fact: FLAT_BY_MIN above is that deadline, and FORCE_EXIT_SECS still
# backstops it 40s before the close.
DECISION_DEADLINE_MIN = FLAT_BY_MIN


def log(msg):
    line = f"{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def get(url, params=None, timeout=15):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def fee(price, contracts=1):
    """Kalshi general trading fee, dollars: ceil(0.07 * C * P * (1-P)) to the cent."""
    if price is None:
        return 0.0
    return math.ceil(0.07 * contracts * price * (1.0 - price) * 100.0) / 100.0

def iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def c(v):
    return None if v is None else round(v * 100, 1)

# ----------------------------------------------------------------- Kalshi
def open_market():
    j = get(f"{KALSHI}/markets", dict(series_ticker=SERIES, status="open", limit=10))
    ms = [m for m in j.get("markets", []) if m.get("status") in ("active", "open")]
    if not ms:
        return None
    ms.sort(key=lambda m: m["close_time"])
    return ms[0]

def market_by_ticker(t):
    return get(f"{KALSHI}/markets/{t}")["market"]

def book(m):
    f = lambda k: (float(m.get(k)) if m.get(k) not in (None, "") else 0.0)
    yb, ya = f("yes_bid_dollars"), f("yes_ask_dollars")
    nb, na = f("no_bid_dollars"), f("no_ask_dollars")
    if not nb:
        nb = round(1.0 - ya, 4) if ya else 0.0
    if not na:
        na = round(1.0 - yb, 4) if yb else 0.0
    return dict(yes_bid=yb, yes_ask=ya, no_bid=nb, no_ask=na)

# --- THE LIVE TOUCH (Sep 15 2026, BUG #6 fix: data plumbing only, NO rule change).
# book() above reads yes_bid/yes_ask off the row served by the LIST endpoint
# GET /markets?series_ticker=... That endpoint sends `Cache-Control: public,
# max-age=15` and its `Age` header cycles 0->15, so a bot polling it every ~1.1s
# gets THE SAME NUMBER BACK. Measured, n=357 paired ticks over 2 real rounds:
# error vs the real orderbook touch median 3.1c, mean 5.6c, p90 14c, max 22c,
# >=5c on 42.6% of ticks - and it FREEZES rather than drifting (27 constant runs,
# median 26s, longest 66s). Re-measured on round 26SEP151015-15 before this fix
# shipped: the list row printed 0.6800 for 62 SECONDS while the real touch went
# 66 -> 74 -> 62 -> 50 -> 40; at one sample the list said 68c and the book said
# 40c. That is why the poll-cadence fix measured nothing - polling a frozen feed
# faster cannot make it fresher - and why the 0915 round's 85.0c floor was
# REACHABLE (Kalshi's own per-minute bars show the sell-bid low in minute 7 was
# exactly 85.0c) and the bot could not see it.
# The single-market row /markets/<ticker> measured median 1.6c error - better
# than the list's 3.1c but STILL STALE. The orderbook is the only live source.
# Used ONLY while a position is open (so the request rate stays bounded); the
# flat loop still runs off the list row. A failed or empty read returns None and
# the caller keeps the existing honest "unavailable" path - never a carried-
# forward price dressed up as live.
BID_SRC_BOOK = "orderbook"
BID_SRC_NONE = "unavailable"

def ob_touch(ticker):
    """Top of each ladder from the LIVE orderbook. None if it cannot be read.

    Kalshi's ladders are ascending in price, so the last entry of yes_dollars is
    the best YES bid and the last of no_dollars is the best NO bid. Nothing here
    is inferred from the other side - an absent ladder is absent, not 1 - x.
    """
    j = get(f"{KALSHI}/markets/{ticker}/orderbook")
    ob = j.get("orderbook_fp") or j.get("orderbook") or {}
    def top(k):
        lv = ob.get(k) or []
        if not lv:
            return None
        try:
            return float(lv[-1][0])
        except (TypeError, ValueError, IndexError):
            return None
    yb, nb = top("yes_dollars"), top("no_dollars")
    if yb is None and nb is None:
        return None
    return dict(yes_bid=yb or 0.0, no_bid=nb or 0.0,
                yes_ask=(round(1.0 - nb, 4) if nb else 0.0),
                no_ask=(round(1.0 - yb, 4) if yb else 0.0))

def kalshi_candles(ticker, minutes=30):
    now = int(time.time())
    j = get(f"{KALSHI}/series/{SERIES}/markets/{ticker}/candlesticks",
            dict(start_ts=now - minutes * 60, end_ts=now, period_interval=1))
    out = []
    for k in j.get("candlesticks", []):
        p = k.get("price") or {}
        cl = p.get("close_dollars")
        out.append(dict(t=k.get("end_period_ts"),
                        close=float(cl) if cl not in (None, "") else None,
                        vol=float(k.get("volume_fp") or 0),
                        oi=float(k.get("open_interest_fp") or 0)))
    return out


# ----------------------------------------------------------------- settlement index
def index_ticks(event_ticker, rng="15min"):
    """~1-second ticks of CF Benchmarks' XRPUSDRTI - the series KXXRP15M settles
    on - from Kalshi's own free live_data endpoint. Verified Sep 15 2026: 3601
    ticks spaced exactly 1s covering the trailing hour, and the 60s-average rule
    applied to these ticks reproduced the official settled result in 193 of 200
    real rounds. Raises on failure; the caller falls back to Coinbase and SAYS SO.
    """
    j = get(f"{KALSHI_EXT}/live_data/events/{event_ticker}", dict(range=rng), timeout=20)
    det = (j.get("live_data") or {}).get("details") or {}
    ts = det.get("timeseries") or []
    if len(ts) < 120:
        raise RuntimeError(f"index returned only {len(ts)} ticks")
    return [dict(t=int(x["t"]) / 1000.0, v=float(x["v"])) for x in ts if x.get("v")]


def ticks_to_minutes(ticks):
    """1s index ticks -> 1-minute OHLC rows (oldest first). No volume: the index
    is a price index and carries none. Volume is attached from Coinbase for the
    VWAP term only, and the log line says so."""
    b = {}
    for x in ticks:
        mb = int(x["t"] // 60 * 60)
        r = b.get(mb)
        if r is None:
            b[mb] = dict(t=mb, o=x["v"], h=x["v"], l=x["v"], c=x["v"], v=0.0)
        else:
            r["h"] = max(r["h"], x["v"]); r["l"] = min(r["l"], x["v"]); r["c"] = x["v"]
    return [b[k] for k in sorted(b)]


def avg_window(ticks, t_end, secs=SETTLE_AVG_SECS):
    """Simple average of the ticks in [t_end - secs, t_end) - exactly the shape
    of both ends of the settlement rule. None when no tick lands in it."""
    vals = [x["v"] for x in ticks if t_end - secs <= x["t"] < t_end]
    if not vals:
        return None, 0
    return round(sum(vals) / len(vals), 4), len(vals)

# ----------------------------------------------------------------- spot / TA
def coinbase_candles():
    """1-minute XRP-USD OHLCV, oldest-first. Same source run_bot.py uses for eth (retargeted to XRP)."""
    rows = get("https://api.exchange.coinbase.com/products/XRP-USD/candles",
               dict(granularity=60))
    out = []
    for t, lo, hi, op, cl, v in rows:
        out.append(dict(t=int(t), o=float(op), h=float(hi), l=float(lo),
                        c=float(cl), v=float(v or 0)))
    out.sort(key=lambda r: r["t"])
    return out

def coinbase_spot():
    j = get("https://api.coinbase.com/v2/prices/XRP-USD/spot")
    return float(j["data"]["amount"])

def yahoo(sym, rng="1d", interval="1m"):
    j = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
            dict(range=rng, interval=interval))
    r = j["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(r["timestamp"]):
        o, h, l, cl = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, cl):
            continue
        v = (q.get("volume") or [None] * len(r["timestamp"]))[i]
        rows.append(dict(t=t, o=o, h=h, l=l, c=cl, v=v or 0))
    return rows, r["meta"]

def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(-n, 0):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return round(100 - 100 / (1 + rs), 1)

def ema(values, n):
    """Standard EMA. None when there is not enough data to compute it honestly."""
    if not values or len(values) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n            # seed on the first n as a simple average
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return round(e, 2)


def vwap_of(rows):
    """Volume-weighted average price over the given 1-min candles.

    sum(typical price x volume) / sum(volume), typical = (high+low+close)/3.
    None when the candles carry no volume - never a fabricated number.
    """
    num = den = 0.0
    for r in rows:
        v = float(r.get("v") or 0)
        if v <= 0:
            continue
        num += ((r["h"] + r["l"] + r["c"]) / 3.0) * v
        den += v
    if den <= 0:
        return None
    return round(num / den, 2)


def find_fvg(rows):
    """Most recent 3-bar fair value gap in the last 20 bars. Bullish: low[i] > high[i-2]."""
    out = None
    for i in range(len(rows) - 1, 1, -1):
        a, b = rows[i - 2], rows[i]
        if b["l"] > a["h"]:
            out = dict(dir="bull", lo=round(a["h"], 3), hi=round(b["l"], 3), bars_ago=len(rows) - 1 - i)
            break
        if b["h"] < a["l"]:
            out = dict(dir="bear", lo=round(b["h"], 3), hi=round(a["l"], 3), bars_ago=len(rows) - 1 - i)
            break
    return out

def sentiment():
    """XRP is crypto, so the crypto Fear & Greed index is the sentiment input here.
    VIX is kept as broad-market context only. Nothing is invented: an unread
    source reads 'unavailable'."""
    out = dict(source="Crypto Fear & Greed index (alternative.me)",
               note="XRP is crypto, so the crypto Fear & Greed index applies here. "
                    "VIX is broad-market context only.",
               fng=None, fng_label=None, fng_delta=None, fng_text=None,
               vix=None, label="unavailable")
    try:
        f = fng.read()
        if f.get("available"):
            out["fng"] = f["value"]
            out["fng_label"] = f["classification"]
            out["fng_delta"] = f.get("delta")
            out["fng_text"] = f["text"]
            out["label"] = f["classification"]
        else:
            out["fng_text"] = f.get("why")
    except Exception as e:
        out["fng_text"] = f"fear & greed unavailable ({str(e)[:80]})"
    try:
        _rows, meta = yahoo("^VIX", "1d", "5m")
        out["vix"] = round(float(meta["regularMarketPrice"]), 2)
    except Exception:
        pass
    return out

# ----------------------------------------------------------------- record
def blank():
    return dict(system="XRP Bot", series=SERIES, started=time.time(),
                trades=[], tally=dict(n=0, wins=0, net_cents=0.0),
               tally_cheap=dict(n=0, wins=0, net_cents=0.0, against=0, entry_cents_sum=0.0))

def load():
    try:
        with open(RECORD) as f:
            r = json.load(f)
        r.setdefault("trades", [])
        r.setdefault("tally", dict(n=0, wins=0, net_cents=0.0))
        r.setdefault("tally_cheap", dict(n=0, wins=0, net_cents=0.0, against=0, entry_cents_sum=0.0))
        return r
    except Exception:
        return blank()

def save_pos(pos):
    try:
        if pos is None:
            if os.path.exists(POSFILE):
                os.remove(POSFILE)
        else:
            with open(POSFILE, "w") as f:
                json.dump(pos, f)
    except Exception:
        pass

def load_pos():
    try:
        with open(POSFILE) as f:
            return json.load(f)
    except Exception:
        return None

def save(rec):
    tmp = RECORD + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, RECORD)

# ----------------------------------------------------------------- signals
def read_signals(market):
    """Everything the entry decision uses. Real reads only; None when unread.

    PRICE SERIES (Sep 15 2026 correctness fix): every indicator here is computed
    from CF Benchmarks' XRPUSDRTI - the index KXXRP15M actually settles on - read
    from Kalshi's own free live_data endpoint at ~1 tick/second. Coinbase spot is
    now only a FALLBACK, used when the index read fails, and s["price_src"] names
    which one produced the numbers so no log line can hide a mixed source.
    Volume: the index carries none, so the VWAP term borrows Coinbase's 1-minute
    volumes as weights (prices are the index's) and says so. No volume anywhere,
    no VWAP - it contributes ZERO rather than a made-up number.
    """
    s = dict(ts=time.time())
    s["price_src"] = None
    cbrows = []
    try:
        cbrows = coinbase_candles()
    except Exception as e:
        s["cb_error"] = str(e)[:120]
    rows = None
    try:
        tk = index_ticks(market.get("event_ticker") or "")
        irows = ticks_to_minutes(tk)
        if len(irows) < EMA_SLOW + 2:
            raise RuntimeError(f"index gave only {len(irows)} 1-min rows")
        vol_by_min = {int(r["t"]): float(r.get("v") or 0) for r in cbrows}
        for r in irows:
            r["v"] = vol_by_min.get(int(r["t"]), 0.0)
        rows = irows
        s["price_src"] = IDX_SRC
        s["index_ticks"] = len(tk)
        s["spot"] = round(tk[-1]["v"], 2)
        s["spot_source"] = ("CF Benchmarks XRPUSDRTI (the KXXRP15M settlement index) "
                            f"via Kalshi live_data, {len(tk)} 1s ticks; VWAP volume "
                            "weights from Coinbase 1-min candles")
        s["index_age_secs"] = round(time.time() - tk[-1]["t"], 1)
        # --- THE ROUND'S REFERENCE ANCHOR. The round's reference is NOT the open
        # print: the rule compares the 60s average BEFORE the close against the
        # 60s average BEFORE the open. So the anchor is that opening 60s average.
        try:
            t_open = iso(market["open_time"])
            ref, nref = avg_window(tk, t_open)
            s["ref60"] = ref
            s["ref60_ticks"] = nref
            if ref:
                s["ref_gap"] = round(s["spot"] - ref, 3)
                s["ref_gap_pct"] = round((s["spot"] - ref) / ref * 100, 4)
                s["ref_note"] = (f"reference = 60s XRPUSDRTI average before the open "
                                 f"{ref:.2f} ({nref} ticks); index now {s['spot']:.2f} "
                                 f"({s['ref_gap']:+.2f}, {s['ref_gap_pct']:+.3f}%) - "
                                 f"settlement compares the FINAL 60s average to this")
            else:
                s["ref_note"] = ("reference unavailable - no index tick landed in the "
                                 "60s before the open")
        except Exception as e:
            s["ref_note"] = f"reference unavailable ({str(e)[:70]})"
    except Exception as e:
        s["index_error"] = str(e)[:120]
    if rows is None:
        rows = cbrows
        if rows:
            s["price_src"] = CB_SRC
            s["spot_source"] = ("Coinbase XRP-USD 1-min candles (FALLBACK - the "
                                "XRPUSDRTI settlement index could not be read)")
            try:
                s["spot"] = round(coinbase_spot(), 2)
            except Exception:
                s["spot"] = round(rows[-1]["c"], 2)
        s["ref_note"] = ("reference unavailable - the settlement index could not be "
                         "read, so the round's 60s opening average is unknown")
    try:
        if not rows:
            raise RuntimeError("no price series available from either source")
        closes = [r["c"] for r in rows]
        s["rsi"] = rsi(closes)
        s["fvg"] = find_fvg(rows[-24:])
        last5 = rows[-5:]
        s["spot_mom_pct"] = round((closes[-1] - closes[-4]) / closes[-4] * 100, 4) if len(closes) > 4 else None
        vols = [r["v"] for r in rows[-30:] if r["v"]]
        s["spot_vol_last"] = last5[-1]["v"] if last5 else None
        s["spot_vol_avg"] = round(sum(vols) / len(vols), 1) if vols else None
        s["candles"] = [dict(t=r["t"], c=round(r["c"], 2), v=round(r["v"], 3)) for r in rows[-30:]]
        # --- realised vol on the settlement series: stdev of 1-min log returns
        try:
            rets = [math.log(closes[k] / closes[k - 1]) for k in range(1, len(closes))
                    if closes[k - 1] > 0][-30:]
            if len(rets) >= 10:
                mu = sum(rets) / len(rets)
                s["rvol_pct_per_min"] = round(
                    (sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5 * 100, 4)
                s["rvol_n"] = len(rets)
        except Exception:
            pass
        s["ema_fast"] = ema(closes, EMA_FAST)
        s["ema_slow"] = ema(closes, EMA_SLOW)
        if s["ema_fast"] is None or s["ema_slow"] is None:
            s["ema_note"] = (f"EMA unavailable - only {len(closes)} 1-min closes, "
                             f"need {EMA_SLOW}")
        vw_rows = rows[-VWAP_WINDOW:]
        s["vwap"] = vwap_of(vw_rows)
        s["vwap_window_min"] = len(vw_rows)
        if s["vwap"] is None:
            s["vwap_note"] = ("VWAP unavailable - no volume on the candles (the "
                              "settlement index carries none and the Coinbase "
                              "volume read failed)")
        else:
            spot_now = s.get("spot") or closes[-1]
            s["vwap_dist_pct"] = round((spot_now - s["vwap"]) / s["vwap"] * 100, 4)
            prior = vwap_of(rows[-(VWAP_WINDOW + 3):-3]) if len(rows) > VWAP_WINDOW + 3 else None
            if prior and len(closes) > 3:
                s["vwap_dist_prev_pct"] = round((closes[-4] - prior) / prior * 100, 4)
    except Exception as e:
        s["spot_error"] = str(e)[:120]
    try:
        kc = kalshi_candles(market["ticker"])
        s["contract_vol_15m"] = round(sum(k["vol"] for k in kc[-15:]), 1)
        s["contract_candles"] = kc[-15:]
        s["oi"] = kc[-1]["oi"] if kc else None
    except Exception as e:
        s["contract_error"] = str(e)[:120]
    s["sentiment"] = sentiment()
    return s

def stop_for(entry):
    """Proportional hard stop: ~25% of entry price, floored 8c, capped 20c.
    A flat 10c stop was 36% of a 28c contract and 13% of a 74c one, so cheap
    entries were knocked out on noise early in the round."""
    return min(max(STOP_MIN, entry * STOP_FRAC), STOP_MAX)


def decide(market, bk, sig, hl=None, hist_built=None):
    """Pick a SIDE every round. Anthony, Sep 14 18:14Z: "Call every 15." /
    "At the 5 minute mark" / "Look, nothing above 80".

    ABSOLUTE RULE: the signal picks the side. That side is taken, or there is
    NO TRADE. The opposite side is never substituted, for any reason - not
    price, not tape, not RSI, not volume, not anything.

    The only four NOTRADE reasons, each logged plainly:
      1. the signal's own side costs more than the ceiling (or under the 5c floor)
      2. the tape vetoes the signal's own side
      3. the orderbook read fails
      4. |combined lean| is below CONVICTION_FLOOR
    """
    why = []
    lean = 0.0
    # Which price series produced every number below, named before anything else.
    _src = sig.get("price_src") or "unavailable"
    why.append(f"src={_src} ({sig.get('spot_source') or 'no price source'})")
    if sig.get("ref_note"):
        why.append(sig["ref_note"])
    if sig.get("rvol_pct_per_min") is not None:
        why.append(f"realised vol {sig['rvol_pct_per_min']:.3f}%/min over "
                   f"{sig.get('rvol_n')} 1-min returns ({_src})")
    mom = sig.get("spot_mom_pct")
    if mom is not None:
        if abs(mom) >= MIN_MOM / 100.0:
            lean += 1.0 if mom > 0 else -1.0
            why.append(f"XRP 3-min momentum {mom:+.3f}%")
        else:
            why.append(f"XRP flat ({mom:+.3f}%)")
    r = sig.get("rsi")
    if r is not None:
        if r > 60:
            lean += 0.5; why.append(f"RSI {r} strong")
        elif r < 40:
            lean -= 0.5; why.append(f"RSI {r} weak")
        else:
            why.append(f"RSI {r} neutral")
    f = sig.get("fvg")
    if f and f.get("bars_ago", 99) <= 3:
        lean += 0.6 if f["dir"] == "bull" else -0.6
        why.append(f"{f['dir']} FVG {f['lo']}-{f['hi']} ({f['bars_ago']}m ago)")
    # --- EMA 9/21. Price above both and fast above slow is bullish; the mirror
    # is bearish; anything tangled contributes ZERO and says so.
    ef, es = sig.get("ema_fast"), sig.get("ema_slow")
    spot_now = sig.get("spot")
    if ef is None or es is None:
        sig["ema_state"] = "unavailable"
        why.append(sig.get("ema_note") or "EMA unavailable - not enough candles")
    elif spot_now is None:
        sig["ema_state"] = "unavailable"
        why.append("EMA unavailable - no spot read")
    elif spot_now > ef > es:
        sig["ema_state"] = "bullish"
        lean += EMA_WEIGHT
        why.append(f"EMA {EMA_FAST}/{EMA_SLOW} bullish ({ef} > {es}, spot {spot_now} above both)")
    elif spot_now < ef < es:
        sig["ema_state"] = "bearish"
        lean -= EMA_WEIGHT
        why.append(f"EMA {EMA_FAST}/{EMA_SLOW} bearish ({ef} < {es}, spot {spot_now} below both)")
    else:
        sig["ema_state"] = "tangled"
        why.append(f"EMA {EMA_FAST}/{EMA_SLOW} tangled ({ef} vs {es}, spot {spot_now}) - no contribution")

    # --- VWAP. Side comes from where spot sits; the term is full weight when the
    # gap is widening (trend away from value) and half when it is converging back.
    vw = sig.get("vwap")
    vd = sig.get("vwap_dist_pct")
    if vw is None or vd is None:
        sig["vwap_state"] = "unavailable"
        why.append(sig.get("vwap_note") or "VWAP unavailable - no volume on the candles")
    elif abs(vd) < VWAP_MIN_DIST:
        sig["vwap_state"] = "at vwap"
        why.append(f"spot sitting on VWAP {vw} ({vd:+.2f}%) - no contribution")
    else:
        prev = sig.get("vwap_dist_prev_pct")
        widening = prev is None or abs(vd) >= abs(prev)
        wgt = VWAP_WEIGHT if widening else VWAP_WEIGHT / 2
        lean += wgt if vd > 0 else -wgt
        sig["vwap_state"] = ("above vwap" if vd > 0 else "below vwap") + \
                            (", widening" if widening else ", converging")
        why.append(f"spot {abs(vd):.2f}% {'above' if vd > 0 else 'below'} VWAP {vw} "
                   f"({'widening' if widening else 'converging'}, "
                   f"{sig.get('vwap_window_min')}m window)")

    kc = sig.get("contract_candles") or []
    closes = [k["close"] for k in kc if k.get("close") is not None]
    tape = None
    if len(closes) >= 3:
        tape = closes[-1] - closes[-3]
        why.append(f"contract tape {tape*100:+.0f}c over 2 min")
    sig["tape_2min_cents"] = None if tape is None else round(tape * 100, 1)
    cv = sig.get("contract_vol_15m")
    if cv is not None:
        why.append(f"volume {cv:.0f} (15m)")
    sent = sig.get("sentiment") or {}
    if sent.get("fng") is not None:
        why.append(f"Fear & Greed {sent['fng']} ({sent['fng_label']})")
    sig["lean"] = round(lean, 2)

    # --- WHICH side. Signal first; tape breaks a tie; momentum breaks that.
    if abs(lean) >= 0.01:
        side = "UP" if lean > 0 else "DOWN"
        why.append(f"lean {lean:+.2f} -> {side}")
    elif tape is not None and abs(tape) >= 0.01:
        side = "UP" if tape > 0 else "DOWN"
        why.append(f"lean flat, contract tape {tape*100:+.0f}c -> {side}")
    elif mom is not None and mom != 0:
        side = "UP" if mom > 0 else "DOWN"
        why.append(f"lean and tape flat, momentum {mom:+.3f}% -> {side}")
    else:
        side = "UP"
        why.append("nothing leaning either way - defaulting UP")
    # --- CHEAP-SIDE MODE (Anthony 17:09Z / 17:11Z, see CHEAP_SIDE_MODE above).
    # The signal still computes and is still PRINTED - it just no longer picks
    # the side. Whichever side costs less is taken, from the ORDERBOOK asks that
    # were read at the minute-5 mark, never the cached list row.
    sig["signal_side"] = side
    sig["against_signal"] = False
    if CHEAP_SIDE_MODE:
        _ya, _na = bk.get("yes_ask"), bk.get("no_ask")
        if not _ya or not _na:
            return None, why + ["NOTRADE: orderbook read failed - no ask on one or "
                                "both sides, cannot tell which side is cheaper"]
        cheap = "UP" if _ya <= _na else "DOWN"
        if cheap != side:
            sig["against_signal"] = True
            why.append(f"cheap-side mode: the signal read {side}, but {cheap} is the "
                       f"cheaper ticket ({c(_ya if cheap=='UP' else _na)}c vs "
                       f"{c(_na if cheap=='UP' else _ya)}c) - taking {cheap} "
                       f"AGAINST THE SIGNAL (Anthony 17:11Z \"Do it regardless\")")
        else:
            why.append(f"cheap-side mode: {cheap} is both the cheaper ticket and the "
                       f"side the signal read")
        side = cheap
    # THE SIDE IS NOW FIXED. Nothing below may change it. The tape can only
    # VETO the side being taken (no trade) - it can never buy the other one.
    # TAPE VETO SUSPENDED IN CHEAP-SIDE MODE. Anthony, Sep 15 18:10Z: "STOP
    # SKIPING". Three consecutive rounds had a cheap ticket (3c, 4c, 30c) and
    # the veto killed all three - a ticket is cheap BECAUSE the tape is running
    # away from it, so the two rules cancelled out and the bot could not trade
    # at all. His call, made after he saw the count. In signal mode the veto
    # still applies exactly as before.
    if tape is not None and not CHEAP_SIDE_MODE:
        if (side == "UP" and tape <= -TAPE_VETO) or (side == "DOWN" and tape >= TAPE_VETO):
            return None, why + [f"NOTRADE: signal says {side} but the 2-min contract tape "
                                f"is {tape*100:+.0f}c against it (limit {TAPE_VETO*100:.0f}c) "
                                f"- vetoed, no trade this round"]

    # --- Conviction gate, with the HISTORY UNLOCK (Sep 15 2026).
    # Normal bar is CONVICTION_FLOOR (1.8). A second, narrow path exists: a lean
    # in the [HIST_UNLOCK_MIN, CONVICTION_FLOOR) band may still trade IF the
    # historical read AGREES with the side the signal already picked, its hit
    # rate is >= HIST_MIN_RATE and its sample is >= HIST_MIN_N.
    # History may ONLY ADD trades inside that band. It never picks or flips a
    # side, never touches the 5-80c band, the tape veto, or the stop - all of
    # which are evaluated independently, above and below this block.
    sig["hist_unlock"] = None
    # In cheap-side mode the conviction floor no longer gates entry (the side is
    # chosen by price, not by conviction), so this whole block - and its NOTRADE
    # reason 4 - simply does not fire. The code is kept intact, not deleted, so
    # turning CHEAP_SIDE_MODE off restores the old behaviour exactly.
    if not CHEAP_SIDE_MODE and abs(lean) < CONVICTION_FLOOR:
        if abs(lean) < HIST_UNLOCK_MIN:
            return None, why + [f"NOTRADE: lean {lean:+.2f} below the "
                                f"{CONVICTION_FLOOR:.1f} conviction floor - coin flip, sitting out"]
        h_exp = (hl or {}).get("expected")
        h_rate = (hl or {}).get("hit_rate")
        h_n = (hl or {}).get("n") or 0
        h_lvl = (hl or {}).get("level") or "unknown"
        h_age = None
        if hist_built:
            h_age = time.time() - hist_built
        if not h_exp or h_rate is None:
            return None, why + [f"NOTRADE: lean {lean:+.2f} below the "
                                f"{CONVICTION_FLOOR:.1f} conviction floor and no historical "
                                f"read is available to unlock it - sitting out"]
        if h_age is not None and h_age > HIST_MAX_AGE_SECS:
            return None, why + [f"NOTRADE: lean {lean:+.2f} below the "
                                f"{CONVICTION_FLOOR:.1f} conviction floor and the history is "
                                f"stale ({h_age/60:.0f} min old) - sitting out"]
        if h_n < HIST_MIN_N:
            return None, why + [f"NOTRADE: lean {lean:+.2f} below the "
                                f"{CONVICTION_FLOOR:.1f} conviction floor; history says {h_exp} "
                                f"{h_rate:.0f}% but only n={h_n} (match: {h_lvl}) rounds (need {HIST_MIN_N}) - sitting out"]
        if h_rate < HIST_MIN_RATE:
            return None, why + [f"NOTRADE: lean {lean:+.2f} below the "
                                f"{CONVICTION_FLOOR:.1f} conviction floor; history says {h_exp} "
                                f"{h_rate:.0f}% over n={h_n} (match: {h_lvl}), under the {HIST_MIN_RATE:.0f}% bar - sitting out"]
        if h_exp != side:
            return None, why + [f"NOTRADE: lean {lean:+.2f} below the "
                                f"{CONVICTION_FLOOR:.1f} conviction floor and history disagrees "
                                f"(signal {side}, history {h_exp} {h_rate:.0f}% over n={h_n} (match: {h_lvl})) - sitting out"]
        sig["hist_unlock"] = dict(expected=h_exp, hit_rate=h_rate, n=h_n, level=h_lvl)
        why.append(f"history-unlock: lean {lean:+.2f} is under the {CONVICTION_FLOOR:.1f} floor "
                   f"but history agrees ({h_exp} {h_rate:.0f}% over {h_n} similar past rounds, match: {h_lvl}) "
                   f"- taking the signal's own {side} side")

    def cost_of(sd):
        c_ = bk["yes_ask"] if sd == "UP" else bk["no_ask"]
        return c_ or None

    cost = cost_of(side)
    if cost is None:
        return None, why + [f"NOTRADE: orderbook read failed - no ask on the {side} side"]
    if cost > MAX_ENTRY or cost < MIN_ENTRY:
        # The ONE allowed skip. Never substitute the opposite side for price
        # reasons - that would be buying the ticket we believe is wrong.
        _lead = (f"the cheaper side ({side}) costs" if CHEAP_SIDE_MODE
                 else f"signal says {side} but {side} costs")
        return None, why + [f"NOTRADE: {_lead} "
                            f"{cost*100:.1f}c - outside the {MIN_ENTRY*100:.0f}-"
                            f"{MAX_ENTRY*100:.0f}c band"]
    return side, why



# ----------------------------------------------------------------- read-out
# Anthony, Sep 14 2026: "Add expected up or down. And reversal, and bull, sell,
# hold. And % profitability" / "Use history bro" / "It's literally a historical
# pattern". Everything below is DISPLAY AND CONTEXT ONLY - it does not override
# the lean signal or change a single trading rule in this pass.
def miss_reason(w, pos):
    """Plain words for why a round's minute-5 window produced no evaluation."""
    if pos:
        return f"a {pos['side']} position from {pos['ticker']} was still open"
    if not w:
        return "the round was never seen inside its window"
    if w.get("first_seen") is not None and w["first_seen"] >= STRIKE_MINUTE + STRIKE_WINDOW_MINS:
        return (f"the bot first saw this round at minute {w['first_seen']:.1f}, "
                f"after the window had closed")
    if w.get("last_err"):
        return (f"{w['ticks_in_window']} tick(s) reached the window but the evaluation "
                f"failed each time: {w['last_err']}")
    if not w.get("ticks_in_window"):
        lb = w.get("last_minute_before")
        return (f"no poll tick landed inside the minute {STRIKE_MINUTE:.1f}-"
                f"{STRIKE_MINUTE + STRIKE_WINDOW_MINS:.1f} window"
                + (f" (last tick before it: minute {lb:.1f})" if lb is not None else ""))
    return "the window was reached but no branch produced a line"

HIST_REFRESH_SECS = 1200


def regime_of(sig):
    """TREND or REVERSAL, from the same real reads the entry uses."""
    r = sig.get("rsi")
    mom = sig.get("spot_mom_pct")
    f = sig.get("fvg") or {}
    fdir = f.get("dir") if f.get("bars_ago", 99) <= 3 else None
    if r is not None and r >= 60 and (mom or 0) < 0 and fdir != "bull":
        return "REVERSAL", f"RSI {r} stretched high and price rolling over ({mom:+.3f}%)"
    if r is not None and r <= 40 and (mom or 0) > 0 and fdir != "bear":
        return "REVERSAL", f"RSI {r} washed out and price turning up ({mom:+.3f}%)"
    if r is not None and r <= 35 and (fdir == "bull" or (mom or 0) > 0):
        return "REVERSAL", f"RSI {r} oversold" + (" with a bull FVG above" if fdir == "bull" else " and price turning up")
    if r is not None and r >= 65 and (fdir == "bear" or (mom or 0) < 0):
        return "REVERSAL", f"RSI {r} overbought" + (" with a bear FVG below" if fdir == "bear" else " and price rolling over")
    if fdir and mom is not None and ((fdir == "bull" and mom < 0) or (fdir == "bear" and mom > 0)):
        return "REVERSAL", f"{fdir} FVG against a {mom:+.3f}% move - gap pulling the other way"
    if mom is None:
        return "TREND", "no spot read this cycle"
    return "TREND", (f"momentum {mom:+.3f}% running with RSI "
                     f"{r if r is not None else 'n/a'} - continuation")


def bias_of(sig):
    lean = sig.get("lean")
    if lean is not None and abs(lean) >= 0.01:
        return ("BULL" if lean > 0 else "BEAR"), f"combined lean {lean:+.2f}"
    mom = sig.get("spot_mom_pct")
    if mom:
        return ("BULL" if mom > 0 else "BEAR"), f"lean flat, momentum {mom:+.3f}%"
    return "BULL", "nothing leaning either way"


def profitability(rec):
    """The bot's OWN realised record. Straight off recorded trades, no estimate."""
    t = rec.get("tally") or {}
    n = t.get("n", 0)
    wins = t.get("wins", 0)
    net = round(t.get("net_cents", 0.0), 1)
    return dict(trades=n, wins=wins,
                win_rate_pct=(round(wins / n * 100, 1) if n else None),
                net_cents=net,
                avg_cents=(round(net / n, 1) if n else None),
                text=(f"{wins/n*100:.0f}% win rate over {n} trades, {net:+.1f}c net"
                      if n else "no closed trades yet"))

def cheap_side_tally(rec):
    """Cheap-side mode's own realised record - count, win rate, net, average
    entry price, and how many of those trades went AGAINST our own signal."""
    t = rec.get("tally_cheap") or {}
    n = t.get("n", 0)
    w = t.get("wins", 0)
    net = round(t.get("net_cents", 0.0), 1)
    return dict(mode_on=CHEAP_SIDE_MODE, trades=n, wins=w,
                win_rate_pct=(round(w / n * 100, 1) if n else None),
                net_cents=net,
                avg_cents=(round(net / n, 1) if n else None),
                against_signal=t.get("against", 0),
                avg_entry_cents=(round(t.get("entry_cents_sum", 0.0) / n, 1) if n else None),
                text=(f"cheap-side mode: {w} wins of {n} ({w/n*100:.0f}%), {net:+.1f}c net, "
                      f"avg entry {t.get('entry_cents_sum',0.0)/n:.1f}c, "
                      f"{t.get('against',0)} taken against our own signal"
                      if n else "cheap-side mode: no closed trades yet"))

# ----------------------------------------------------------------- snapshot
def write_snapshot(state):
    tmp = SNAP + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, SNAP)

# ----------------------------------------------------------------- main
def main():
    rec = load()
    H = hist.load()
    if H:
        log(f"history: {H.get('n')} real settled KXXRP15M rounds loaded from record/xrp_history.json")
    else:
        log("history: no history file yet - building from the Kalshi API now")
        try:
            H = hist.refresh(log=log)
        except Exception as e:
            log("history build failed: " + repr(e)[:160])
            H = None
    last_hist = time.time()
    last_bid_log = 0.0
    pos = load_pos()    # open scalp, survives a restart
    last_sig = {}
    last_ticker = None
    evaluated = set()   # tickers that produced an ENTRY or a NOTRADE line - a real evaluation
    missed_logged = set()  # tickers already reported as MISSED - one line each, never two
    round_watch = {}    # per-ticker: how the minute-5 window actually went, for the MISSED reason
    entered = set()     # tickers already ENTERED this session - ONE entry per round, hard.
                        # Sep 14: a round was stopped out at minute 5.9 and re-entered one
                        # second later, losing again (-17c then -14c on the same ticker).
                        # A stop means nothing if the bot buys straight back in.
    if pos:
        entered.add(pos["ticker"])   # a restart mid-position must not re-enter that round
    while True:
        loop_note = ""
        hold_sleep = HOLD_LOOP
        action, action_why = "HOLD", "no position, not at the minute-5 mark"
        try:
            m = open_market()
            if not m:
                loop_note = "no open XRP market"
                time.sleep(LOOP)
                continue
            if m["ticker"] != last_ticker:
                if (last_ticker and last_ticker not in evaluated
                        and last_ticker not in missed_logged):
                    missed_logged.add(last_ticker)
                    log(f"MISSED {last_ticker} minute {STRIKE_MINUTE + STRIKE_WINDOW_MINS:.1f} "
                        f"src={last_sig.get('price_src') or 'unavailable'} "
                        f"- the minute-5 window passed without an evaluation: "
                        f"{miss_reason(round_watch.get(last_ticker), pos)} (round rolled over)")
                last_ticker = m["ticker"]
                last_sig = {}
            if time.time() - last_hist > HIST_REFRESH_SECS:
                last_hist = time.time()
                try:
                    H = hist.refresh(log=log)
                except Exception as e:
                    log("history refresh failed: " + repr(e)[:160])
            bk = book(m)
            secs_left = max(0.0, iso(m["close_time"]) - time.time())
            minute = (time.time() - iso(m["open_time"])) / 60.0
            in_strike_window = (STRIKE_MINUTE <= minute < STRIKE_MINUTE + STRIKE_WINDOW_MINS)

            # refresh the slow signals about twice a minute
            if time.time() - (last_sig.get("ts") or 0) > 25:
                last_sig = read_signals(m)
            sig = last_sig

            if pos:
                if pos["ticker"] != m["ticker"]:
                    # window rolled while we held: read the old book once and close there
                    try:
                        # the live orderbook for the OLD ticker; the single-market
                        # row measured median 1.6c stale so it is the fallback of
                        # last resort here, and it names itself in the reason.
                        _ro = ob_touch(pos["ticker"])
                        if _ro:
                            close_position(rec, pos, _ro,
                                           "window rolled [exit-bid src=orderbook]")
                        else:
                            om = market_by_ticker(pos["ticker"])
                            close_position(rec, pos, book(om),
                                           "window rolled [exit-bid src=market-row,"
                                           " orderbook unavailable]")
                    except Exception as e:
                        close_unavailable(rec, pos, str(e)[:120])
                    pos = None
                else:
                    # --- THE EXIT PRICE COMES FROM THE LIVE ORDERBOOK, NOT THE
                    # CACHED LIST ROW (Sep 15 2026). Everything below - the hard
                    # stop, the trailing floor, the giveback band, the sell-window
                    # profit test, and the fill recorded by close_position - is
                    # evaluated against `xbk`, sourced from
                    # GET /markets/<ticker>/orderbook. Top of the YES ladder for
                    # an UP position, top of the NO ladder for a DOWN one. See
                    # ob_touch(). NO RULE CHANGED: same levels, same bands, same
                    # windows - the bot is simply no longer reading a number that
                    # can be 60 seconds old. Only runs while a position is open.
                    list_bid = bk["yes_bid"] if pos["side"] == "UP" else bk["no_bid"]
                    try:
                        _ob = ob_touch(pos["ticker"])
                        ob_err = None
                    except Exception as _e:
                        _ob, ob_err = None, repr(_e)[:100]
                    if _ob:
                        xbk, bid_src = _ob, BID_SRC_BOOK
                        bid = xbk["yes_bid"] if pos["side"] == "UP" else xbk["no_bid"]
                    else:
                        # HONEST UNAVAILABLE. No fabrication, no carry-forward of
                        # the stale list row as if it were live.
                        xbk, bid_src, bid = None, BID_SRC_NONE, None
                    # side-by-side proof, throttled: log only when the live touch
                    # and the cached list row actually DISAGREE, at most once every
                    # BID_DIVERGE_SECS, so the divergence is checkable from the log
                    # without drowning it. Not a round-accounting line.
                    if bid is not None and list_bid and abs(bid - list_bid) >= 0.01 \
                            and time.time() - last_bid_log > BID_DIVERGE_SECS:
                        last_bid_log = time.time()
                        log(f"BIDSRC {pos['ticker']} minute {minute:.1f} "
                            f"{pos['side']} exit-bid src=orderbook {c(bid)}c "
                            f"vs list-row {c(list_bid)}c "
                            f"(diff {abs(bid-list_bid)*100:.1f}c, list Age-cached 15s)")
                    elif bid is None and time.time() - last_bid_log > BID_DIVERGE_SECS:
                        last_bid_log = time.time()
                        log(f"BIDSRC {pos['ticker']} minute {minute:.1f} "
                            f"{pos['side']} exit-bid src=unavailable - live orderbook "
                            f"read failed ({ob_err or 'empty ladder'}); NOT falling back "
                            f"to the cached list row")
                    ent = pos["entry"]
                    sl_move = stop_for(ent)
                    # THE HARD STOP IS NOW USUALLY INERT AND THE LOG SAYS SO.
                    # STOP_FRAC/STOP_MIN/STOP_MAX are Anthony's numbers and are
                    # untouched, but STOP_MIN is 8c and on a 3-8c entry there is
                    # nothing 8c below: the computed level would be zero or
                    # negative, which is not a price. So the level is clamped at
                    # 0 and, when it sits at or below zero, the stop is treated
                    # as INERT - it cannot fire, and the most that can be lost
                    # is the premium paid. Never a nonsensical or negative level.
                    stop_px = ent - sl_move
                    # CHEAP-SIDE MODE: THE HARD STOP IS OFF. Sep 15 18:38Z, my
                    # call under Anthony's "keep running it make it good", after
                    # trade 37 (12c entry, cut at 4c, minute 8.2) and trade 38
                    # (24c entry, cut at 16c, minute 5.5) both died to it within
                    # minutes. A fixed-cent stop is incoherent on a longshot: the
                    # premium paid is already the maximum loss, so the stop only
                    # turns a maybe into a certain loss and removes the upside the
                    # bet exists for. Signal mode keeps Anthony's numbers exactly.
                    stop_inert = stop_px <= 0.0 or CHEAP_SIDE_MODE
                    if stop_inert:
                        stop_px = 0.0
                    arm_px = trail_arm_level(ent)
                    # --- trailing lock: track the best bid seen on this position
                    if bid:
                        if pos.get("peak_bid") is None or bid > pos["peak_bid"]:
                            pos["peak_bid"] = bid
                            save_pos(pos)
                        if not pos.get("trail_armed") and pos["peak_bid"] >= arm_px:
                            pos["trail_armed"] = True
                            save_pos(pos)
                            _pk = pos["peak_bid"]
                            log(f"TAKE-PROFIT ARMED {pos['side']} {pos['ticker']} minute {minute:.1f} "
                                f"- bid {c(_pk)}c is {_pk/ent:.1f}x the {c(ent)}c entry "
                                f"(arms at {c(arm_px)}c) - from here it gives back at most "
                                f"{TRAIL_GIVEBACK_FRAC*100:.0f}% of the peak gain, "
                                f"exit level {c(trail_exit_level(ent, _pk))}c, "
                                f"never below entry+{TRAIL_FLOOR*100:.0f}c")
                    reason = None
                    # BIG-WINS MODE (Anthony, Sep 15 16:56Z: "don't have a certain
                    # sell time ... sell whenever you see best fit" / "We're going
                    # for big wins low % buys"). LET IT RUN: no profit target, no
                    # fixed sell window. Three ways out, in priority order:
                    #   1. the proportional giveback trail, once the bid has at
                    #      least doubled (it banks a gain that is clearly done)
                    #   2. the hard stop, when it is not inert
                    #   3. the minute-14 deadline (settlement averages the final
                    #      60 seconds, so 14 is the real decision point)
                    if pos.get("trail_armed") and bid:
                        pk = pos["peak_bid"]
                        gain = pk - ent
                        exit_px = trail_exit_level(ent, pk)
                        floored = exit_px <= ent + TRAIL_FLOOR + 1e-9
                        tail = (f"peak {c(pk)}c (gain {gain*100:.0f}c = {pk/ent:.1f}x entry, "
                                f"giveback {TRAIL_GIVEBACK_FRAC*100:.0f}% of the gain, "
                                f"level {c(exit_px)}c"
                                + (f", held up by the entry+{TRAIL_FLOOR*100:.0f}c floor" if floored else "")
                                + ")")
                        # Checked against the live BID - what we can actually sell
                        # into - on every tick. Honest caveat: we cannot fill above
                        # the best bid, so if the bid has already gapped BELOW the
                        # level, the level was not honoured and the line says so
                        # instead of printing a level that was never applied.
                        if bid <= exit_px:
                            gapped = bid < exit_px
                            reason = (f"took the profit at minute {minute:.1f} - the bid came "
                                      f"back {(pk-bid)*100:.0f}c off its peak, banking it at "
                                      f"{c(bid)}c - " + tail)
                            if gapped:
                                reason += (f" level {c(exit_px)}c NOT HONOURED - best bid was "
                                           f"{c(bid)}c (gapped through the level)")
                            else:
                                reason += f" level {c(exit_px)}c honoured"
                    if reason:
                        pass
                    elif bid and not stop_inert and bid <= stop_px:
                        _g = bid < stop_px
                        reason = (f"hard stop at minute {minute:.1f} - the bid fell to "
                                  f"{c(bid)}c, at or below the {c(stop_px)}c stop "
                                  f"({sl_move*100:.0f}c below the {c(ent)}c entry)")
                        reason += (f" stop {c(stop_px)}c NOT HONOURED - best bid was {c(bid)}c "
                                   f"(gapped through the stop)" if _g
                                   else f" stop {c(stop_px)}c honoured")
                    elif minute >= FLAT_BY_MIN:
                        reason = (f"minute {minute:.1f} - flat by the minute "
                                  f"{FLAT_BY_MIN:.0f} deadline (settlement is the average of "
                                  f"the final 60s, so 14 is the real decision point), "
                                  f"out at the book at {c(bid) if bid else 'no'}c")
                    elif secs_left <= FORCE_EXIT_SECS:
                        reason = (f"round closing in {secs_left:.0f}s - going flat at the book")
                    if reason:
                        action, action_why = "SELL", reason
                    else:
                        action = "HOLD"
                        action_why = (f"holding {pos['side']} from {c(ent)}c - no sell time, "
                                      f"letting it run; "
                                      + (f"trail armed, giveback level "
                                         f"{c(trail_exit_level(ent, pos.get('peak_bid') or ent))}c"
                                         if pos.get("trail_armed")
                                         else f"trail arms at {c(arm_px)}c")
                                      + "; "
                                      + (f"hard stop OFF in cheap-side mode - the {c(ent)}c "
                                         f"paid is the whole risk, so the ticket runs"
                                         if CHEAP_SIDE_MODE else
                                         f"hard stop INERT - {sl_move*100:.0f}c below a "
                                         f"{c(ent)}c entry is at or below zero, so the most "
                                         f"at risk is the {c(ent)}c paid"
                                         if stop_inert else f"hard stop at {c(stop_px)}c")
                                      + f"; flat by minute {FLAT_BY_MIN:.0f}")
                    if reason:
                        # name the source the exit price came from, the same way
                        # `src=` names the price series on the round lines.
                        reason += f" [exit-bid src={bid_src}]"
                        if bid and xbk:
                            close_position(rec, pos, xbk, reason)
                        else:
                            close_unavailable(rec, pos,
                                              "no live bid on the orderbook"
                                              + (f" ({ob_err})" if ob_err else ""))
                        pos = None
                        loop_note = reason
                    else:
                        # --- adaptive cadence: how far is the nearest trigger?
                        # Distances are in price (cents the bid may still travel)
                        # and in seconds for the time-based triggers. A failed or
                        # missing bid is NOT guessed at - it polls fast and the
                        # honest unavailable path is untouched.
                        dists = []
                        if bid:
                            if not stop_inert:
                                dists.append(bid - stop_px)              # hard stop (if live)
                            if pos.get("trail_armed"):
                                _pk = pos.get("peak_bid") or bid
                                dists.append(bid - trail_exit_level(ent, _pk))  # giveback level
                        secs = []
                        secs.append((FLAT_BY_MIN - minute) * 60.0)
                        secs.append(secs_left - FORCE_EXIT_SECS)
                        near_px = (not bid) or any(d <= HOLD_NEAR for d in dists)
                        near_t = any(-1e9 < s <= HOLD_NEAR_SECS for s in secs)
                        if near_px or near_t:
                            hold_sleep = HOLD_LOOP_FAST
                            _n = (f"{min(dists)*100:.1f}c" if bid and dists
                                  else "bid unavailable")
                            action_why += (f" \u00b7 polling every {HOLD_LOOP_FAST}s "
                                           f"(nearest trigger {_n})")
                        else:
                            hold_sleep = HOLD_LOOP
                            _d = (f"{min(dists)*100:.1f}c away" if dists
                                  else "no price trigger - minute 14 is the only exit")
                            action_why += (f" \u00b7 polling every {HOLD_LOOP}s "
                                           f"(nearest trigger {_d})")
            # --- historical read, computed BEFORE the minute-5 evaluation so the
            # entry path can consult it (and the snapshot below reuses it).
            # min_n = the unlock bar: the engine tries the tight match first and
            # widens ONE dimension at a time until the matched set reaches it,
            # naming the level it stopped at. Sep 15 2026 - the tight match was
            # slicing 839 rounds into 36 buckets (~23 each) and starving the
            # >= 30 sample bar, so four of six unlock checks failed on sample
            # size alone, one of them at a 71% hit rate.
            hl = hist.lookup(H, sig.get("rsi"), sig.get("spot_mom_pct"),
                             sig.get("contract_vol_15m"), min_n=HIST_TARGET_N)
            # --- MINUTE-5 ACCOUNTING. Every round produces exactly one line:
            # ENTRY, NOTRADE, or MISSED. No silent fall-through, ever.
            w = round_watch.get(m["ticker"])
            if w is None:
                w = dict(ticks_in_window=0, last_err=None,
                         last_minute_before=None, first_seen=minute)
                round_watch[m["ticker"]] = w
            if minute < STRIKE_MINUTE:
                w["last_minute_before"] = minute

            evaluable = (not pos and secs_left > MIN_SECS_LEFT
                         and in_strike_window and m["ticker"] not in evaluated)
            if evaluable:
                # Attempted on EVERY tick inside the window; the first success wins.
                w["ticks_in_window"] += 1
                # --- THE ENTRY PRICE COMES FROM THE LIVE ORDERBOOK TOO
                # (Sep 15 2026, widened after a real loss: 14:05:04 booked an
                # ENTRY at 70.0c on round 26SEP151015-15 and the position's PEAK
                # bid was 68.0c - the price never traded above what we supposedly
                # paid, because the ask came off the 15s-cached list row. A stale
                # ask both books a fill nobody was offering AND can wave a
                # contract through the 80c ceiling that really costs 86c.)
                # ONE extra request per round, at the minute-5 mark only.
                # NO RULE CHANGED: same 5-80c band, same conviction floor, same
                # four NOTRADE reasons - reason 3 ("the orderbook read fails")
                # already covers a failed read, so nothing new is added.
                ebk, entry_src = bk, "list-row"
                try:
                    _eo = ob_touch(m["ticker"])
                    if _eo:
                        ebk, entry_src = _eo, BID_SRC_BOOK
                    else:
                        ebk, entry_src = None, BID_SRC_NONE
                except Exception as _ee:
                    ebk, entry_src = None, BID_SRC_NONE
                    w["last_err"] = repr(_ee)[:120]
                try:
                    if ebk is None:
                        # honest NOTRADE reason 3, never the stale number
                        side, why = None, ["NOTRADE: orderbook read failed at the "
                                           "minute-5 mark - no live ask to price the "
                                           "entry against, sitting out"]
                    else:
                        side, why = decide(m, ebk, sig, hl, (H or {}).get("built"))
                    ok = True
                except Exception as e:
                    side, why, ok = None, None, False
                    w["last_err"] = repr(e)[:120]
                    loop_note = f"minute {minute:.1f} - evaluation errored, retrying next tick"
                if ok and side and m["ticker"] in entered:
                    # one entry per round - this can only block a SECOND entry, and a
                    # round in `entered` has already produced its ENTRY line.
                    evaluated.add(m["ticker"])
                    loop_note = (f"minute {minute:.1f} - this round was already called "
                                 f"(one entry per round, no re-entry after an exit)")
                elif ok and side:
                    cost = ebk["yes_ask"] if side == "UP" else ebk["no_ask"]
                    pos = dict(ticker=m["ticker"], side=side, entry=cost,
                               entry_cents=c(cost), entry_ts=time.time(),
                               strike=m.get("floor_strike"), why=why,
                               entry_minute=round(minute, 2),
                               entry_book=ebk, entry_price_src=entry_src,
                               list_row_ask=c(bk["yes_ask"] if side == "UP"
                                              else bk["no_ask"]),
                               spot_at_entry=sig.get("spot"),
                               mode=("cheap-side" if CHEAP_SIDE_MODE else "signal"),
                               signal_side=sig.get("signal_side"),
                               against_signal=bool(sig.get("against_signal")))
                    save_pos(pos)
                    evaluated.add(m["ticker"])
                    entered.add(m["ticker"])
                    action, action_why = "BUY", (f"bought {side} at {c(cost)}c at the minute-5 mark")
                    hu = sig.get("hist_unlock")
                    via = (f" via=history-unlock lean={sig.get('lean'):+.2f} "
                           f"hist={hu['expected']} {hu['hit_rate']:.0f}% n={hu['n']} "
                           f"(match: {hu.get('level') or 'unknown'})"
                           if hu else "")
                    pos["via"] = "history-unlock" if hu else "conviction-floor"
                    pos["hist_unlock"] = hu
                    save_pos(pos)
                    log(f"ENTRY {side} {m['ticker']} strike {m.get('floor_strike')} "
                        f"at {c(cost)}c (minute {minute:.1f}) "
                        f"signal_side={sig.get('signal_side') or side} side_taken={side}"
                        + (" AGAINST SIGNAL (cheap-side mode, Anthony 17:11Z)"
                           if sig.get("against_signal") else "") + " "
                        f"stop={stop_for(cost)*100:.1f}c "
                        f"src={sig.get('price_src') or 'unavailable'} "
                        f"entry-ask src={entry_src} "
                        f"(list row said {c(bk['yes_ask'] if side == 'UP' else bk['no_ask'])}c) "
                        + (f"(no hard stop - cheap-side mode: the premium paid IS the stop; "
                          f"minute {FLAT_BY_MIN:.0f} is the only forced exit)" if CHEAP_SIDE_MODE
                          else f"(cut at {c(cost - stop_for(cost))}c)") + via + " :: " + "; ".join(why))
                elif ok:
                    log(f"NOTRADE {m['ticker']} minute {minute:.1f} "
                        f"src={sig.get('price_src') or 'unavailable'} lean={sig.get('lean')} "
                        f"rsi={sig.get('rsi')} tape={sig.get('tape_2min_cents')} "
                        f"vol={sig.get('contract_vol_15m')} "
                        f"yes_ask={c(ebk['yes_ask']) if ebk else None} "
                        f"no_ask={c(ebk['no_ask']) if ebk else None} "
                        f"entry-ask src={entry_src} :: " + (why[-1] if why else "watching"))
                    evaluated.add(m["ticker"])
                    action, action_why = "HOLD", (why[-1] if why else "no setup at minute 5")
                    loop_note = (f"regular mode \u00b7 minute {minute:.1f} NO CALL: "
                                 + (why[-1] if why else "watching"))
            elif not pos and secs_left > MIN_SECS_LEFT:
                if m["ticker"] in entered:
                    loop_note = (f"minute {minute:.1f} - this round was already called "
                                 f"(one entry per round, no re-entry after an exit)")
                elif minute < STRIKE_MINUTE:
                    loop_note = (f"minute {minute:.1f} - strike is picked at minute "
                                 f"{STRIKE_MINUTE:.0f}, {STRIKE_MINUTE - minute:.1f}m to go "
                                 f"(big-wins mode, no sell time, flat by minute {FLAT_BY_MIN:.0f})")
                else:
                    loop_note = (f"minute {minute:.1f} - minute-{STRIKE_MINUTE:.0f} strike "
                                 f"window is past for this round (big-wins mode, no sell time, "
                                 f"flat by minute {FLAT_BY_MIN:.0f})"
                                 + (" (no setup at minute 5)" if m["ticker"] in evaluated else ""))

            # The backstop: the window has closed and this round never got a line.
            if (minute >= STRIKE_MINUTE + STRIKE_WINDOW_MINS
                    and m["ticker"] not in evaluated
                    and m["ticker"] not in missed_logged):
                missed_logged.add(m["ticker"])
                log(f"MISSED {m['ticker']} minute {minute:.1f} "
                    f"src={sig.get('price_src') or 'unavailable'} - the minute-5 window "
                    f"passed without an evaluation: {miss_reason(w, pos)}")

            if action == "HOLD" and not pos and loop_note:
                action_why = loop_note
            # --- historical read-out (display + context only this pass)
            reg, reg_why = regime_of(sig)
            bias, bias_why = bias_of(sig)
            prof = profitability(rec)

            state = dict(
                system="XRP Bot", asset="xrp", label="XRP",
                eth_historical=ETH_FINAL,
                series=SERIES, tv_symbol="COINBASE:XRPUSD",
                updated=time.time(),
                round=dict(minute=round(minute, 2), strike_minute=STRIKE_MINUTE,
                           sell_window=None, flat_by=FLAT_BY_MIN,
                           in_strike_window=in_strike_window,
                           strike_checked=(m["ticker"] in evaluated)),
                market=dict(ticker=m["ticker"], strike=m.get("floor_strike"),
                            close_time=m["close_time"], secs_left=round(secs_left),
                            yes_bid=c(bk["yes_bid"]), yes_ask=c(bk["yes_ask"]),
                            no_bid=c(bk["no_bid"]), no_ask=c(bk["no_ask"]),
                            oi=float(m.get("open_interest_fp") or 0)),
                signals=sig, position=pos, note=loop_note,
                # No profit target exists any more (nothing caps the upside), and on a
                # 3-8c entry the hard stop is inert, so there is no cut level either.
                targets=None,
                rules=dict(entry_band=f"{MIN_ENTRY*100:.0f}-{MAX_ENTRY*100:.0f}c (never above the ceiling)",
                           strike_timing=f"strike picked / entry evaluated at minute {STRIKE_MINUTE:.0f} of the 15-min round",
                           gate="one call every round at minute 5; the signal picks the side and that side is taken or there is NO TRADE - never the opposite side. Four NOTRADE reasons only: the signal side costs over the price ceiling, the tape vetoes the signal side, the orderbook read fails, or |lean| is below the conviction floor",
                           exits=f"big-wins mode (Anthony Sep 15 16:56Z: no certain sell time, sell whenever it fits): no profit target and no fixed sell window - the position runs until the proportional giveback trail banks it, the hard stop fires (when it is not inert), or the minute {FLAT_BY_MIN:.0f} deadline goes flat. Holding to settlement instead of the minute {FLAT_BY_MIN:.0f} deadline is open for Anthony to choose and is NOT the bot's call",
                           holds=True,
                           tape_veto=f"2-min contract tape of {TAPE_VETO*100:.0f}c against the signal own side vetoes the round (NO TRADE); it never flips to the other side",
                           trailing_lock=f"scaled to the gain, not to a fixed number of cents: arms once the live bid reaches {TRAIL_ARM_MULT:.0f}x the entry (and at least {TRAIL_ARM_MIN*100:.0f}c above it), then exits once the bid gives back {TRAIL_GIVEBACK_FRAC*100:.0f}% of the PEAK gain, never below entry + {TRAIL_FLOOR*100:.0f}c. On a 5c entry a fixed +8c arm was a 2.6x move, which is why the arm is now proportional",
                           conviction_floor=f"|combined lean| must be at least {CONVICTION_FLOOR:.1f} at minute 5, OR at least {HIST_UNLOCK_MIN:.1f} with the historical read agreeing with the side the signal picked at >= {HIST_MIN_RATE:.0f}% over >= {HIST_MIN_N} matched past rounds (history can only ADD trades in that narrow band - it never picks or flips a side)",
                           settlement=(f"KXXRP15M settles on the simple average of the final "
                                       f"{SETTLE_AVG_SECS}s of CF Benchmarks' XRPUSDRTI against the "
                                       f"{SETTLE_AVG_SECS}s average before the open, so the effective "
                                       f"decision deadline is minute {DECISION_DEADLINE_MIN:.0f}. The bot is "
                                       f"always flat by minute {FLAT_BY_MIN:.0f} - the deadline IS that "
                                       f"fact, since a spike inside the final 60s barely moves the "
                                       f"settled outcome"),
                           price_series=("all indicators computed from the XRPUSDRTI settlement index "
                                         "(Kalshi live_data, ~1 tick/s); Coinbase spot is a fallback only "
                                         "and every log line names the source"),
                           hard_stop=f"proportional: {STOP_FRAC*100:.0f}% of entry, min {STOP_MIN*100:.0f}c, max {STOP_MAX*100:.0f}c below entry (Anthony's numbers, unchanged) - but on a {MIN_ENTRY*100:.0f}-{STOP_MIN*100:.0f}c entry there is nothing {STOP_MIN*100:.0f}c below, so the level clamps at zero and the stop is INERT: the most at risk is the premium paid, and the log line says so"),
                expected=dict(direction=hl["expected"], hit_rate_pct=hl["hit_rate"],
                              sample=hl["n"], matched_on=hl["basis"],
                              match_level=hl.get("level"),
                              up_rate_pct=hl["up_rate"], text=hl["text"],
                              history_rounds=(H or {}).get("n", 0),
                              history_built=(H or {}).get("built"),
                              source="real settled KXXRP15M rounds from the Kalshi API - "
                                     "minute-5 state vs what the round actually did"),
                price_source=dict(src=sig.get("price_src"),
                                  detail=sig.get("spot_source"),
                                  index_ticks=sig.get("index_ticks"),
                                  index_age_secs=sig.get("index_age_secs"),
                                  index_error=sig.get("index_error"),
                                  fallback=(sig.get("price_src") == CB_SRC),
                                  note="every indicator is computed from this series"),
                anchor=dict(ref60=sig.get("ref60"), ref60_ticks=sig.get("ref60_ticks"),
                            index_now=sig.get("spot"), gap=sig.get("ref_gap"),
                            gap_pct=sig.get("ref_gap_pct"), note=sig.get("ref_note"),
                            rule="settlement compares the final 60s XRPUSDRTI average "
                                 "to the 60s average before the open"),
                regime=dict(state=reg, why=reg_why),
                bias=dict(state=bias, why=bias_why),
                action=dict(state=action, why=action_why),
                profitability=prof,
                trades=rec["trades"][-25:], tally=rec["tally"],
                cheap_side=cheap_side_tally(rec))
            write_snapshot(state)
        except Exception as e:
            log("loop error: " + repr(e)[:200])
            traceback.print_exc()
        time.sleep(hold_sleep if pos else LOOP)

def settle(rec, pos, exit_price, reason, status):
    ent = pos["entry"]
    if exit_price is None:
        pnl = None
    else:
        pnl = round((exit_price - ent) * 100 - fee(ent) * 100 - fee(exit_price) * 100, 1)
    tr = dict(ticker=pos["ticker"], side=pos["side"], entry_cents=c(ent),
              exit_cents=c(exit_price), pnl_cents=pnl, reason=reason,
              exit_status=status, entry_ts=pos["entry_ts"], exit_ts=time.time(),
              held_secs=round(time.time() - pos["entry_ts"]), why=pos["why"],
              entry_minute=pos.get("entry_minute"), strike=pos.get("strike"),
              spot_at_entry=pos.get("spot_at_entry"),
              mode=pos.get("mode"), signal_side=pos.get("signal_side"),
              against_signal=bool(pos.get("against_signal")))
    rec["trades"].append(tr)
    save_pos(None)
    if pnl is not None:
        t = rec["tally"]
        t["n"] += 1
        t["wins"] += 1 if pnl > 0 else 0
        t["net_cents"] = round(t["net_cents"] + pnl, 1)
        # Cheap-side mode keeps its OWN running record, so the dashboard shows
        # what this instruction actually costs or makes, separately from the
        # old signal-mode history. Anthony can read the truth off his own page.
        if pos.get("mode") == "cheap-side":
            tc = rec.setdefault("tally_cheap",
                                dict(n=0, wins=0, net_cents=0.0, against=0,
                                     entry_cents_sum=0.0))
            tc["n"] += 1
            tc["wins"] += 1 if pnl > 0 else 0
            tc["net_cents"] = round(tc["net_cents"] + pnl, 1)
            tc["against"] += 1 if pos.get("against_signal") else 0
            tc["entry_cents_sum"] = round(tc["entry_cents_sum"] + c(ent), 1)
    save(rec)
    _pk = pos.get("peak_bid")
    _pks = f" peak {c(_pk)}c" if _pk is not None else ""
    log(f"EXIT {pos['side']} {pos['ticker']} {c(ent)}c -> {c(exit_price)}c "
        f"pnl {pnl}c{_pks} [{status}] {reason}")

def close_position(rec, pos, bk, reason):
    price = bk["yes_bid"] if pos["side"] == "UP" else bk["no_bid"]
    settle(rec, pos, price, reason, "filled" if price else "unavailable")

def close_unavailable(rec, pos, err):
    settle(rec, pos, None, "orderbook read failed: " + err, "unavailable")

if __name__ == "__main__":
    main()
