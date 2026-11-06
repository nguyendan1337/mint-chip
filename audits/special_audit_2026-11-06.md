# Mint (Chip) — Special Audit: Scorer and EV Formula

**Date:** Friday 2026-11-06 · **Type:** One-time, report-first audit (Dan-commissioned 2026-11-02; ETF leg widened 2026-11-04)
**Scope:** Phase A scorer (`base_score`) and the EV formula only, plus the Addendum B ETF-leg widening. **Proposes only — nothing has been implemented.** The methodology freeze otherwise remains in force until the Nov 30 monthly audit, which is the validation venue for anything below.
**Report:** `~/workspace/stock-screener/special_audit_2026-11-06.md` · **Published copy:** `~/workspace/mint-chip-pages/audits/special_audit_2026-11-06.md`
**Work dir:** `~/workspace/stock-screener/special_audit_2026-11-06_work/` (spells, replay scripts, CSVs — all numbers below are recomputable from it)

## Headline verdict

- **Ship set: NONE.** No candidate passes the pre-registered ship-set bar (REPLAYED + improves pick-vs-control spread on October data + does not worsen the MAE ≤ −15% rate + not HIGH overfit risk, ≤2 moving parts). Every E/S/F candidate is rejected or deferred, with reasons in the Proposal Ledger (§7). This is a **ship-nothing** audit.
- **ETF leg: no replay-supported ETF fix.** None of F1/F2/F3 passes the B3 bar (§6). F2 is the Nov 30 re-test candidate only.
- **Scorer leg: UNREPLAYED as a combined claim.** The A2 preflight FAILED for the scorer (no per-ticker z-scores recorded anywhere). The rescue re-execution passes exactly for Oct 30 stocks (+ Nov 6 stocks) but fails for Oct 30 ETFs (a same-day cache overwrite destroyed the run-1 inputs). S1/S2 were therefore testable only on one outcome day (Oct 30), where they change **zero** picks — the `final_score` floor is slack, so scorer variants can only move picks via pool membership, which is unmeasurable because entrants were never researched.
- **EV leg: REPLAYED** (est_next_1y reproduced exactly on 2,753/2,753 tested rows; corrected E0 baseline replicates the actual picks: 406/412, 6 explained residuals, 0 extras). **No E-variant improves the October pick-vs-control spread**: E1/E3/E4 change zero picks; E2 changes two picks, both for the worse (PK −1.1% → AVT −11.9%; PEB −1.9% → RELY −7.6%).
- **Two preflight failures / headline data gaps open this report** (§1.3 and §2): the scorer A2 failure, and the B1 Oct 30 ETF diagnosis mismatch (the final frame has 100 ETFs, median EV −1.96%, **4** clearing +3% — not 98 / −2.8% / 1).
- **Valuation-light is the legitimate headline conclusion for the EV layer:** implied upside is the strongest winner/loser separator among stock picks (Cohen's d +0.82), while multiples give no usable signal (forward P/E medians barely differ; price-to-book is recorded nowhere). Whether that survives the corrected EV replay is §4's question.

---

## 1. Configuration (reconciled against MEMORY.md and the decision record)

`screener.py` sha256 = `7e53a483983c335725a88ef0499b1041e32f0a369c8fb03a8fac8aa125aba99a` (5,694 lines; matches all 29 manifests Oct 30–Nov 6). **No drift** in scorer weights, EV formula, or the Oct 30 changes vs the decision record. Two corrections to the record itself (§1.4) and two instrumentation findings (§1.5) are the only config-level news.

### 1.1 EV formula (verbatim, `est_parts()` ~line 991, `estimate_next_year()` ~1087)

```
EV = conf × (1 + 0.25 × character) × (cont × upside − (1−cont) × downside − dep × 20%)
```

- **Upside** = `ret_6m_skip1m` (6-month return excluding the most recent 21 trading days), dampened: first 25% at full weight, beyond at half, capped at 50% (max upside 0.375).
- **Downside** = max(|maxDD| × 0.5, 10%) × (1 + drawdown frequency), where drawdown frequency = fraction of 1-month windows losing >10%. Earnings-gap floor (earnings ≤30d → downside = max(existing, ½ × ticker's median absolute post-announcement-session move; 12% fallback)) **never binds** on any Oct 30–Nov 6 row.
- **Character** = Composure z-sum of quality/entry-timing/structure (excludes momentum + vol), clamped [−2, 2] in `est_parts()`.
- `dep` in the EV uses `dep_used` = `effective_dep()` (~1174): diversified ETFs capped at 0.15 (Oct 30 change). The dep veto (0.7) and the two-strike dep strike also use `dep_used`.

### 1.2 Scorer weights

- **Stocks** (`build_scores()` ~504): momentum 0.22, blowoff 0.08, quality 0.30, low-risk 0.30, DTE 0.05, multiples 0.05 (forward P/E 0.03 + price-to-book 0.02).
- **ETFs** (`build_etf_scores()` ~1762): momentum 0.40, low-risk/liquidity 0.40, cost/scale 0.20 (expense 0.10 + AUM 0.10).
- **`final_score`** (`apply_llm_outputs()`, `llm_research.py` ~509): `base_w − 1.5×dep_raw + 1.0×cont − 0.5×excess×dep_raw`, where `base_w` = base_score winsorised at the p95 cap over the risk-gated set and `excess` = base_score − cap. **Asymmetry (verified real): `final_score` uses RAW dep; EV, veto, and strikes use `dep_used`.** Selection is EV-descending among `final_score`-floor passers.
- **Defaults:** `--w-down 1.5`, `--w-up 1.0`, `--w-outlier 0.5`, `--veto-dep 0.7`, `--min-est 0.03`, `--min-score 0.0`. ETF category cap 2; stock sector cap 2; 10 slots per class.

### 1.3 Replay-integrity preflight (A2)

| Check | Verdict |
|---|---|
| EV (`est_next_1y` recomputed from recorded inputs) | **PASS** — max abs err 0.0 on 2,753/2,753 tested rows (15 daily frames Oct 30–Nov 6) |
| `final_score` | **PASS** — exact, 0.0 err, incl. Oct 30 watchlist (excess = 0) |
| Scorer (`base_score` from recorded components) | **FAIL** — no per-ticker z-scores recorded anywhere (frames, bundles, logs — logs hold only 2dp-rounded grouped sums, max err 0.027 vs recorded base). By the job's definition S1/S2 are UNREPLAYED unless the re-execution rescue passes |

**Re-execution rescue** (re-running the real `build_scores()` / `build_etf_scores()` on cached point-in-time inputs, network blocked):

| Leg | Verdict |
|---|---|
| Stocks, Oct 30 | **PASS, exact** — 110/110, max err 0.0 (requires the insider term, §1.4) |
| ETFs, Oct 30 | **FAIL** — mean abs err 0.148, max 0.275, 0/100 within 1e-6. Cause: run-1 ETF fundamentals were a partial fetch (928/1,344); a second same-day run fetched the missing 416 and **overwrote `etf_fundamentals_aa9381f8e3a9.pkl` in place**, destroying the run-1 inputs. Re-execution reproduces run 2 exactly (104/104 vs second bundle 162919). 19 tickers flipped trash outcome (all ex-US ETFs) |
| Stocks, Nov 6 | **PASS, exact** — 110/110, max err 0.0; 632 scored stocks |
| ETFs, Nov 6 | **PASS on the numbers** — max abs err 0.0, 100/100 vs both bundles (the script's verdict string says FAIL due to a falsy-zero bug, `(max_err or 9) <= 1e-6`; the numbers, not the verdict string, are correct) |

Consequence: S-variants are re-executable for Oct 30 + Nov 6 **stocks only**. ETF-leg S-variants are not computed (Oct 30 run-1 components unrecoverable; Nov 6 alone has no outcomes). Partial ETF fundamentals fetches are the norm (Nov 6 worst: 614/1,346); Oct 30 is the only date a later same-day run augmented the dict after the recorded run.

### 1.4 Correction to the decision record: the insider term exists — in `main()`, not `build_scores()`

The config extraction stated "no insider term exists in the formula". That is true of `build_scores()` but **false of the recorded `base_score`**. `main()` step 6b (`screener.py` lines ~5357–5380), verified by direct read:

```python
enr_tickers = df.head(min(100, len(df)))["ticker"].tolist()   # top 100 by PRE-insider base_score
df["insider_ratio"] = df["ticker"].map(lambda t: enr.get(t, {}).get("insider_ratio"))
mask = df["ticker"].isin(enr.keys())
df.loc[mask, "z_insider"] = zscore(-pd.to_numeric(df.loc[mask, "insider_ratio"], errors="coerce").fillna(0)).values
df["z_insider"] = df["z_insider"].fillna(0)                    # outside the mask: exactly 0
df["base_score"] = df["base_score"] + 0.06 * df["z_insider"]
df = df.sort_values("base_score", ascending=False)             # re-sorted AFTER the adjustment
```

- `z_insider` is z-scored over the masked top 100 only (sample std, ddof=1; missing `insider_ratio` → 0 before standardising), then 0 elsewhere. It is **not winsorised**: Oct 30 has two extremes, LFST −6.87 and FA −6.86, each moving `base_score` by ≈ −0.41 (LFST: raw 0.383 → recorded −0.029). Raw pre-insider base errs by up to 0.0114 vs recorded; adjusted errs by 0.0. Exactly 100 of 626 stocks carry nonzero `z_insider` (Oct 30 and Nov 6 alike). All S-variants reuse the recovered `z_insider` verbatim.
- "Byte-identical since Oct 5" is verifiable only back to Oct 30 from manifests (the earliest surviving manifest); the claim before that date is uncheckable from the records.

### 1.5 Instrumentation findings (not drift — pre-existing properties, out-of-scope for ship, §9)

- **`z_expense` is a constant — the ETF expense weight differentiates nothing.** `etf_facts()` reads `expenseRatio` with fallback `annualReportNetExpenseRatio`; it never reads `netExpenseRatio` (0 occurrences in `screener.py`), the field yfinance actually populates for most ETFs. Verified from the re-executed components: `expense_ratio` null for **all** ETFs (Oct 30 second run 415/415; Nov 6 466/466) and `z_expense` constant (−0.9988 Oct 30; 0.0 Nov 6). The 10% expense weight in the ETF `base_score` (and 10% in ETF character) therefore moves no ETF relative to any other.
- **Dead code** in `estimate_next_year()` lines 1111–1112 (unreachable second formula copy without the character term); stale "insider/valuation/value 10%" comment.
- **Partial-fetch cache overwrite** (see §1.3 ETF Oct 30 FAIL) — a same-day re-run can destroy the recorded run's inputs. Instrumentation-only fix belongs on the Nov 30 docket (§9).

---

## 2. DATA GAPS (mandatory — read before any analysis)

Spells: 2,420 ticker-date spells, Oct 5–Nov 6 2026, outcomes to Nov 6 close (`spells.csv`; 412 pick spells — 346 Oct + 66 Nov — 1,877 rejects, 129 honorable mentions, 2 removed pick corrections). **Unit note:** this audit's unit is ticker-date spells (412 pick spells) vs the Oct audit's consecutive-day pick spells (121) — totals are **not comparable** across audits.

| Gap | Size / pattern | Consequence |
|---|---|---|
| `upside` / `downside` EV components | 100% null in spells.csv (never persisted) | Must be recomputed from `ret_6m_skip1m` / `maxdd` / `dd_freq` via `est_parts()` (done by the EV replay; verified exact) |
| `z_*` scorer components | No such columns anywhere (frames, bundles, logs) | A2 scorer preflight FAILED; S-variants re-executable only via the §1.3 rescue (Oct 30 + Nov 6 stocks) |
| `implied_upside` | Null 1,580/2,420 (65%): **ETFs 100% null** (1,379/1,379 — the Fuse check is stocks-only); stocks 19% null; ~15–20%/day pre-Oct-30, ~52% Oct 30+ | E1/E2 and S1 are stocks-only in practice; no ETF implied-upside evidence exists |
| `final_score` | Null 825 (all pre-Oct-30 rejects) | Pre-Oct-30 rejected control has no `final_score` |
| `stability` | Null 834 (same pattern) | Same |
| `risk_strike_count` / `risk_strikes` | Null 1,160 / 1,963 (48% / 81%); recorded only on Oct 30+ frame rows | Two-strike gate untestable pre-Oct-30; **the gate must not be applied retroactively** (see §4 correction note) |
| `ret_6m_skip1m` | Null for all Oct 5–16 spells (620) — the Oct-18 serialization bug | Recorded EV on those dates used un-skipped `ret_6m` (est_parts fallback); the skip never took effect before Oct 18 |
| Pre-Oct-30 rejected control is a SELECTED subset | Ledger records ≤15 "audit-interesting" rejects/run, mostly EV-floor fails; ~30/day vs ~180–200 evaluated/day | The October control under-represents gate-barred and cap-evicted names; full evaluated set exists only Oct 30–Nov 6 |
| Honorable mentions | Exist only Oct 30–Nov 6 (129 spells, all stocks in Oct) | October HM population is one day (Oct 30, n=20) |
| Same-day multi-run conflicts | Resolved frame-wins (Oct 30: 22 conflicts; DIVB/FELV/JQUA floor-rejected in intermediate runs, picked in final 18:00 frame; excluded ledger-only records incl. a VGT-as-pick artifact). Oct 18: FELV/VFMF reject→pick, PM pick→reject (latest wins) | Explains the B1 Oct 30 mismatch (§6.1): B1's figures came from an intermediate run, not the final frame |
| 9 spells with no return | Delisted/halted: ACA, INDV, MSGS, KVUE, LXP, RAMP | Excluded from return denominators; stated per table |
| StepCache Oct 5–29 gone | keep_days=7 pruning; Nov-6 cache values are pre-close snapshots, overridden by yfinance closes | Re-execution possible only Oct 30 + Nov 6 |
| `dep_capped=True` counts drop sharply Nov 5–6 | 52 → 8 → 6, unexplained in frame data | Flagged, not explained — Nov 30 docket |
| Ledger pick_price vs close differs >0.5% on 8 spells | Worst JQUA Oct 30 +1.51% | Entry-price noise, stated |
| `fpe` / `ptb` | Not columns of spells.csv; `fpe` joinable from bundles (stocks, frame dates only); **`ptb` recorded NOWHERE** (absent from bundles, frames, spells) | S2's multiples test rests on `fpe` only; no ptb evidence exists |
| PM Oct 18 spell | Reason text cites est +2.0% ("EV floor") while `est_next_1y` records +7.15% | Reason/EV inconsistency in one spell |
| Oct 18 dating | 93 spells dated Sunday Oct 18 carry `entry_trading_day` Oct 16 (Friday-close run) | Dating quirk, stated |

---

## 3. Step 2 — Findings (October primary; November immature, consistency check only)

**Definitions:** return = entry close → Nov 6 close; benchmark-relative = vs VGT (stocks) / VOO (ETFs); winner = return > 0; loser = return < 0. November statistics use Nov 2–5 entries only (Nov 6 rows carry 0.00% by construction). Spells are NOT independent observations — the same ticker re-enters on consecutive days (SN 20 pick spells, CON 20, AAMI 18); distinct-ticker sensitivities are shown alongside. Machine-readable tables: `findings_tables.csv` (250 rows).

### 3.1 Three-population table, OCTOBER (primary)

| Population | Universe | n spells | Mean ret | Median ret | % positive | MAE ≤ −15% | Mean rel_ret |
|---|---|---|---|---|---|---|---|
| Picks | all | 346 | **−3.31%** | −1.88% | 39.3% | 16.5% | +5.73% |
| Picks | stocks | 214 | −1.65% | +0.36% | 51.9% | 17.3% | +10.18% |
| Picks | ETFs | 132 | −5.99% | −4.54% | 18.9% | 15.2% | −1.49% |
| Honorable mentions | stocks (Oct 30 only) | 20 | −2.73% | −2.15% | 30.0% | 0.0% | +4.50% |
| Rejects | all | 1,002 | −2.28% | −1.27% | 31.5% | 5.1% | +3.74% |
| Rejects | stocks | 255 | −3.57% | −1.90% | 40.3% | 13.9% | +7.54% |
| Rejects | ETFs | 747 | −1.85% | −1.19% | 28.5% | 2.1% | +2.47% |

Removed spells (excluded above): TWLO Oct 16 (−17.3%), FTNT Oct 21 (−31.7%) — same-day pick corrections.

**Split-half (Oct 5–20 vs Oct 21–30):** picks −3.09% / −3.65%; rejects −2.68% / −1.69%. **Distinct-ticker sensitivity:** October picks are 67 tickers, mean of ticker-mean returns **−7.20%** (only 22.4% positive) vs −3.31% at spell level — the spell mean is flattered by repeats (SN, CON, VCTR positive early) and by the long tail of one-off losers. Rejects: 298 tickers, −4.66%.
**November (immature, Nov 2–5 entries, n=50 pick spells):** picks −1.49% vs rejects −1.85%; same ETF-weak pattern (ETF picks 0% positive, n=12). No contradiction with October; no confirmation either.

**F1 — Picks lost more than rejects in absolute terms, and the loss is an ETF story.** Stock picks beat stock rejects (−1.65% vs −3.57%) while ETF picks lost far more than ETF rejects (−5.99% vs −1.85%). Picks also drew down deeper (MAE ≤ −15%: 16.5% vs 5.1%). Benchmark-relative, picks look better (+5.73% vs +3.74%) — but that mixes VGT-relative stocks with VOO-relative ETFs. Any fix that treats the two universes as one population will misread this table (A4).

### 3.2 (a) EV calibration under the CURRENT formula (E0), October

| Population | EV bucket | n | Mean ret | % positive | MAE ≤ −15% |
|---|---|---|---|---|---|
| Picks | 3–6% | 94 | −4.44% | 25.5% | 8.5% |
| Picks | 6–9% | 87 | −2.38% | 31.0% | 9.2% |
| Picks | 9–12% | 121 | −1.47% | 54.5% | 24.0% |
| Picks | 12%+ | 44 | **−7.77%** | 43.2% | **27.3%** |
| Rejects | < 0% | 589 | −2.18% | 31.7% | 5.1% |
| Rejects | 0–3% | 385 | −1.83% | 32.6% | 3.6% |
| Rejects | 3–6% | 15 | −8.15% | 20.0% | 6.7% |
| Rejects | 6–9% | 9 | −12.00% | 0.0% | 22.2% |
| Rejects | 9–12% | 4 | −15.82% | 0.0% | 100.0% |

Spearman (EV vs realized return, October): picks ρ = +0.175 (p = 0.001, n = 346); rejects ρ = −0.017 (p = 0.59, n = 995). **Higher EV predicted higher realized return only weakly and non-monotonically among picks — and not at all among rejects.** The break is at the top: the 12%+ bucket (44 spells, 11 tickers) is driven by DELL ×4 (−43.2%) and FTNT ×6 (−37.5%), partly offset by SN (+12.6%) and VCTR (+5.7%). Per-ticker, the 12%+ bucket is just 2 tickers (AAMI −2.3%, DELL −43.2%). The 28 rejects that cleared the floor but were evicted (mostly ETF-overlap dedupe) averaged **−10.48%**.

### 3.3 (b) Scorer audit

- **base_score Spearman vs return, October picks:** all −0.044 (ns); **stocks −0.179 (p = 0.009, NEGATIVE)**; ETFs −0.035. Highest-base quartile of stock picks −6.65% vs lowest quartile +1.18%.
- **Z-level attribution is impossible** (no z_* recorded — A2). Everything below is a proxy comparison on recorded variables, not a decomposition of `base_score`.

### 3.4 (c) Valuation as data

**Implied upside (October, stocks only — 0% ETF coverage):** among stock picks, winners +8.1% mean / +6.6% median, 2% negative; losers +1.8% / +1.1%, 45% negative (coverage 89% winners / 93% losers — no coverage bias within stocks). Cohen's d **+0.82**, the strongest separator in the file. Reject-side coverage is ~8%, nearly uninformative; November (immature, 10 winners) reverses the direction.
**Multiples:** forward P/E medians barely differ (Oct 30 stocks: winners 21.6 vs losers 17.0, means reversed 21.6 vs 28.5 on a few high-P/E losers; November immature 19.8 vs 18.8). **Price-to-book is recorded nowhere** — no ptb comparison was possible. Multiples give no usable winner/loser signal from this file.

### 3.5 (d) Winner/loser profiles (October picks; Cohen's d, positive = winners higher)

| Variable | Pooled d | Stocks d | ETFs d | Direction |
|---|---|---|---|---|
| implied_upside | **+0.82** | +0.82 | n/a (0% coverage) | Winners higher — strongest signal |
| dep | −0.35 | **−0.56** | −0.42 | Lower is better |
| final_score | +0.50 | +0.47 | +0.49 | Higher is better |
| character | **−0.51** | −0.47 | −0.38 | **REVERSED** — winners lower |
| cont | +0.48 | +0.35 | — | Winners higher (modest) |
| conf | −0.13 | −0.38 (reversed) | — | Negligible / reversed |
| ret_6m_skip1m (EV upside input) | +0.12 | −0.05 | **−0.84 reversed** (winning ETFs had smaller 6m runs, +22.1% vs +32.7%) | No signal for stocks; backwards for ETFs |
| dd_freq | +0.39 | — | — | **REVERSED** — winners dipped more often |
| base_score | −0.04 | −0.20 (reversed) | −0.05 | No signal; backwards for stock picks |

Stability does not separate pick winners from losers (A 74% vs 72%). Risk-strike counts do not separate (172 Oct rejects carrying them: 0.70 vs 0.67) — but the sample is one frame day.

**Narrative:** the recorded variables that separated winners from losers are `dep` (lower is better), `final_score` (higher is better), and — for stocks — `implied_upside`. The character multiplier and the momentum leg of EV are the two places the October evidence contradicts the formula's direction.

### 3.6 Would-be winners (October)

- 750 of 1,015 rejects+HM (73.9%) beat the pick mean −3.31% — a low bar, since the pick mean is negative; only 31.4% of them were positive at all. 50 above +5%.
- Top 10: EXEL +18.2% (dep veto), CAH +16.6% (floor), IPAR +16.5% (floor), VTRS +13.0% (floor), BIIB +11.1% (floor), SEPN +11.0% (stability C), LH +10.7%, XLV +10.4% (ETF), GWW +10.3%, WELL +9.9%.
- **94% of the 254 would-be winners were EV-floor rejects** — the floor, not the gates, is where the missed winners live (expected: the floor is the binding screen for 90% of rejects).

---

## 4. EV replays (E1–E4 + combinations)

**Replay status: REPLAYED.** `est_next_1y` reproduced exactly (A2 preflight, §1.3). E0 = recorded `est_next_1y` (exact by definition); E2/E3/E4/combos are exact rescalings of recorded EV, so no row is excluded; E1 recomputes upside/downside via `est_parts()` with exact fallbacks and is verified exact on all 252 rows where it bites (max err 1e-6). Definitions per `candidate_specs.md`: **E1** = upside × max(0, 1+implied_upside) when implied_upside < 0; **E2** = bracket += 0.10 × implied_upside; **E3** = character clamp [−2,2] → [−1,1]; **E4** = conf capped at 0.75. Combinations E3+E4, E1+E3, E2+E3 tested per the combination rule (each component must pass alone first — none does, so combos inherit component verdicts).

**Correction note (methodology, kept on the record):** the first EV pass had a defective baseline — it applied the two-strike gate retroactively to pre-Oct-30 dates (72 of 95 missed picks) and omitted the ETF-overlap repair (23 extras), producing a flattered E0 cohort (October stock-pick mean +3.56% vs the actual −1.65%). The coordinator caught this by independent check of `_stage2_replay.csv` and ordered a corrected re-run. The fixed baseline applies gates only from their ship dates (stability ≥ Oct 16, two-strike ≥ Oct 30, using recorded counts — recomputation verified to match recorded counts exactly on all 1,260 frame rows), implements the overlap repair faithfully from the 10 recorded pairs (25 rows, 0 unparseable), and treats PM Oct 18 + the 2 `removed` spells as ineligible. The first pass's numbers are withdrawn; only the fixed numbers below are quoted. A real bug in the agent's own gate code (`~bool` = −2, which would have forced gates open) was also caught and fixed before re-running.

### 5.1 E0 replication — the fixed baseline IS the picks

| | Actual picks | Replay E0 picks | Misses | Extras |
|---|---|---|---|---|
| All dates Oct 5–Nov 6 | 412 | 406 | 6 | 0 |
| Frame dates Oct 30–Nov 6 | 80 | 80 | 0 | 0 |
| Ledger dates Oct 5–Oct 29 | 332 | 326 | 6 | 0 |

Exact on **23 of 26 dates**. The 6 residuals are all picks beyond the 10-slot cap on multi-run days (ledger merges same-day runs): Oct 5 FELC (+5.7%, 3rd Large Blend), XNTK (+9.6%, 3rd Technology), NDSN (+9.4%, 11th stock), SN (+9.6%, 12th stock); Oct 18 AVT (+7.2%, 11th stock); Oct 27 FFIV (+6.4%, 11th stock). **Bias check (independently re-verified by the coordinator from `ev_replays.csv`):** replay E0 October stock-pick mean −1.54% vs recorded −1.65%. The first draft's +3.56% is gone.

### 5.2 E0 baseline — October (primary evidence)

Replay universe: 1,345 candidates (1,370 October spells − 25 ineligible: 22 research-excluded + 2 removed + PM). Control = eligible candidates not picked under the variant. Outcomes to Nov 6 close.

| | Picks n | Pick mean | % positive | MAE ≤ −15% | Control n | Control mean | **Spread** |
|---|---|---|---|---|---|---|---|
| **E0 ALL** | 340 | −3.20% | 40% | 16.2% (55) | 1,005 | −2.18% | **−1.02pp** |
| E0 stocks | 210 | −1.54% | 52% | 17.1% | 275 | −3.46% | **+1.92pp** |
| E0 ETFs | 130 | −5.87% | 19% | 14.6% | 730 | −1.70% | **−4.17pp** |

- **Split-half:** H1 (Oct 5–20) ALL spread −0.60pp (stocks **+3.75pp**, ETFs −4.55pp); H2 (Oct 21–30) ALL spread −1.64pp (stocks **−0.03pp**, ETFs −3.59pp). The stock spread does **not** hold across halves; the ETF spread is negative in both. No stable selection edge in either universe on this window.
- **Drawdown warning:** October picks' MAE ≤ −15% rate (16.2%) is far above control (4.5%), in both halves (H1 20.6% vs 6.2%; H2 9.2% vs 2.2%). Being picked (high EV) was associated with *more* frequent ≥15% drawdowns, not fewer — the "don't go negative" aim is not met in October on drawdowns, even though the stock mean spread is positive.
- **November (consistency only, immature):** E0 ALL spread +0.50pp (stocks +0.80pp, ETFs +0.02pp); picks 66. Nov-6 entries have ret = 0.0 by construction. Never fit to November.
- **Gate retrospective (diagnostic only — the gate was NOT applied pre-Oct-30 in the replay):** the 74 recorded pre-Oct-30 picks that *would* have carried ≥2 recomputed strikes averaged **−9.64%** (median −3.77%, 35% positive, MAE-bad 35.1%) vs −3.34% for all 332 recorded pre-Oct-30 picks. The gate, had it existed, would have barred a deeply-losing cohort. This validates the Oct-30 gate decision; it is not a replay result.

### 5.3 Variants vs E0 — October (primary)

Pass bar (pre-declared): October ALL spread improves ≥ +0.50pp AND neither universe's spread worsens by > 0.25pp AND pick MAE-bad rate does not worsen AND the spread-change sign is the same in both halves.

| Variant | Oct spread chg ALL / stocks / ETFs | H1 chg | H2 chg | Oct pick-n chg | Oct picks lost / gained | MAE-bad |
|---|---|---|---|---|---|---|
| E0 | −1.02 / +1.92 / −4.17pp (baseline) | — | — | 340 | — | 55/340 |
| E1 | **+0.00 / +0.00 / +0.00** | +0.00 | +0.00 | +0 | 0 / 0 | 55/340 |
| E2 | **−0.07 / −0.14 / +0.00** | −0.07 | −0.06 | +0 | 2 / 2 | 55/340 |
| E3 | **+0.00 / +0.00 / +0.00** | +0.00 | +0.00 | +0 | 0 / 0 | 55/340 |
| E4 | **+0.00 / +0.00 / +0.00** | +0.00 | +0.00 | +0 | 0 / 0 | 55/340 |
| E3+E4 | +0.00 / +0.00 / +0.00 | +0.00 | +0.00 | +0 | 0 / 0 | 55/340 |
| E1+E3 | +0.00 / +0.00 / +0.00 | +0.00 | +0.00 | +0 | 0 / 0 | 55/340 |
| E2+E3 | −0.07 / −0.14 / +0.00 | −0.07 | −0.06 | +0 | 2 / 2 | 55/340 |

**No variant passes. No variant moves the October pick set except E2, and E2 moves it the wrong way.** (Coordinator re-verified from `ev_replays.csv`: E1/E3/E4 pick sets byte-identical to E0; E2's two swaps confirmed; spread changes match within 0.02pp of the agent's eligible-control figures — the residual difference is the 25 ineligible rows in the control, which the agent documents.)

- **E1 — valuation dampener (252 rows changed):** zero October pick changes. Its 4 October floor cross-downs are all Oct-30 honorable mentions that were never picks (CDNA +3.9% → +0.9%, EXPD +3.9% → +2.9%, PAYC +3.5% → +2.6%, RVTY +3.3% → +2.6%). **Winners lost: none. Losers avoided: none.** November swaps 2 (loses THG −1.8%, AAPL −2.0%; gains SLDE −6.0%, CPAY +8.7%) on 1–3-session outcomes — not evidence. Oct-30 and Nov-6 pages unchanged. **Verdict: REJECT** — no replay-supported benefit at this strength; the dampener demotes only names that were never going to be picked.
- **E2 — implied-upside blend (840 rows changed):** October **2 lost / 2 gained, both swaps bad.** Lost: PK Oct 18 (−1.1%), PEB Oct 30 (−1.9%) — mild losers avoided. Gained: AVT Oct 18 (−11.9%, MAE −15.0%) and RELY Oct 30 (−7.6%, MAE −14.2%) — much worse losers added. Spread change −0.07pp ALL / −0.14pp stocks, negative in **both** halves. November: gains RELY Nov 2 (−11.9%, MAE −18.2% — the only November MAE-bad pick) and RNG Nov 2 (−7.3%); spread −0.16pp. Same direction as October: E2 promotes high-implied-upside names that then fall. Oct-30 page: **PEB out, RELY in** (the only Oct-30 page change of any variant). Nov-6 page unchanged. **Verdict: REJECT** — directionally negative in both halves and in November; the analyst signal, at 0.10 weight inside the bracket, points the wrong way in this window.
- **E3 — character clamp (4 rows changed):** literally identical to E0 on every date. Only 4 rows in 2,420 have |character| > 1 — CQP Oct 30 / Nov 2 / Nov 3 (+1.25/+1.32/+1.44, EV −11.6%/−11.8%/−13.9%, all rejected) and GKOS Nov 2 (−1.03, EV +1.5%, rejected) — none near selection. Character's recorded distribution is mean +0.13, SD 0.16, max +1.44 — the tail the audit flagged barely exists in the evaluated set. **Verdict: DEFER (untestable, not disproven)** — no identifying variation in Oct 5–Nov 6; shipping a change with zero measured effect would violate replay discipline. Re-test at Nov 30 with a wider window.
- **E4 — confidence cap 0.75 (98 rows changed):** zero October pick changes. Its single October floor cross-down is SPYG Oct 9 (+3.03% → +2.84%), which is not an E0 pick (overlap-evicted). November swaps 3 on 1–3-session outcomes (spread +0.18pp) — not evidence. Oct-30 and Nov-6 pages unchanged. **Verdict: DEFER** — the diagnosis it targets is real (the 12%+ EV bucket is the worst pick bucket: mean −7.77%, MAE-bad 27.3%, and pick MAE-bad rises monotonically with EV bucket, 8.5% → 9.2% → 24.0% → 27.3%), but this dose (cap 0.75) does not move October selection at all. Re-test at Nov 30; a stronger cap would be a new candidate, not a tweak to ship now.
- **Combinations:** E3+E4 = E4 exactly; E1+E3 = E1 exactly; E2+E3 = E2 exactly. E3 contributes nothing anywhere, so no interaction benefit exists. **E3+E4 DEFER, E1+E3 REJECT, E2+E3 REJECT.**

### 5.4 Counterfactuals (A5)

- **Oct 30 page (live 14: IDT, AAMI, VFLO, NTAP, WST, SEIC, IEX, ABBV, NDSN, PEB, RHP, JQUA, DIVB, FELV):** unchanged under E0/E1/E3/E4/combos. Under E2 and E2+E3: **PEB leaves, RELY enters** (RELY +2.5% → +4.9% EV; PEB −1.9% to Nov 6 vs RELY −7.6% — the swap loses).
- **Nov 6 page (live 16: VFLO, DSTL, DIVB, JQUA, ADP, ABBV, CNO, RHP, WST, IDT, AAPL, SLDE, IHF, FTDR, XHS, EXPD):** unchanged under **every** variant. Floor crossings (E1: EXEL down; E2: RNG, TGTX, VOYA up, EXEL down; E3/E4: none) — none of the crossers make the top 10. November outcomes immature — consistency check only.

### 5.5 Overfit verdicts — EV

| Variant | Verdict | Reasons |
|---|---|---|
| E1 | **LOW** | Round, pre-declared (proportional dampener, no fitted constant); 340 October picks; one tape regime (down tape, picks averaged negative) — no second regime observed |
| E2 | **LOW** | Round, pre-declared (0.10 weight); same single-regime limit |
| E3 | **LOW formal risk; UNTESTABLE** | 4 affected rows in 2,420; no variation to test |
| E4 | **LOW** | Round, pre-declared (cap 0.75); same single-regime limit |
| Combos | **LOW** (inherit components) | Same as components |

No variant is barred by HIGH overfit risk — they are barred (or deferred) by **lack of benefit**, not by overfitting. The binding constraints are the single-regime sample (one down-tape month, 340 October picks) and the split-half non-replication of even the E0 stock spread.

### 5.6 EV recommendation

**Ship set (EV leg): NONE.** E1 REJECT, E2 REJECT, E3 DEFER (re-test Nov 30), E4 DEFER (re-test Nov 30), combos inherit. The October audit's deferred items (valuation dampener, conf tails) remain deferred. The valuation-light headline stands: the strongest winner/loser separator (implied upside, d=+0.82) does **not** survive being blended into EV at this weight — E2's October swaps both lose — while multiples give no usable signal at all.

---
## 5. Scorer replays (S1 / S2 / S3)

**Replay status: REPLAYED for Oct 30 stocks only (+ Nov 6 stocks, counterfactual only). ETFs UNREPLAYED.** S-variants were computed from the verified component tables (`scorer_components_oct30_stocks_verified.csv`, 626 stocks; `scorer_components_nov6_stocks.csv`, 632 stocks). Definitions per `candidate_specs.md`: **S1** = +0.10 × z_implied (z-scored over the scored set; 109 observed values Oct 30, mean +4.3%, std 11.1%, max ORKA +61.2% at z +5.11; the other 517 stocks are NaN → 0), funded ×42/52 from momentum+quality; **S2** = multiples 0.03/0.02 → 0.06/0.04, momentum ×17/22; **S3** = S1+S2 only if each passes alone. The 0.06×z_insider term (§1.4) is added on top in every variant with the production mask. Winsorisation + `final_score` verified exactly under S0 (cap 0.352912 Oct 30 / 0.373420 Nov 6, reproducing the recorded bundle caps; `final_score` max abs err 1.1e-16).

### 6.1 S0 control reproduces both recorded pages exactly

- **Oct 30:** 10 stocks (IDT, AAMI, NTAP, WST, SEIC, IEX, ABBV, NDSN, PEB, RHP) + 4 ETFs (VFLO, JQUA, DIVB, FELV) — 14 picks, exact.
- **Nov 6:** 10 stocks (WST, ABBV, IDT, ADP, SLDE, CNO, AAPL, EXPD, RHP, FTDR) + 6 ETFs (VFLO, JQUA, IHF, XHS, DSTL, DIVB) — 16 picks, exact. ETF side held at S0 in every variant.

### 6.2 Oct 30 results — S1 and S2 change NO picks within the recorded pool

| Variant | Stock picks (10) | Enter | Leave |
|---|---|---|---|
| S0 | AAMI, ABBV, IDT, IEX, NDSN, NTAP, PEB, RHP, SEIC, WST | — | — |
| S1 | identical to S0 | none | none |
| S2 | identical to S0 | none | none |
| S3 | not computed (combination rule — neither S1 nor S2 passes alone) | — | — |

| Cohort (Oct 30 spells → Nov 6) | n | Mean ret | % positive | MAE ≤ −15% | Bench-rel mean |
|---|---|---|---|---|---|
| S0 stock picks | 10 | −2.48% | 30% | 10.0% (1/10: NTAP) | +4.75% |
| S0 blended page (14) | 14 | −2.44% | 21.4% | 7.1% | +3.50% |
| S0 control (evaluated, not picked) | 192 | −2.73% | 22.4% | 4.2% | +2.24% |
| **S0 pick-vs-control spread, stocks** | | **+0.25pp** | | | |
| S1 / S2 stock picks | 10 | −2.48% | 30% | 10.0% | +4.75% |
| **S1/S2 spread change vs S0** | | **0.00pp** | | | |

**Winners lost: none. Losers avoided: none. Winners gained: none. Losers added: none** — under either variant, within the recorded pool. Note the S0 baseline itself: the picks' deep-loss rate (10.0%) is *worse* than control (4.2%); the +0.25pp spread comes from the mean, not from avoiding blowups.

### 6.3 Why nothing changes — the structural finding

**`final_score` is a floor, not a ranking:** picks are ordered by EV, and `final_score` only excludes names below 0. Among the 15 stock EV-floor passers that also clear stability and the two-strike gate, the minimum `final_score` is **0.264 (S0), 0.243 (S1), 0.242 (S2)** — the floor is slack by ~0.24 on every variant. S1 moves finals by up to 0.52 (mean 0.065) and S2 by up to 0.21 (mean 0.022) across the 110, yet no EV-passer comes near 0 under any variant. The floor binds for 26/110 names under S0 — but all of them are already excluded by EV, stability, or strikes. **On Oct 30 a scorer variant can only change picks by changing research-pool membership, never by moving `final_score`.** Zero picks sit within ±1pp of the EV floor or ±0.05 of the final floor under any variant — no pick hangs on a bucket edge.

### 6.4 Pool churn — the only channel, and it is unmeasurable for selection

The research pool is the top 100 by post-insider `base_score` after risk gates + share-class dedupe; faithful reconstruction reproduces the recorded S0 pool **100/100**. Under the variants the pool churns substantially — but entrants were never researched, so they carry no EV/dep/cont and no spells row; their selection impact **cannot be replayed**. Churn is reported as a finding, with no spread credit claimed.

| Variant | Enter pool | Leave pool | S0 pick pushed OUT of the pool |
|---|---|---|---|
| S1 | 15 (AME, CATY, CHCO, CLH, DD, EPD, FCF, GEF-B, LTH, OVV, SFNC, WAB, WELL, WSFS, WTS) | 15 (incl. AMRX, EXEL, **PEB**) | **PEB** (rank 26 → 109; Oct 30→Nov 6 ret −1.9%) |
| S2 | 7 (CATY, CHCO, CLH, DD, OVV, VOYA, WAB) | 7 (incl. AMRX, **NTAP**) | **NTAP** (rank 95 → 116; ret **−15.1%**, MAE ≤ −15% = TRUE — the worst S0 pick) |

S2's pool effect removes the single worst S0 pick; S1's removes a mild loser. Both are **suggestive, not evidence**: the replacement picks are unknown and no counterfactual spread can be computed.

### 6.5 Nov 6 counterfactual (A5) — re-execution PASSES; no within-pool pick changes

S1 and S2 change no Nov 6 stock picks (10/10 identical); min `final_score` among the 25 EV-passers is 0.255 (S1) / 0.372 (S2) — the floor is slack again. Pool churn: S1 18 enter / 18 leave, pushing out Nov 6 picks **ADP** (rank 48 → 130) and **FTDR** (70 → 182); S2 5 enter / 5 leave, no pick pushed out. Nov 6 entries carry 0-day outcomes — **no outcome comparison is made; consistency check only.**

### 6.6 Split-half (A3) — CANNOT be performed for S-variants

Scorer replays are possible **only for Oct 30 and Nov 6** (the only dates with recoverable components). There are no recoverable components for any Oct 5–29 entry. The A3 check as specified — rank on Oct 5–20, validate on Oct 21–30 — **cannot be performed**. S1 and S2 are therefore **ineligible for the ship set on the available evidence**: A3 exists precisely to stop a one-day, one-regime result from shipping, and the S-variants have exactly one replayable outcome day (Oct 30) and one consistency day with no outcomes (Nov 6). What would make them eligible: re-execution of additional October dates (recovering `z_*` per date from that date's cache) to build a multi-day replay, then the A3 split-half.

### 6.7 Overfit verdicts — scorer

| Variant | Verdict | Reasons |
|---|---|---|
| S1 | **HIGH** | Single replayable outcome day; one tape regime; zero within-pool pick changes, so the only effect is pool churn, which cannot be validated; `z_implied` rests on 109 observed values with a +5.1σ outlier (ORKA +61%) and 517/626 stocks at exactly 0; requires a pipeline change (attach implied upside pre-scoring) whose production behaviour is untested |
| S2 | **MEDIUM** | Same single-day / single-regime / no-split-half limits and zero demonstrated pick improvement — but no new data dependency (pure weight change), a bounded round doubling (5%→10%) funded from momentum, and its one observable effect (NTAP, the −15.1% pick, leaving the pool) points the right way. Suggestive, not validated |
| S3 | — | Not computed (gated out by the combination rule); no verdict possible |

**Scorer recommendation: S1 — DO NOT SHIP. S2 — DO NOT SHIP (yet).** S2 is the first candidate for a multi-day scorer replay (re-execute additional October dates, then A3 split-half) before any ship decision. S1 is a live idea only if a multi-day replay shows the pool churn systematically removing losers *and* the entrants surviving assessment — neither is observable here.

---

## 6. ETF leg (Addendum B)

**Replay status: REPLAYED on the frame window** — 6 canonical daily frames (Oct 30, Nov 2–6), 100 ETFs each, 600 ETF rows. F0 selection replication reproduces the actual ETF picks **exactly on all 6 days** (and the actual stock picks exactly on all 6 days). `est_parts()` re-implementation reproduces recorded `est_next_1y` with max abs err 0.0 over all 600 ETF rows. Research-excluded rows (3–7/day) are ineligible in every variant. Stock universe verified untouched (F-variants change ETF EV only). Tape context: VOO fell −2.71% Oct 30→Nov 6, so all cohorts are negative in absolute terms and the VOO-relative mean is the informative metric; MAE ≤ −15% is 0% for every cohort (no deep loss can show in ≤5 trading days) — uninformative, reported for completeness.

### 7.1 B1 evidence re-verification

**Oct 30 diagnosis — MISMATCH (material).**

| Claim (B1) | Verified (final frame `frame_20261030_180008_daily.json`, 210 rows) | Verdict |
|---|---|---|
| 98 scored ETF candidates | **100 ETF rows** in frame; 100 candidates in bundle `research_bundle_20261030_133527_VGT.json` | Mismatch |
| Median ETF EV −2.8% | **Median EV −1.96%** (spells.csv agrees) | Mismatch |
| Exactly 1 ETF ≥ +3% (VFLO +12.4%) | **4 ETFs ≥ +3%:** VFLO +12.37%, JQUA +4.23%, DIVB +3.95%, FELV +3.27% — all four were **picked** Oct 30 | Mismatch |
| Median ETF upside +9.4% vs stock +26.0% | Dampened upside: ETF **+9.30%**, stock **+25.97%**; raw `ret_6m_skip1m`: ETF +9.30%, stock +26.95% | Stock exact; ETF 0.1pp low |
| (implied) few ETFs clear the floor | 22 more ETFs sit 0–+3% | Directionally true, count understated |

No subset of the Oct 30 frame reproduces 98 / −2.8% / 1 (tested: excl. research-excluded n=96, median −1.93%, 4 ≥+3%; stability A/B n=96; final_score ≥ 0 n=95; combined gates n=91, −1.75%). `rejected_ledger.jsonl` Oct 30 has 100 ETF events. **Most likely explanation:** B1's Oct 30 numbers were computed on an **intermediate same-day run**, not the final published frame — DIVB, FELV and JQUA were rejected in the ledger (intermediate run) and picked in the final 18:00 frame as assessments moved between runs. The final frame is the authoritative published record and is what this replay uses.
**What survives:** the structural diagnosis. Median ETF EV is still negative, the upside gap is still ~3× (ETF +9.3% vs stock +26.0%), and the floor still clears few ETFs (4 of 100). But the scarcity claim is overstated against the final frame: **4 ETFs cleared, not 1, and the page picked 4 ETFs on Oct 30.**

**Nov 4 — MATCH.** 100 researched ETFs; median EV **−2.06%** (≈ −2.1% claimed); exactly 2 ≥ +3% (VFLO, JQUA); 17 more at 0–+3%.

**DIVB sensitivity exhibit — MATCH, mechanism documented.** Nov 3: EV +3.57% (picked) → Nov 4: +2.79% (rejected), with identical continuation (0.60), confidence (0.60), dep (0.30 / capped 0.15), downside (10.00%). The −0.78pp flip is ~2/3 market-input move (6m-skip window rolled: upside 21.32% → 19.50%) and ~1/3 character swing (+0.112 → −0.038). DIVB re-entered as a pick Nov 6 at +3.73% — a borderline-name wobble, not a thesis change.

### 7.2 F-variant definitions (one round parameterization each)

- **F0 (status quo):** EV as recorded; +3% floor; gates/caps as §2.
- **F1 — vol-scaled upside (ETFs only):** upside′ = upside × clip(median stock-universe vol60 / this ETF's vol60, 1.0, 2.0); median stock vol60 per day 0.230–0.249. EV recomputed via `est_parts` with upside′; +3% floor re-applied.
- **F2 — ETF persistence band:** day-by-day over the 6 frames. An ETF picked on day T stays pick-eligible while EV ≥ **+1%**, all other gates intact (stability, strikes < 2, dep veto, final_score ≥ 0, category cap 2, not research-excluded). Entry still requires EV ≥ +3%. The category cap can still evict an incumbent (it does on Nov 6).
- **F3 — benchmark screen:** pick-eligible iff the ETF beats VOO on **both** ret_1y and ret_6m **and** its maxdd is no worse than VOO's. EV ranks the screened-in set (top 10, gates/caps/dedupe apply); the +3% floor does **not** apply to screened-in ETFs. An eligibility screen, not a lowered floor — no benchmark-relative EV anywhere. VOO per frame date: ret_1y +12.07/+11.30/+12.96/+14.46/+10.61/+10.84%; ret_6m +5.82/+4.48/+3.35/+5.37/−0.07/+0.02%; maxdd −8.90% every day.

### 7.3 Results

**ETF pick counts per day (F0 = 22 spells total):**

| Date | F0 | F1 | F2 | F3 |
|---|---|---|---|---|
| Oct 30 | 4 — VFLO JQUA DIVB FELV | 8 | 4 | 10 |
| Nov 2 | 4 — VFLO JQUA DIVB FELV | 6 | 4 | 10 |
| Nov 3 | 4 — VFLO JQUA FELV DIVB | 10 | 4 | 10 |
| Nov 4 | 2 — VFLO JQUA | 6 | 4 | 10 |
| Nov 5 | 2 — VFLO JQUA | 5 | 4 | 10 |
| Nov 6 | 6 — VFLO DSTL DIVB JQUA IHF XHS | 7 | 6 | 10 |
| **Total** | **22** | **42 (+24)** | **26 (+4)** | **60 (+40)** |

**Expanded-cohort quality — additional picks vs F0 picks vs ETF control** (spell returns to Nov 6 close; rel = VOO-relative):

| Cohort | n | Mean ret | Median ret | % positive | Mean rel (VOO) | MAE ≤ −15% |
|---|---|---|---|---|---|---|
| F0 ETF picks | 22 | −1.55% | −0.96% | 0% | +0.15% | 0% |
| ETF control (evaluated, not picked) | 578 | −1.46% | −1.22% | 8.1% | +0.42% | 0% |
| **F1 additional** | 24 | **−1.36%** | −2.01% | 12.5% | **+0.75%** | 0% |
| **F2 additional** | 4 | −1.79% | −1.93% | 0% | +0.10% | 0% |
| **F3 additional** | 40 | −1.88% | −2.13% | 7.5% | −0.00% | 0% |

- **F1:** mixed — better than both F0 picks and the control on mean return and mean-relative (+0.75% vs +0.15% / +0.42%), worse than both on median (−2.01% vs −0.96% / −1.22%). The mean-relative edge is the only positive quality signal in the exercise, but it rests on 24 spells over ≤5 trading days with a worse median — noise, not a finding.
- **F2:** loses to the control on **every** metric. The 4 spells are the Nov 4–5 DIVB/FELV holds — the held names kept falling into a −4% tape. By the pre-registered B3 criterion ("more picks that lose to the control = failure"), **F2 fails on quality**, on n=4.
- **F3:** loses to the control on **every** metric. **F3 fails on quality.**

**Day-over-day pick-membership churn (5 transitions):**

| Variant | Total churn (entries + exits) | Mean / transition | Worst transition |
|---|---|---|---|
| F0 | **6** | 1.2 | Nov 5→6: 4 (DIVB DSTL IHF XHS enter) |
| F1 | **31** | 6.2 | 8, 8, 8 on the first three transitions |
| F2 | **4** | 0.8 | Nov 5→6: 4 (DSTL IHF XHS enter, FELV exits) |
| F3 | **34** | 6.8 | Nov 4→5: 10 |

F1 and F3 churn ~5× F0 — they add picks **without** cutting churn, so both **fail Dan's second requirement outright**. F2 is the only variant that cuts churn (4 vs 6), removing the DIVB Nov 4 exit / Nov 6 re-entry round-trip. (F2's Nov 6 FELV exit is not an EV<+1% exit: FELV was persistence-eligible at +2.64% but was the third Large Value name and the category cap evicted it.)

**Floor-crossing counts and Nov 6 counterfactual:** F1 up-crosses (EV′ ≥ +3% while EV < +3%): 149 total across the 6 days; down-crosses 0. But most up-crossers never get picked: Oct 30 has 34 floor+gate passers, 23 of them Large Value, so the category cap binds hard and only 8 are picked. F1's factor distribution explains why: the **2.0 cap binds for 68/59/64/64/36/35 of 100 ETFs** per day (median factor 2.00 Oct 30–Nov 4), while the 1.0 floor binds for only 1–2. Median ETF vol60 (~0.11–0.13) is roughly half median stock vol60 (~0.24), so F1 as specified is effectively **"double most ETFs' upside"**, not a differentiated vol adjustment — and it then concentrates the survivors in Large Value until the cap stops it. (Nov 2 F1 even picks **VOO itself**.) F2 persistence-held: DIVB + FELV on Nov 4 and Nov 5 only. F3 picks with EV < +3%: 40 total — all of F3's additional picks; the screen admits 52–65 ETFs/day.
**Nov 6 counterfactual (F0: DIVB DSTL IHF JQUA VFLO XHS):** F1 enters COWZ/FELV/IUS, leaves DIVB/DSTL (7 picks). F2 identical to F0 (6 picks). F3 enters CALF/COWZ/DBMF/FTXH/IWN/MKTN, leaves IHF/XHS (10 picks).

**F3 regime note:** F3's selectivity depends on VOO's own trailing returns. On Nov 5–6 VOO's ret_6m is ≈ 0% (−0.07% / +0.02%), so the 6-month leg admits nearly every ETF with a positive 6m run and screened-in jumps to 65/100. The screen is weakest exactly when the benchmark is flat/down — the regime where a benchmark screen is most wanted.

**Split-half (A3) — not replayable for any F-variant.** F1 needs vol60, F2 needs stability/strikes/final_score per candidate per day, F3 needs ret_1y — none of these exist in `spells.csv` pre-Oct-30. All three variants are frame-only (Oct 30 + Nov 2–6). Within-window consistency split (Oct 30 vs Nov 2–6 additional-pick mean-rel): F1 +1.36% / +0.63% (same sign, weak); F3 +0.34% / −0.06% (sign flips); F2 Nov-only. November outcomes are immature — consistency check only.

### 7.4 Overfit verdicts — ETF

| Variant | Verdict | Reasons |
|---|---|---|
| F1 | **MEDIUM-HIGH** | Parameterization was pre-declared (not fitted), but its effective behavior is an artifact of the 2.0 cap interacting with the actual vol distribution: the cap binds for the majority of ETFs, so the "vol scaling" is near-uniform doubling, and picks pile into Large Value until the category cap binds. The one positive quality signal (mean-rel +0.75%) is small-n, short-window, contradicted by the median; churn is 5× F0 |
| F2 | **LOW** | One round parameter (+1%), pre-declared, mechanism transparent, no fitted threshold; churn improves as designed. The failure is evidential, not overfit: n=4 additional spells, all in a 2-day down-tape, lose to control |
| F3 | **MEDIUM** | Pre-declared screen, no fitted thresholds, but selectivity is regime-dependent in a way the spec didn't anticipate (6m leg goes slack when VOO's own 6m ≈ 0), and it fills all 10 slots with names that lose to the control on every metric while churning 5× F0 |

### 7.5 ETF verdict: **no replay-supported ETF fix**

- **F1 — do not ship.** Fails churn (31 vs F0's 6); quality signal mixed and small-n; the 2.0 cap saturates for most ETFs, so the variant doesn't do what its name says.
- **F2 — do not ship (re-test at Nov 30).** Passes churn (4 vs 6) and is the only variant whose mechanism works as designed, but its additional picks lose to the control on every quality metric (n=4, 2 holding days, immature window). It is the Nov 30 re-test candidate, not a ship line.
- **F3 — do not ship.** Fails churn (34 vs 6) and fails quality vs control on every metric; selectivity collapses when VOO's 6m return is near zero.
- **F-combinations:** not tested — the rule requires each component to pass alone first, and none does.

The ETF-leg problem (median ETF EV negative; upside ~⅓ of stocks') is confirmed real, but none of the three bounded fixes repairs it on replayed evidence. The B1 Oct 30 scarcity figures that motivated the urgency were also overstated against the final frame (4 ETFs cleared, not 1).

---

## 7. Proposal ledger (every candidate tested)

Total candidates tested: **13** — 8 EV (E0 status quo + E1–E4 + 3 combinations), 3 scorer (S1, S2; S3 gated out, not computed), 3 ETF (F1–F3; no F-combinations — rule requires each component to pass alone first). Counting the status quo as a candidate, the ledger covers E0–E4 + 3 E-combos, S0–S2 (+S3 not computed), F0–F3.

| # | Candidate | Layer | Replay verdict | Fate | Reason (one line) |
|---|---|---|---|---|---|
| 1 | E0 status quo | EV | REPLAYED | Baseline | E0 baseline: Oct spread −1.02pp ALL (+1.92pp stocks / −4.17pp ETFs); stock spread does not hold across halves |
| 2 | E1 valuation dampener on upside | EV | REPLAYED | **REJECT** | Zero October effect; only demotes 4 never-picked HMs below the floor |
| 3 | E2 implied-upside blend (+0.10) | EV | REPLAYED | **REJECT** | Negative in both halves and November; both October swaps replace mild losers with much worse losers (PK→AVT, PEB→RELY) |
| 4 | E3 character clamp [−1,1] | EV | REPLAYED | **DEFER** | Untestable — only 4 rows in 2,420 have \|character\|>1, none near selection; re-test Nov 30 |
| 5 | E4 conf cap 0.75 | EV | REPLAYED | **DEFER** | No October effect at this dose despite a real calibration pattern (12%+ bucket worst); re-test Nov 30 |
| 6 | E3+E4 | EV | REPLAYED | **DEFER** | = E4 exactly |
| 7 | E1+E3 | EV | REPLAYED | **REJECT** | = E1 exactly |
| 8 | E2+E3 | EV | REPLAYED | **REJECT** | = E2 exactly |
| 9 | S0 status quo | Scorer | REPLAYED (stocks Oct 30 + Nov 6) | Baseline | `final_score` is a floor, not a ranking — slack ~0.24; scorer can only move picks via pool membership |
| 10 | S1 implied-upside z (+0.10) | Scorer | **UNREPLAYED as a combined claim; REPLAYED for Oct 30 + Nov 6 stocks only** | **REJECT (do not ship)** | Zero within-pool pick changes on the only outcome day; HIGH overfit; A3 split-half impossible |
| 11 | S2 multiples 5%→10% | Scorer | **UNREPLAYED as a combined claim; REPLAYED for Oct 30 + Nov 6 stocks only** | **DEFER** | Zero within-pool pick changes; MEDIUM overfit; pool churn removes the worst S0 pick (NTAP −15.1%) — suggestive only; first candidate for a multi-day scorer replay |
| 12 | S3 = S1+S2 | Scorer | Not computed | — | Gated out by the combination rule (neither component passes alone) |
| 13 | F0 status quo | ETF | REPLAYED | Baseline | 22 ETF pick spells; median ETF EV negative; upside ~⅓ of stocks' |
| 14 | F1 vol-scaled upside (ETFs only) | ETF | REPLAYED | **REJECT** | Churn 31 vs F0's 6; mixed small-n quality; 2.0 cap saturates for most ETFs ("double most ETFs' upside") |
| 15 | F2 ETF persistence band (+1%) | ETF | REPLAYED | **DEFER (re-test Nov 30)** | Only variant that cuts churn (4 vs 6) and whose mechanism works as designed — but its 4 additional spells lose to the control on every metric (n=4, immature window) |
| 16 | F3 benchmark screen (VOO) | ETF | REPLAYED | **REJECT** | Churn 34 vs 6; loses to control on every metric; selectivity collapses when VOO's 6m ≈ 0% |

Note: the ledger counts 16 rows because the three status quos (E0/S0/F0) are listed alongside the 13 tested variants/combinations. **Total candidates tested = 13** (4 EV variants + 3 EV combos + 2 scorer variants + 3 ETF variants + S3 gated out untested). No candidate of any layer passes the ship-set bar.

---

## 8. Ship set

**Ship set: NONE — ship nothing.**

No candidate passes the pre-registered bar (REPLAYED + improves pick-vs-control spread on October data + does not worsen the MAE ≤ −15% rate + not HIGH overfit risk, ≤2 moving parts across both layers, at most ONE F-variant as its own line). The failure mode is lack of benefit, not overfitting: every E-variant either changes nothing (E1/E3/E4) or changes picks for the worse (E2); both S-variants change zero picks within the recorded pool on the only outcome day and cannot be split-half validated; all three F-variants fail the B3 bar.

There are therefore **no judged-by lines attached to a shipment** (nothing ships). The deferred/re-test items below carry Nov 30 pre-registration instead, so the Nov 30 monthly audit can score them before narrative:

- **E3 (character clamp) — judged by: Nov 30 monthly audit, metric: October+November pick-vs-control spread change under E3 vs E0 on a wider window with identifying variation (|character|>1 rows near selection), success: spread improves ≥ +0.50pp with no universe worsening > 0.25pp.**
- **E4 (conf cap 0.75) — judged by: Nov 30 monthly audit, metric: pick-vs-control spread change under E4 vs E0, plus the 12%+ EV bucket's MAE-bad rate, success: spread improves ≥ +0.50pp and the 12%+ bucket's MAE-bad rate falls.**
- **S2 (multiples 5%→10%) — judged by: Nov 30 monthly audit, metric: multi-day scorer replay (re-executed October dates, A3 split-half) pick-vs-control spread change under S2 vs S0, success: spread improves in both halves with no universe worsening.**
- **F2 (ETF persistence band +1%) — judged by: Nov 30 monthly audit, metric: persistence-held spells' VOO-relative mean vs the ETF control, success: beats control with n ≥ 20 held spells, and day-over-day churn stays ≤ F0's.**
- **S1 (implied-upside z) — no judged-by line:** rejected, not deferred; it would need a multi-day replay showing pool churn systematically removing losers *and* entrants surviving assessment before it can re-enter as a candidate.

**Nothing has been implemented.** The methodology freeze otherwise remains in force until the Nov 30 monthly audit. Any shipment requires Dan's approval in chat, after this report.

---
## 9. Out-of-scope notes (for the Nov 30 monthly-audit docket — observations only, no proposals here)

These touch gates, floor, veto, caps, research, watchlist, or the freeze — outside this audit's bounded candidate set. They are recorded for Nov 30, not decided here.

1. **B1 Oct 30 figures came from an intermediate run** (§6.1). The Oct 30 daily ran multiple times; the final 18:00 frame is authoritative. Any future diagnosis must state which run its numbers come from. (Process note, not a formula change.)
2. **`z_expense` is a constant** (§1.5) — the ETF expense weight differentiates nothing. Instrumentation-only fix candidate: read `netExpenseRatio` (or verify the `annualReportNetExpenseRatio` fallback actually populates). Nov 30 to decide; it is an ETF-scorer input change, so it needs the same replay discipline as S-variants.
3. **Partial-fetch cache overwrite destroyed Oct 30 run-1 ETF inputs** (§1.3). Instrumentation-only fix candidate: version cache files per run instead of overwriting in place, so a recorded day's inputs survive re-execution. Nov 30 to decide.
4. **`final_score` uses RAW dep while EV, veto, and strikes use `dep_used`** (§1.2). A diversified ETF with raw dep 0.30 (capped to 0.15 for EV) is still docked the full 0.30 in `final_score`. Whether that asymmetry is intended is a Nov 30 question — it touches the scorer/EV boundary and was not in this audit's bounded candidate set.
5. **Insider term is unwinsorised** (§1.4): two Oct 30 stocks carry z_insider ≈ −6.9, moving base_score by ≈ −0.41 — larger than most scorer weights' entire contribution. Whether to winsorise is a Nov 30 question.
6. **High-EV rejects averaged −10.48%** (§3.2): the 28 October rejects clearing +3% but evicted (mostly ETF-overlap dedupe) fell hard. The overlap/dedupe eviction looks like it is removing genuinely bad names, not just duplicates. Nov 30 may want the dedupe-eviction cohort as a control-group check.
7. **Gate efficacy in October is a 7-day, single-frame sample** (§3.5): risk-strike counts do not separate winners from losers (0.70 vs 0.67, n=172 Oct rejects); stability grades do not separate pick winners from losers (A 74% vs 72%). The Nov 30 audit — with a full month of frames — is where gate efficacy can be judged, not here.
8. **Earnings-gap downside floor never binds** Oct 30–Nov 6 (§1.1). No conclusion either way; the floor's binding frequency over a longer window is a Nov 30 instrumentation question.
9. **`dep_capped=True` counts drop sharply Nov 5–6** (52 → 8 → 6), unexplained in frame data (§2). Flagged for Nov 30's data-gaps section.
10. **Pool coverage remains the weakest stage** (standing Nov 30 item): base-score rank and EV disagree ~50% of the time; the S-variants' only observable effect is pool churn, which this audit cannot validate (§5.4). A multi-day scorer replay (re-executing additional October dates to recover `z_*`, then A3 split-half) is the precondition for any future scorer ship decision.
11. **Watchlist:** untouched by this audit; no findings.
12. **Freeze:** re-instated Oct 30, still in force. This audit is report-first; nothing in it ships without Dan's approval in chat, and every ship-set candidate (none here) would be judged by the Nov 30 audit.

---

## 10. Files and reproducibility

**Report:** `~/workspace/stock-screener/special_audit_2026-11-06.md` · **Published copy:** `~/workspace/mint-chip-pages/audits/special_audit_2026-11-06.md`
**Work dir:** `~/workspace/stock-screener/special_audit_2026-11-06_work/`

| File | Contents |
|---|---|
| `spells.csv` / `spells.json` (2,420 rows) | Ticker-date spells Oct 5–Nov 6, recorded components + outcomes to Nov 6 close |
| `price_panel.csv` | yfinance price panel behind the outcomes |
| `data_gaps.md`, `data_assembly_notes.md`, `build_spells.py`, `build_outcomes.py` | Step 1 assembly + missingness |
| `config_extraction.md` (39KB) | Full config extraction (note §1.4 correction: insider term lives in `main()`) |
| `replay_preflight.md`, `replay_reproduce.py`, `replay_reproduction_results.json` | A2 preflight (EV PASS / scorer FAIL / final_score PASS) |
| `candidate_specs.md` | Exact one-round forms of every candidate (E1–E4, S1–S3, F1–F3) |
| `reexecution_report.md`, `reexecute_scorer_oct30.py`, `scorer_components_oct30_stocks_verified.csv`, `scorer_components_oct30_etfs_second_run.csv`, `reexecution_results_nov6.json` | Scorer re-execution rescue |
| `findings.md`, `findings_tables.csv` (250 rows), `compute_findings.py` | Step 2 findings |
| `ev_replay_results.md`, `ev_replays.csv` (19,360 rows = 2,420 spells × 8 variants), `ev_calibration.md`, `replication_table.csv`, `ev_replay_fixed.py`, `ev_replay_fixed_stats.py`, `ev_fixed_stats.json` | EV replays (fixed baseline; first-draft `_stage2_replay.csv` retained for audit trail, numbers withdrawn) |
| `scorer_replay_results.md`, `scorer_replays.csv` (3,774 rows), `scorer_replay_oct30.py`, `scorer_replay_nov6.py` | Scorer replays |
| `etf_results.md`, `etf_variants.csv` (2,400 rows), `etf_replay.py` | ETF F-variant replays |

**Nothing has been implemented.** No production code was touched (`screener.py` sha256 re-verified unchanged after every agent's work). No schedules were edited. The freeze otherwise remains in force.
