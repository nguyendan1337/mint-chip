# Mint (Chip)

*Straight from the Mint.*

Finds last year's top performers that beat your benchmark, then picks the
stocks and ETFs most likely to **keep doing well without going negative** —
with downside protection as the first priority. See [PHILOSOPHY.md](PHILOSOPHY.md)
for the investment principles every run serves.

Free data only: yfinance (prices, fundamentals, news) and Google News RSS.
No API keys needed.

## The two paths

### 1. Benchmark mode (the main screen)

Two phases, because the research layer is an agent (Muse), not an API:

**Phase A — quant screen + research bundle**
```bash
python3 screener.py --benchmark VGT --with-llm
```
Pulls the universe (top 52-week gainers: US-listed, ≥$2B market cap, ≥$5
price), keeps names that beat the benchmark, applies trash filters (US-only,
liquid, no blowups), scores for continuation + low post-buy drawdown risk,
and writes a research bundle:
`research_bundle_<timestamp>_<BENCH>.json`.

Tip: `--etf-benchmark VOO` runs the ETF leg against a separate benchmark
(VGT stays the stock hurdle) — a broader ETF benchmark diversifies the ETF
candidate pool instead of concentrating it in the stock benchmark's sector.

**Research (agent does this)** — read the bundle, research each candidate
against current news, and write `llm_outputs.json` in the bundle's schema:
per ticker, `event_dependence` (0–1), `continuation` (0–1), `confidence`
(0–1), risks, and rationale.

**Phase B — apply research, pick, chart**
```bash
python3 screener.py --llm-apply research_bundle_<ts>_VGT.json llm_outputs.json --benchmark VGT --etf-benchmark VOO
```
Reranks by research (`final = base_w − w_down·dep + w_up·cont −
w_outlier·outlier_excess·dep`), applies risk gates, the expected-value
floor, and the event-dependence veto, then writes the chart:
`chart_<timestamp>_VGT_llm.html` — top 10 stocks + top 10 ETFs, each sorted
by estimated next-year return, highest first.

Without `--with-llm`, the run uses the rules-based news layer instead and
completes in one step (faster, no research depth).

### 2. Watchlist mode (your own tickers)

No benchmark, no picking, no trash filters — every ticker you list that has
usable price history is scored, researched, and charted, sorted by estimated
next-year return. Foreign-domiciled names are flagged, not dropped. (Trash
filters apply to benchmark mode only.)

```bash
# Phase A: quant + headlines → bundle
python3 screener.py --watchlist "DAC,AYA,DIVB,XBI,HNGE,CDNA,VFLO"

# (research the bundle, write watchlist_outputs.json)

# Phase B: apply research → chart sorted by est. next 1y
python3 screener.py --watchlist-apply watchlist_bundle_<ts>.json watchlist_outputs.json
```

## How the estimated next-year return works

A heuristic expected value, **not a prediction**:

```
est = confidence × (continuation × upside
                    − (1 − continuation) × downside
                    − event_dependence × 20%)
```

- **upside** — last-6-month run with a mean-reversion dampener: the first
  25% counts in full, beyond that at half weight (capped at 50%).
  Monster half-years fade; the estimate must not let a historic run
  repeat at full weight.
- **downside** — half the historical 1-year max-drawdown magnitude
  (floor 10%): if it breaks, you eat a serious but not worst-case loss.
- **event_dependence × 20%** — haircut for how much the run rides fragile
  event drivers (war, headlines, binary catalysts).
- **confidence** — the researcher's confidence shrinks the estimate
  toward zero when headlines are thin or contradictory.

## Key options

| Flag | Default | Meaning |
|---|---|---|
| `--benchmark` | (required) | `SPMO`, `VGT`, `DIVB`, `VFLO` |
| `--min-est` | 0.03 | Expected-value floor: picks must have est ≥ this. Slots stay empty rather than hold filler |
| `--veto-dep` | 0.7 | Event-dependence above this is vetoed from final picks |
| `--max-vol` / `--min-dd` | 0.80 / −0.40 | Absolute risk gates on trailing volatility / max drawdown |
| `--max-per-sector` | 2 | Max stocks per sector |
| `--max-per-etf-category` | 2 | Hard max ETFs per category — a third tech ETF is redundant, not diversifying; empty slots stay empty |
| `--min-score` | 0.0 | Quality floor on the reranked score |
| `--w-down` / `--w-up` / `--w-outlier` | 1.5 / 1.0 / 0.5 | Rerank weights: event-dependence penalty, continuation reward, outlier×event interaction |
| `--n-stocks` / `--n-etfs` | 10 / 10 | Final pick counts |
| `--us-only` / `--no-us-only` | on | US-domiciled stocks / US-focused ETFs only |
| `--no-cache` | off | Bypass the date-partitioned cache for fully fresh data |

## Outputs (all in this directory)

- `chart_<ts>_<BENCH>_llm.html` / `watchlist_chart_<ts>.html` — the final
  chart. Columns: 1-year return, sector, est. next 1y, confidence,
  stability grade. Readable in light and dark mode.
- `screener_results_<date>_<BENCH>_llm.csv` / `watchlist_results_<date>.csv`
- `run_summary_<ts>_<BENCH>_llm.json` / `watchlist_summary_<ts>.json` —
  picks, scores, self-checks, and the investor philosophy.
- `logs/screener_<ts>.log` — **the full audit trail**: investor philosophy,
  per-ticker quant score breakdowns, research assessments (dep/cont/conf +
  rationale), the est formula with per-ticker component math, gate/floor
  cuts, and final picks. Any LLM reading the log can verify the process
  and critique any stage.
- `research_bundle_<ts>_<BENCH>.json` / `watchlist_bundle_<ts>.json` —
  Phase A handoff; `llm_outputs.json` / `watchlist_outputs.json` — research.
- `cache/<date>/` — date-partitioned cache (prices, fundamentals,
  headlines). Re-running the same day reuses everything.

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
- Runs are on-demand (no keys, no automation). Ask Muse to run it, or run
  the commands above yourself.
