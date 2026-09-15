#!/usr/bin/env python3
"""
kalshi_tape.py - a LIVE TAPE RECORDER for any Kalshi 15-minute crypto series.

Usage: python3 kalshi_tape.py KXXRP15M   (series is argv[1], default KXXRP15M)
Files land in BD/tape/<SERIES>/<ticker>.ndjson so two series never share a directory.

Anthony, Sep 15 2026 20:58Z: "Literally WATCH and make ur own data".

Standalone. Imports NOTHING from eth_bot.py (which is live and trading).
Writes one newline-delimited-JSON file per round to BD/tape/<ticker>.ndjson,
append-only and flushed every line, so a restart resumes mid-round.

Record types (field "k"):
  round   - one per round on first sight: strike, open/close time, pre-open 60s index avg
  book    - every ~2s: orderbook touch + depth at touch and within 3c, both ladders
  index   - every ~10s: new CF Benchmarks ETHUSDRTI 1s ticks since the last one written
  trades  - every ~15s: recent trade prints
  settle  - once, after close: official result
  hb      - heartbeat every 30s, with loop error counters (liveness checkable from outside)
  err     - any exception text, never fatal

PRICE SOURCE RULE (Bug #6, cost real money): the touch comes ONLY from
GET /markets/{ticker}/orderbook. The list endpoint /markets?series_ticker=...
serves Cache-Control max-age=15 and freezes for up to 66s; it is used here for
ROUND DISCOVERY ONLY (ticker / open / close / strike), never for a price.
"""
import datetime as dt
import glob
import gzip
import json
import math
import os
import sys
import time
import traceback
import urllib.parse
import urllib.error
import urllib.request

BD = os.path.dirname(os.path.abspath(__file__))
TAPE = os.path.join(BD, "tape", (sys.argv[1] if len(sys.argv) > 1 else "KXXRP15M").strip().upper())
LOGDIR = os.path.join(BD, "logs")
os.makedirs(TAPE, exist_ok=True)
os.makedirs(LOGDIR, exist_ok=True)

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_EXT = "https://external-api.kalshi.com/trade-api/v2"
SERIES = (sys.argv[1] if len(sys.argv) > 1 else "KXXRP15M").strip().upper()
UA = {"User-Agent": "Mozilla/5.0 (tape recorder)"}

BOOK_EVERY = 5.0
INDEX_EVERY = 20.0
TRADES_EVERY = 20.0
HB_EVERY = 30.0
DISCOVER_EVERY = 60.0
DEPTH_CENTS = 0.03
DISK_LIMIT_MB = 500

STATE = dict(ticks=0, errors=0, last_err="", started=time.time(),
             backoff_until=0.0, rate_limited=0)


def now():
    return time.time()


def utc(ts=None):
    return dt.datetime.fromtimestamp(ts or time.time(), dt.timezone.utc).isoformat()


def slog(msg):
    line = f"{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)


def get(url, params=None, timeout=12):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    # The LIVE trading bot shares Kalshi's rate limit with this recorder and its
    # fills are the priority. Any 429 puts the whole tape to sleep for 30s.
    while time.time() < STATE.get("backoff_until", 0):
        time.sleep(1)
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            STATE["backoff_until"] = time.time() + 30
            STATE["rate_limited"] = STATE.get("rate_limited", 0) + 1
            slog("429 from Kalshi - backing off 30s (live bot has priority)")
        raise


def iso_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# ------------------------------------------------------------------ writing
_handles = {}


def writer(ticker):
    h = _handles.get(ticker)
    if h is None:
        for t, fh in list(_handles.items()):
            try:
                fh.close()
            except Exception:
                pass
            _handles.pop(t, None)
        h = open(os.path.join(TAPE, f"{ticker}.ndjson"), "a")
        _handles[ticker] = h
    return h


def emit(ticker, rec):
    rec["ts"] = round(now(), 3)
    rec["iso"] = utc(rec["ts"])
    h = writer(ticker)
    h.write(json.dumps(rec, separators=(",", ":")) + "\n")
    h.flush()


def resume_state(ticker):
    """What is already on disk for this round, so a restart does not duplicate."""
    p = os.path.join(TAPE, f"{ticker}.ndjson")
    st = dict(has_round=False, settled=False, last_index_t=0.0, seen_trades=set())
    if not os.path.exists(p):
        return st
    try:
        with open(p) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                k = r.get("k")
                if k == "round":
                    st["has_round"] = True
                elif k == "settle":
                    st["settled"] = True
                elif k == "index":
                    for tk in r.get("ticks", []):
                        st["last_index_t"] = max(st["last_index_t"], tk[0])
                elif k == "trades":
                    for tr in r.get("trades", []):
                        st["seen_trades"].add(tr.get("id"))
    except Exception:
        pass
    return st


# ------------------------------------------------------------------ fetchers
def open_markets():
    j = get(f"{KALSHI}/markets", dict(series_ticker=SERIES, status="open", limit=20))
    ms = [m for m in j.get("markets", []) if m.get("status") in ("active", "open")]
    ms.sort(key=lambda m: m["close_time"])
    return ms


def market_by_ticker(t):
    return get(f"{KALSHI}/markets/{t}")["market"]


def ladder_stats(levels, best, side_is_yes):
    """levels ascend in price. Return (best, size_at_best, size_within_3c)."""
    if not levels:
        return None, 0.0, 0.0
    at = 0.0
    near = 0.0
    for lv in levels:
        try:
            px = float(lv[0])
            sz = float(lv[1])
        except Exception:
            continue
        if best is not None and abs(px - best) < 1e-9:
            at += sz
        if best is not None and px >= best - DEPTH_CENTS - 1e-9:
            near += sz
    return best, at, near


def ob_full(ticker):
    j = get(f"{KALSHI}/markets/{ticker}/orderbook")
    ob = j.get("orderbook_fp") or j.get("orderbook") or {}
    yes = ob.get("yes_dollars") or []
    no = ob.get("no_dollars") or []

    def top(lv):
        if not lv:
            return None
        try:
            return float(lv[-1][0])
        except Exception:
            return None

    yb, nb = top(yes), top(no)
    if yb is None and nb is None:
        return None
    _, y_at, y_near = ladder_stats(yes, yb, True)
    _, n_at, n_near = ladder_stats(no, nb, False)
    return dict(
        yes_bid=yb, no_bid=nb,
        yes_ask=(round(1.0 - nb, 4) if nb is not None else None),
        no_ask=(round(1.0 - yb, 4) if yb is not None else None),
        yes_bid_sz=y_at, yes_bid_sz3=y_near,
        no_bid_sz=n_at, no_bid_sz3=n_near,
        yes_levels=len(yes), no_levels=len(no),
    )


def index_ticks(event_ticker, rng="15min"):
    j = get(f"{KALSHI_EXT}/live_data/events/{event_ticker}", dict(range=rng), timeout=20)
    det = (j.get("live_data") or {}).get("details") or {}
    ts = det.get("timeseries") or []
    return [(int(x["t"]) / 1000.0, float(x["v"])) for x in ts if x.get("v")]


def _f(*vals):
    for v in vals:
        if v not in (None, ""):
            try:
                return float(v)
            except Exception:
                continue
    return None


def trades(ticker, limit=50):
    j = get(f"{KALSHI}/markets/trades", dict(ticker=ticker, limit=limit))
    return j.get("trades", [])


# ------------------------------------------------------------------ housekeeping
def disk_mb():
    tot = 0
    for p in glob.glob(os.path.join(TAPE, "*")):
        try:
            tot += os.path.getsize(p)
        except Exception:
            pass
    return tot / 1e6


def gzip_old(current_ticker):
    if disk_mb() < DISK_LIMIT_MB:
        return
    files = sorted(glob.glob(os.path.join(TAPE, "*.ndjson")), key=os.path.getmtime)
    for p in files[:-2]:
        if current_ticker and os.path.basename(p).startswith(current_ticker):
            continue
        try:
            with open(p, "rb") as fin, gzip.open(p + ".gz", "wb") as fout:
                fout.writelines(fin)
            os.remove(p)
        except Exception:
            pass


# ------------------------------------------------------------------ main
def main():
    slog(f"{SERIES} tape starting - orderbook {BOOK_EVERY}s, index {INDEX_EVERY}s, "
         f"trades {TRADES_EVERY}s, heartbeat {HB_EVERY}s (live bot has rate-limit priority)")
    cur = None           # market dict
    st = None            # resume state
    last = dict(book=0.0, index=0.0, tr=0.0, hb=0.0, disc=0.0)
    pending_settle = []  # (ticker, close_ts) awaiting official result

    while True:
        loop_started = now()
        try:
            # ---- discover / roll the round
            if cur is None or now() - last["disc"] > DISCOVER_EVERY:
                last["disc"] = now()
                try:
                    ms = open_markets()
                except Exception as e:
                    ms = []
                    STATE["errors"] += 1
                    STATE["last_err"] = f"discover: {e}"
                if ms:
                    m = ms[0]
                    if cur is None or m["ticker"] != cur["ticker"]:
                        if cur is not None:
                            pending_settle.append((cur["ticker"], iso_ts(cur["close_time"])))
                        cur = m
                        st = resume_state(m["ticker"])
                        slog(f"round {m['ticker']} strike={m.get('floor_strike')} "
                             f"resume={'yes' if st['has_round'] else 'new'}")
                        if not st["has_round"]:
                            ot = iso_ts(m["open_time"])
                            pre = None
                            npre = 0
                            try:
                                tk = index_ticks(m.get("event_ticker") or "")
                                vals = [v for (t, v) in tk if ot - 60 <= t < ot]
                                if vals:
                                    pre = round(sum(vals) / len(vals), 4)
                                    npre = len(vals)
                            except Exception as e:
                                STATE["errors"] += 1
                                STATE["last_err"] = f"preopen: {e}"
                            emit(m["ticker"], dict(
                                k="round", ticker=m["ticker"],
                                event_ticker=m.get("event_ticker"),
                                title=m.get("title"),
                                strike=m.get("floor_strike"),
                                open_time=m.get("open_time"),
                                close_time=m.get("close_time"),
                                open_ts=ot, close_ts=iso_ts(m["close_time"]),
                                preopen_index_avg60=pre, preopen_ticks=npre,
                            ))
                        gzip_old(m["ticker"])

            if cur is None:
                time.sleep(2)
                continue

            ot = iso_ts(cur["open_time"])
            ct = iso_ts(cur["close_time"])
            minute = round((now() - ot) / 60.0, 3)

            # ---- orderbook touch + depth, every 2s
            if now() - last["book"] >= BOOK_EVERY:
                last["book"] = now()
                try:
                    b = ob_full(cur["ticker"])
                    if b is None:
                        emit(cur["ticker"], dict(k="book", minute=minute, empty=True))
                    else:
                        b.update(k="book", minute=minute)
                        emit(cur["ticker"], b)
                    STATE["ticks"] += 1
                except Exception as e:
                    STATE["errors"] += 1
                    STATE["last_err"] = f"book: {e}"
                    emit(cur["ticker"], dict(k="err", where="book", minute=minute, err=str(e)))

            # ---- settlement index ticks, every 10s (only new ones)
            if now() - last["index"] >= INDEX_EVERY:
                last["index"] = now()
                try:
                    tk = index_ticks(cur.get("event_ticker") or "")
                    since = st["last_index_t"] if st else 0.0
                    new = [[round(t, 3), v] for (t, v) in tk if t > since]
                    if new:
                        st["last_index_t"] = max(t for t, _ in new)
                        emit(cur["ticker"], dict(k="index", minute=minute,
                                                 n=len(new), ticks=new))
                except Exception as e:
                    STATE["errors"] += 1
                    STATE["last_err"] = f"index: {e}"
                    emit(cur["ticker"], dict(k="err", where="index", minute=minute, err=str(e)))

            # ---- trade prints, every 15s
            if now() - last["tr"] >= TRADES_EVERY:
                last["tr"] = now()
                try:
                    tr = trades(cur["ticker"])
                    fresh = [t for t in tr if t.get("trade_id") not in st["seen_trades"]]
                    for t in fresh:
                        st["seen_trades"].add(t.get("trade_id"))
                    if fresh:
                        emit(cur["ticker"], dict(
                            k="trades", minute=minute, n=len(fresh),
                            trades=[dict(id=t.get("trade_id"),
                                         yes=_f(t.get("yes_price_dollars"), t.get("yes_price")),
                                         no=_f(t.get("no_price_dollars"), t.get("no_price")),
                                         count=_f(t.get("count_fp"), t.get("count")),
                                         taker=t.get("taker_side"),
                                         taker_book=t.get("taker_book_side"),
                                         t=t.get("created_time")) for t in fresh]))
                except Exception as e:
                    STATE["errors"] += 1
                    STATE["last_err"] = f"trades: {e}"

            # ---- settlement results for rounds that have closed
            if pending_settle:
                still = []
                for (tk_, cts) in pending_settle:
                    if now() < cts + 20:
                        still.append((tk_, cts))
                        continue
                    try:
                        m = market_by_ticker(tk_)
                        if m.get("result") in (None, ""):
                            if now() < cts + 900:
                                still.append((tk_, cts))
                            continue
                        emit(tk_, dict(k="settle", ticker=tk_, result=m.get("result"),
                                       status=m.get("status"),
                                       settlement_value=m.get("settlement_value"),
                                       last_price=m.get("last_price"),
                                       volume=m.get("volume")))
                        slog(f"settled {tk_} -> {m.get('result')}")
                    except Exception as e:
                        STATE["errors"] += 1
                        STATE["last_err"] = f"settle: {e}"
                        still.append((tk_, cts))
                pending_settle = still

            # ---- heartbeat
            if now() - last["hb"] >= HB_EVERY:
                last["hb"] = now()
                hb = dict(k="hb", minute=minute, ticker=cur["ticker"],
                          ticks=STATE["ticks"], errors=STATE["errors"], rate_limited=STATE["rate_limited"],
                          last_err=STATE["last_err"][:200],
                          uptime_s=round(now() - STATE["started"], 1),
                          pid=os.getpid(), tape_mb=round(disk_mb(), 2))
                emit(cur["ticker"], hb)
                try:
                    with open(os.path.join(BD, f"tape_heartbeat_{SERIES}.json"), "w") as f:
                        json.dump(dict(hb, iso=utc()), f)
                except Exception:
                    pass
                slog(f"hb {cur['ticker']} min {minute} ticks {STATE['ticks']} "
                     f"errors {STATE['errors']} {STATE['last_err'][:80]}")

            # round is over -> force a discovery next loop
            if now() > ct + 5:
                last["disc"] = 0.0

        except Exception as e:
            STATE["errors"] += 1
            STATE["last_err"] = f"loop: {e}"
            slog("LOOP ERROR " + repr(e))
            traceback.print_exc()
            time.sleep(1)

        slept = now() - loop_started
        time.sleep(max(0.2, 1.0 - slept))


if __name__ == "__main__":
    main()
