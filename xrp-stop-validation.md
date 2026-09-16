# XRP half-stop — adversarial validation
Written 2026-09-16T04:06:52.716573Z. Independent re-implementation (own loader, own simulator); prior report's code not reused.
Tape: /home/user/tab/files/kalshi-bots/tape/KXXRP15M — 25 round files, **24 settled** (prior report had 20).
Fills: entry pays the ASK at the first book line in the entry minute; exits take the BID; Kalshi fee ceil(0.07*P*(1-P)*100)/100 on BOTH legs. Never the mid.
Simulator matches the LIVE rules as read from xrp_bot.py: arm = max(min(1.5x entry, entry+0.45*(1-entry)), entry+0.03); trail gives back 40% of peak gain, floor entry+1c; half-stop only while NOT armed and before HALF_STOP_BY_MIN; flat at minute 14.

## 1. INDEPENDENCE — the 25-of-25 is 9 distinct rounds
- minute-5 cheap tickets (3–30c): **13 of 24 settled rounds** (prior: 11 of 20).
- early halvings (bid <= 50% of entry before minute 8) at minute 5: **5 cases, 5 distinct rounds**.
- expanded (one entry per minute 3–9): 100 observations from 21 rounds; **22 early halvings from 9 distinct rounds**.
- union behind the whole claim: **9 rounds**, contributing 5,5,4,4,3,2,2,1,1 observations.
Recovery to entry after an early halving: **0 of 27** — but the independent unit is 9.
A 0/9 run has a one-sided 95% upper bound near 28% recovery, not the ~11% that 0/25 implies.
The expanded-sample p=0.0002 is inflated: the paired test counts the same path up to 7 times.

## 2. THE GRID (mean c/trade; in brackets: paired delta vs no stop, and paths changed)
MINUTE-5 SAMPLE, n=13, no stop = -7.67c
| cutoff | 40% | 50% | 60% |
|---|---|---|---|
| m6 | -7.67 (+0.00, 0) | -6.75 (+0.92, 1) | -6.60 (+1.07, 1) |
| m7 | -7.41 (+0.26, 1) | -5.28 (+2.38, 4) | -4.46 (+3.21, 5) |
| **m8 (SHIPPED)** | -5.92 (+1.75, 3) | **-4.96 (+2.71, 5)** | -6.54 (+1.13, 6) |
| m9 | -5.02 (+2.65, 6) | -4.14 (+3.53, 7) | -5.35 (+2.32, 8) |
| m10 | -4.67 (+3.00, 7) | -4.14 (+3.53, 7) | -5.35 (+2.32, 8) |
| none | -4.67 (+3.00, 7) | -4.14 (+3.53, 7) | -5.35 (+2.32, 8) |

EXPANDED (NOT independent, 21 rounds), n=100, no stop = -3.02c
| cutoff | 40% | 50% | 60% |
|---|---|---|---|
| m6 | -2.81 | -2.29 | -1.62 |
| m7 | -2.47 | -1.65 | -0.90 |
| m8 | -1.93 | -1.28 | -0.92 |
| m9 | -1.39 | -4.03 | -3.60 |
| m10 | -4.80 | -4.80 | -4.15 |
| none | -5.12 | -4.32 | -3.68 |

Paired Wilcoxon on the minute-5 sample vs no stop: m8/50% **p=0.062** (not 0.045); best cell m9/50% and m10/50% p=0.016; m9/40% and m10/40% p=0.016. 18 cells tested — nothing survives a Bonferroni correction (0.016*18 = 0.29).

READING: every cell on the real minute-5 sample is >= 0. The benefit is **not a spike at minute 8**; it rises monotonically with a LATER cutoff and is largest with NO cutoff at all. The two samples disagree about the cutoff, and only the non-independent one supports minute 8.
Why the disagreement is mechanical, not mysterious: the live stop is gated on `not trail_armed`, and the arm is now 1.5x. An armed winner can never be cut, so the "always-on stop cuts winners" effect that motivated the cutoff is already handled by the arm gate. The cutoff is protecting against a risk the arm gate already covers.

## 3. WORST CASE — no runner ever came back
Across all 27 early-halving observations, the highest multiple ever reached AFTER the halving is **0.71x entry**. Not one got back to entry, let alone ran.
So in this tape the cost of the rule is zero. The day it happens: the trail's own winners in this tape banked +4.0c, +1.0c, +0.5c, +5.0c, +11.0c (5 wins of 13). A halved ticket that then ran to the tape's largest observed peak multiple (3.64x) and exited on the trail would be worth roughly +13c on a 9c entry; the stop would instead book about -5c. So one such event costs ~18c, which the +2.71c/trade would repay in ~7 trades. That is a modelled cost, not an observed one — no such path exists in the tape.

## 4. INTERACTION WITH THE 1.5x ARM
Checked every path in the minute-5 sample at m8/50%, m9/50% and no-cutoff: **zero paths where the stop made the result worse.** The stop never fired on a path that would have armed and won. The arm gate is doing that work.

## 5. ETH OUT-OF-SAMPLE — NO TEST EXISTS
tape/KXETH15M/ holds 4 files, 3 settled, and **0 minute-5 cheap tickets**. The expanded construction yields 9 overlapping observations from 3 rounds with **1 early halving (it did not recover)**. At m8/50% the rule changes **0 of 9 paths**.
This is neither a pass nor a failure. It is too little data to be evidence either way, and it is reported as such rather than dressed as an out-of-sample pass.

## 6. VERDICT ON JOB 1
The rule is **not overfitted in its direction** — every cell of an 18-cell grid helps or is neutral on the independent sample, and the mechanism (an early-halved cheap ticket is dead) holds 27/27 with a worst-ever recovery of 0.71x.
The rule **is overfitted in its cutoff**. Minute 8 is the best cell only on the overlapping sample. On the 13 real decision points, later cutoffs are strictly better and no cutoff is best. The evidence for the number 8 specifically is 9 rounds.
Nothing here says pull the stop. It says the cutoff is a fitted parameter resting on 9 rounds, and that the honest statement of the finding is "0 of 27 observations from 9 rounds", not "25 of 25".
