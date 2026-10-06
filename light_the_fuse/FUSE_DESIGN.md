# Light the Fuse — pipeline design (draft v0.2, tested 2026-10-05)

Goal: find stocks *before* exceptional runs, while keeping Mint's downside discipline.
Status: prototype tested on 502 S&P 500 names. NOT production. No schedule, no page.

## What the research says (from momentum-without-crashes report 2026-10-05)
- "Exceptional gains with zero downside" is not supported by history. Frame as
  *early-cycle entry + defined invalidation*, never a guarantee.
- Lead with **fundamental momentum** (earnings surprise/SUE, earnings acceleration,
  analyst estimate-revision breadth), confirm with price:
  - nearness to 52-week high, breakout with volume, canonical 12-1 momentum (skip latest month).
- Early-cycle candidates: low-attention/low-turnover winners; extreme volume/attention = late-stage glamour.
- Require profitability/quality. Regime gate: cut exposure in panic states
  (VIX high / market in drawdown); do NOT auto-lever in calm states.
- Crashes cluster in sharp rebounds after bear markets (Daniel & Moskowitz) —
  distressed losers snap back. Fuse must avoid buying post-crash junk rallies.
- Pure price breakouts have decayed; require a real fundamental catalyst.

## Prototype gates (tested 2026-10-05, /tmp/fuse.py)
Price/technical: price ≥$5; 52w-high nearness ≥0.85; 12-1 momentum >0;
price > 50-day MA; 20d/60d volume ratio ≥1.0; volume-mania proxy <3.5;
60d ann. vol ≤80%; 1y maxDD ≥−40%.
Fundamental: mcap ≥$2B; ROE >5%; pass if SUE >3% OR EPS accel >0 OR rev growth >15%.
Regime: OPEN when SPY trailing 24m return >0 AND VIX <25.
Score: 35% z(SUE) + 20% z(EPS accel) + 20% z(52w nearness) + 15% z(vol ratio) + 10% z(12-1 mom).

## Test results 2026-10-05 (regime OPEN: SPY 24m +39.6%, VIX 15.5)
41 stage-one passers → 33 fundamental passers out of 502.
Top raw: PSX, GOOGL, MPC, ILMN, VLO, HPE, DELL, CVX, EXPD, WSM, MTD, TMO, WST, JCI, ADM...

## Known flaws to fix before production
1. EPS acceleration compares sequential quarterly EPS growth (nonsense values:
   PSX +1900%, MPC +1034%). Replace with YoY quarterly EPS/revenue acceleration,
   winsorize SUE/accel inputs.
2. No estimate-revision breadth yet (literature's best early signal) — add where
   obtainable without API keys.
3. No true early-cycle gate: DELL (+259% 12-1), HPE (+123%), ILMN (+119%),
   VLO (+132%) already ran. Add exclusion/penalty for 12-1 >~80-100% and for
   extreme attention/volume gaps.
4. S&P-500-only test universe biases to large caps. Production should reuse Mint's
   full US universe with mcap/liquidity floors (not S&P-only).
5. Energy/refiners (PSX, MPC, VLO, CVX, DVN, OXY, COP) are commodity/geopolitical
   beneficiaries — conflict with the no-war/no-commodity-spike mandate. Hard
   event-dependence research, likely exclusion.
6. Add per-pick invalidation (support/ATR-based tripwire), max initial drawdown
   estimate, panic-state gate, asymmetric vol scaling.
7. Every finalist needs current-news catalyst research: catalyst already occurred
   (good — post-earnings drift) vs future binary catalyst (bad — don't buy into it).

## RESEARCHER INSTRUCTIONS (frozen 2026-10-05)

The quant screen finds the set; the researcher kills the false positives.
Score these formally for every finalist — they are inputs, not narration.

### Guidance vs consensus (-2 to +2)
The closest thing to analyst estimate revisions available without API keys.
- +2: guided materially above consensus (>~10% on revenue or EPS) — SNX 2026-10-05.
- +1: guided above consensus, or strong analyst revision momentum without a
  formal raise (UBS lifting EXPD estimates above consensus).
- 0: reaffirmed guidance, in-line.
- -1: guided below consensus on any key metric → thesis fails → VETO.
- -2: guided down / withdrew guidance → VETO.
A VETO here removes the name from the pick list regardless of quant score.

### Event dependence (reuse Mint's scale)
Score 0..1 like Mint's researcher assessment; veto at >=0.7.
Occurred beat-and-raise = positive (post-earnings drift). Upcoming binary
event within ~30 days (earnings, spin-off, FDA, trial readout) = veto or
heavy penalty. One-time items inflating the surprise (tariff refunds,
litigation gains — the DDS case) = the surprise is junk; reject.

### Sector breadth
Note whether peers are confirming (AVT + SNX + ARW all beating = cycle
turn, stronger than a lone beat). Names on the same theme are ONE bet, not
independent picks — max two per sector in any presented list.

### Piotroski boundary
The F>=5 hard gate applies to NON-FINANCIALS with complete statements only.
Financials/insurers (different statement structures — RGA unscored, AAMI
scored 4 on bank-style ratios that mean something different there) get
fail-soft: flag "unscored/manual review", researcher verifies financial
health by hand. Never let a missing or miscalibrated score silently exclude
a sector.

### Anchoring guardrail
Quant scores, streaks, and flags are context, not verdicts. A high score
does not raise conviction by itself; a low Piotroski on an early-cycle
name does not end the conversation by itself. Write down what would change
your mind for each name.

## v3 test results 2026-10-05 (the four additions)
Additions: (1) beat streak — consecutive positive surprises, trailing 8,
as 7th score factor at 0.15 weight (v2 weights rescaled to 0.85, NOT re-tuned);
(2) Piotroski F-score gate ≥5; (3) earnings-proximity veto (projected next
earnings ≤14d out); (4) capped-extreme SUE flag (>100% → researcher review).
Guidance-vs-consensus + event dependence scored in the researcher stage
(formal inputs, not narration).

- 125 technical → 56 fundamental passers (Piotroski dropped 6 scored <5:
  AVT, SHOO, CNXN, ADM, WSM, AES; earnings veto dropped 0).
- v3.1 correction: Piotroski None changed from fail-CLOSED to fail-SOFT
  (flag "unscored", researcher verifies). Reason: insurers/financials use
  different statement structures — fail-closed was silently excluding a whole
  sector (RGA would have been lost). The silent-exclusion bug class again.
- AVT DELIBERATELY EXCLUDED by the gate (F=4 < 5). Not tuned around: of two
  distributor names on the same cycle turn, the system prefers SNX (F=7) over
  AVT (F=4). That is the don't-go-negative rule working as designed.
- GOOGL still ranks #1 on capped artifact values — the !!EXTREME-SUE flag
  fires, researcher excludes. The formula cannot be trusted unsupervised
  at the extremes.

### Researcher verdicts (v3.1, guidance scored -2..+2)
- SNX: guidance +2 (Q4 rev guide 13.5% above consensus, EPS guide above).
  Pio 7, streak 6, 79d to earnings. dep 0.2. THE pick — freshest ignition.
  Invalidation ~$246 (-11%).
- EXPD: guidance +1 (no formal raise; UBS target to $220 + Q3 est above
  consensus Oct 2). Streak 8, Pio 7. dep 0.4 (freight/trade). Tightest chart.
  Invalidation ~$181 (-7%).
- AMRX: guidance +1 (rev guide +1.6%, EPS slightly raised, EBITDA above
  consensus — modest). Streak 6, Pio 7. dep 0.4. ASTERISK: D/E 77.65,
  P/E 41, PEG 2.36 — extreme leverage + full valuation vs the
  don't-go-negative mandate. Included on momentum, ranked third, eyes open.
  Invalidation ~$17.40 (-15%).
- NTCT: guidance 0 (raised FY26 in Q2, but REAFFIRMED FY27 in latest quarter;
  $10-15M of orders pulled forward — timing, not demand). Streak 8, Pio 7,
  beta 0.64, $668M cash, no debt. Honorable mention. Invalidation ~$38 (-8%).
- LGND: guidance +1 (raised EPS above consensus) but stock FELL post-beat —
  expectations priced in. Streak now only 1 (hurts v3 score). Honorable mention.
- SECTOR BREADTH: AVT + SNX + ARW all beating = distribution cycle turn
  confirmed (peer confirmation strengthens the theme; the three are one bet,
  not three — max 2 per sector presentation rule applies).
- REJECTED: DDS (the +48.6% "surprise" was a $37.2M one-time tariff refund +
  $104M litigation gain; sales flat, stock fell 4%, analysts 3 Hold/2 Sell
  with avg target BELOW price, earnings expected -8.6% next year — the false
  positive the researcher stage exists to kill); RGA (+37% beat was lumpy
  insurance claims experience off a weak base, no guidance culture, earnings
  expected down next year — wrong kind of earnings for a momentum thesis;
  the unscored-flag workflow worked: reviewed, rejected on thesis fit);
  HRMY (single-drug binary, dep 0.8); MSGS (championship one-off + Oct 26
  spin-off, dep 0.8).
- 2,124 priced → 125 technical passers → 62 fundamental passers (all with SUE,
  55 with EPS accel). Dedupe: none needed.
- Filters added in v2: US-domiciled only (info country), Energy AND Basic
  Materials excluded (commodity beneficiaries vs mandate), preferred shares
  (-P suffix) skipped, religion keywords, 12-1 momentum hard-capped at +80%.
- BUG FOUND + FIXED mid-test: a tz-aware earnings-date filter silently dropped
  ALL SUE/accel values (v1's unfiltered version worked). Fixed; v2 rerun clean.

### Researched verdicts (v2)
- SNX (TD Synnex, $23B): FRESHEST IGNITION. Sept 24 Q3: EPS $5.68 vs $4.70
  (+20.8%), rev +37.7%, guided Q4 rev 13.5% ABOVE consensus; new 52w high;
  CEO cites enterprise AI adoption + data-center modernization; PEG 0.79,
  fwd P/E 13.7. +6.9% above 50d — not extended. Invalidation ~$246 (-11%).
- AVT (Avnet, $8.7B): TWO straight beat-and-raises (Q1 +12% EPS beat, Q2 +29%);
  guided Q3 rev 19% above consensus; semiconductor distribution cycle turn;
  9.7x fwd P/E. +13.2% above 50d — warmer. Invalidation ~$90 (-15%).
  Negative FCF (-$308M) is inventory build into the upcycle — watch it.
- LGND (Ligand, $6.1B): royalty model (diversified, not binary); raised FY26
  EPS guide above consensus; XOMA acquisition doubles portfolio to 200+ assets;
  analyst targets raised — BUT stock fell post-beat (lukewarm). Invalidation
  ~$282 (-8%).
- EXPD: carried from v1 (UBS target raise Oct 2). Tightest setup, inval ~$181.
- SHOO (honorable mention): beat + raised FY26 guide, but fashion cyclicality
  and -32% DD history weaken the don't-go-negative case.
- REJECTED: HRMY (single-drug WAKIX biotech, -34% DD, CFO exit — binary);
  MSGS (+84% surprise = Knicks championship playoffs, non-recurring; Rangers
  spin-off Oct 26 = binary event 3 weeks out — event-dependent); GOOGL (SUE
  artifact); GEO/CXW (private prisons — flagged, not listed).
- CONVERGENT VALIDITY: Fuse v2 independently re-surfaced Mint names ARW, VCTR,
  CON (current Mint picks), NTAP, WST, TMO — the two pipelines agree on
  fundamental-momentum quality.

## Researched verdicts 2026-10-05 (v1, S&P 500 — superseded by v2)
- HPE: cleanest ignition. Sept 2 FY26Q3 beat-and-raise, raised FY26 rev guide to
  34-37% AND FY27 framework (13-17% rev, 16-20% EPS growth), $3.5B hyperscaler
  inference contract, Oracle AI-networking expansion, record $7.6B AI backlog,
  estimates revised +24% in a month, Zacks #1 Strong Buy. Caveat: +22% above
  50-day — extended; pullback entry toward $58-60 breakout zone preferred.
- EXPD: +20.8% EPS / +20.7% revenue beat (Aug 4), UBS raised target to $220 on
  Oct 2 and lifted Q3 estimates above consensus; AI-hyperscaler airfreight demand;
  dividend aristocrat; only +4.8% above 50-day — tightest setup. Invalidation
  ~$181 (-7%). Caveat: freight cyclicality; part of Q2 spike was tariff
  front-running, may not repeat.
- NTAP: record quarter (+30% rev, +66% EPS), raised FY26 AND FY27 guidance,
  billings +36%, AI data-infrastructure story, analyst target hikes. Mid-run
  (+15.7% above 50-day), premium P/E, NAND cost pressure flagged.
- TMO: clean beat (+10% rev, +13% adj EPS), raised FY26, biotech end-market
  recovery, +8% reaction, consolidating. Q3 earnings Oct 26 = next catalyst/risk.
  Quality anchor, not an exploder.
- EXCLUDED ILMN: high score but +160% from Oct 2025 lows, −10% single-day drop on
  outlook, China Unreliable Entities List risk, ~50x forward P/E. Fails "don't go
  negative" despite the turnaround story.
- EXCLUDED: DELL (already +259%), refiners (commodity/war), WSM (sold off 4-5%
  post-earnings, below 50-day), GOOGL (+214% SUE = data artifact).
