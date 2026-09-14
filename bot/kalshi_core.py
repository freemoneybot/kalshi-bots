"""
kalshi_bots core — Anthony's private 15-minute signal bots.

NOT A PRODUCT. NOT AN EDGE. These are near-coin-flip markets.
"""
import json, math, os, statistics, time, urllib.parse, urllib.request, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
RECORD_DIR = os.path.join(HERE, "record")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "Mozilla/5.0 (kalshi-bots; personal use)"}

# ---------------------------------------------------------------- assets
ASSETS = {
    "gold":   dict(label="GOLD",   series="KXGOLD15M",   unit="$", dec=2,
                   spot=("gold_api", "XAU"), ohlc=("yahoo", "GC=F"), color=0xD4AF37),
    "silver": dict(label="SILVER", series="KXSILVER15M", unit="$", dec=3,
                   spot=("gold_api", "XAG"), ohlc=("yahoo", "SI=F"), color=0xC0C0C0),
    "oil":    dict(label="OIL",    series="KXWTI15M",    unit="$", dec=2,
                   spot=("cnbc", "@CL.1"),  ohlc=("yahoo", "CL=F"), color=0x6E7B8B),
    "btc":    dict(label="BTC",    series="KXBTC15M",    unit="$", dec=2,
                   spot=("coinbase", "BTC"), ohlc=("coinbase", "BTC"), color=0xF7931A),
    "sol":    dict(label="SOL",    series="KXSOL15M",    unit="$", dec=2,
                   spot=("coinbase", "SOL"), ohlc=("coinbase", "SOL"), color=0x9945FF),
    "eth":    dict(label="ETH",    series="KXETH15M",    unit="$", dec=2,
                   spot=("coinbase", "ETH"), ohlc=("coinbase", "ETH"), color=0x627EEA),
    "xrp":    dict(label="XRP",    series="KXXRP15M",    unit="$", dec=4,
                   spot=("coinbase", "XRP"), ohlc=("coinbase", "XRP"), color=0x23292F),
    "doge":   dict(label="DOGE",   series="KXDOGE15M",   unit="$", dec=5,
                   spot=("coinbase", "DOGE"), ohlc=("coinbase", "DOGE"), color=0xC2A633),
}

# Which assets are crypto. The crypto Fear & Greed index applies ONLY to these;
# gold, silver and oil get their signals from their own candles.
CRYPTO = {"btc", "eth", "sol", "xrp", "doge"}


def is_crypto(asset):
    return asset in CRYPTO

DISCLAIMER = ("These are near coin-flip markets. This read is not an edge — it is a filter "
              "that tries to stay out of the noise. Size accordingly, or don't trade it at all.")


# ---------------------------------------------------------------- http
def _get(url, params=None, timeout=20):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- spot
class Quote:
    def __init__(self, price, ts, source):
        self.price, self.ts, self.source = float(price), float(ts), source

    @property
    def age(self):
        return time.time() - self.ts


def get_spot(asset):
    kind, sym = ASSETS[asset]["spot"]
    if kind == "coinbase":
        j = _get(f"https://api.coinbase.com/v2/prices/{sym}-USD/spot")
        return Quote(j["data"]["amount"], time.time(), "Coinbase spot")
    if kind == "gold_api":
        j = _get(f"https://api.gold-api.com/price/{sym}")
        ts = dt.datetime.strptime(j["updatedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc).timestamp()
        return Quote(j["price"], ts, "gold-api.com spot")
    if kind == "yahoo_spot":
        j = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                 dict(range="1d", interval="1m"))
        meta = j["chart"]["result"][0]["meta"]
        return Quote(meta["regularMarketPrice"], float(meta["regularMarketTime"]),
                     "Yahoo " + sym)
    if kind == "cnbc":
      try:
        j = _get("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol",
                 dict(symbols=sym, requestMethod="itv", noform=1, partnerId=2,
                      exthrs=1, output="json"))
        q = j["FormattedQuoteResult"]["FormattedQuote"][0]
        ts = dt.datetime.fromisoformat(q["last_time"]).timestamp()
        return Quote(q["last"].replace(",", ""), ts, "CNBC " + sym)
      except Exception:
        # CNBC answers 403 from this box at times; the Yahoo futures chart is the
        # same instrument and is used as the fallback rather than going blind.
        ysym = ASSETS[asset]["ohlc"][1]
        j = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}",
                 dict(range="1d", interval="1m"))
        meta = j["chart"]["result"][0]["meta"]
        return Quote(meta["regularMarketPrice"], float(meta["regularMarketTime"]),
                     "Yahoo " + ysym)
    raise ValueError(kind)


def seed_candles(asset, minutes=40):
    """Historical 1-minute OHLC so the bot is not blind on its first round.
    Returns list of dicts oldest-first: {t, o, h, l, c}."""
    kind, sym = ASSETS[asset]["ohlc"]
    out = []
    try:
        if kind == "coinbase":
            rows = _get(f"https://api.exchange.coinbase.com/products/{sym}-USD/candles",
                        dict(granularity=60))
            for t, lo, hi, op, cl, _v in rows[:minutes]:
                out.append(dict(t=t, o=op, h=hi, l=lo, c=cl))
            out.reverse()
        else:
            j = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                     dict(range="1d", interval="1m"))
            r = j["chart"]["result"][0]
            q = r["indicators"]["quote"][0]
            for i, t in enumerate(r["timestamp"]):
                if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
                    continue
                out.append(dict(t=t, o=q["open"][i], h=q["high"][i],
                                l=q["low"][i], c=q["close"][i]))
            out = out[-minutes:]
    except Exception:
        return []
    return out


# ---------------------------------------------------------------- tick buffer
class Ticker:
    """Samples spot on a timer and builds its own 1-minute candles."""

    def __init__(self, asset):
        self.asset = asset
        self.ticks = []                       # (ts, price)
        self.seeded = seed_candles(asset)
        self.seed_used = bool(self.seeded)

    def sample(self, q):
        self.ticks.append((q.ts, q.price))
        cut = time.time() - 60 * 60
        self.ticks = [t for t in self.ticks if t[0] >= cut]

    def live_candles(self):
        buckets = {}
        for ts, p in self.ticks:
            m = int(ts // 60) * 60
            b = buckets.setdefault(m, dict(t=m, o=p, h=p, l=p, c=p))
            b["h"] = max(b["h"], p); b["l"] = min(b["l"], p); b["c"] = p
        return [buckets[k] for k in sorted(buckets)]

    def candles(self, minutes=16):
        """Live candles built from this process's own ticks, backfilled with
        seeded history where the bot has not been running long enough."""
        live = self.live_candles()
        if len(live) >= minutes:
            return live[-minutes:]
        need = minutes - len(live)
        first = live[0]["t"] if live else time.time()
        seed = [c for c in self.seeded if c["t"] < first][-need:]
        return seed + live

    def sigma_per_minute(self, window=15):
        """Dollar standard deviation of 1-minute closes changes over the window."""
        cs = self.candles(window + 1)
        closes = [c["c"] for c in cs]
        diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        if len(diffs) < 3:
            return None, 0
        return statistics.pstdev(diffs), len(diffs)

    def source_note(self):
        n_live = len(self.live_candles())
        if n_live >= 15:
            return f"{n_live} live 1-min bars this session"
        return f"{n_live} live bars + {'seeded history' if self.seed_used else 'NO history (cold)'}"
