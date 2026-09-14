#!/usr/bin/env python3
"""GitHub Actions entrypoint for the Kalshi 15-minute signal bots.

ONE process runs ALL six assets in one loop instead of six daemons, because a
GitHub Actions job is one process tree with a hard 6-hour ceiling.

Every loop (default 25s) it:
  * samples spot, reads the open Kalshi contract, builds each asset's call
  * posts the open note at minute 1 and the CALL at minute 7 (Discord webhook)
  * settles anything whose close time has passed and writes the result
  * writes docs/snapshot.json and bakes it into docs/index.html (server-side)
  * every PUBLISH_EVERY seconds, commits record/ + docs/ back to the repo

Kalshi is fetched HERE, on GitHub's runner. The published page never calls
Kalshi from the browser (Kalshi 403s any request carrying a browser Origin).

Env:
  RUN_SECONDS          how long to loop before exiting cleanly (default 19500 = 5h25m)
  DISCORD_WEBHOOK_URL  optional; when set, posts go to Discord instead of the log
  GIT_PUSH             "1" to commit+push (the workflow sets it; local test leaves it off)
"""
import json, os, subprocess, sys, time, traceback, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from kalshi_core import ASSETS, Ticker, get_spot, RECORD_DIR
import engine as E
import discord_post as D

DOCS = os.path.join(REPO, "docs")
SNAP = os.path.join(DOCS, "snapshot.json")
TEMPLATE = os.path.join(DOCS, "index.html")
CFG_PATH = os.path.join(REPO, "config.json")

RUN_SECONDS = int(os.environ.get("RUN_SECONDS") or 19500)
PUBLISH_EVERY = int(os.environ.get("PUBLISH_EVERY") or 120)
GIT_PUSH = os.environ.get("GIT_PUSH") == "1"

BAKE_OPEN = "<!--SNAPSHOT_BAKE_START-->"
BAKE_CLOSE = "<!--SNAPSHOT_BAKE_END-->"


def log(m):
    print(f"[{dt.datetime.utcnow().strftime('%H:%M:%S')}Z] {m}", flush=True)


def load_cfg():
    cfg = json.load(open(CFG_PATH))
    hook = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    if hook:
        cfg["shared_webhook_url"] = hook
        cfg["settings"]["dry_run"] = False
    else:
        cfg["settings"]["dry_run"] = True
    return cfg


# ------------------------------------------------------------------ per asset
class AssetRunner:
    def __init__(self, asset):
        self.asset = asset
        self.ticker = Ticker(asset)
        self.seen_open = set()
        self.seen_call = set()
        # Unsettled calls are rebuilt from the committed record, so a call made
        # in run #7 still settles in run #8. That is what makes the history
        # continuous across GitHub's 6-hour job ceiling.
        rec = E.load_record(asset)
        self.pending = {c["ticker"]: c for c in rec["calls"] if c.get("result") is None}
        for c in rec["calls"]:
            base = c["ticker"].split(":")[0]
            self.seen_open.add(base)
            if not c.get("is_scalp"):
                self.seen_call.add(f"{base}@{int(c.get('mark') or 12)}m")
        self.row = {"label": ASSETS[asset]["label"], "dec": ASSETS[asset]["dec"]}

    def step(self, cfg):
        st = cfg["settings"]
        a = self.asset
        row = {"label": ASSETS[a]["label"], "dec": ASSETS[a]["dec"]}
        try:
            q = get_spot(a)
            self.ticker.sample(q)
            row["spot"] = q.price
            row["spot_source"] = q.source
            row["spot_age"] = q.age
            m = E.open_market(ASSETS[a]["series"])
            if m is None:
                row["closed"] = True
                row["note"] = ("BETWEEN ROUNDS \u2014 the last contract settled and Kalshi hasn't "
                               "listed the next one yet. Usually back within a minute or two."
                               ) if a in ("sol", "eth", "xrp") else (
                    f"MARKET CLOSED \u2014 Kalshi lists the 15-minute {ASSETS[a]['label'].lower()} "
                    f"contracts only while that market trades (quiet from Friday afternoon to "
                    f"Sunday evening ET).")
            else:
                row["closed"] = False
                tk = m["ticker"]
                c = E.build_call(a, m, q, self.ticker, cfg)
                minute = (time.time() - E.iso(m["open_time"])) / 60.0
                disp = dict(c)
                disp.pop("ts", None)
                row.update(disp)
                row["elapsed_min"] = minute
                row["bars"] = self.ticker.source_note()

                if minute >= st["open_post_minute"] and tk not in self.seen_open:
                    self.seen_open.add(tk)
                    D.send(a, cfg, D.open_embed(c))
                    log(f"{a}: open post sent for {tk}")

                for mk in E.call_marks(st):
                    key = f"{tk}@{mk}m"
                    if minute < mk or key in self.seen_call:
                        continue
                    self.seen_call.add(key)
                    final = (mk == E.final_mark(st))
                    rec = E.load_record(a)
                    t = E.tally(rec["calls"], st["small_sample_threshold"])
                    D.send(a, cfg, D.call_embed(c, E.tally_line(ASSETS[a]["label"], t),
                                                mark=mk, final=final))
                    entry = E.make_entry(c, mk, final)
                    E.append_call(a, entry)
                    self.pending[entry["ticker"]] = entry
                    log(f"{a}: CALL {mk}m {c['call']} ({entry['conviction']}) "
                        f"{c['price_label']} for {tk}")
                    if final and c.get("scalp"):
                        sc = E.scalp_entry(c, entry, mk)
                        E.append_call(a, sc)
                        self.pending[sc["ticker"]] = sc
                        log(f"{a}: SCALP {sc['call']} at {sc['entry_price']*100:.0f}c")
        except Exception as ex:
            row["error"] = f"{type(ex).__name__}: {ex}"
            log(f"{a}: cycle error {row['error']}")
        self.row = row
        return row

    def settle(self, cfg):
        """Close out anything whose contract has expired. Returns True if the
        record changed (which is what triggers a commit)."""
        st = cfg["settings"]
        a = self.asset
        changed = False
        for tk, entry in list(self.pending.items()):
            try:
                if time.time() < E.iso(entry["close_time"]) + 90:
                    continue
                mm = E.market_by_ticker(tk.split(":")[0])
                res = (mm.get("result") or "").lower()
                if res not in ("yes", "no"):
                    if time.time() > E.iso(entry["close_time"]) + 1800:
                        self.pending.pop(tk, None)
                    continue
                correct = ((entry["call"] == "UP" and res == "yes") or
                           (entry["call"] == "DOWN" and res == "no"))
                entry["result"] = res
                entry["correct"] = bool(correct) if entry["call"] != "NO CALL" else None
                rec = E.append_call(a, entry)
                t = E.tally(rec["calls"], st["small_sample_threshold"])
                c = dict(label=ASSETS[a]["label"], ticker=tk, call=entry["call"])
                if not E.EARLY(entry):
                    D.send(a, cfg, D.settle_embed(c, res, correct,
                                                  E.tally_line(ASSETS[a]["label"], t), t))
                log(f"{a}: settled {tk} -> {res} ({'right' if correct else 'wrong'})")
                self.pending.pop(tk, None)
                changed = True
            except Exception as ex:
                log(f"{a}: settle error on {tk}: {type(ex).__name__}: {ex}")
        return changed


# ------------------------------------------------------------------ snapshot
def build_snapshot(runners, cfg):
    out = {"generated_at": time.time(), "assets": {}, "record": {}}
    allc = []
    for a, r in runners.items():
        out["assets"][a] = r.row
        rec = E.load_record(a)
        allc += rec["calls"]
        t = E.tally(rec["calls"], cfg["settings"]["small_sample_threshold"])
        t["scalp"] = E.scalp_tally(rec["calls"])
        t["line"] = E.tally_line(ASSETS[a]["label"], t)
        out["record"][a] = t
        head = [c for c in rec["calls"]
                if not c.get("is_scalp") and not E.EARLY(c)
                and c.get("call") in ("UP", "DOWN") and c.get("correct") is not None]
        out.setdefault("recent", {})[a] = [
            {"call": c["call"], "correct": bool(c["correct"]), "ts": c.get("ts"),
             "ticker": c.get("ticker"), "entry_price": c.get("entry_price")}
            for c in head[-14:]]
        streak = 0
        if head:
            want = bool(head[-1]["correct"])
            for c in reversed(head):
                if bool(c["correct"]) != want:
                    break
                streak += 1
            if not want:
                streak = -streak
        t["streak"] = streak
    ov = E.tally(allc, cfg["settings"]["small_sample_threshold"])
    ov["scalp"] = E.scalp_tally(allc)
    ov["line"] = E.tally_line("ALL ASSETS", ov)
    out["record"]["_overall"] = ov
    rows = sorted(allc, key=lambda c: c.get("ts", 0), reverse=True)
    keys = ("ticker", "call", "conviction", "gap", "band", "mark", "price_label",
            "price_yes", "price_no", "result", "correct", "ts", "entry_price",
            "is_scalp", "is_early", "reversal_flag", "reversal_text", "rsi")
    out["log"] = [{k: c.get(k) for k in keys} for c in rows[:60]]
    return out


def write_site(snap):
    """Write snapshot.json AND bake it into index.html at build time.

    The baked copy is what makes this genuinely server-rendered: the page has
    the numbers in its own HTML before a browser runs a line of JavaScript. The
    fetch of ./snapshot.json is a same-origin refresh on top of that, never a
    call to Kalshi."""
    os.makedirs(DOCS, exist_ok=True)
    blob = json.dumps(snap, default=str)
    with open(SNAP + ".tmp", "w") as f:
        json.dump(snap, f, indent=1, default=str)
    os.replace(SNAP + ".tmp", SNAP)

    html = open(TEMPLATE, encoding="utf-8").read()
    payload = (BAKE_OPEN + "<script>window.__SNAPSHOT__=" +
               blob.replace("</", "<\\/") + ";</script>" + BAKE_CLOSE)
    if BAKE_OPEN in html:
        pre = html.split(BAKE_OPEN)[0]
        post = html.split(BAKE_CLOSE, 1)[1]
        html = pre + payload + post
    else:
        html = html.replace("</head>", payload + "\n</head>", 1)
    tmp = TEMPLATE + ".tmp"
    open(tmp, "w", encoding="utf-8").write(html)
    os.replace(tmp, TEMPLATE)


# ------------------------------------------------------------------ git
def git(*args, check=False):
    r = subprocess.run(["git"] + list(args), cwd=REPO, capture_output=True, text=True)
    if check and r.returncode != 0:
        log("git " + " ".join(args) + " failed: " + (r.stderr or r.stdout)[-300:])
    return r


def commit_and_push(msg):
    if not GIT_PUSH:
        return
    # A rebase left half-done by an earlier cycle wedges every commit after it,
    # and the loop keeps running while nothing reaches the repo. Clear it first.
    for d in ("rebase-merge", "rebase-apply"):
        if os.path.isdir(os.path.join(REPO, ".git", d)):
            log("clearing a stuck rebase before committing")
            git("rebase", "--abort")
    git("add", "record", "docs")
    if not git("diff", "--cached", "--quiet").returncode:
        return                                   # nothing changed
    git("commit", "-m", msg, check=True)
    branch = (git("rev-parse", "--abbrev-ref", "HEAD").stdout or "main").strip() or "main"
    for attempt in range(4):
        r = git("push", "origin", "HEAD")
        if r.returncode == 0:
            log("pushed: " + msg)
            return
        log("push rejected: " + ((r.stderr or r.stdout).strip()[-400:] or "(no output)"))
        # Someone else pushed in between. record/ and docs/ are rewritten whole
        # by this process every cycle, so replant our tree on top of whatever
        # landed instead of rebasing into a conflict we can never resolve.
        f = git("fetch", "origin", branch)
        if f.returncode != 0:
            log("fetch failed: " + ((f.stderr or f.stdout).strip()[-300:] or "(no output)"))
        else:
            git("reset", "--soft", "origin/" + branch)
            git("add", "record", "docs")
            if git("diff", "--cached", "--quiet").returncode:
                git("commit", "-m", msg, check=True)
            else:
                log("nothing to push after replanting on origin/" + branch)
                return
        time.sleep(2 + attempt * 3)
    log("push failed after 4 tries; the next cycle will try again")


def setup_git():
    if not GIT_PUSH:
        return
    git("config", "user.name", "kalshi-bots[bot]")
    git("config", "user.email", "kalshi-bots@users.noreply.github.com")


# ------------------------------------------------------------------ main
def main():
    cfg = load_cfg()
    log(f"boot: {'DISCORD LIVE' if not cfg['settings']['dry_run'] else 'dry run (no webhook secret)'}"
        f" · run_seconds={RUN_SECONDS} · git_push={GIT_PUSH} · record={RECORD_DIR}")
    setup_git()
    runners = {}
    for a in ASSETS:
        runners[a] = AssetRunner(a)
        log(f"{a}: seeded {len(runners[a].ticker.seeded)} historical 1-min bars, "
            f"{len(runners[a].pending)} unsettled call(s) carried over")

    started = time.time()
    last_pub = 0.0
    cycles = 0
    while time.time() - started < RUN_SECONDS:
        t0 = time.time()
        cfg = load_cfg()
        changed = False
        for a, r in runners.items():
            r.step(cfg)
            if r.settle(cfg):
                changed = True
        snap = build_snapshot(runners, cfg)
        write_site(snap)
        cycles += 1
        live = sum(1 for x in snap["assets"].values()
                   if not x.get("closed") and not x.get("error"))
        log(f"cycle {cycles}: snapshot written, {live}/{len(ASSETS)} live markets "
            f"({time.time()-t0:.1f}s)")
        if changed or time.time() - last_pub >= PUBLISH_EVERY:
            commit_and_push(f"bots: snapshot {dt.datetime.utcnow().strftime('%Y-%m-%d %H:%MZ')}"
                            + (" + settled call(s)" if changed else ""))
            last_pub = time.time()
        sleep = max(2.0, cfg["settings"]["poll_seconds"] - (time.time() - t0))
        if time.time() - started + sleep >= RUN_SECONDS:
            break
        time.sleep(sleep)

    log(f"run window done after {cycles} cycles; final commit then exit 0")
    commit_and_push("bots: final snapshot of this run")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("interrupted")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
