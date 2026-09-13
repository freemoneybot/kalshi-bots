"""Order-flow reads: whale orders, whale trades, spot prints, projected flip, scalps.

Every one of these is a HEURISTIC. A big resting order can be a market maker, a
spoof, or a hedge; a big print can be someone closing. None of it is a proven
edge on a 15-minute binary. Anything that cannot be computed from data actually
fetched is returned as unavailable, never guessed.
"""
import statistics, time
from kalshi_core import KALSHI, _get

_prev_books = {}          # ticker -> {"yes": {price: size}, "no": {...}}


def _book(ticker):
    j = _get(f"{KALSHI}/markets/{ticker}/orderbook")
    ob = j.get("orderbook_fp") or j.get("orderbook") or {}
    out = {}
    for side in ("yes", "no"):
        rows = ob.get(side + "_dollars") or ob.get(side) or []
        out[side] = [(float(p), float(s)) for p, s in rows if float(s) > 0]
    return out


def whale_orders(ticker, min_ratio=3.0, min_size=100.0, near=0.20):
    """Unusually large resting orders, and orders that appeared/vanished since
    the last look at this contract."""
    try:
        book = _book(ticker)
    except Exception as ex:
        return {"available": False, "why": f"orderbook unavailable ({type(ex).__name__})"}
    # The deep tails of these books are full of huge 0.1c resting quotes that are
    # not information. Only the part of the book anywhere near the money counts.
    mid = None
    try:
        ya = max((p for p, _s in book["yes"]), default=None)
        na = max((p for p, _s in book["no"]), default=None)
        if ya is not None and na is not None:
            mid = (ya + (1 - na)) / 2
    except Exception:
        mid = None
    big, note = [], []
    for side in ("yes", "no"):
        rows = [(p, s) for p, s in book[side]
                if 0.02 <= p <= 0.98 and
                (mid is None or abs((p if side == "yes" else 1 - p) - mid) <= near)]
        if len(rows) < 4:
            continue
        sizes = [s for _p, s in rows]
        med = statistics.median(sizes)
        for p, s in rows:
            if s >= min_size and med > 0 and s / med >= min_ratio:
                big.append({"side": side.upper(), "price": p, "size": s,
                            "x_median": s / med})
    big.sort(key=lambda b: -b["size"])
    prev = _prev_books.get(ticker)
    if prev:
        for side in ("yes", "no"):
            now = {p: s for p, s in book[side]}
            was = {p: s for p, s in prev[side]}
            for p, s in now.items():
                if s >= min_size and s - was.get(p, 0) >= min_size:
                    note.append(f"a {s:,.0f}-lot {side.upper()} order appeared at {p*100:.0f}\u00a2")
            for p, s in was.items():
                if s >= min_size and s - now.get(p, 0) >= min_size:
                    note.append(f"a {s:,.0f}-lot {side.upper()} order left {p*100:.0f}\u00a2")
    _prev_books[ticker] = book
    return {"available": True, "big": big[:3], "changes": note[:3],
            "depth": {s: sum(x[1] for x in book[s]) for s in ("yes", "no")}}


def whale_trades(ticker, minutes=5, min_ratio=3.0, min_size=50.0):
    """Large executed trades on this contract and which side lifted them."""
    try:
        j = _get(f"{KALSHI}/markets/trades", {"ticker": ticker, "limit": 200})
        tr = j.get("trades", [])
    except Exception as ex:
        return {"available": False, "why": f"trades feed unavailable ({type(ex).__name__})"}
    cut = time.time() - minutes * 60
    rows = []
    for t in tr:
        try:
            ts = time.mktime(time.strptime(t["created_time"][:19], "%Y-%m-%dT%H:%M:%S"))
            ts -= time.timezone
        except Exception:
            continue
        if ts < cut:
            continue
        rows.append({"size": float(t.get("count_fp") or 0),
                     "side": (t.get("taker_side") or "").upper(),
                     "price": float(t.get("yes_price_dollars") or 0),
                     "block": bool(t.get("is_block_trade")), "ts": ts})
    if not rows:
        return {"available": True, "n": 0, "big": [], "yes_vol": 0, "no_vol": 0,
                "note": f"no trades in the last {minutes} min"}
    sizes = [r["size"] for r in rows]
    med = statistics.median(sizes) or 1
    big = [r for r in rows if r["size"] >= min_size and r["size"] / med >= min_ratio]
    big.sort(key=lambda r: -r["size"])
    yv = sum(r["size"] for r in rows if r["side"] == "YES")
    nv = sum(r["size"] for r in rows if r["side"] == "NO")
    return {"available": True, "n": len(rows), "big": big[:3], "yes_vol": yv,
            "no_vol": nv, "median": med}


def spot_whales(product, minutes=5, min_ratio=8.0):
    """Unusually large prints on Coinbase's public trade feed."""
    try:
        rows = _get(f"https://api.exchange.coinbase.com/products/{product}-USD/trades?limit=200")
    except Exception as ex:
        return {"available": False, "why": f"Coinbase trades unavailable ({type(ex).__name__})"}
    cut = time.time() - minutes * 60
    out = []
    for r in rows:
        try:
            ts = time.mktime(time.strptime(r["time"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        except Exception:
            continue
        if ts < cut:
            continue
        out.append({"size": float(r["size"]), "price": float(r["price"]),
                    # Coinbase's "side" is the MAKER's side: a "sell" maker means
                    # the taker BOUGHT. Flipped here so it reads as aggression.
                    "dir": "buy" if r["side"] == "sell" else "sell", "ts": ts})
    if len(out) < 8:
        return {"available": True, "n": len(out), "big": [],
                "note": f"only {len(out)} prints in the last {minutes} min"}
    med = statistics.median([o["size"] for o in out]) or 1
    big = [dict(o, x_median=o["size"] / med) for o in out if o["size"] / med >= min_ratio]
    big.sort(key=lambda o: -o["size"])
    buys = sum(o["size"] for o in out if o["dir"] == "buy")
    sells = sum(o["size"] for o in out if o["dir"] == "sell")
    return {"available": True, "n": len(out), "big": big[:3], "median": med,
            "buy_vol": buys, "sell_vol": sells}


def projected_flip(candles, spot, target, mins_left):
    """Pure linear extrapolation of the last 3 minutes. Not a forecast."""
    if len(candles) < 4:
        return {"available": False, "why": "not enough 1-minute bars yet"}
    rate = (candles[-1]["c"] - candles[-4]["c"]) / 3.0     # $ per minute
    gap = spot - target
    need = -gap                                            # move required to cross
    if rate == 0 or (need > 0) != (rate > 0):
        return {"available": True, "on_pace": False, "rate": rate, "need": need,
                "mins_left": mins_left}
    eta = abs(need) / abs(rate)
    return {"available": True, "on_pace": eta <= mins_left, "eta": eta, "rate": rate,
            "need": need, "mins_left": mins_left}


def scalp(yes_ask, no_ask, signal_lean, max_price=0.20, yes_bid=0.0, no_bid=0.0,
          trades=None, orders=None):
    """Short-horizon scalp read. Runs on EVERY asset, every round.

    Side: the way the price action leans; with no lean, whichever side the
    recent aggressor flow is lifting; failing that, the cheaper side (more
    payoff per cent risked). Entry is the ask you would pay, exit target is
    the ask plus 5c capped at 95c, and the cheap flag says whether the entry
    is under max_price. Low probability, high payoff: most of these lose, and
    they live in their own bucket, never in the headline record.
    """
    yes_ask = float(yes_ask or 0); no_ask = float(no_ask or 0)
    if not yes_ask and not no_ask:
        return None
    side = None
    why = ""
    if signal_lean == "up":
        side, why = "YES", "price action leans UP"
    elif signal_lean == "down":
        side, why = "NO", "price action leans DOWN"
    else:
        t = trades or {}
        yv, nv = float(t.get("yes_vol") or 0), float(t.get("no_vol") or 0)
        if t.get("available") and (yv or nv) and abs(yv - nv) / max(yv + nv, 1) >= 0.2:
            side = "YES" if yv > nv else "NO"
            why = f"signals mixed; 5-min aggressor flow is {max(yv,nv)/(yv+nv)*100:.0f}% {side}"
        else:
            side = "YES" if (yes_ask and (not no_ask or yes_ask <= no_ask)) else "NO"
            why = "signals mixed and flow is balanced; taking the cheaper side"
    price = yes_ask if side == "YES" else no_ask
    if not price or price <= 0 or price >= 1:
        side = "NO" if side == "YES" else "YES"
        price = yes_ask if side == "YES" else no_ask
        why += " (other side unquoted)"
    if not price or price <= 0 or price >= 1:
        return None
    bid = (yes_bid if side == "YES" else no_bid) or 0.0
    target = min(0.95, round(price + 0.05, 2))
    return {"side": side, "price": price, "bid": float(bid), "target": target,
            "payoff": 1.0 / price, "cheap": price <= max_price,
            "move_cents": round((target - price) * 100), "why": why}
