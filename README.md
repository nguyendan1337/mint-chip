# Mint (Chip)

*Straight from the Mint.*

Finds last year's top performers that beat your benchmark, then picks the
stocks and ETFs most likely to **keep doing well without going negative** —
with downside protection as the first priority. See
[PHILOSOPHY.md](PHILOSOPHY.md) for the investment principles every run serves.

Free data only: yfinance (prices, fundamentals, news) and Google News RSS.
No API keys needed.

**Live pages:** [Mint (Chip)](https://nguyendan1337.github.io/mint-chip/) ·
[Watchlist](https://nguyendan1337.github.io/mint-chip/watchlist.html)

## Repo layout

```
├── index.html, watchlist.html   # the published pages (GitHub Pages serves from root)
├── README.md, PHILOSOPHY.md
├── watchlist.json               # versioned record of the watchlist's tickers
├── code/                        # the pipeline: screener.py, llm_research.py, cache.py, requirements.txt
├── ledgers/                     # thesis_ledger.jsonl, rejected_ledger.jsonl — the permanent outcome record
├── audits/                      # monthly audit reports (audit_report_YYYY-MM.md)
└── light_the_fuse/               # the Light-the-Fuse experiment: design, outcome ledger, prototype
```

## How it runs

A daily job runs the full pipeline after US market close and publishes the
pages. The pipeline has two phases, because the research layer is an agent
(Muse), not an API:

**Phase A — quant screen + research bundle**
```bash
python3 screener.py --benchmark VGT --etf-benchmark VOO --with-llm
```
Pulls the universe (US-listed, ≥$2B market cap, ≥$5 price), keeps names that
beat the benchmark (stocks vs VGT, ETFs vs VOO — the split keeps the ETF leg
from concentrating in tech), applies trash filters (US-only, liquid, no
blowups, no religious-themed securities), scores for continuation + low
post-buy drawdown risk, and writes a research bundle.

**Research (agent)** — reads the bundle, researches each candidate against
current news, writes `llm_outputs.json`: per ticker, `event_dependence`
(0–1), `continuation` (0–1), `confidence` (0–1), risks, and rationale.

**Phase B — apply research, pick, chart**
```bash
python3 screener.py --llm-apply research_bundle_<ts>_VGT.json llm_outputs.json --benchmark VGT --etf-benchmark VOO
```
Reranks by research, applies risk gates (60-day annualized volatility ≤80%,
1-year max drawdown ≥−40%), the +3% expected-value floor, and the
event-dependence veto (≥0.7), then writes the chart: top 10 stocks + top 10
ETFs by expected value, highest first. Empty slots stay empty — filler never
earns a place.

**Watchlist mode** (`--watchlist "TICKER,..."` / `--watchlist-apply`) runs
your own tickers through the same pipeline with no benchmark and no picking:
every name is scored, researched, and rated BUY / HOLD / SELL.

## How the expected value works

A heuristic expected value, **not a prediction**:

```
EV = confidence × (1 + 0.25 × character)
     × (continuation × upside − (1 − continuation) × downside
        − event_dependence × 20%)
```

- **upside** — six-month run *excluding the most recent month* (the academic
  one-month skip), with a mean-reversion dampener: the first 25% counts in
  full, beyond that at half weight, capped at 50%. Recent spikes don't get
  to repeat at full weight.
- **downside** — half the historical 1-year max-drawdown magnitude (floor
  10%), scaled by (1 + drawdown frequency). Depth tells you how bad it can
  get; frequency tells you how often.
- **character** — quality / entry-timing / structure as a z-sum clamped to
  [−2, 2]. Steady compounders get lifted (up to ~1.5×); fragile spikes get
  cut (down to ~0.5×). Momentum and volatility are excluded — the 6-month
  run and max drawdown already cover them.
- **event_dependence × 20%** — haircut for theses riding wars, headlines,
  binary catalysts, or commodity spikes.
- **confidence** — the researcher's confidence shrinks the estimate toward
  zero when headlines are thin or contradictory.

## The accountability loop

- **Thesis ledger** (`ledgers/thesis_ledger.jsonl`): every pick with its
  price, expected value, dep/cont/conf, character, and rationale — plus
  dual-confirmation fields (see Fuse below). Append-only, never rewritten.
- **Rejected ledger** (`ledgers/rejected_ledger.jsonl`): the control group —
  vetoed, excluded, and EV-floor-failed names, so the audit can ask whether
  the nos were right.
- **Daily thesis check**: price-checks every pick from the last 90 days
  (intact / watch at −8% / broken at −15% from pick price); the agent
  re-verifies the news behind watch/broken names.
- **Monthly audit** (`audits/`): measures predictions against realized
  outcomes — calibration, short-horizon downside stats, probability
  calibration, autopsies on broken theses — and *proposes* formula changes
  from the evidence. It proposes; Dan disposes. Weights are never auto-tuned.

## Light the Fuse (experiment)

A separate, manually-run research prototype that hunts stocks *before*
exceptional runs — fundamental momentum first (earnings surprise, beat
streaks, guidance vs consensus), price confirms (52-week-high nearness,
breakout volume). Same don't-go-negative mandate, different lens: Mint
picks 1-year compounders, Fuse looks for fresh ignitions. See
[`light_the_fuse/FUSE_DESIGN.md`](light_the_fuse/FUSE_DESIGN.md). No schedule, no public page —
its outcome ledger is scored by the monthly audit, which is the only
evidence base on which it could ever graduate.

## Storage design

Run logs (~300KB each) are archived as immutable GitHub Release assets (one
per run, tag `run-YYYY-MM-DD-HHMM`) and **never enter git history**. Git
holds only code, the pages, ledgers, audits, and docs — tiny forever.
A monthly janitor enforces this (pack <100MB, no logs in history, release
coverage) and prunes local intermediates older than 120 days.

## Setup notes

```bash
pip3 install --break-system-packages -r requirements.txt
```

The runtime environment is occasionally replaced, which wipes pip packages.
If a run fails with `ModuleNotFoundError`, reinstall from
`requirements.txt` before debugging further.

## Notes

- Not financial advice. Past performance doesn't predict future returns.
- yfinance is free but rate-limited; if you see timeouts, wait and re-run.
