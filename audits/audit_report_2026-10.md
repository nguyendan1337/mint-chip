# Monthly audit — October 2026 (Mint (Chip))

**Audit month:** 2026-10. Gate verdict: PROCEED (2026-10-30 is the last trading day of October).
**Coverage:** the full ledger history, which is also exactly one month: first pick events 2026-10-02,
last data 2026-10-30. This is the first audit, so there is no prior month to compare against.

## Method — read this before the numbers

- **Unit of analysis: pick spells, not pick events.** The ledger logs a "picked" event for a ticker
  on every day it is picked (414 events). Counting those would count one month-long pick twenty times.
  A spell = consecutive trading days a ticker is picked (weekend runs map to the next trading day);
  entry = the spell's first day, with that day's EV/dep/cont/conf/character. Result: **121 pick spells
  across 78 tickers** (110 closed before Oct 30, 11 still open at Oct 30), and **394 rejected spells
  across 247 tickers** from 1,081 reject events, grouped the same way.
- **Prices:** yfinance Adj Close. Forward return at horizon h = close(h trading days after entry) /
  close(entry) − 1. Entry on a weekend run uses the next trading day's close as the base.
- **Horizon warning — the binding limit of this audit.** No spell has a full 21-trading-day (1-month)
  window by Oct 30 (the earliest picks, Oct 2, reach only 20 trading days). Available horizons:
  1d for 119 spells, 5d for 99, 10d for 67, 21d for **0**. The main yardstick below is therefore
  "to-Oct-30" (entry close → Oct 30 close), which mixes window lengths (1–20 trading days) and
  mixes market regimes. Every 1-year EV prediction below is being judged on at most one month of
  realized data, in a month when the benchmark itself fell (VGT −6.9% Sep 30 → Oct 30; VOO −0.9%).
  Treat every finding as provisional until it survives a second regime.
- **Benchmark-relative** = spell return minus the same-window return of VGT (stocks) or VOO (ETFs).
- **MAE** = worst close-to-close dip after entry within the window (to Oct 30 unless stated).
- Ledgers used: `thesis_ledger.jsonl` (6,061 lines) and `rejected_ledger.jsonl` (1,081 lines) in
  `~/workspace/stock-screener/`; the copies in `~/workspace/mint-chip-pages/ledgers/` are byte-identical
  (same md5), and no yearly archive files exist yet, so there was nothing to fetch from releases.
  Daily `screener_results_*_VGT_llm.csv` files supplied stability grades, earnings proximity, and
  daily EVs for the persistence analysis.

## Headline numbers

| | Picks (121 spells) | Rejected control (394 spells) |
|---|---|---|
| Mean return, entry → Oct 30 | −1.9% | −0.9% |
| Median return, entry → Oct 30 | −0.5% | 0.0% |
| Mean benchmark-relative, entry → Oct 30 | +2.3% | +2.7% |
| % negative, entry → Oct 30 | 55% | 47% |
| % with return ≤ −8% | 18% | 11% |
| % with return ≤ −15% | 7% | 6% |
| Mean max adverse excursion (MAE) | −4.8% | −3.4% |
| % with MAE ≤ −15% | 8% | 7% |

The honest headline: **in October, picks beat their benchmark (+2.3% relative) but did not beat the
rejected control group (+2.7% relative, −0.9% absolute vs −1.9%).** October was a momentum-correction
month (VGT −6.9%), and the system's picks — by construction last year's winners — fell with it.
Nothing here says the system is broken; it says one month, in one regime, cannot separate skill from
the regime. The sections below look for the discriminating signals inside that month, and three of
them are strong enough to act on as proposals: valuation (negative implied upside), earnings
proximity, and the stability gate's two catches.

## 1. Calibration — does predicted EV rank realized returns?

Predicted EV is a 1-year expected value; realized windows here are ≤20 trading days, so this tests
ranking, not level. Mean predicted EV vs mean realized (entry → Oct 30), by EV bucket:

| Predicted EV bucket | n | Mean predicted | Mean 10d realized | Mean to-Oct-30 | Median to-Oct-30 | Mean bench-relative | % positive |
|---|---|---|---|---|---|---|---|
| 3–5% | 31 | 3.9% | −2.5% (n=14) | −1.5% | −0.5% | +0.3% | 45% |
| 5–8% | 37 | 6.4% | −0.1% (n=19) | −2.0% | −1.5% | +2.3% | 38% |
| 8–12% | 46 | 9.8% | −0.7% (n=29) | −1.5% | +0.2% | +3.9% | 52% |
| 12%+ | 7 | 13.4% | −6.2% (n=5) | −6.3% | −4.6% | +0.5% | 14% |

**Verdict: ranking is weak and non-monotone.** The 8–12% bucket is the best on benchmark-relative
return (+3.9%) and hit rate (52%), but the top bucket (12%+, n=7) is the worst on every measure —
it contains DELL (12.2%) and AAMI (15.6%, 14.9%) and, on 10-day windows, the same pattern. Two readings, both plausible: (a) the
highest EVs are the most extended momentum names, and October punished extension; (b) n=7 is too
small to judge — the bucket is AAMI (×2 spells), VCTR, XLK, WST, DELL, HALO, and DELL's −26.6%
alone drags its mean by ~3 points. The level gap is expected and not a finding: a +10% 1-year EV cannot show up in
20 trading days. **No proposal from this section alone** — but it corroborates the valuation
finding in §8: the names the market already prices as expensive (negative implied upside) are
concentrated in the top of the EV distribution and dragged it down.

## 2. Short-horizon downside — the mandate's core question

"Not go negative after buying," measured on picks (rejected control in parentheses):

| Horizon | n (picks) | Mean | Median | % negative | % ≤ −8% |
|---|---|---|---|---|---|
| 1 trading day | 119 | −0.2% | +0.2% | 45% (40%) | 2% (1%) |
| 5 trading days | 99 | −0.5% | −0.0% | 51% (42%) | 4% (2%) |
| 10 trading days | 67 | −1.3% | −1.3% | 58% (56%) | 12% (11%) |
| Entry → Oct 30 | 121 | −1.9% | −0.5% | 55% (47%) | 18% (11%) |

Stocks vs ETFs (to Oct 30): stocks (n=70) mean −1.3%, 47% negative, MAE −5.4%, 12% with MAE ≤ −15%;
ETFs (n=51) mean −2.9%, 65% negative, MAE −3.9%, only 2% with MAE ≤ −15%. ETFs went negative more
often (they are the tech-heavy leg in a tech-correction month) but blew up less; stocks produced
nearly all the deep losses (DELL, FFIV, TWLO, FTNT, AVT — see §12).

**Verdict: the mandate held at short horizons and frayed by day 10.** At 1 day, picks were negative
45% of the time — close to a coin flip, and only slightly worse than rejects (40%). By 10 days the
negative rate reached 58% and 12% of picks were down 8%+. That deterioration is mostly the October
regime (two sector selloffs: Oct 14 AI-capex credit fears, Oct 22 semiconductor −6.7%), not a
pick-specific failure — rejects deteriorated in parallel (56% negative at 10d). The mandate-relevant
excess is the tail: picks' MAE ≤ −15% rate (8%) vs rejects' (7%) is not an excess at all; the excess
is concentrated in nine identifiable spells (§12), five of which share one profile (§10).

## 3. Medium-term — benchmark-relative, hit rate, tails

No 21-day or 63-day windows exist yet (see Method). On the longest available common window
(10 trading days, n=67 picks): mean −1.3%, median −1.3%, 42% positive. Benchmark-relative to Oct 30:
picks +2.3% mean / +2.3% median; rejects +2.7% / +2.3%. Hit rate (positive to Oct 30): picks 45%,
rejects 53%. Downside tail: picks' worst decile averages roughly −15% to −27% (the §12 names);
rejects' worst decile is similar. **No medium-term edge is detectable in one month, and none is
claimed.** The November audit (with 21-day and some 42-day windows) is the first that can speak
to the 1-month horizon properly.

## 4. Probability calibration — do cont/conf mean what they say?

Continuation (cont) is the closest thing to a predicted probability of a positive outcome.
Empirical % positive (entry → Oct 30), by cont bucket:

| cont bucket | n | % positive to Oct 30 | % positive at 10d | SD of outcomes |
|---|---|---|---|---|
| 0.40–0.55 | 5 | 0% | 0% | 6.5% |
| 0.55–0.65 | 56 | 32% | 37% | 8.3% |
| 0.65–0.75 | 60 | 58% | 46% | 8.5% |
| 0.75+ | 0 | — | — | — |

**Cont discriminates in the right direction** (32% → 58% positive across the two populated buckets),
though levels are depressed by the regime (a 0.65–0.75 continuation score "should" beat 58% over a
full year, not 20 days in a correction). No cont bucket above 0.75 exists in October picks — the
researcher is not handing out extreme continuation scores, which is honest.

Confidence (conf) does **not** behave as advertised:

| conf bucket | n | % positive to Oct 30 | % positive at 10d | SD of outcomes |
|---|---|---|---|---|
| 0.40–0.60 | 2 | 50% | 100% | 7.9% |
| 0.60–0.70 | 46 | 35% | 30% | 8.1% |
| 0.70–0.80 | 66 | 52% | 53% | 8.4% |
| 0.80+ | 7 | 29% | 0% | 9.6% |

High-confidence picks (0.80+, n=7) had the *widest* outcome distribution (SD 9.6%) and the worst
hit rate at 10d (0%) — the opposite of "tighter outcomes when confident." The 0.80+ bucket is small
and contains DELL (conf 0.80), so this is suggestive, not proven. **Flagged for the November audit,
no proposal yet:** if high-conf picks keep showing wider tails, conf is measuring researcher
enthusiasm, not outcome tightness, and the EV formula's conf multiplier is amplifying the wrong names.

## 5. Attribution — dep, character, and what actually priced risk

**Event dependence (dep) — the penalty works directionally, and the tail is where it bites:**

| dep bucket | n | Mean to Oct 30 | % negative | Mean MAE | % with MAE ≤ −15% |
|---|---|---|---|---|---|
| < 0.30 | 83 | −0.9% | 52% | −3.6% | 2% |
| 0.30–0.50 | 37 | −4.4% | 62% | −7.8% | 19% |
| 0.50–0.70 | 1 | +3.4% | 0% | +3.4% | 0% |

Low-dep picks almost never blew up (2% MAE ≤ −15%); mid-dep picks blew up nearly one time in five
(19%). The dep penalty's direction is validated. But note the level: even low-dep picks averaged
−0.9% — dep prices the tail, not the regime. No dep ≥ 0.70 pick exists (the veto works as a gate);
the single 0.50–0.70 spell is not evidence of anything.

**Character multiplier — the one component that looks backwards:**

| character bucket | n | Mean to Oct 30 | Median | % negative |
|---|---|---|---|---|
| < 0 | 16 | −2.3% | −0.2% | 50% |
| 0–0.25 | 85 | −1.2% | 0.0% | 49% |
| ≥ 0.25 | 20 | −4.8% | −3.4% | 80% |

High-character picks (the formula's quality/entry-timing/structure premium, up to +50% EV via the
1 + 0.25×character term) did the worst: −4.8% mean, 80% negative. Character here is dominated by
quality/entry-timing scores of extended winners — in a correction month, the "best" charts fell
hardest. This is one regime, and character is deliberately not momentum — but the sign is wrong
enough to question whether the character term should *raise* EV at all for extended names. Proposal 4
(§15) asks the November audit to test character net of the momentum-extension effect before any
change; no change is proposed on this month alone.

## 6. Fuse cross-check analysis

Pick spells carry dual_confirm/sue/beat_streak/implied_upside only for spells starting on/after
~Oct 5 (74 of 121 spells predate the instrumentation or lack the fields). All Fuse results below
are therefore partial-coverage and suggestive only.

**(a) dual_confirm — no edge detected, small n.** dual=True: n=7, mean −7.5%, median −6.6%,
71% negative, mean MAE −11.0%. dual=False: n=40, mean −1.6%, median 0.0%, 50% negative,
MAE −4.7%. The dual-confirmed picks did *worse* — but n=7, and the bucket contains FFIV (Oct 27,
dual=True, −26.3%) and NTAP (Oct 13, dual=True, −16.7%), both October-correction casualties.
Fuse's fundamental-momentum screen did not protect these names from the sector drawdown, and with
seven names it cannot be judged. **No researcher-input change proposed.**

**(b) beat_streak — longer streaks did worse, against the hypothesis.** Streak 0–2: n=16,
+1.2% mean, 62% positive. Streak 3–5: n=10, −5.3%, 30% positive. Streak 6+: n=22, −3.7%,
36% positive. The serial-beater premium did not survive October: the longest streaks belong to
the most extended names (FTNT streak 8, FFIV streak 8), which corrected hardest. This cuts against
Fuse's streak factor (0.15) as a positive signal *in a correction regime* — it may still be a
positive signal over full years. **Research question carried forward, not a tuning knob.**

**(c) implied_upside ("priced in") — the strongest single signal in this audit.**

| implied_upside | n | Mean to Oct 30 | Median | % negative |
|---|---|---|---|---|
| Negative (analysts see it as fully valued) | 15 | −8.8% | −2.3% | 80% |
| Zero or positive | 31 | +1.3% | +1.2% | 39% |

A 10-point mean gap and a doubled negative rate. The negative-implied bucket holds FTNT's later
spells, FFIV, NTAP (×3 spells), TWLO — i.e. it overlaps the blowup list, which is exactly why it
matters: the market's own valuation verdict (mean analyst target below the price) predicted the
correction better than any Mint input. Caveat: implied_upside is only recorded for 46 of 121 spells,
and analyst targets move with price, so part of this is mechanical (falling prices raise implied
upside after the fact — but these are *entry-time* values, so the direction is genuine). This is
the evidence base for Proposal 1 (§15, valuation dampener) and the combination test in §10.

## 7. Fuse outcome review — the experiment's scorecard

Fuse ledger entries (all dated 2026-10-05; returns to Oct 30, 25 calendar days):

| Ticker | Role | To Oct 30 | MAE | Tripwire hit? | Note |
|---|---|---|---|---|---|
| SNX | pick | −14.5% | −14.5% | **Yes** (invalidation −11.2% breached) | Flagship fresh-ignition case; kept falling past the tripwire |
| EXPD | pick | +2.4% | −1.7% | No | |
| AMRX | pick | +5.7% | −7.1% | No | |
| NTCT | honorable mention | −6.9% | −7.6% | No (grazed: tripwire −7.2%) | |
| LGND | honorable mention | +5.2% | −4.4% | No | |
| **Picks mean** | | **−2.1%** | | | |
| DDS | control (rejected: one-time items) | −7.8% | −7.8% | — | Rejection right |
| RGA | control (rejected: lumpy earnings) | +5.5% | 0.0% | — | Rejection wrong |
| HRMY | control (rejected: event risk) | −2.9% | −9.7% | — | Rejection roughly right |
| MSGS | control (rejected: event risk) | +2.9% | −4.0% | — | Rejection wrong-ish |
| AVT | control | −16.9% | −17.1% | — | Rejection right — avoided a blowup |
| GOOGL | control | −19.2% | −19.2% | — | Rejection right — avoided the worst control loss |
| SHOO | control | −4.8% | −6.6% | — | Rejection right |
| WSM | control | +6.2% | +0.1% | — | Rejection wrong |
| **Control mean** | | **−4.6%** | | | |

**Blunt scorecard: picks beat the control group (−2.1% vs −4.6%), but the win is not a Fuse win.**
The gap is driven by the control group's disasters (AVT, GOOGL) that the researcher rejected, not by
pick strength — the picks themselves lost money on average, and the flagship pick, SNX, is the
worst pick in the ledger: its −11.2% invalidation tripwire was breached and it kept falling to
−14.5%, so the tripwire marked a genuine exit, not a noise bottom (at least through Oct 30).
Researcher rejections scored 5 right / 3 wrong (DDS, HRMY, AVT, GOOGL, SHOO right; RGA, MSGS, WSM
wrong). **Verdict: too early to tell, and nothing here justifies a third page.** The experiment
stays a private research feed. What the next audit must collect: more Fuse entries (only 13 exist,
all from one day), and tripwire outcomes tracked past the breach date to distinguish "exit saved
money" from "noise bottom" — SNX so far supports the former.

## 8. Pool-coverage review — the gate's false negatives

Documented case SNX (2026-10-05): Piotroski 7, 6 straight beats, +21% surprise, Q4 guide 13.5%
above consensus, event dep ~0.2, clears vol/drawdown gates — hidden from Mint's research pool by
(1) momentum-bucket z-scores against the year's explosive winners grading its +78% trailing run as
mediocre, (2) the quality bucket penalizing thin distributor margins, (3) blowoff avoidance dinging
the post-earnings pop. The outcome (§7) complicates the moral: SNX as a Fuse pick fell −14.5% to
Oct 30 and breached its tripwire. The gate hid a name that then lost money — but the gate's
*reasoning* (thin margins = low quality; post-earnings pop = blowoff risk) is precisely what the
researcher should have been allowed to judge, and the October correction punished exactly the
extended-momentum profile the gate was grading. SNX does not settle the question either way.

**False-negative scan (outcome-based):** the rejected spells that gained most to Oct 30 are all
EV-floor rejects, not pool exclusions: EXEL +18.8% (MAE −0.1% — a clean winner the floor cut),
SLDE +17.5%, PR +17.0%, XOM +14.2%, CVX +13.9%, BIIB +13.4%. These are energy, biotech, and
specialty names the EV bar judged below +3% at entry — a reminder that the floor trades winners
for downside protection by design (only 4% of floor rejects gained >10%; see §13). No second
documented pool miss beyond SNX surfaced in the logs. **Pattern judgment: one case is not a
pattern.** The favored direction from the October evidence still errs toward inclusion (absolute
momentum bars instead of z-vs-winners; let the researcher plus EV discriminate), but the SNX
outcome counsels against rushing it — the proposal stays a proposal (§15, Proposal 5), and the
November audit should look for a second case before Dan is asked to decide.

## 9. Pick-robustness review

**(a) Whipsaw rate — the list churns hard.** 94 of 121 spells lasted ≤3 trading days; excluding
spells still open at Oct 30, **88 of 110 closed spells (80%) lasted ≤3 trading days**. There were
43 re-entries (a ticker exiting picks and later returning): 28 with gaps ≤3 trading days
(whipsaw-like) and 15 with longer gaps (genuine re-entries after absence, e.g. AVT returning
Oct 18 after its Oct 2 spell broke). For the 28 whipsaw-like gaps, the exit→re-entry price move
averaged +0.4%: exiting cost money in 16 of 28 cases (total gains missed +34.1pp across cases)
and saved money in 12 (total losses dodged −22.9pp). Net across whipsaw gaps, exiting slightly
*hurt* (+0.4% average move missed). The deeper finding is structural: **exiting picks usually
means exiting the research bundle too** — no interim daily EV exists for any of the 43 re-entries
(the ticker vanishes from the CSVs while out of picks), so a persistence band cannot be replayed
from daily EVs. The pool cutoff, not just the pick rule, is a whipsaw mechanism: a name that dips
below the top-100-by-base_score line loses its assessment entirely, and returns only when its
score recovers. Research catch-up vs genuine news: DELL is the documented catch-up case (IDC's
Oct 8 PC-shipment report was public before the Oct 12 pick affirmation but absent from the
assessment); FTNT's Oct 21 removal is genuine news arrival (Arete downgrade that day). Most
other exits have no logged trigger — the daily EV wobble around the +3% floor and the pool cutoff
is the default explanation, and it is unlogged. **Data gap to fix:** log an exit reason per spell
(EV fell below floor / gate trip / cap / pool exit), without which the whipsaw rate cannot be
attributed.

**(b) Stability gate — the two catches are the two disasters.** Stability grades at spell start
(from the daily CSVs): A/B spells n=108, mean −1.3%, 54% negative, mean MAE −4.0%, 5% with
MAE ≤ −15%. C/D spells: **n=2 — DELL (Oct 7, EV 12.2%, −26.6%) and TWLO (Oct 15, EV 8.8%, −25.0%),
both with MAE ≤ −25% and 100% negative.** Both predate the gate (shipped Oct 16); post-gate
C/D pick spells: **0** (the Oct 16 TWLO correction — republished the same day with SM Energy in
its place — is the gate working as designed, and TWLO then fell a further ~25% from its pick
price). Pre-gate spells overall: mean MAE −6.4%, 10% MAE ≤ −15%; post-gate: mean MAE −3.1%,
5% MAE ≤ −15% (confounded by regime — the worst selloffs were mid-October — but the direction
is consistent with the gate binding on exactly the fragile names). **The gate is validated as far
as n=2 can validate anything, and no change to it is proposed.** The November audit's replay
(gate over historical picks, barred vs survivors at 1/5/21d + MAE) should now be run on this
month's completed data: on October's own evidence, barring DELL and TWLO would have removed the
two worst spells of the month.

**(c) Valuation — see §6(c).** Negative implied_upside picks: −8.8% mean, 80% negative (n=15)
vs +1.3% / 39% for non-negative (n=31). The effect is consistent across the only horizons
available (5d and to-Oct-30 both show the gap) and survives the sample-size caveat in the sense
that it is the largest gap of any single input tested. Proposal 1 (§15).

**(d) Earnings-penalty cliff — confirmed in the bundle data, then removed.** The −0.25 earn_soon
dock (earnings within 14 days) was deleted Oct 18 in favor of researcher-owned earnings risk.
Its fingerprint is visible in AAMI: in the bundle Oct 13 (base_score 0.308, 15 days to earnings,
EV 12.1%, pick), **absent from the bundle entirely Oct 14–16** (as earnings crossed inside
14 days and the dock pushed it below the top-100 cutoff), then back Oct 19 after the dock's
removal (base_score 0.380, 9 days to earnings, EV 14.9%, pick). The pick spell mirrors it:
Oct 5–13, gap, Oct 19–30. The exit→re-entry move was +1.8% — the whipsaw cost a small gain, and
the case was intact throughout (the watchlist rated AAMI BUY +12.4% on the same carried assessment
while Mint showed nothing). Earnings-proximate picks overall (earnings ≤14 days at spell start,
n=20): mean −4.6% to Oct 30, **75% negative**, mean 5d return −4.2% with 75% negative at 5d —
vs earnings >14 days (n=35): +0.4% mean, 37% negative. **The dock was protecting against a real
risk (earnings-proximate picks went negative three times as often), but it did so by deleting the
name instead of pricing the risk** — and the deletion itself was the whipsaw Dan objected to.
The question for November is whether researcher-owned earnings risk (post-Oct-18) prices it
better: early evidence says not yet — see §10(b).

## 10. Earnings-whipsaw review

**(a) Sweep trigger discipline — the hold log has exactly two entries, and they split.**

| Date | Ticker | Trigger | Fundamental or sentiment? | Outcome judgment |
|---|---|---|---|---|
| 2026-10-13 | DELL | IDC Oct 8 PC shipments −25% y/y (worse than market −20%) + Morningstar 2-star "overvalued" cut Oct 12 | Fundamental (market-share deterioration) + valuation opinion | **Justified.** The re-score demoted DELL Oct 13; the Oct 14 Barclays downgrade and −19% break (Oct 15) followed. The sweep caught a real deterioration — though the IDC report was public Oct 8 and *should* have been in the Oct 12 assessment (research catch-up failure, §9a). |
| 2026-10-21 | FTNT | Arete Research Buy→Sell (target *raised* $104→$117, still ~31.5% below close; Mizuho/Wells Fargo already underweight; consensus Hold) | Sentiment/valuation opinion alone — the re-score rationale concedes "analyst sentiment, not fundamentals" | **False positive as a re-score trigger.** It removed a pick that Dan holds, seven days before earnings, on an opinion that raised its own target. The subsequent −22% AH fall does not retroactively justify it: the fall was driven by the print's guidance mix (§11), not by the Arete call, and a sentiment-only trigger cannot claim credit for a fundamentals-driven move. |

The DELL counter-case (a downgrade that "looked like just an analyst call" — Barclays Oct 14 —
preceded real deterioration) does not save the FTNT trigger: DELL's demotion rested on IDC's
fundamental shipment data plus the Morningstar cut, while FTNT's rested on one firm's rating
change that came with a higher target. **Proposal 2 (§15): narrow the sweep so a downgrade alone
does not force a re-score absent fundamental news** (guidance cuts, regulatory action, accounting
issues, lost customers, shipment/order data) — symmetric with the good-news side, which already
excludes analyst chatter. The DELL case survives the narrowing (IDC data is fundamental); the
FTNT case does not.

**(b) Earnings-dep discipline — proximity is not moving dep, and outcomes say it should be
flagged, not priced into dep.** Post-Oct-18 (researcher-owned earnings risk), spells with earnings
≤14 days at entry (n=18) carry mean dep 0.27, vs 0.29 for earnings >14 days (n=7) — **no dep
premium for earnings proximity at all**. Pre-Oct-18 the pattern was the same (0.30 vs 0.22, n=2
vs 28 — too small to read). Against the FTNT re-score: dep 0.20→0.55 seven days pre-print was a
calendar-proximity raise on already-known information (the Oct 28 date was known at the Oct 21
assessment; the Arete call added no earnings information). The print then *did* justify a higher
dep — but only after the fact, via the guidance mix. **Judgment: the re-score's dep raise was
unjustified at the time and is not a model for earnings risk.** The discipline rule proposed
(§15, Proposal 3) is: a scheduled earnings date known at assessment time cannot by itself raise
dep; earnings move scores after the print, via news triggers, on actual information — and
proximity itself is handled by the entry-timing flag (§15 Proposal 3), not by dep.

**(c) Pick persistence — simulated from the only observable: exit→re-entry moves.** A persistence
band (entry bar +3% EV; exit only below 0% EV or on a hard trip: dep ≥ 0.7 veto, stability below
B, broken thesis) cannot be replayed from daily EVs because out-of-pick names leave the bundle
(§9a) — interim EVs do not exist. What can be measured: across all 43 re-entries, holding through
the gap (what the band would have done, if interim EVs had stayed ≥0%) would have gained the
exit→re-entry move in 22 cases and lost it in 21 — mean +0.1% per gap, essentially a wash in
aggregate, with the whipsaw-like subset (gap ≤3d, n=28) slightly favoring holding (+0.4% mean)
and the longer-absence subset (n=15) slightly favoring exiting (−0.4% mean). The asymmetry that
matters is not the mean but the tail: the band would have forced holding through DELL-like
continued falls *only if* DELL's interim EV had stayed ≥0% — DELL's case is protected by the
hard-trip clause (broken thesis Oct 15), so the band would have exited it anyway. **Verdict:
the band is roughly neutral on October's data and protects against the FTNT/AAMI-type whipsaw
without forcing DELL-type holds, provided the hard-trip clause is real.** Propose it (§15,
Proposal 3) with the explicit caveat that the replay is indirect — the November audit should
replay it properly once exit reasons are logged (§9a data gap).

## 11. FTNT post-buy failure autopsy + prevention research

Dan holds FTNT because it was a Mint pick and reports it down ~−24% from his buy price. This is
treated here as a first-class mandate failure, separate from the list-membership question in §10.
All figures below use ledger/yfinance closes; Dan's stated ~−24% matches the ledger's −23.1%
(Oct 29 check vs the Oct 2 pick price $180.95) and this audit's −24.8% (Oct 2 close → Oct 30 close).
No brokerage cost basis is invented.

### (1) Timeline autopsy

| Date | Event | What the system knew |
|---|---|---|
| Oct 2 | Picked @ $180.95, EV 11.6%, dep 0.25, cont 0.65, conf 0.70. Rationale: "at all-time highs on CANSLIM-type momentum, but the driver is durable cybersecurity demand, not a headline. Trend + quality, not event." | A momentum pick with an explicit durability claim. Earnings Oct 28 known from the start (later assessments cite it). |
| Oct 3 | Watch: FortiMail zero-day CVE-2026-104286 (CVSS 9.8) added to CISA KEV Oct 1; then back to intact. | Security flaw in its own product — flagged by news re-verification, then judged non-thesis-breaking. |
| Oct 9–12 | Watch (FortiMail flaw persists), then intact. Price +6.6% vs pick at Oct 12 check. | |
| Oct 13 | Fresh assessment: dep 0.30, EV 9.6%. "Concrete new development… negative: Fortinet disclosed Oct 1…" (disclosure detail in log). | |
| Oct 14 | Tech selloff on AI-capex credit fears; FTNT dips to −13% vs Oct 2 pick at worst. | Sector shock, no company news. |
| Oct 16–20 | Watch (dip + Morgan Stanley downgrade to Underweight Oct 19: firewall-refresh cycle may be smaller). Spell 1 ends Oct 15; spell 2 starts Oct 18/19 @ ~$169–171, EV 9.5%, dep 0.30. | The Oct 16 exit / Oct 19 re-entry is itself a 1-trading-day whipsaw (§9a). |
| Oct 21 | First Phase B pass picks FTNT +9.6% EV (dep 0.20, cont 0.62, conf 0.60, Q3 earnings Oct 28 known). Pre-pick sweep re-scores on Arete Buy→Sell (target raised $104→$117, ~31.5% below the $170.93 close; Mizuho/Wells Fargo already underweight; consensus Hold $161.75): dep →0.55, cont →0.48, conf →0.50, EV →+0.9%, below floor → removed. NTAP fills the slot. Pick-correction event logged. | The whipsaw removal. Rationale concedes "analyst sentiment, not fundamentals" and that Q2 stands (revenue +25.6% to $2.05B, billings +33% to $2.37B, FY guidance raised). |
| Oct 22 | Sector-wide cybersecurity selloff continues (SOX −6.7% that day). | |
| Oct 28 after close | Q3 print: EPS $0.91 vs $0.86 expected, revenue +20% to $2.08B, billings +29%, billings guidance raised for the third time this year — **yet −22% after hours**, after a 132% run in 2026. Watch held: "Thursday's regular session will show whether the drop holds." | Headline beat, market rejection. The Oct 28 check reason names the mechanism: "the market wanted more than a small beat." |
| Oct 29 | **Broken.** Fell ~20% in the session, −29% max dip vs Oct 2 pick. Broken reason: product growth slowed 52%→37%, FY service-revenue guidance trimmed ~$40M, Bank of America downgraded to Neutral. "The growth-cooling story behind the original pick thesis is now in doubt." | Thesis break confirmed one session after the gap — the daily cadence working as designed. |

**What was foreseeable at pick time:** (i) earnings proximity — the Oct 28 print was known at every
assessment from Oct 2 on; (ii) valuation — a 132% run in 2026 with negative implied upside
(−13.1% on Oct 13, narrowing to −0.7% by Oct 18 only because the price had already fallen);
(iii) segment dependence — the thesis rested on product/services growth acceleration, and the
eventual break was exactly product deceleration (52%→37%) plus a services-guide trim. **What
arrived only with the print:** the magnitude and mix of the guide trim — no pre-print signal
quantified a $40M services trim. **What monitoring caught late:** nothing about the gap itself
(no system prevents a −22% after-hours gap), but the watch framework never converted "fully
valued + earnings in 7 days" into protection before the print — the Oct 21 sweep removal did that
accidentally, on the wrong trigger (§10a), and Dan's position was unaffected by the list removal
either way. The system watched FTNT more closely than any other name (10+ watch flags) and still
had no pre-print safeguard. That is the gap the proposals below close.

### (2) Cohort research — picks sharing FTNT's pre-failure profile

Profile flags (spell-start values): earnings ≤14 days out; trailing 6m run (skip-1m) ≥50%;
negative implied upside; dep ≥0.40. Spells matching ≥2 flags (n=20) vs the rest (n=101):

| | n | Mean to Oct 30 | % negative | Mean MAE | % MAE ≤ −15% |
|---|---|---|---|---|---|
| Cohort (≥2 flags) | 20 | −6.7% | 75% | −8.3% | 26% |
| Rest | 101 | −1.0% | 50% | −4.1% | 4% |

The cohort contains every blowup: FTNT (Oct 18 spell, 3 flags, −20.7%), FFIV (Oct 23, all 4 flags,
−25.7%; Oct 27, −26.3%), TWLO (Oct 15, 3 flags, −25.0%), NTAP (Oct 13/21/28, 2 flags each,
−16.7%/−8.3%/−10.9%). Comparison autopsies: DELL matches only via C-stability, not this profile
(its failure was fundamental news + research catch-up, §12); AVT's Oct 2 spell matches none of
these flags (its failure was sector drawdown, §12). The cohort signal is strong but confounded:
these flags also select "most extended momentum," and October punished extension generally.
Sample: 20 spells, one month, one regime — **suggestive, not proven** — but the 26%-vs-4% deep-loss
rate is the widest separation of any grouping tested in this audit.

### (3) External evidence — what the literature says about preventing this

- **Guidance quality beats headline EPS.** Post-earnings-announcement drift (Ball & Brown 1968;
  Bernard & Thomas 1989) shows extreme-surprise stocks drift ~3–5% over the following 60 trading
  days — but the drift follows the *surprise-and-guide* combination, not the headline: a beat with
  a lowered guide is a fade regardless of the EPS number (Kunkel Capital's summary of the PEAD
  literature, https://kunkelcapital.com/blog/earnings-drift-ignore-beat). FTNT is the textbook
  case: EPS beat (+5.8% vs consensus) with a services-guide trim → −22%.
- **Earnings-day moves are near coin flips with ~12% magnitude.** A May–Oct 2026 sample of 213
  post-earnings reactions found 56.8% positive / 43.2% negative, with positive reactions averaging
  +12.30% and negative ones −12.04% (Paper Trading Journal,
  https://papertradingjournal.com/2026/10/21/are-stocks-more-likely-to-go-up-or-down-after-earnings/).
  Buying into a print is therefore buying a ±12% lottery ticket on average — the downside-budget
  logic in safeguard (c) below is calibrated to this, not to Mint's −8%/−15% watch/broken bands.
- **Stocks move on expectations, not results, and valuation sensitivity is elevated.** "Even
  strong results can trigger selling if growth slows slightly… A stock trading at 35x earnings
  must justify that premium every quarter" (SHC Wealth Management,
  https://www.shcwealthmanagement.com/blog/why-stocks-fall-after-strong-earnings). FTNT at a
  132% YTD run is the extreme version.
- **Sector risk swamps single-name signals in stress.** On Oct 22 the Philadelphia Semiconductor
  Index fell 6.72% (worst since June) on a supply-chain/financing repricing unrelated to any
  company's results; "momentum positions… treat the tape as an input rather than an output"
  (AI's Market Cap ledger, https://aismarketcap.com/blog/pre-earnings-momentum-small-cap-ai).
  This is the AVT/FTNT mid-October drawdown mechanism and a standing caveat on all single-name
  safeguards: they cannot protect against sector repricing.
- Speculation, separated: no cited source supports a precise "wait N days after earnings" rule,
  options-implied-move budgeting from free data, or analyst-revision momentum as a standalone
  Mint input — those would need new data the project does not currently collect.

### (4) Candidate safeguards — replayed over October's pick history, both sides

| # | Safeguard | Would have prevented | Would have blocked / cost | Net judgment |
|---|---|---|---|---|
| (a) **Entry-timing flag** — a pick with earnings ≤~14 days out and high event sensitivity stays a pick but is flagged "event-risk: print pending," so the buy decision can wait for the print. List membership untouched (resolves the §10 whipsaw tension instead of re-litigating it). | The FTNT-type gap exposure: 20 earnings-proximate spells averaged −4.6% with 75% negative; flagging them costs nothing in list terms and lets a buyer wait out a ±12% lottery ticket. | Nothing in list terms by construction. Cost is opportunity: some flagged names rose through earnings (the 25% of earnings-proximate spells that were positive). | **Rank 1. Recommend.** Pure information, no formula change, no whipsaw side effect. |
| (b) **Guidance-quality checklist for earnings-proximate picks** — segment growth, recurring/services mix, billings/RPO, guidance breadth; dep must reflect dependence on a single segment's acceleration. | Would have raised FTNT's dep *on fundamentals* (product-growth dependence) before the print instead of via the Arete sentiment trigger — and would have flagged FFIV (software-transition growth dependence) the same way. | Adds researcher burden per earnings-proximate pick (~20/month). Risk of dep inflation if applied loosely — needs the §10(b) discipline rule as its guardrail. | **Rank 2. Recommend**, paired with the discipline rule. |
| (c) **Downside-budget test** — typical earnings-day absolute move (~12% from the cited sample; or the ticker's own historical earnings-day moves where computable) vs the pick's downside allowance. Picks whose plausible earnings gap blows the budget need compensating EV or the event-risk flag. | Formalizes (a): FTNT's plausible −12% to −22% gap vs a mandate that treats −8% as a watch trip — the budget fails on its face for a fully-valued name into earnings. | Requires a new ledger field (typical earnings-day move) and a judgment call on the budget threshold; historical earnings-day moves for ~80 tickers are computable from price history but not yet collected. | **Rank 3. Recommend as a flag input** (feeds (a)), not as a veto — the ±12% figure is a cross-sectional average, not a ticker-level budget. |
| (d) **Valuation combination test** — negative implied_upside PLUS big trailing run PLUS near-term earnings as a combined haircut/veto candidate (test the combination, not each factor alone). | The cohort (§11.2): 26% deep-loss rate vs 4% for the rest. The combination selects FTNT, FFIV, TWLO, NTAP — four of the five worst spells. | Would also have caught HALO (Oct 29 spell, 3 flags, −0.2% — no loss) and WST (Oct 18, 2 flags, +7.5% — a winner blocked). As a veto it would have blocked real winners; as a haircut/flag it is affordable. | **Rank 4. Recommend as a haircut/flag, not a veto.** The single-factor valuation dampener (Proposal 1) is the cleaner version; the combination test should be validated on November data before any veto is considered. |
| (e) **Faster post-print re-underwriting** — a guidance-mix deterioration breaks the thesis at the next daily run even on a headline beat. | FTNT was already broken Oct 29 (one session after the print) — this safeguard is **already effectively in place** via the daily thesis check. Its marginal value is formalizing "headline beat + guide trim = break" so a future researcher doesn't hold for the drift. | None observed. | **Rank 5. Recommend as a written rule** (researcher instruction), not a system change. |

**Ranked recommendation: (a) + (b) + (e) now (all researcher-instruction or flag-level, no formula
change); (c) as the data-collection step for November; (d) as a flag only, veto decision deferred
to the November audit with more data.** If the evidence were thinner, the honest answer would be
"too thin" — it is not thin on direction (every grouping points the same way), only on sample size
(one month, one regime), which is why nothing above is a veto or a formula weight.

## 12. Constraint audit

**Sector caps — displaced names roughly broke even against survivors, with one large exception.**
From 170 unique opportunity-cost log lines (165 with prices): displaced-minus-survivor return to
Oct 30 averaged −3.0% per line (−1.7% per unique pair, n=119); the displaced name won in 46% of
unique pairs. On average the caps did not cost return — but the tail is dominated by FFIV as
survivor: on Oct 27, Healthcare-cap displacements LLY (+8.1% EV, +10.6% realized), TGTX (+7.6%,
+8.4%), and LGND (+9.0%, +3.5%) were all displaced by FFIV (+6.4% EV, −26.3% realized), gaps of
+37/+35/+30 points. That is not a cap failure (FFIV passed every gate on Oct 27); it is the
earnings-gap failure of §11 wearing a cap costume. Excluding FFIV-survivor lines, displaced vs
survivor is close to a wash. **Verdict: caps bought diversification at roughly zero realized cost
in October; no cap change proposed.** The ETF category cap (hard 2) cannot be separately measured
from these lines; ETF-overlap rejects are covered in §13.

## 13. Autopsies — every pick spell with MAE ≤ −15%

| Spell | Entry EV / dep / cont / conf | To Oct 30 / MAE | What the system missed (original rationale quoted) |
|---|---|---|---|
| DELL Oct 7–12 | 12.2% / 0.30 / 0.70 / 0.80 | −26.6% / −26.6% | "Dell raised FY2027 revenue guidance to $192B (+69% y/y)… $95B ending backlog." **Missed:** IDC's Oct 8 PC-shipment report (−25% y/y, worse than market −20%) was public before the Oct 12 affirmation but absent from the assessment — research catch-up failure. C-stability (gate now bars it). Demoted Oct 13 on IDC + Morningstar 2-star; Barclays downgrade Oct 14; broken Oct 15 at −18.2% (ledger). |
| FFIV Oct 23; Oct 27 | 5.8% / 0.45 / 0.62 / 0.60; 6.4% / 0.45 / 0.62 / 0.60 | −25.7%; −26.3% | "Q3 was a strong beat… The software transition… is producing durable growth." **Missed:** the thesis rested on growth acceleration, and the Oct 27 print guided slower growth (8–9% vs 10.5% prior year) — a guidance-mix break on a name picked days before earnings with all four §11 cohort flags. Broken Oct 28 at −19.7% (ledger). |
| TWLO Oct 15–16 | 8.8% / 0.35 / 0.60 / 0.75 | −25.0% / −25.0% | "Joined the S&P 500 on Oct 6, drawing sustained index-fund buying… The main check is Q3 results on Oct 29 at a full valuation." **Missed:** the check named was right (earnings at full valuation) but the name was C-stability — the Oct 16 gate correction removed it the same day, and the subsequent −25% is the gate's save, not a system loss. |
| FTNT Oct 2–15; Oct 18–21 | 11.6% / 0.25 / 0.65 / 0.70; 9.5% / 0.30 / 0.60 / 0.75 | −24.8%; −20.7% | See §11 full autopsy. "Trend + quality, not event" — but the return depended on a segment-growth story into a known print at a 132% YTD run. |
| AVT Oct 2–3 | 11.6% / 0.30 / 0.65 / 0.75 | −17.5% / −17.6% | "Record Q4 sales $8.3B (+48%), AI data-center demand pulling through distribution." **Missed nothing company-specific:** broken Oct 16 with "no company-specific bad news — swept up in the tech/AI-debt selloff." Sector drawdown, and the Oct 18 re-pick (EV 7.2%, intact at Oct 30, −2.0% vs that entry) shows the case was intact — the Oct 2 spell's "broken" label is a sector casualty, not a thesis failure. |
| SMH Oct 3 | 4.6% / 0.30 / 0.50 / 0.65 | −17.6% / −17.6% | One-day ETF pick caught in the semiconductor drawdown (SOX −6.7% Oct 22). No thesis break; sector beta, not selection failure. |
| NTAP Oct 13–14 | 11.7% / 0.25 / 0.65 / 0.75 | −16.7% / −16.7% | "Hit a record high… Evercore upgraded to Outperform… Enterprise AI storage demand looks structural." **Missed:** no company-specific break found — Oct 26 watch: 15% max dip, "no company-specific bad news… analysts split on what multiple the stock deserves ($195–$250 targets)." Valuation + sector drawdown; negative implied upside (−11.4%) flagged it in advance. |

## 14. Control group + threshold review

**Did vetoes save us? Yes, on this month.** Veto rejects (n=8 spells): mean −3.1%, median −1.6%,
25% positive, **0% gained >10%**. The dep ≥ 0.7 veto removed names that then lost money. No veto
looks wrong in hindsight. **No veto change proposed** (raising it to 0.8 would have admitted
names from a bucket with no observed winners; the veto bucket is too small to tune).

**Did the EV floor cut winners? Occasionally, by design.** Floor rejects (n=359 spells):
mean −0.4%, median +0.3%, 53% positive, 4% gained >10% (best: EXEL +18.8% with MAE −0.1% — a
genuinely clean winner the floor cut). The floor's cost is a thin tail of winners; its benefit is
excluding 359 spells averaging below zero. **No floor change proposed** — but note the floor's
interaction with §9(a): names oscillating around +3% generate most of the 1-day spells, so the
floor's *sharpness* (not its level) is a whipsaw contributor, which the persistence band (§10c)
addresses without moving the level.

**Did ETF-overlap exclusions cost us? No.** Overlap rejects (n=15): mean −5.7%, median −7.4%,
13% positive, 0% >10%. The evicted (lower-EV) member underperformed the kept member's cohort —
the overlap rule kept the better wrapper. **No overlap-cap change proposed** (30% stands).

**Did research exclusions cost us? No — and they are mandate, not performance, rules.**
Exclusion rejects (n=12, foreign/emerging-market funds and non-US names): mean −9.2%, 0%
positive. They underperformed anyway; the mandate would exclude them regardless.

**Threshold summary: +3% floor, 0.7 veto, 30% overlap cap all survive October's evidence.**
The changes worth Dan's attention are not thresholds but inputs: valuation, earnings timing,
sweep discipline, and the persistence band.

## 15. Proposals — Dan approves every change; nothing here is implemented

1. **Valuation dampener (researcher input first, formula term second).** Evidence: negative
   implied_upside picks −8.8% mean / 80% negative (n=15) vs +1.3% / 39% (n=31); the §11 cohort's
   26%-vs-4% deep-loss split. Proposal: the researcher must report implied_upside for every pick,
   and a negative value requires either a compensating EV margin or the event-risk flag
   (Proposal 3); a formula dampener on upside (as floated in October) is deferred until the
   November audit confirms the effect in a second regime. This resolves the long-standing
   "valuation dampener — proposed, not approved" item with evidence for the researcher-input
   version now and the formula version later.
2. **Narrow the pre-pick news sweep.** Evidence: the hold log's two entries split cleanly —
   DELL (fundamental IDC data, justified) vs FTNT (sentiment-only Arete call, false positive).
   Proposal: a downgrade alone does not force a re-score absent fundamental news (guidance/target
   cuts tied to fundamentals, regulatory action, accounting issues, lost customers, shipment/order
   data). Symmetric with the good-news side's existing exclusion of analyst chatter.
3. **Entry-timing flag + pick persistence band (together, not separately).** Evidence: §10(c)
   (band roughly neutral, protects FTNT/AAMI-type whipsaws, DELL protected by the hard-trip
   clause) and §11.4(a) (flag costs nothing in list terms). Proposal: (i) earnings ≤~14 days +
   high event sensitivity → "event-risk: print pending" flag on the pick row, thesis and list
   membership unchanged; (ii) a listed name exits only below 0% EV or on a hard trip (dep ≥ 0.7
   veto, stability below B, broken thesis). The flag resolves the tension with Dan's whipsaw
   principle (thesis validity ≠ entry timing); the band resolves the list churn (80% of closed
   spells ≤3 days).
4. **Earnings-dep discipline rule (researcher instruction).** Evidence: §10(b) — post-Oct-18,
   earnings-proximate picks carry no dep premium (0.27 vs 0.29) while going negative 75% of the
   time; the FTNT dep raise (0.20→0.55) was calendar proximity on known information. Proposal:
   a scheduled earnings date known at assessment time cannot by itself raise dep; earnings move
   scores after the print, via news triggers, on actual information. Proximity is handled by
   Proposal 3's flag, not by dep.
5. **Pool-coverage inclusion direction (deferred decision).** Evidence: §8 — one documented case
   (SNX) whose outcome (−14.5%) does not vindicate the gate's reasoning either way. Proposal
   direction (not a change): absolute momentum bars instead of z-vs-winners, letting the
   researcher plus EV discriminate. Ask Dan to decide only after the November audit finds a
   second case or the SNX thesis resolves.
6. **Guidance-quality checklist for earnings-proximate picks (researcher instruction).**
   Evidence: §11 — FTNT and FFIV both broke on guidance mix, not headline EPS. Proposal:
   segment growth, recurring/services mix, billings/RPO, and guidance breadth must be addressed
   in every earnings-proximate pick's assessment, with dep reflecting dependence on a single
   segment's acceleration (guarded by Proposal 4's discipline rule).
7. **Log exit reasons + record stability grade and typical earnings-day move in the ledger
   (instrumentation only, no selection effect).** Evidence: §9(a) (whipsaw unattributable
   without exit reasons), §10(c) (band unreplayable without interim EVs), §11.4(c) (downside
   budget needs the earnings-day-move field). This is the data the November audit needs to turn
   this month's "suggestive" into "confirmed or refuted."

## 16. What future runs must start collecting (so the next audit can decide)

- Exit reason per spell (EV below floor / gate trip / cap / pool exit / thesis break).
- Stability grade per pick event in the ledger (currently only in daily CSVs, which are not
  the permanent record).
- Typical earnings-day absolute move per ticker (historical earnings-day moves, or options-implied
  where available) for the downside-budget test.
- Interim daily EVs for out-of-pick names (or a retained assessment) so persistence-band replays
  are direct, not inferred from exit→re-entry moves.
- More Fuse entries: the experiment has 13 entries from a single day; a second Fuse run would
  double the evidence base.

## 17. Limitations — what this audit cannot say

- One month, one regime (a momentum correction), 121 spells, no 21-day windows, no 12-month
  windows. Every proposal above is provisional until it survives two different market regimes,
  per the audit's standing rule. No weights were tuned and no formula was changed.
- Spell windows mix lengths (1–20 trading days); "to Oct 30" comparisons across spells with
  different start dates embed different regime exposures. Benchmark-relative figures mitigate
  this but do not remove it.
- dual_confirm, sue, beat_streak, and implied_upside cover only ~46 of 121 spells; stability
  grades are missing for 11 spells; earnings proximity uses the daily CSV's earnings_in_days,
  which is an estimate, not a confirmed print calendar.
- October's two sector selloffs (Oct 14, Oct 22) dominate the tail; single-name attribution
  (thesis vs sector) is judgmental in several autopsies and is labeled as such.
- The January-style question — whether picks beat the market over a full year — is unanswerable
  until the ledger has a full year. The November audit (Nov 30) will add 21-day and 42-day
  windows; the 12-month verdict arrives with the October 2027 audit.

---
*Audit method note: spells, prices, and all figures computed from the ledgers and yfinance Adj
Close by the audit script (analysis code kept with the goal's working state). Report written
2026-10-30; published to the repo's `audits/` folder as the permanent public record.*
