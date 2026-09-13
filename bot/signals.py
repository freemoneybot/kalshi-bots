"""Price-action signals: FVG, sweeps/reversals, momentum, S/R proximity.

Honest framing, and it belongs in the code as much as on the page: FVG and
sweep reads are widely-used discretionary heuristics. On a 15-minute binary
they are NOT a proven edge. The bot tracks high-conviction vs plain calls
separately so that, after a few hundred rounds, the split itself answers
whether this layer adds anything.
"""

def _f(v, dec):
    return f"{v:,.{dec}f}"


def find_fvg(candles, lookback=15):
    """3-candle imbalance. Bullish: candle[n-2].high < candle[n].low.
    Bearish: candle[n-2].low > candle[n].high. Returns the most recent
    UNFILLED one within lookback, or None."""
    cs = candles[-(lookback + 2):]
    found = []
    for i in range(2, len(cs)):
        a, c = cs[i - 2], cs[i]
        if a["h"] < c["l"]:
            found.append(dict(dir="bullish", lo=a["h"], hi=c["l"], idx=i, t=c["t"]))
        elif a["l"] > c["h"]:
            found.append(dict(dir="bearish", lo=c["h"], hi=a["l"], idx=i, t=c["t"]))
    # filled = price has traded back through the gap since it formed
    out = None
    for g in found:
        after = cs[g["idx"] + 1:]
        filled = any(c["l"] <= g["lo"] and c["h"] >= g["hi"] for c in after)
        if not filled:
            g = dict(g)
            last = cs[-1]["c"]
            g["filled"] = False
            g["price_inside"] = g["lo"] <= last <= g["hi"]
            g["price_above"] = last > g["hi"]
            out = g
    return out


def find_sweep(candles, lookback=15):
    """Liquidity sweep / stop hunt: a candle takes out the prior range high
    (or low) and closes back inside it."""
    cs = candles[-(lookback + 1):]
    if len(cs) < 5:
        return None
    for i in range(len(cs) - 1, max(len(cs) - 5, 2), -1):
        prior = cs[max(0, i - lookback):i]
        if len(prior) < 3:
            continue
        ph, pl = max(c["h"] for c in prior), min(c["l"] for c in prior)
        c = cs[i]
        if c["h"] > ph and c["c"] < ph:
            return dict(dir="bearish", level=ph, kind="swept the high, closed back below",
                        bars_ago=len(cs) - 1 - i, t=c["t"])
        if c["l"] < pl and c["c"] > pl:
            return dict(dir="bullish", level=pl, kind="swept the low, closed back above",
                        bars_ago=len(cs) - 1 - i, t=c["t"])
    return None


def run_and_exhaustion(candles):
    """Consecutive-direction run length, and whether the last 3 bars are
    extending or stalling (range contraction)."""
    cs = candles[-6:]
    if len(cs) < 4:
        return None
    dirs = [1 if c["c"] >= c["o"] else -1 for c in cs]
    run, d = 1, dirs[-1]
    for x in reversed(dirs[:-1]):
        if x == d:
            run += 1
        else:
            break
    rng = [c["h"] - c["l"] for c in cs[-3:]]
    stalling = len(rng) == 3 and rng[0] > rng[1] > rng[2]
    return dict(run=run, dir="up" if d > 0 else "down", stalling=stalling,
                ranges=rng)


def momentum(candles):
    cs = candles
    if len(cs) < 6:
        return None
    last = cs[-1]["c"]
    m3 = last - cs[-4]["c"]
    m5 = last - cs[-6]["c"]
    first_half = cs[-4]["c"] - cs[-6]["c"]
    accelerating = abs(m3) > abs(first_half)
    return dict(m3=m3, m5=m5, accelerating=accelerating)


def sr_proximity(candles, target, spot):
    cs = candles[-15:]
    if not cs:
        return None
    hi = max(c["h"] for c in cs)
    lo = min(c["l"] for c in cs)
    return dict(high=hi, low=lo, to_high=hi - spot, to_low=spot - lo,
                to_target=spot - target)


def signal_lean(fvg, sweep, runx, mom, sr, gap, dec=2):
    """Each signal votes. Returns (lean, votes[list of (name, lean, text)])."""
    votes = []
    if fvg:
        # an unfilled gap is a magnet: price tends to return INTO it.
        if fvg["price_inside"]:
            lean = "up" if fvg["dir"] == "bullish" else "down"
            txt = (f"{fvg['dir']}, {_f(fvg['lo'],dec)}\u2013{_f(fvg['hi'],dec)}, unfilled, "
                   f"price inside it \u2014 leans {lean.upper()} if it holds")
        else:
            lean = "down" if fvg["price_above"] else "up"
            txt = (f"{fvg['dir']}, {_f(fvg['lo'],dec)}\u2013{_f(fvg['hi'],dec)}, unfilled, price "
                   f"{'above' if fvg['price_above'] else 'below'} it \u2014 magnet pulls "
                   f"{lean.upper()}")
        votes.append(("FVG", lean, txt))
    else:
        votes.append(("FVG", None, "no unfilled 3-candle gap in the last 15 min"))

    if sweep:
        votes.append(("Sweep", "up" if sweep["dir"] == "bullish" else "down",
                      f"{sweep['kind']} at {_f(sweep['level'],dec)}, "
                      f"{sweep['bars_ago']} bar(s) ago \u2014 {sweep['dir']}"))
    else:
        votes.append(("Sweep", None, "no liquidity sweep in the last 15 min"))

    if runx:
        if runx["run"] >= 4 or runx["stalling"]:
            lean = "down" if runx["dir"] == "up" else "up"
            votes.append(("Exhaustion", lean,
                          f"{runx['run']} bars {runx['dir']}"
                          + (", ranges contracting" if runx["stalling"] else "")
                          + f" \u2014 exhaustion leans {lean.upper()}"))
        else:
            votes.append(("Trend", "up" if runx["dir"] == "up" else "down",
                          f"{runx['run']} bars {runx['dir']}, still extending"))

    if mom:
        lean = "up" if mom["m3"] > 0 else ("down" if mom["m3"] < 0 else None)
        votes.append(("Momentum", lean,
                      f"{mom['m3']:+,.{dec}f} over 3m, {mom['m5']:+,.{dec}f} over 5m, "
                      f"{'accelerating' if mom['accelerating'] else 'decelerating'}"))

    if sr:
        votes.append(("S/R", None,
                      f"round high {_f(sr['high'],dec)} ({sr['to_high']:+,.{dec}f} away), "
                      f"low {_f(sr['low'],dec)} ({-sr['to_low']:+,.{dec}f} away), "
                      f"target line {sr['to_target']:+,.{dec}f} away (the line is a magnet)"))

    ups = sum(1 for _, l, _ in votes if l == "up")
    downs = sum(1 for _, l, _ in votes if l == "down")
    if ups > downs:
        lean = "up"
    elif downs > ups:
        lean = "down"
    else:
        lean = "mixed"
    return lean, votes, ups, downs


def all_fvgs(candles, lookback=15):
    """Every UNFILLED gap in the window, kept separate by direction, with age."""
    cs = candles[-(lookback + 2):]
    if len(cs) < 3:
        return {"bullish": [], "bearish": []}
    last = cs[-1]["c"]
    out = {"bullish": [], "bearish": []}
    for i in range(2, len(cs)):
        a, c = cs[i - 2], cs[i]
        g = None
        if a["h"] < c["l"]:
            g = dict(dir="bullish", lo=a["h"], hi=c["l"], idx=i)
        elif a["l"] > c["h"]:
            g = dict(dir="bearish", lo=c["h"], hi=a["l"], idx=i)
        if not g:
            continue
        after = cs[i + 1:]
        if any(x["l"] <= g["lo"] and x["h"] >= g["hi"] for x in after):
            continue                      # filled
        g["age_min"] = len(cs) - 1 - i
        g["inside"] = g["lo"] <= last <= g["hi"]
        out[g["dir"]].append(g)
    return out


# ---------------------------------------------------------------- RSI
def rsi(candles, period=14):
    """Classic Wilder RSI on 1-minute closes. Returns None until there are
    enough bars to compute it honestly."""
    cs = [c["c"] for c in candles]
    if len(cs) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(cs)):
        ch = cs[i] - cs[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def rsi_state(v):
    if v is None:
        return "unavailable"
    if v >= 70:
        return "overbought"
    if v <= 30:
        return "oversold"
    if v >= 60:
        return "hot"
    if v <= 40:
        return "cold"
    return "neutral"


# ---------------------------------------------------------------- momentum 3/5/15
def momentum_multi(candles):
    """Rate of change over the last 3, 5 and 15 minutes, plus whether the most
    recent 3 minutes are faster or slower than the 3 before them."""
    cs = candles
    if len(cs) < 4:
        return None
    last = cs[-1]["c"]
    def back(n):
        return last - cs[-(n + 1)]["c"] if len(cs) > n else None
    m3, m5, m15 = back(3), back(5), back(15)
    prev3 = None
    if len(cs) > 6:
        prev3 = cs[-4]["c"] - cs[-7]["c"]
    decel = None
    if m3 is not None and prev3 is not None:
        decel = abs(m3) < abs(prev3) * 0.5
    return dict(m3=m3, m5=m5, m15=m15, prev3=prev3, decelerating=decel,
                rate_per_min=(m3 / 3.0 if m3 is not None else None))


# ---------------------------------------------------------------- FVG distance
def fvg_distance(gaps, spot):
    """Nearest unfilled gap above and below spot, and how far price is from
    filling each one (a gap is a magnet: price tends to trade back into it)."""
    out = {"above": None, "below": None, "inside": None}
    for d in ("bullish", "bearish"):
        for g in gaps.get(d) or []:
            row = dict(g)
            if g["lo"] <= spot <= g["hi"]:
                row["distance"] = 0.0
                out["inside"] = row
            elif g["lo"] > spot:
                row["distance"] = g["lo"] - spot
                if out["above"] is None or row["distance"] < out["above"]["distance"]:
                    out["above"] = row
            else:
                row["distance"] = spot - g["hi"]
                if out["below"] is None or row["distance"] < out["below"]["distance"]:
                    out["below"] = row
    return out


# ---------------------------------------------------------------- reversal risk
def reversal_risk(direction, rsi_v, mom, fvgd, runx, gap, sigma, dec=2, near_line_x=0.5, spot=None):
    """A WARNING shown next to the call, never a second call.

    direction is the way price is currently travelling / the side being called
    ("up" or "down"). Each check below is a reason that move may be about to
    turn against it."""
    reasons = []
    if direction not in ("up", "down"):
        return dict(flag=False, reasons=[], level="none")

    st = rsi_state(rsi_v)
    if direction == "up" and st in ("overbought",):
        reasons.append(f"RSI {rsi_v:.0f} — overbought against an UP move")
    if direction == "down" and st in ("oversold",):
        reasons.append(f"RSI {rsi_v:.0f} — oversold against a DOWN move")

    if mom and mom.get("decelerating") and mom.get("m3") is not None:
        reasons.append(f"momentum decelerating hard ({mom['m3']:+,.{dec}f} over 3m vs "
                       f"{mom['prev3']:+,.{dec}f} the 3m before)")

    if fvgd:
        magnet = fvgd["below"] if direction == "up" else fvgd["above"]
        if magnet and sigma and magnet["distance"] <= 3 * sigma:
            reasons.append(
                f"unfilled {magnet['dir']} FVG {magnet['lo']:,.{dec}f}–{magnet['hi']:,.{dec}f} "
                f"{'below' if direction == 'up' else 'above'} price, "
                f"{magnet['distance']:,.{dec}f} away ({magnet['distance']/sigma:.1f}x/min vol) "
                f"— acts as a magnet back the other way")

    if runx and runx.get("stalling") and runx.get("dir") == direction:
        reasons.append(f"{runx['run']} bars {direction} with ranges contracting — the move is stalling")

    if sigma and gap is not None and abs(gap) <= near_line_x * sigma:
        reasons.append(f"price is sitting on the target line (${abs(gap):,.{dec}f} away, "
                       f"under half a minute's vol) — it can flip either side on one print")

    # where / when a flip would show, when the data supports an estimate
    flip_price, eta = None, None
    magnet = (fvgd or {}).get("below") if direction == "up" else (fvgd or {}).get("above")
    if magnet:
        flip_price = (magnet["lo"] + magnet["hi"]) / 2.0
    rate = (mom or {}).get("rate_per_min")
    if flip_price is not None and rate:
        dist = abs(flip_price - (spot if spot is not None else flip_price))
        if dist and abs(rate) > 0:
            eta = dist / abs(rate)

    level = "high" if len(reasons) >= 2 else ("watch" if reasons else "none")
    text = ""
    if reasons:
        text = ("PROJECTED FLIP INCOMING \u2014 the move is "
                + ("UP" if direction == "up" else "DOWN")
                + ", the signals point to a flip "
                + ("DOWN" if direction == "up" else "UP") + ": "
                + "; ".join(reasons))
        if flip_price is not None:
            text += f". It would show around {flip_price:,.{dec}f}"
            if eta is not None and eta < 60:
                text += f" (~{eta:.0f} min at the current rate)"
        text += "."
    return dict(flag=bool(reasons), reasons=reasons, level=level,
                direction=("down" if direction == "up" else "up"),
                flip_price=flip_price, eta_min=eta, text=text)
