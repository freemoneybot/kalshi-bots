# XRP exit study 2 — H4: THE HALF-STOP

Sample: 62 tape rounds with a minute-5 book; 31 cheap-side eligible (ask 3-30c) with a complete
path to minute 14. Fills: entry pays the ASK at minute 5, exits take the observed BID at the
snapshot the trigger is detected on (~5s book cadence), Kalshi fee ceil(0.07*P*(1-P)*100)/100 each leg.
All counts are DISTINCT ROUNDS.

## 1. The "0 of 27" was pseudo-replication, and it is also wrong on its own terms
Distinct rounds in this tape where the bid touched <=50% of entry while the trail was NOT armed: **17**.
Of those 17: **6 recovered to entry, 5 to entry+3c, 5 to a post-fee profit.** Not 0.
Restricting to halvings before minute 8 (n=12 rounds): 5 recovered to entry, 4 to a post-fee profit.
The earlier finding counted book observations inside a handful of rounds, and its round-level
version (9 rounds, reported later) was simply a smaller, luckier sample.

Worst individual case, the one that pays for the whole rule: KXXRP15M-26SEP160045-45, entry 26.0c,
halved at minute 9.3, then ran to a best bid of **95.9c**. With the half-stop: **-17.0c**.
Without it: **+68.9c**. One round, an 86c swing.

## 2. Realised P&L per trade, n=31 rounds, all variants on the same 31 paths
| rule | mean/trade | wins | median |
|---|---|---|---|
| live: half-stop 50%, no minute cutoff | **-2.76c** | 11 | -7.20c |
| no half-stop at all | **-2.15c** | 14 | -0.80c |
| half-stop 50%, only before minute 8 | **-0.86c** | 12 | -6.00c |
| half-stop 50%, only before minute 10 | -2.93c | 11 | -7.20c |
| deeper stop at 35% of entry | -4.18c | 11 | -8.20c |
| deeper stop at 25% of entry | -4.02c | 12 | -8.10c |

Paired Wilcoxon on the same rounds:
- no half-stop vs live: **+0.61c/trade, 17/31 paths differ, p=0.149**
- before-min-8 vs live: +1.90c/trade, only 5/31 paths differ, p=0.500
- before-min-8 vs no half-stop: +1.30c/trade, 12/31 differ, p=0.158

## 3. The honest answer
**The half-stop as shipped (50%, no minute cutoff) costs money.** Every variant of it that removes
or limits it beats it; nothing beats it. The sign is consistent across two independent worker
passes (-2.99 vs -2.00 on 27 rounds; -2.76 vs -2.15 on 31 rounds) and the mechanism is plain:
6 of 17 halved tickets come back, and one of them came back 3.7x, so an unconditional cut at 50%
sells the tail that the whole strategy depends on.
**The size of the gain is NOT statistically established** (p=0.149) — 31 rounds cannot resolve
+0.6c/trade. What is established is that the "never recovers" premise the stop was shipped on is
false at round level. Mechanism, not pattern: cheap-side tickets are convex; the stop truncates
the only fat part of the distribution.

**Recommendation: unship the unconditional half-stop.** If a stop is kept at all, keep it only
before minute 8 (that is the cell where the recoveries are rarest), and know that the minute-8
variant differs from the live rule on just 5 of 31 paths — it is close to doing nothing.

# H2 — THE GAPPED-THROUGH GIVEBACK EXIT: it is LIQUIDITY, not polling

## The 09:05 round, quoted from the tape (KXXRP15M-26SEP160515-15, our side = NO, entry 5.6c ask at 09:05:00)
Best NO bid, consecutive 5-second book snapshots:
    09:06:25  bid 0.19  ask 0.20  bid_sz 20    sz-within-3c 204
    09:06:30  bid 0.19  ask 0.20  bid_sz 17    sz-within-3c 396
    09:06:35  bid 0.19  ask 0.20  bid_sz 20    sz-within-3c 326
    09:06:40  bid 0.071 ask 0.079 bid_sz 11    sz-within-3c 1698   <-- 12c in one 5s window
    09:06:45  bid 0.091 09:06:50 bid 0.093 ... 09:07:36 bid 0.20
The bot's fill was 7.1c at minute 6.68 = 09:06:40. **The tape's own 5-second snapshot at that
instant shows 7.1c.** The bot was not looking at a stale book; it saw what the book was.
The level (14.2c) never existed as a bid: the depth above 7.1c evaporated and reappeared
(sz-within-3c jumps 326 -> 1698 as the touch falls), i.e. an air pocket, and the bid was back at
20c 56 seconds later.

## Tape-wide, n=31 eligible rounds, 882 consecutive snapshot pairs taken while a position was in profit
- pairs where the best bid fell more than 5c between consecutive snapshots: **66 (7.5%)**
- distinct rounds containing at least one: **17 of 31**
- of those 66 gaps, the bid returned to within 10% of its pre-gap level inside 60 seconds: **35 of 66**
So half these drops are transient vacuums, not repricings.

## Is faster polling worth anything? No. Same 31 paths, live rule, poll cadence varied:
| cadence | mean/trade | wins |
|---|---|---|
| ~5 s (tape native) | -2.76c | 11 |
| ~11 s (what the bot does) | **-1.72c** | 10 |
| ~20 s | -2.81c | 7 |
Same with the half-stop removed: 5s -2.15c, 11s -1.45c, 20s -2.39c.
**Polling faster does not help and reads slightly worse** — a faster eye sees the air pocket and
sells into it. The differences are inside noise at n=31; the point is that there is no gain to buy.

**Mechanism:** the exit is a market order into a book whose top three cents hold 20-300 contracts.
A giveback level computed off a peak that was itself 20 contracts deep is a level no one is bidding.
This is a liquidity problem. Faster polling is the wrong fix, and the earlier advice to Anthony
that faster polling is the fix should be withdrawn.

# H3 — AT THE CHEAP END THE GIVEBACK IS SMALLER THAN BOOK NOISE (confirmed), BUT A CENTS FLOOR DOES NOT FIX IT (null)

## Book noise, tickets priced 5-15c, n=31 eligible rounds (observations pooled, stated as such)
Absolute change in the best bid over a window, while the bid sat in 5-15c:
| window | observations | median move | 75th | 90th |
|---|---|---|---|---|
| 10 s | 1071 | 1.00c | 2.00c | 3.30c |
| 20 s | 1027 | 1.30c | 2.70c | 4.64c |
| 30 s | 986  | 1.80c | 3.10c | 5.00c |

## 40% of the gain at the moment the trail arms, entries 5-15c, n=7 DISTINCT ROUNDS that armed
1.80c, 2.08c, 3.60c, 2.96c, 2.32c, 2.80c, 1.96c — **median 2.32c**.
So the exit threshold at the cheap end sits between the median and the 75th percentile of pure
10-second noise. **The finding is confirmed: the cheap-end giveback fires on book noise.** The live
13:05 trade (+0.9c, held 26 seconds, 2c giveback) is exactly that.

## Minimum giveback floor of Nc, same 31 paths
| floor | with half-stop | without half-stop |
|---|---|---|
| 0c | -2.76c, 11 wins | -2.15c, 14 wins |
| 3c | -2.82c, 10 | -2.21c, 13 |
| 4c | -2.66c, 10 | -2.05c, 13 |
| 5c | -2.78c, 9 | -2.24c, 11 |
| 6c | -2.81c, 9 | -2.27c, 11 |
Cheap end only (entry 5-15c, n=12 rounds, half-stop off): 0c -4.98c · 3c -5.13c · 4c -4.72c ·
5c -5.19c · 6c -5.19c.
Paired, floor 5c vs no floor: **-0.08c/trade, only 5/31 paths differ, p=0.500**.
**Null result, stated plainly: a minimum giveback floor in cents does not improve realised P&L.**
It is flat across N=3-6 and slightly negative. Mechanism for the null: a fixed cent floor widens
the exit on small gains but does nothing on the large ones, and the large ones are where the money is.

# THE ASYMMETRY — the one change the data points at
Live rule on these 31 rounds: mean win +12.5c, mean loss -11.2c, 11 wins.
Widening the giveback FRACTION (and dropping the half-stop) changes the shape, not the hit rate:
| giveback, no half-stop | mean/trade | wins | mean win | mean loss |
|---|---|---|---|---|
| 20% | -1.28c | 15 | | |
| 40% (current) | -2.15c | 14 | | -14.1c |
| **60%** | **+0.25c** | 10 | **+30.4c** | -14.1c |
| 80% | -0.72c | 7 | | |
| 100% (hold once armed) | -1.43c | 5 | | |
| no trail, hold to min 14 | -3.99c | 7 | | |

**Caveat, stated before the conclusion:** +0.25c is the only positive cell in a 5-point sweep, the
sweep is NON-MONOTONIC (20% beats 40%), and paired against the live rule it is +3.01c/trade with
**p=0.166** (26/31 paths differ); against 40% alone p=0.790. **It is not statistically established.**
Its mean is carried by 4 of 31 rounds (+57.1, +72.8, +68.9, +80.2).

But that is the mechanism, not a coincidence: these tickets are convex — almost all the money is in
4 of 31 paths — and every rule now in the bot (a 50% stop, a 40% giveback computed off a shallow
peak) shaves exactly that tail while leaving the full-premium losses untouched. That is the
asymmetry. **The single change: stop protecting gains and start protecting the tail — remove the
half-stop and widen the giveback to ~60% of peak gain.** On the tape that is -2.76c -> +0.25c per
trade (n=31, p=0.166, not significant). The honest version of the recommendation is: this is the
only direction in the whole study that has both a mechanism and a non-negative number, and 31
rounds cannot confirm it. Another day of tape can.
