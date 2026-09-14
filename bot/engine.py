"""The call engine + record keeping + Kalshi fees."""
import json, math, os, time, datetime as dt
from kalshi_core import ASSETS, KALSHI, RECORD_DIR, _get, DISCLAIMER
import signals as S
import flow as F


# ---------------------------------------------------------------- Kalshi fees
def kalshi_fee(price, contracts=1, multiplier=1.0):
    """Kalshi's published general trading fee (fee schedule, July 2026 update):
         fees = round_up_to_next_cent( M x 0.07 x C x P x (1-P) )
       P is the contract price in dollars (45c = 0.45). Returned in DOLLARS."""
    raw = multiplier * 0.07 * contracts * price * (1.0 - price)
    return math.ceil(raw * 100.0) / 100.0


def breakeven_win_rate(entry_price, multiplier=1.0):
    """You pay entry + fee. A win returns $1.00, a loss returns $0.
       w*(1 - entry - fee) = (1-w)*(entry + fee)  =>  w = entry + fee."""
    if entry_price is None:
        return None
    return min(1.0, entry_price + kalshi_fee(entry_price, 1, multiplier))


# ---------------------------------------------------------------- prices
def cents(v):
    return f"{v*100:.0f}\u00a2" if v is not None else "\u2014"


def no_side(yes_bid, yes_ask, no_bid=0.0, no_ask=0.0):
    """The NO side in dollars. Kalshi quotes it directly; when it is missing it is
    exactly 100 minus the YES side (no ask = 1 - yes bid, no bid = 1 - yes ask)."""
    nb = no_bid if no_bid else (1.0 - yes_ask if yes_ask else 0.0)
    na = no_ask if no_ask else (1.0 - yes_bid if yes_bid else 0.0)
    return nb, na


def price_strings(call, yes_bid, yes_ask, no_bid, no_ask):
    """What the contract costs, in cents, labelled plainly."""
    nb, na = no_side(yes_bid, yes_ask, no_bid, no_ask)
    yes_str = f"YES {cents(yes_bid)} / {cents(yes_ask)}"
    no_str = f"NO {cents(nb)} / {cents(na)}"
    if call == "UP":
        label = f"UP \u2014 YES costs {cents(yes_ask)}"
    elif call == "DOWN":
        label = f"DOWN \u2014 NO costs {cents(na)}"
    else:
        label = f"NO CALL \u2014 {yes_str}, {no_str}"
    return label, yes_str, no_str, nb, na


# ---------------------------------------------------------------- Kalshi reads
def open_market(series):
    j = _get(f"{KALSHI}/markets", dict(series_ticker=series, status="open", limit=5))
    ms = [m for m in j.get("markets", []) if m.get("status") in ("active", "open")]
    if not ms:
        return None
    ms.sort(key=lambda m: m["close_time"])
    return ms[0]


def market_by_ticker(t):
    return _get(f"{KALSHI}/markets/{t}")["market"]


def parse_target(m):
    if m.get("floor_strike") is not None:
        return float(m["floor_strike"])
    sub = (m.get("yes_sub_title") or "")
    if "$" in sub:
        try:
            return float(sub.split("$")[1].replace(",", "").strip())
        except Exception:
            return None
    return None


def iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------- the call
def build_call(asset, market, quote, ticker, cfg):
    st = cfg["settings"]
    A = ASSETS[asset]
    dec = A["dec"]
    target = parse_target(market)
    spot = quote.price
    gap = spot - target
    gap_pct = (gap / spot) * 100.0 if spot else 0.0

    yes_bid = float(market.get("yes_bid_dollars") or 0)
    yes_ask = float(market.get("yes_ask_dollars") or 0)
    no_bid = float(market.get("no_bid_dollars") or 0)
    no_ask = float(market.get("no_ask_dollars") or 0)
    oi = float(market.get("open_interest_fp") or 0)
    spread = yes_ask - yes_bid
    secs_left = max(0.0, iso(market["close_time"]) - time.time())
    mins_left = secs_left / 60.0

    sigma, nbars = ticker.sigma_per_minute(15)
    candles = ticker.candles(16)

    # ---- price-action signals
    fvg = S.find_fvg(candles)
    sweep = S.find_sweep(candles)
    runx = S.run_and_exhaustion(candles)
    mom = S.momentum(candles)
    sr = S.sr_proximity(candles, target, spot)
    sig_lean, votes, ups, downs = S.signal_lean(fvg, sweep, runx, mom, sr, gap, dec)

    # ---- bull / bear gaps kept separate (both present = chop)
    gaps = S.all_fvgs(candles)
    chop = bool(gaps["bullish"] and gaps["bearish"])

    # ---- order flow (each reports unavailable rather than guessing)
    wo = F.whale_orders(market["ticker"])
    wt = F.whale_trades(market["ticker"])
    sw_spot = (F.spot_whales(ASSETS[asset]["spot"][1])
               if ASSETS[asset]["spot"][0] == "coinbase"
               else {"available": False,
                     "why": "no public trade feed for this asset's spot source"})
    flip = F.projected_flip(candles, spot, target, mins_left)

    gap_dir = "up" if gap > 0 else ("down" if gap < 0 else "flat")

    # ---- richer technical set (RSI, multi-horizon momentum, FVG distance, skew)
    rsi_v = S.rsi(candles, 14)
    rsi_st = S.rsi_state(rsi_v)
    mom_multi = S.momentum_multi(candles)
    fvg_dist = S.fvg_distance(gaps, spot)
    dist_vol = (abs(gap) / sigma) if sigma else None
    yes_mid = ((yes_bid + yes_ask) / 2.0) if (yes_bid and yes_ask) else None
    skew = dict(yes_mid=yes_mid,
                market_dir=("up" if (yes_mid or 0) > 0.5 else "down") if yes_mid else None,
                edge=(abs((yes_mid or 0.5) - 0.5) * 200))    # in cents off the coin flip
    hist = asset_history(asset, gap_dir)

    # ---- base filter: the noise band, scaled to the time LEFT in the round
    band = (sigma * math.sqrt(max(mins_left, 0.25)) * st["noise_k"]) if sigma else None
    gate_k = float(st.get("conviction_gate_k") or 1.5)
    reasons = []
    blocked = None

    if quote.age > st["max_quote_age_seconds"]:
        blocked = (f"NO CALL \u2014 the {quote.source} price is {int(quote.age)}s old "
                   f"(stale); not calling on a stale quote.")
    elif sigma is None:
        blocked = (f"NO CALL \u2014 not enough price history yet to measure the noise "
                   f"({nbars} 1-min bars). Warming up.")
    elif oi < st["hard_min_open_interest"]:
        blocked = (f"NO CALL \u2014 open interest is only {oi:,.0f}. Too thin to trust "
                   f"the quote or to get filled.")
    elif abs(gap) < band:
        blocked = (f"NO CALL \u2014 gap ${abs(gap):,.{dec}f} is inside the noise "
                   f"({A['label']} moving ~${sigma:,.{dec}f}/min, band "
                   f"${band:,.{dec}f} over the {mins_left:.1f} min still to run).")

    conviction = "none"
    call = "NO CALL"
    weak = False
    drivers = []

    # ---- the reversal / projected-flip read, on the CURRENT direction of travel
    travel = gap_dir
    if mom_multi and mom_multi.get("m3"):
        travel = "up" if mom_multi["m3"] > 0 else "down"
    flip_warn = S.reversal_risk(travel if gap_dir == "flat" else gap_dir, rsi_v, mom_multi,
                                fvg_dist, runx, gap, sigma, dec=dec, spot=spot)

    if blocked is None:
        confirms = (sig_lean == gap_dir)
        conflicts = (sig_lean in ("up", "down") and sig_lean != gap_dir)
        rsi_against = ((gap_dir == "up" and rsi_st == "overbought") or
                       (gap_dir == "down" and rsi_st == "oversold"))
        hist_bad = (hist["n"] >= 5 and hist["wr"] is not None and hist["wr"] < 0.5)
        strong_gap = (band and abs(gap) >= gate_k * band)

        fails = []
        if not confirms:
            fails.append("the price action does not agree with the gap"
                         if not conflicts else
                         f"the price action leans {sig_lean.upper()} against a {gap_dir.upper()} gap")
        if not strong_gap:
            fails.append(f"the gap is {abs(gap)/band:.0%} of the noise band, under the "
                         f"{gate_k*100:.0f}% this gate needs")
        if rsi_against:
            fails.append(f"RSI {rsi_v:.0f} is {rsi_st} against the move")
        if hist_bad:
            fails.append(f"this asset's own {gap_dir.upper()} setups are {hist['w']}W-{hist['l']}L "
                         f"({hist['wr']*100:.0f}%) \u2014 under water")
        if chop:
            fails.append("an unfilled bullish AND bearish gap are both open (chop)")
        if spread > st["max_spread_dollars"]:
            fails.append(f"the spread is {spread*100:.0f}\u00a2 wide")

        if fails:
            call = "NO CALL"
            conviction = "conflict" if conflicts else "none"
            reasons.append("NO CALL \u2014 not a confident read at minute 1: " +
                           "; ".join(fails) + ". Standing down rather than manufacturing a call.")
        else:
            call = "UP" if gap_dir == "up" else "DOWN"
            conviction = "high"
            drivers = [
                f"gap ${abs(gap):,.{dec}f} = {abs(gap)/sigma:.1f}x per-minute vol and "
                f"{abs(gap)/band:.0%} of the {mins_left:.0f}-min noise band",
                f"price action {ups}\u2013{downs} {sig_lean.upper()} (agrees with the gap)",
                f"RSI {rsi_v:.0f} ({rsi_st})" if rsi_v is not None else "RSI unavailable",
            ]
            if mom_multi and mom_multi.get("m3") is not None:
                drivers.append(f"momentum {mom_multi['m3']:+,.{dec}f}/3m"
                               + (f", {mom_multi['m15']:+,.{dec}f}/15m" if mom_multi.get("m15") is not None else ""))
            if hist["n"]:
                drivers.append(f"this asset's {gap_dir.upper()} setups: {hist['w']}W-{hist['l']}L"
                               + (" (small sample \u2014 weighted lightly)" if hist["n"] < 5 else ""))
            reasons.append("CALL " + call + " \u2014 driven by " + "; ".join(drivers[:3]) + ".")
            if hist["n"] and hist["n"] < 5:
                reasons.append(f"History note: only {hist['n']} settled {gap_dir.upper()} calls on "
                               f"{A['label']} so far \u2014 too few to lean on, so it does not raise conviction.")
            if skew["market_dir"] and skew["market_dir"] != gap_dir.replace("flat", ""):
                reasons.append(f"Market disagrees: YES mid {yes_mid*100:.0f}\u00a2 prices the other side.")
    else:
        reasons.append(blocked)

    if call != "NO CALL" and flip_warn["flag"]:
        reasons.append(flip_warn["text"])

    price_label, price_yes, price_no, no_bid_eff, no_ask_eff = price_strings(
        call, yes_bid, yes_ask, no_bid, no_ask)
    entry = yes_ask if call == "UP" else (no_ask_eff if call == "DOWN" else None)
    be = breakeven_win_rate(entry, st["kalshi_fee_multiplier"]) if entry else None

    return dict(
        asset=asset, label=A["label"], ticker=market["ticker"], target=target, spot=spot,
        gap=gap, gap_pct=gap_pct, sigma=sigma, band=band, nbars=nbars,
        yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask, spread=spread,
        no_bid_eff=no_bid_eff, no_ask_eff=no_ask_eff, price_label=price_label,
        price_yes=price_yes, price_no=price_no,
        open_interest=oi, mins_left=mins_left, call=call, weak=weak, conviction=conviction,
        reason=" ".join(reasons), signals=[dict(name=n, lean=l, text=t) for n, l, t in votes],
        signal_lean=sig_lean, gap_dir=gap_dir, entry_price=entry, breakeven=be,
        fee=kalshi_fee(entry, 1, st["kalshi_fee_multiplier"]) if entry else None,
        fvgs=gaps, chop=chop, whale_orders=wo, whale_trades=wt, spot_whales=sw_spot,
        flip=flip, scalp=F.scalp(yes_ask, no_ask_eff, sig_lean, yes_bid=yes_bid,
                                 no_bid=no_bid_eff, trades=wt, orders=wo),
        rsi=rsi_v, rsi_state=rsi_st, momentum_multi=mom_multi, fvg_distance=fvg_dist,
        dist_vol=dist_vol, skew=skew, history=hist, reversal=flip_warn,
        reversal_flag=bool(flip_warn["flag"]), reversal_text=flip_warn["text"],
        drivers=drivers, gate_k=float(st.get("conviction_gate_k") or 1.5),
        spot_source=quote.source, quote_age=quote.age, bars_note=ticker.source_note(),
        close_time=market["close_time"], dec=A["dec"], ts=time.time(),
    )


# ---------------------------------------------------------------- marks
DEFAULT_MARKS = [1]


def call_marks(st):
    """ONE call per round, at roughly the 1-minute mark. The noise band is scaled
    to the time still to run (~14 min at minute 1), so most rounds are NO CALL —
    that is the honest answer, not a bug."""
    ms = st.get("call_marks") or DEFAULT_MARKS
    ms = sorted({int(m) for m in ms})
    return ms[:1]


def final_mark(st):
    return call_marks(st)[0]


ENTRY_KEYS = ("ticker", "call", "weak", "conviction", "gap", "gap_pct", "sigma",
              "band", "spot", "target", "yes_bid", "yes_ask", "no_bid", "no_ask",
              "no_bid_eff", "no_ask_eff", "price_label", "price_yes", "price_no",
              "open_interest", "entry_price", "breakeven", "fee", "reason",
              "signal_lean", "gap_dir", "mins_left", "ts",
              "rsi", "rsi_state", "dist_vol", "reversal_flag", "reversal_text",
              "drivers")


def make_entry(c, mark, final):
    entry = {k: c.get(k) for k in ENTRY_KEYS}
    entry["signals"] = c["signals"]
    entry["reversal"] = c.get("reversal")
    entry["close_time"] = c["close_time"]
    entry["result"] = None
    entry["mark"] = int(mark)
    entry["is_early"] = not final
    if not final:
        entry["conviction_raw"] = c["conviction"]
        entry["conviction"] = "early"
        entry["ticker"] = f"{c['ticker']}:{int(mark)}m"
    return entry


def scalp_entry(c, entry, mark):
    sc = dict(entry)
    s = c["scalp"]
    sc["ticker"] = f"{c['ticker']}:scalp"
    sc["is_scalp"] = True
    sc["is_early"] = False
    sc["mark"] = int(mark)
    sc["conviction"] = "scalp"
    sc["call"] = "UP" if s["side"] == "YES" else "DOWN"
    sc["entry_price"] = s["price"]
    sc["breakeven"] = breakeven_win_rate(s["price"])
    sc["fee"] = kalshi_fee(s["price"])
    sc["price_label"] = (f"SCALP {s['side']} \u2014 costs {cents(s['price'])}, "
                         f"target {cents(s['target'])} (pays {s['payoff']:.1f}x if it settles)")
    sc["reason"] = (f"Scalp bucket: {s['side']} at {cents(s['price'])} "
                    f"\u2192 target {cents(s['target'])} ({s['move_cents']}\u00a2 move), "
                    f"pays {s['payoff']:.1f}x if held to settle \u2014 {s['why']}. "
                    f"Most of these lose \u2014 separate bucket, never in the headline record.")
    return sc


# ---------------------------------------------------------------- the record
def record_path(asset):
    return os.path.join(RECORD_DIR, f"{asset}.json")


def load_record(asset):
    p = record_path(asset)
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            pass
    return dict(asset=asset, calls=[])


def save_record(asset, rec):
    os.makedirs(RECORD_DIR, exist_ok=True)
    tmp = record_path(asset) + ".tmp"
    json.dump(rec, open(tmp, "w"), indent=1)
    os.replace(tmp, record_path(asset))


def append_call(asset, entry):
    rec = load_record(asset)
    rec["calls"] = [c for c in rec["calls"] if c.get("ticker") != entry.get("ticker")]
    rec["calls"].append(entry)
    rec["calls"] = rec["calls"][-2000:]
    save_record(asset, rec)
    return rec


def EARLY(c):
    """True only for entries explicitly tagged early — the old 5/7/9-minute marks.
    They stay in the file and stay out of the headline tally. Everything else
    (the old 12-minute calls of record, and the new single minute-1 call) counts."""
    return bool(c.get("is_early")) or c.get("conviction") == "early"



def asset_history(asset, direction):
    """THIS BOT'S OWN record for this asset: how its settled headline calls in
    this direction have actually gone. Small samples are reported as small —
    2-of-3 never drives a high-conviction call."""
    try:
        rec = load_record(asset)
    except Exception:
        return dict(n=0, w=0, l=0, wr=None, small=True, direction=direction)
    want = "UP" if direction == "up" else "DOWN"
    s = [c for c in rec.get("calls", [])
         if not c.get("is_scalp") and not EARLY(c) and c.get("call") == want
         and c.get("result") in ("yes", "no")]
    w = sum(1 for c in s if c.get("correct"))
    n = len(s)
    return dict(n=n, w=w, l=n - w, wr=(w / n if n else None), small=n < 5,
                direction=direction)

def tally(calls, small_sample=30):
    calls = [c for c in calls if not c.get("is_scalp") and not EARLY(c)]
    settled = [c for c in calls if c.get("result") in ("yes", "no") and c.get("call") in ("UP", "DOWN")]
    nocalls = [c for c in calls if c.get("call") == "NO CALL"]
    right = sum(1 for c in settled if c.get("correct"))
    wrong = len(settled) - right
    def wr(sub):
        s = [c for c in sub if c.get("result") in ("yes", "no") and c.get("call") in ("UP", "DOWN")]
        r = sum(1 for c in s if c.get("correct"))
        return (r / len(s) if s else None), r, len(s) - r
    all_wr, _, _ = wr(settled)
    last20_wr, l20r, l20w = wr(settled[-20:])
    hi = [c for c in settled if c.get("conviction") == "high"]
    pl = [c for c in settled if c.get("conviction") in ("plain", "weak")]
    hi_wr, hir, hiw = wr(hi)
    pl_wr, plr, plw = wr(pl)
    entries = [c["entry_price"] for c in settled if c.get("entry_price")]
    avg_entry = sum(entries) / len(entries) if entries else None
    be = breakeven_win_rate(avg_entry) if avg_entry else None
    return dict(
        right=right, wrong=wrong, no_calls=len(nocalls), settled=len(settled),
        win_rate=all_wr, last20=last20_wr, last20_w=l20r, last20_l=l20w,
        high_conv=dict(wr=hi_wr, w=hir, l=hiw), plain=dict(wr=pl_wr, w=plr, l=plw),
        avg_entry=avg_entry, breakeven=be,
        small_sample=len(settled) < small_sample, threshold=small_sample,
        beats_breakeven=(all_wr is not None and be is not None and all_wr > be),
    )


def tally_line(label, t):
    if t["settled"] == 0:
        return f"{label}: no settled calls yet ({t['no_calls']} no-calls)"
    pct = f"{t['win_rate']*100:.0f}%"
    s = f"{label}: {pct} ({t['right']}W-{t['wrong']}L, {t['no_calls']} no-calls)"
    if t["small_sample"]:
        s += f" \u2014 sample too small to mean anything (under {t['threshold']} settled calls)"
    if t["breakeven"] is not None:
        s += (f" | breakeven {t['breakeven']*100:.0f}% at avg entry "
              f"{t['avg_entry']*100:.0f}\u00a2 incl. fees")
    if t["last20"] is not None:
        s += f" | last 20: {t['last20']*100:.0f}% ({t['last20_w']}W-{t['last20_l']}L)"
    return s


def scalp_tally(calls):
    """The cheap-contract bucket. NEVER blended into the headline win rate:
    most of these lose by design, and the payoff is what makes them worth it."""
    s = [c for c in calls if c.get("is_scalp") and c.get("result") in ("yes", "no")]
    if not s:
        return dict(n=0, w=0, l=0, wr=None, avg_price=None, roi=None)
    w = sum(1 for c in s if c.get("correct"))
    prices = [c["entry_price"] for c in s if c.get("entry_price")]
    avg = sum(prices) / len(prices) if prices else None
    staked = sum(c.get("entry_price") or 0 for c in s)
    returned = sum(1.0 for c in s if c.get("correct"))
    fees = sum(kalshi_fee(c.get("entry_price") or 0) for c in s)
    roi = ((returned - staked - fees) / staked) if staked else None
    return dict(n=len(s), w=w, l=len(s) - w, wr=w / len(s), avg_price=avg, roi=roi)
