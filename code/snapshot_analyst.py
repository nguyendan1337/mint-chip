#!/usr/bin/env python3
"""Analyst snapshot archive (added 2026-11-19, Dan approved).

Records one dated snapshot per run of the analyst numbers the pipeline
already touches — mean/high/low price targets, the number of analysts
behind them, and the consensus EPS estimates for the current year and
the forward twelve months — for every stock in the day's frame plus the
watchlist. The point is the ARCHIVE: estimate revisions (the signal
family behind the Zacks Rank and most commercial scorers) are changes
in these numbers over time, and no point-in-time history of them exists
anywhere Mint can reach. Snapshots taken daily from 2026-11-19 forward
become that history; a future audit (first honest read late 2027) can
then test a genuine mean-revision signal on Mint's own population.

Source: the same yfinance .info call family fuse_checks uses for
implied_upside (one call per ticker, 8 workers, never raises). The free
feed exposes consensus MEANS and analyst counts only — not individual
analyst forecasts — so the archive supports mean-revision measures,
not up/down breadth. eps_ttm_actual is the reported trailing EPS,
recorded as context so estimate-vs-actual gaps stay interpretable.

RECORDING ONLY: nothing in the screener reads this archive; no score,
gate, ranking, rating, or page uses it. A failure here must never fail
a run — callers log and move on.

Usage:
  python3 snapshot_analyst.py --from-frame frames/frame_YYYYMMDD_*.json \
      --watchlist watchlist.json [--date YYYY-MM-DD]
  python3 snapshot_analyst.py AAPL MU WDC [--date YYYY-MM-DD]

Output: research_data/analyst_snapshots/snapshots_YYYY-MM-DD.json
(re-running for the same date overwrites that date's file).
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "research_data", "analyst_snapshots")

try:
    import yfinance as yf
    _YF_OK = True
except Exception:
    _YF_OK = False


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _one(ticker):
    """One info call -> the snapshot record for one ticker (never raises;
    unavailable fields are None)."""
    rec = {"price": None, "target_mean": None, "target_high": None,
           "target_low": None, "n_analysts": None,
           "eps_est_current_year": None, "eps_est_forward": None,
           "eps_ttm_actual": None}
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        return ticker, rec
    rec["price"] = _num(info.get("currentPrice")
                         or info.get("regularMarketPrice")
                         or info.get("previousClose"))
    rec["target_mean"] = _num(info.get("targetMeanPrice"))
    rec["target_high"] = _num(info.get("targetHighPrice"))
    rec["target_low"] = _num(info.get("targetLowPrice"))
    n = info.get("numberOfAnalystOpinions")
    rec["n_analysts"] = int(n) if isinstance(n, (int, float)) and n else None
    rec["eps_est_current_year"] = _num(info.get("epsCurrentYear"))
    rec["eps_est_forward"] = _num(info.get("epsForward"))
    rec["eps_ttm_actual"] = _num(info.get("epsTrailingTwelveMonths"))
    return ticker, rec


def collect_tickers(frame_path=None, watchlist_path=None, extra=()):
    tickers = set(str(t).upper() for t in extra)
    if frame_path:
        frame = json.load(open(frame_path))
        for r in frame.get("rows", []):
            if r.get("kind") == "stock" and r.get("ticker"):
                tickers.add(str(r["ticker"]).upper())
    if watchlist_path:
        wl = json.load(open(watchlist_path))
        for t in wl.get("tickers", []):
            tickers.add(str(t).upper())
    return sorted(tickers)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--from-frame", default=None)
    ap.add_argument("--watchlist", default=None)
    ap.add_argument("--date", default=None,
                    help="YYYY-MM-DD (default: today, local)")
    ap.add_argument("--out-dir", default=OUT_DIR)
    args = ap.parse_args(argv)
    day = args.date or date.today().isoformat()
    tickers = collect_tickers(args.from_frame, args.watchlist, args.tickers)
    if not tickers:
        print("snapshot_analyst: no tickers requested; nothing written")
        return 1
    if not _YF_OK:
        print("snapshot_analyst: yfinance unavailable; nothing written")
        return 1
    rows = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t, rec in ex.map(_one, tickers):
            rows[t] = rec
    payload = {
        "date": day,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "yfinance .info (same call family as fuse_checks "
                  "implied_upside); consensus means and counts only",
        "frame": os.path.basename(args.from_frame) if args.from_frame else None,
        "n_tickers": len(rows),
        "rows": rows,
    }
    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, f"snapshots_{day}.json")
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=1)
    n_tgt = sum(1 for r in rows.values() if r["target_mean"] is not None)
    n_eps = sum(1 for r in rows.values()
                if r["eps_est_current_year"] is not None)
    print(f"snapshot_analyst: {len(rows)} tickers, {n_tgt} with targets, "
          f"{n_eps} with EPS estimates -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
