#!/usr/bin/env python3
"""Mechanical fact pre-fill for the research stage (added 2026-11-19,
Dan approved).

Reads research_data/fundamentals/mechanical_facts_pit.csv (the point-in-time
mechanical versions of the two human-coded EV facts, built 2026-11-19 from
SEC companyfacts) and prints, for each requested ticker, the mechanical
values knowable on the as-of date:

  dividend_up_mech  -- TTM declared dividends per share rose vs the prior
                       TTM (declared concept primary, cash-paid fallback).
                       Fidelity vs the coded fact on the 240 panel:
                       agreement 83.4%, Cohen's kappa 0.668. A CHECK on the
                       coded fact, never a replacement: the rule cannot see
                       variable/special payers and lags just-announced
                       raises by up to a quarter.
  big_acq_5pct      -- TTM cash acquisition spend >= 5% of beginning total
                       assets. ATTENTION FLAG ONLY: it measures closed
                       aggregate cash deals scaled by assets, a DIFFERENT
                       variable from the coded big_acquisition fact
                       (announced single-deal market-value materiality;
                       agreement only 73.6%, kappa 0.28). Never code
                       big_acquisition from this flag.

As-of join (mirrors mechanical_facts_build_2026-11-19/validate_mechanical_
facts.py exactly): the company's versions are sorted by (filed_date,
period_end); the value is the last version with filed_date <= as_of --
exactly what a decision on as_of could know.

The data file is a SNAPSHOT (built from the 2026-11-19 companyfacts pull);
its newest filed_date is printed in the header. A dividend raise announced
after that date reads mech=0 until the file is rebuilt -- that lag is
expected and resolves in the coded value's favor.

Usage:
  python3 mech_prefill.py AAPL MSFT [--as-of YYYY-MM-DD] [--json]

Never raises on an unknown ticker: it reports the name as uncovered.
Recording only: nothing in the screener reads this module's output.
"""

import argparse
import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
MECH_CSV = os.path.join(
    HERE, "research_data", "fundamentals", "mechanical_facts_pit.csv")

_VALUE_COLS = ("dividend_up_mech", "div_ttm", "div_ttm_prior", "div_source",
               "big_acq_5pct", "acq_assets_ratio", "acq_ttm", "acq_source")


def _load():
    mech = pd.read_csv(MECH_CSV, dtype={"cik": str})
    mech = mech.sort_values(["cik", "filed_date", "period_end"],
                            kind="mergesort")
    return mech


def _ticker_to_cik(mech):
    """Ticker -> CIK, mirroring the fidelity validator's map: the SEC
    company tickers map first (a copy lives in research_data/fundamentals/
    company_tickers.json), then the facts file's own labels as fallback
    (labels are ';'-separated when a CIK carries several tickers)."""
    out = {}
    sec_json = os.path.join(HERE, "research_data", "fundamentals",
                            "company_tickers.json")
    if os.path.exists(sec_json):
        with open(sec_json) as fh:
            for row in json.load(fh).values():
                out[str(row["ticker"]).upper()] = str(row["cik_str"]).zfill(10)
    lab = mech[["tickers", "cik"]].dropna(subset=["tickers"])
    for ticks, cik in zip(lab["tickers"], lab["cik"]):
        for tick in str(ticks).split(";"):
            out.setdefault(tick.strip().upper(), cik)
    return out


def mech_values(mech, by_cik, cik, as_of):
    """The mechanical values for one CIK knowable on as_of (None fields
    when no version had been filed by then)."""
    row = {c: None for c in _VALUE_COLS}
    row["mech_filed"] = None
    row["mech_period_end"] = None
    g = by_cik.get(cik)
    if g is None:
        return row
    cand = g[g["filed_date"] <= as_of]
    if len(cand):
        last = cand.iloc[-1]
        for c in _VALUE_COLS:
            v = last[c]
            row[c] = None if pd.isna(v) else v
        row["mech_filed"] = last["filed_date"]
        row["mech_period_end"] = last["period_end"]
    return row


def prefill(tickers, as_of):
    mech = _load()
    by_cik = {cik: g for cik, g in mech.groupby("cik")}
    t2c = _ticker_to_cik(mech)
    vintage = str(mech["filed_date"].max())
    out = {}
    for t in tickers:
        t = str(t).upper()
        cik = t2c.get(t)
        rec = {"ticker": t, "cik": cik, "as_of": as_of}
        if cik is None:
            rec.update({c: None for c in _VALUE_COLS})
            rec["mech_filed"] = None
            rec["mech_period_end"] = None
            rec["note"] = "not in mechanical facts file"
        else:
            rec.update(mech_values(mech, by_cik, cik, as_of))
        for c in ("dividend_up_mech", "big_acq_5pct"):
            if rec[c] is not None:
                rec[c] = int(rec[c])
        out[t] = rec
    return vintage, out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--as-of", default=None,
                    help="YYYY-MM-DD (default: today, local)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    as_of = args.as_of
    if as_of is None:
        from datetime import date
        as_of = date.today().isoformat()
    vintage, out = prefill(args.tickers, as_of)
    if args.json:
        print(json.dumps({"as_of": as_of, "data_vintage": vintage,
                          "prefill": out}, indent=1, default=str))
        return 0
    print(f"mechanical pre-fill as of {as_of} "
          f"(data snapshot newest filing: {vintage})")
    for t, r in out.items():
        if r["cik"] is None:
            print(f"{t}: NOT COVERED ({r['note']})")
            continue
        print(f"{t}: dividend_up_mech={r['dividend_up_mech']} "
              f"(div_ttm={r['div_ttm']}, prior={r['div_ttm_prior']}, "
              f"src={r['div_source']}) "
              f"big_acq_5pct={r['big_acq_5pct']} "
              f"(acq/assets={r['acq_assets_ratio']}, src={r['acq_source']}) "
              f"as filed {r['mech_filed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
