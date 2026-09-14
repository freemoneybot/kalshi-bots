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

        # A 15-minute binary is not "will it keep going" — it is "will it stay on
        # this side of the line". When price is already far past the target with
        # ~8 minutes left, mean-reversion vetoes (RSI stretched, chop, a
        # disagreeing price-action lean) are weak evidence against the contract.
        # So above 2x the band they become NOTES on a plain-conviction call
        # instead of a hard stand-down. The gap gate, the spread and this asset's
        # own losing history are still hard vetoes at every distance.
        very_strong = bool(band and abs(gap) >= 2.0 * band)
        unanimous = ((ups > 0 and downs == 0 and sig_lean == gap_dir) or
                     (downs > 0 and ups == 0 and sig_lean == gap_dir))
        outside_band = bool(band and abs(gap) >= band)
        soft = []
        if (very_strong and strong_gap) or (unanimous and outside_band):
            hard = [x for x in fails
                    if x.startswith("the spread") or "under water" in x
                    or (x.startswith("the gap is") and not (unanimous and outside_band))]
            soft = [x for x in fails if x not in hard]
            fails = hard

        # ---- EVERY ROUND GETS A DIRECTION (Anthony, Sep 13 2026: "Make calls
        # every single time"). The honesty moved from whether to call into the
        # conviction tier: STRONG / MEDIUM / COIN FLIP.
        direction = gap_dir
        if direction not in ("up", "down"):
            direction = sig_lean if sig_lean in ("up", "down") else None
        if direction is None and mom_multi and mom_multi.get("m3"):
            direction = "up" if mom_multi["m3"] > 0 else "down"
        if direction is None:
            direction = skew.get("market_dir") or "up"
        call = "UP" if direction == "up" else "DOWN"

        inside_band = bool(band and abs(gap) < band)
        clean = (not fails) and confirms and strong_gap
        if inside_band:
            tier = "coin_flip"
        elif clean:
            tier = "strong"
        else:
            tier = "medium"
        # "better yourself": this asset's own settled history in this direction
        # moves the tier, it never invents one.
        if hist["n"] >= 5 and hist["wr"] is not None:
            if hist["wr"] < 0.5 and tier == "strong":
                tier = "medium"
            elif hist["wr"] < 0.4 and tier == "medium":
                tier = "coin_flip"
        conviction = tier

        drivers = [
            f"gap ${abs(gap):,.{dec}f} = {abs(gap)/sigma:.1f}x per-minute vol and "
            f"{abs(gap)/band:.0%} of the {mins_left:.0f}-min noise band",
            (f"price action {ups}\u2013{downs} {sig_lean.upper()} "
             + ("(agrees with the gap)" if sig_lean == gap_dir else "(does not confirm the gap)")),
            f"RSI {rsi_v:.0f} ({rsi_st})" if rsi_v is not None else "RSI unavailable",
        ]
        if mom_multi and mom_multi.get("m3") is not None:
            drivers.append(f"momentum {mom_multi['m3']:+,.{dec}f}/3m"
                           + (f", {mom_multi['m15']:+,.{dec}f}/15m" if mom_multi.get("m15") is not None else ""))
        if hist["n"]:
            drivers.append(f"this asset's {gap_dir.upper()} setups: {hist['w']}W-{hist['l']}L"
                           + (" (small sample \u2014 weighted lightly)" if hist["n"] < 5 else ""))
        reasons.append(("CALL " + call + " \u2014 " + TIER_LABEL[tier] + " \u2014 driven by "
                        + "; ".join(drivers[:3]) + "."))
        if tier == "coin_flip":
            reasons.append("COIN FLIP \u2014 the gap is INSIDE the noise band "
                           f"(${abs(gap):,.{dec}f} vs a ${band:,.{dec}f} band over the "
                           f"{mins_left:.0f} min still to run). This is a direction because a call "
                           "is made every round, not because the read is good. Near 50/50 \u2014 "
                           "size it that way or skip it.")
        elif fails:
            reasons.append("Reads against this call: " + "; ".join(fails) + ".")
        if soft:
            reasons.append("Downgraded from a clean read by: " + "; ".join(soft) + ".")
        if hist["n"] and hist["n"] < 5:
            reasons.append(f"History note: only {hist['n']} settled {gap_dir.upper()} calls on "
                           f"{A['label']} so far \u2014 too few to lean on.")
        if skew["market_dir"] and skew["market_dir"] != gap_dir.replace("flat", ""):
            reasons.append(f"Market disagrees: YES mid {yes_mid*100:.0f}\u00a2 prices the other side.")
    else:
        reasons.append(blocked)


    # ---- PRICE-EDGE GATE: no call when the market has already decided it.
    # An UP call at YES 95c risks 95c to win 5c; one loss erases nineteen wins.
    # This is not a NO CALL (no signal) — it is signal with no money in it.
    no_edge = False
    no_edge_price = None
    edge_points = None
    model_p = None
    market_p = None
    if call in ("UP", "DOWN"):
        _nb, _na = no_side(yes_bid, yes_ask, no_bid, no_ask)
        side_ask = yes_ask if call == "UP" else _na
        wp = win_probability(call, spot, target, sigma, secs_left, yes_bid, yes_ask)
        if wp:
            model_p, market_p = wp.get("model"), wp.get("market")
            if model_p is not None and market_p is not None:
                edge_points = (model_p - market_p) * 100.0
        max_price = float(st.get("max_call_price") or 0.85)
        if side_ask and side_ask > max_price:
            no_edge = True
            no_edge_price = side_ask
            reasons.append(f"NO EDGE \u2014 the signal is {('UP' if gap_dir == 'up' else 'DOWN')} and it "
                       f"still reads that way, but that side already costs {cents(side_ask)}. "
                       f"Risking {cents(side_ask)} to win {cents(1 - side_ask)} is not a trade \u2014 "
                       f"one loss erases {max(1, int(side_ask / max(1 - side_ask, 0.01)))} wins. "
                       f"The call stands \u2014 don't buy it at this price.")

    if call != "NO CALL" and flip_warn["flag"]:
        reasons.append(flip_warn["text"])

    # ---- the pre-minute-7 LEAN (never recorded, never settled) and the live
    # win probability of the headline call once it has actually fired.
    lean = expected_lean(gap_dir, sig_lean, mom_multi, skew, dist_vol)
    fired = headline_entry(asset, market["ticker"])
    fired_call = (fired or {}).get("call")
    live = None
    conf = confidence_state(None)
    if fired_call in ("UP", "DOWN"):
        live = win_probability(fired_call, spot, target, sigma, secs_left,
                               yes_bid, yes_ask)
        conf = confidence_state((live or {}).get("p"))
        if live and live.get("model") is not None and live.get("market") is not None:
            edge_points = (live["model"] - live["market"]) * 100.0
    phase = "called" if fired else ("lean" if mins_left > 0 else "closing")

    price_label, price_yes, price_no, no_bid_eff, no_ask_eff = price_strings(
        call, yes_bid, yes_ask, no_bid, no_ask)
    entry = yes_ask if call == "UP" else (no_ask_eff if call == "DOWN" else None)
    be = breakeven_win_rate(entry, st["kalshi_fee_multiplier"]) if entry else None

    tier = conviction if conviction in TIER_LABEL else None
    return dict(
        tier=tier, tier_label=(TIER_LABEL.get(tier) or ""),
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
        secs_left=secs_left, phase=phase, expected=lean,
        fired_call=fired_call, fired_mark=(fired or {}).get("mark"),
        fired_ts=(fired or {}).get("ts"),
        fired_price_label=(fired or {}).get("price_label"),
        fired_entry_price=(fired or {}).get("entry_price"),
        fired_reason=(fired or {}).get("reason"),
        fired_drivers=(fired or {}).get("drivers"),
        win_prob=((live or {}).get("p")), win_prob_model=((live or {}).get("model")),
        win_prob_market=((live or {}).get("market")),
        no_edge=no_edge, no_edge_price=no_edge_price, edge_points=edge_points,
        cand_model_p=model_p, cand_market_p=market_p,
        confidence=conf["state"], confidence_label=conf["label"],
        confidence_note=conf["note"],
    )


# ---------------------------------------------------------------- live win probability
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def win_probability(call, spot, target, sigma_per_min, secs_left,
                    yes_bid=0.0, yes_ask=0.0, w_model=0.6):
    """Live probability that a made call wins.

    Model core: price is a random walk with per-minute vol sigma. Over the
    remaining time the standard deviation is sigma*sqrt(minutes_left); the
    chance the contract settles YES is the normal CDF of the distance from the
    target line in units of that. Cross-checked against the market's own
    implied probability (the YES mid). Returns None when an input is missing
    rather than inventing a number.
    """
    if call not in ("UP", "DOWN") or spot is None or target is None:
        return None
    mid = ((yes_bid + yes_ask) / 2.0) if (yes_bid and yes_ask) else None
    model = None
    if sigma_per_min:
        mins = max(secs_left, 0.0) / 60.0
        sd = sigma_per_min * math.sqrt(max(mins, 1e-4))
        z = (spot - target) / sd if sd > 0 else (50.0 if spot > target else -50.0)
        p_yes = _norm_cdf(z)
        model = p_yes if call == "UP" else 1.0 - p_yes
    market = None
    if mid is not None:
        market = mid if call == "UP" else 1.0 - mid
    if model is None and market is None:
        return None
    if model is None:
        p, w = market, 0.0
    elif market is None:
        p, w = model, 1.0
    else:
        w = w_model
        p = w * model + (1.0 - w) * market
    p = min(0.995, max(0.005, p))
    return dict(p=p, model=model, market=market, model_weight=w,
                secs_left=max(secs_left, 0.0))


TIER_LABEL = {"strong": "STRONG", "medium": "MEDIUM", "coin_flip": "COIN FLIP"}


CONF_HIGH = 0.70
CONF_BAIL = 0.45


def confidence_state(p):
    """His three states. HIGH >~70%, LOW ~45-70% (shaky), BAIL under 45%."""
    if p is None:
        return dict(state="UNKNOWN", label="NO READ",
                    note="not enough data to price the call right now")
    if p >= CONF_HIGH:
        return dict(state="HIGH", label="HIGH CONFIDENCE",
                    note="the call is comfortably ahead")
    if p >= CONF_BAIL:
        return dict(state="LOW", label="LOW CONFIDENCE",
                    note="shaky — it can go either way from here")
    return dict(state="BAIL", label="BAIL",
                note="the call is going wrong — consider exiting the position")


def expected_lean(gap_dir, sig_lean, mom_multi, skew, dist_vol):
    """The pre-minute-7 LEAN. Not a call, never recorded, never settled.

    Simple honest vote: which way the gap sits, which way the price action
    leans, which way the last 3 minutes moved, which way the market is priced.
    """
    votes = {"up": 0, "down": 0}
    why = []
    if gap_dir in ("up", "down"):
        votes[gap_dir] += 1
        why.append(f"spot is {gap_dir} against the target line")
    if sig_lean in ("up", "down"):
        votes[sig_lean] += 1
        why.append(f"price action leans {sig_lean.upper()}")
    m3 = (mom_multi or {}).get("m3")
    if m3:
        d = "up" if m3 > 0 else "down"
        votes[d] += 1
        why.append(f"3-min momentum {d.upper()}")
    md = (skew or {}).get("market_dir")
    if md in ("up", "down"):
        votes[md] += 1
        why.append(f"market is priced {md.upper()}")
    if votes["up"] == votes["down"]:
        return dict(dir=None, text="Expected: no lean yet — the reads disagree",
                    votes=votes, why=why, strength=0)
    d = "up" if votes["up"] > votes["down"] else "down"
    n = max(votes.values()); tot = votes["up"] + votes["down"]
    return dict(dir=d.upper(), text=f"Expected: {d.upper()}", votes=votes, why=why,
                strength=(n / tot if tot else 0))


def headline_entry(asset, ticker):
    """The ONE settled-into-record headline call for this contract, if the
    minute-7 call has already fired. Leans are never in here."""
    try:
        rec = load_record(asset)
    except Exception:
        return None
    for c in reversed(rec.get("calls", [])):
        if c.get("ticker") == ticker and not c.get("is_scalp") and not EARLY(c):
            return c
    return None


# ---------------------------------------------------------------- marks
DEFAULT_MARKS = [7]


def call_marks(st):
    """ONE call per round, at roughly the 7-minute mark (~8 min left). The noise
    band is scaled to the time still to run, so at minute 7 it is tighter than it
    was at minute 1 and more rounds clear it. A round with no signal is still
    allowed to say NO CALL."""
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
              "drivers", "tier", "tier_label", "no_edge", "no_edge_price", "edge_points")


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
    def tier_of(c):
        t = c.get("tier") or c.get("conviction")
        return t if t in ("strong", "medium", "coin_flip") else None
    tiers = {}
    for name in ("strong", "medium", "coin_flip"):
        sub = [c for c in settled if tier_of(c) == name]
        twr, tw, tl = wr(sub)
        tiers[name] = dict(wr=twr, w=tw, l=tl, n=len(sub))
    entries = [c["entry_price"] for c in settled if c.get("entry_price")]
    avg_entry = sum(entries) / len(entries) if entries else None
    be = breakeven_win_rate(avg_entry) if avg_entry else None
    return dict(
        right=right, wrong=wrong, no_calls=len(nocalls), settled=len(settled),
        win_rate=all_wr, last20=last20_wr, last20_w=l20r, last20_l=l20w,
        high_conv=dict(wr=hi_wr, w=hir, l=hiw), plain=dict(wr=pl_wr, w=plr, l=plw),
        tiers=tiers,
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
