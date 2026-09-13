# Kalshi 15-minute signal bots — GitHub Actions edition

Same bots, same call engine, same honest track record. The difference: they run
on **GitHub's computers for free** instead of a server you pay for, and the
dashboard is served free by **GitHub Pages**. No credit card at any step.

**Non-technical setup instructions are in [SETUP.md](SETUP.md). Start there.**

---

## What runs where

| piece | file | where it runs |
|---|---|---|
| the bots (all six assets, one loop) | `bot/gha_runner.py` | a GitHub Actions runner |
| the call engine / signals / fees | `bot/engine.py`, `bot/signals.py`, `bot/flow.py` | same |
| the track record | `record/*.json` | committed back into this repo |
| the dashboard | `docs/index.html` + `docs/snapshot.json` | GitHub Pages |
| the schedule | `.github/workflows/bots.yml` | GitHub Actions |

## Why one looping job instead of a cron

The call is locked at the **12-minute mark of a 15-minute contract**. A cron
that fires every 5 minutes would miss that mark by up to 5 minutes and the call
would be wrong — and GitHub's scheduled runs are routinely late by 5–15 minutes
on top of that. So a cron alone is not good enough, and is not the mechanism here.

Instead: **one job loops for 5 hours 25 minutes**, polling every 25 seconds,
then exits cleanly and asks GitHub to start its successor
(`gh workflow run bots.yml`, using the token GitHub hands every job — no
personal access token, nothing to create). GitHub kills any job at 6 hours;
5h25m leaves a comfortable margin and the job also carries a 350-minute
`timeout-minutes` as a hard stop.

**The `schedule:` cron every 30 minutes is the safety net, not the mechanism.**
Every half hour it tries to start a run. A `guard` job checks the GitHub API
for another in-progress run of this same workflow and stands down if one is
healthy, so nothing ever double-writes. If a runner dies at 2am — which does
happen — the cron brings the bots back within 30 minutes, unattended.

The handoff has one deliberate rule: a run older than `STALE_AFTER`
(5h20m) is treated as "already exiting", so the successor a finishing run
dispatches is allowed through. Overlap is a few seconds at most, and because
both the record and the snapshot are written by whole-file replace + commit,
a few seconds of overlap cannot corrupt anything.

On a **public** repo, Actions minutes are unlimited and free. That is the whole
reason the repo must be public.

## Why the track record survives

Two independent mechanisms:

1. **Commit-back.** Every ~2 minutes (and immediately whenever a call settles)
   the job commits `record/` and `docs/` and pushes with the built-in
   `GITHUB_TOKEN`. If someone else pushed in between, it rebases and retries up
   to 4 times.
2. **`actions/cache` fallback.** `record/` is saved to and restored from the
   Actions cache each run, so even if pushing breaks the history is not lost.

Unsettled calls are rebuilt from the committed record at boot, so a call made
in run #7 still settles — and still lands in the win/loss column — in run #8.
The win rate is continuous across the 6-hour ceiling. That continuity *is* the
product.

## Why Kalshi is never fetched from the browser

Kalshi's API returns **403 to any request carrying a browser `Origin` header**.
So the fetching happens on the runner, in Python. The runner writes
`docs/snapshot.json` **and bakes the same JSON straight into `docs/index.html`**
between `<!--SNAPSHOT_BAKE_START-->` markers, so the page has its numbers in its
own HTML before a browser executes a line of JavaScript. The small fetch the
page does afterwards is a same-origin re-read of the file GitHub Pages serves —
never Kalshi.

## Discord

Optional. Add a repo **Secret** named `DISCORD_WEBHOOK_URL` and posts go live;
leave it out and the bot logs what it would have posted and keeps the record
anyway. **Never paste a webhook into `config.json` in a public repo** — a secret
is invisible to the public, a committed file is not.

## Tuning

Everything lives in `config.json` (edit it on github.com, commit, done):
`noise_k` (raise to be pickier), `min_open_interest`, `poll_seconds`,
`call_minute`.

## Running it on a normal computer

```bash
RUN_SECONDS=300 python3 bot/gha_runner.py     # 5 minutes, no Discord, no git
```

## The honest part, unchanged

These are near coin-flip markets. The record starts at zero, every settled call
lands in it win or lose, and the page shows the breakeven line after Kalshi's
fees — because beating 50% is not the bar; beating breakeven is.
