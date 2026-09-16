# XRP tape findings — KXXRP15M, 21 round files (20 settled), Sep 15 2026 18:00–23:00 UTC
Written from /home/user/tab/files/kalshi-bots/tape/KXXRP15M/*.ndjson. No network used.

## Data and method
Schema (read from file, not assumed): line kinds `round`, `book`, `index`, `trades`, `hb`, `settle`.
- `book` ~ every 5s (179 lines/round): yes_bid/yes_ask/no_bid/no_ask, yes_bid_sz, yes_bid_sz3 (depth within 3c), no_bid_sz, no_bid_sz3, yes_levels/no_levels, `minute`.
- `index` lines are INCREMENTAL after the first (only new 1s ticks) — accumulating them is required; taking the last line alone gives ~19 ticks and silently produces NaNs.
- `strike` is null in 3 of 21 files; the pre-open 60s index average was used instead (they agree to <0.0006 where both exist). Flagged, not hidden.

Fills: entry pays the ASK at the first book line in [5.0,6.0]; exits take the BID; Kalshi fee ceil(0.07*P*(1-P)*100)/100 on BOTH legs. No mid anywhere.

**Sample sizes.** 20 settled rounds. At minute 5 the cheaper side is inside 3–30c in **11 of 20 rounds**. That is the whole real sample for the entry question: **n=11**.
A secondary EXPANDED pseudo-sample takes the same rule at each of minutes 3–9: **83 observations from 17 rounds**. These overlap heavily and are NOT independent; every number from it is labelled as such and none of it is used as proof.

## 1. THE ENTRY FILTER — NO SIGNAL
Twelve candidates, all computable at or before minute 5 (no look-ahead: the index tick stream is truncated to ts <= the entry book line's ts; the "how did it get cheap" term uses the minute-2 ask only):
dist_vol (index-to-strike in realised-vol units, signed toward our side), slope over minutes 3–5, move from the pre-open anchor, bid-ask spread, our-side depth at touch, our-side depth within 3c, opposite-side depth within 3c, depth ratio, drop in our ask from minute 2 to minute 5, print volume, print count, fraction of print volume taken on our side, realised vol.

At n=11, Spearman vs the max multiple reached:
| feature | rho | p | ≥1.5x median | dead median | MWU p |
|---|---|---|---|---|---|
| drop m2→m5 | +0.46 | 0.16 | 0.177 | 0.145 | 0.45 |
| flow our side | -0.37 | 0.26 | 0.585 | 0.548 | 1.00 |
| realised vol | +0.33 | 0.33 | — | — | 0.16 |
| depth ratio | -0.32 | 0.34 | 0.907 | 0.995 | 0.65 |
| spread | +0.24 | 0.48 | 0.007 | 0.010 | 1.00 |
| slope 3–5 | -0.23 | 0.50 | -2.09 | -3.00 | 0.93 |
| anchor move | +0.19 | 0.57 | | | 0.79 |
| depth within 3c | -0.15 | 0.65 | 714.7 | 611.6 | 1.00 |
| print count | +0.20 | 0.56 | | | 0.53 |
| volume | +0.13 | 0.71 | | | 0.53 |
| opp depth 3c | +0.10 | 0.77 | | | 0.79 |
| dist to strike (vol units) | +0.08 | 0.81 | -7.63 | -8.16 | 0.93 |

**Nothing is significant. The best of twelve tests is p=0.16 uncorrected, which is what twelve tests produce from noise.** The two runners and the nine dead tickets sit on top of each other on every variable.

### The one candidate I will not bury, and why it is still a no
On the expanded pseudo-sample, distance-to-strike in vol units correlates with the multiple: rho +0.44 (n=83 overlapping), and at round level (median per round, n=17 rounds) rho +0.73, p=0.0009. Direction is mechanically sensible — a cheap ticket whose index is near the strike is a live coin flip, one 9 vol-units away is a corpse.
**The minute-5 sample contradicts it: rho +0.08, p=0.81 (n=11).** Of the two big runners, one was 0.72 vol from the strike (fits the story) and the other was 9.14 vol away (destroys it). The round-level correlation is also confounded: it is a median over entries at minutes 3–9, and a round that ends near the strike spends most of its life near the strike.
**Verdict: a hypothesis worth re-testing when the tape has ~100 minute-5 tickets, not a filter to trade today.**

Variants tried before believing anything: 12 features × 3 samples (minute-5 n=11, expanded n=83, round-level n=17) ≈ 36 tests, plus 5 arm levels × 5 stop rules in section 4. One "winner" out of ~60 comparisons at p=0.0009 that its own out-of-sample check refutes.

## 2. MAX-BID DISTRIBUTION FROM A 3–30c MINUTE-5 ENTRY (full path, 5s resolution)
n=11 minute-5 tickets. Multiples of entry reached by minute 14:
0.96, 0.97, 0.99, 1.00, 1.05, 1.06, 1.08, 1.53, 1.67, 3.33, 3.64 — **median 1.06x**.
- ≥1.25x: 4/11 (36%), first reached at minutes 5.6, 5.6, 5.8, 6.2
- ≥1.5x: 4/11 (36%), at minutes 5.7, 5.8, 6.2, 7.9
- ≥2x: 2/11 (18%), at minutes 7.3 and 12.1
- ≥3x: 2/11 (18%), at minutes 11.2 and 12.2
- ≥5x: 0/11
Peak minute across all 11: 5.0, 5.0, 5.0, 5.1, 5.1, 5.8, 6.7, 6.8, 7.4, 11.4, 12.3 — **six of eleven peak inside 100 seconds of entry and never trade higher again.**
Expanded pseudo-sample (n=83, 17 rounds, not independent): ≥1.5x 33%, ≥2x 27%, ≥3x 16%, ≥5x 1%, median 1.08x.

**This does NOT beat the 417-round approximation — it matches it.** Your 30.5% / 25.1% vs the tape's 33% / 27% on the expanded sample. The expectation that three sampled points were missing intraday spikes is not supported: at 5s resolution the spikes are mostly there in the sampled points too.

## 3. DOES A HALVED TICKET RECOVER? — NO, AND THIS IS THE CLEANEST RESULT IN THE TAPE
First time the bid trades below 50% of entry:
- halved **before minute 8**: n=5 (minute-5 sample) — **0 recovered to entry.** Expanded: n=20 — **0 of 20 recovered to entry.**
- halved before minute 10: 10 of 11 tickets halve at some point; 2 recover (those are the 3.3x and 3.6x, which halve late on the way down from their own peak, not early).
So an early halving is terminal in 25 of 25 observations across both samples.
P&L of adding a 50%-of-entry stop that is only live before minute 8 (arm 2x otherwise unchanged):
- minute-5 n=11: mean **+3.20c/trade better** than no stop, 5 of 11 paths changed, paired p=0.045
- expanded n=83: mean **+1.88c/trade better**, 20 paths changed, paired p=0.0002
An ALWAYS-ON 50% stop is worse, not better (expanded: −2.41c/trade vs no stop) — it cuts winners that dip after arming. The time limit is what makes it work.
**Recommendation: a 50%-of-entry stop that expires at minute 8. Caveat: 25 observations, and it is still a loss reduction, not a profit.**

## 4. IS 2x THE RIGHT ARM? — ALL ARM LEVELS ARE WITHIN NOISE
Minute-5 n=11, fills at ask/bid, fees both legs, 40% giveback unchanged:
| arm | mean | median | wins | trail exits |
|---|---|---|---|---|
| 1.5x | −10.52c | −11.20c | 3/11 | 4 |
| 1.75x | −12.14c | −11.40c | 2/11 | 2 |
| 2.0x | −12.14c | −11.40c | 2/11 | 2 |
| 2.5x | −14.23c | −12.00c | 1/11 | 2 |
| 3.0x | −14.23c | −12.00c | 1/11 | 2 |
| no trail, hold to m14 | −15.10c | | 0/11 | |
Paired differences: 1.75x and 2.0x are **identical on all 11 paths**; 2.5x and 3.0x likewise. The largest gap, 1.5x vs 3.0x, is +3.71c on 4 changed paths, p=0.25.
**SAY IT PLAINLY: these are all within noise of each other. Do not retune the arm off this data.** 1.5x is nominally best in both samples (expanded: −5.49c at 1.5x vs −6.45c at 2.0x) but no pairwise comparison reaches significance and the near-miss at 1.86x remains a coincidence.

## 5. THE BOTTOM LINE
Every variant tested loses money after real fills and fees. Best cell measured: arm 1.5x with a 50% stop expiring at minute 8, expanded sample — still negative.
Of the 11 minute-5 cheap tickets, **1 settled in the money.**
Nothing observable at the moment of entry separates the runners from the dead at the sample size that exists. The filter Anthony is waiting on is not in five hours of tape.
