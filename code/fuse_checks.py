"""Per-ticker Fuse checks for Mint ledger integration (dual-confirmation).

For a small list of tickers (final picks), computes audit-instrumentation
fields using the Light-the-Fuse v3.1 screen definition:

  - dual_confirm: True / False / None
      True  = the ticker passes Fuse v3.1's full screen
              (technical gates + fundamental pass + Piotroski gate +
               14-day earnings veto + mandate filters)
      False = it was evaluated and failed at least one gate
      None  = could not be determined (missing data / download failure)
  - sue: most recent reported earnings surprise (fraction, unwinsorized)
  - beat_streak: consecutive positive surprises, trailing 8 quarters
  - implied_upside: (analyst mean target - price) / price, None if unavailable

This is AUDIT INSTRUMENTATION, not selection. It must never raise and must
never materially slow the ledger append: every yfinance call is best-effort
with timeouts, per-ticker work is threaded, and any failure yields None
fields. A failure here must never break record_picks_ledger — the caller
wraps this in try/except as a second layer.

Definition of record (frozen 2026-10-05, Fuse v3.1):
  Technical: price>=$5, 52w-high nearness>=0.85, 0<12-1 momentum<=80%,
             above 50-day MA, 20d/60d volume ratio>=1.0, volume-mania<3.5,
             60d ann. vol<=80%, 1y maxDD>=-40%.
  Fundamental: mcap>=$2B, ROE>5% (when known), and one of
               SUE>3% / EPS-accel>0 / YoY revenue growth>15%.
  Quality: Piotroski F>=5 for non-financials with complete statements;
           financials/insurers (or any name with incomplete statements)
           -> dual_confirm None (needs manual review, never auto-fail:
           the statements mean different things there).
  Veto: projected next earnings within 14 days.
  Mandate filters: US-domiciled only; Energy and Basic Materials excluded;
                   religious-themed names excluded; preferred shares skipped.
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fuse"))
try:
    from fuse_v3 import (fund_metrics, tech_metrics, tech_pass, fund_pass,
                         RELIGION_WORDS)
    _FUSE_OK = True
except Exception as _e:
    _FUSE_OK = False
    _FUSE_ERR = str(_e)[:200]

try:
    import yfinance as yf
    import pandas as pd
    _YF_OK = True
except Exception:
    _YF_OK = False

_FINANCIAL_SECTORS = {"Financial Services"}


def _one(ticker):
    """Returns dict of fields for one ticker; never raises."""
    out = {"ticker": ticker, "dual_confirm": None, "sue": None,
           "beat_streak": None, "implied_upside": None}
    if not (_FUSE_OK and _YF_OK):
        return out
    try:
        # --- technicals from 1y daily bars ---
        df = yf.download(ticker, period="1y", auto_adjust=True,
                         progress=False, threads=False, timeout=25)
        if df is None or df.empty or len(df) < 70:
            return out
        close = df["Close"]
        vol = df["Volume"]
        if hasattr(close, "columns") and not isinstance(close, pd.Series):
            # single-ticker download should be a Series; be defensive
            close = close.iloc[:, 0]
            vol = vol.iloc[:, 0]
        m = tech_metrics(close, vol)
        if m is None or m["price"] < 5.0:
            return out
        price = m["price"]
        tech_ok = tech_pass(m)

        # --- fundamentals ---
        f = fund_metrics(ticker)
        out["sue"] = (float(f["sue_raw"]) if f.get("sue_raw") is not None
                      else None)
        out["beat_streak"] = f.get("beat_streak")
        try:
            info = yf.Ticker(ticker).info or {}
            tgt = info.get("targetMeanPrice")
            if tgt and price > 0:
                out["implied_upside"] = float(tgt) / price - 1.0
        except Exception:
            pass

        # --- dual-confirmation decision (mirrors fuse_v3 assembly) ---
        if not tech_ok:
            out["dual_confirm"] = False
            return out
        name = (f.get("longName") or ticker).lower()
        if any(w in name for w in RELIGION_WORDS):
            out["dual_confirm"] = False
            return out
        if (f.get("country") or "United States") != "United States":
            out["dual_confirm"] = False
            return out
        if (f.get("sector") or "") in ("Energy", "Basic Materials"):
            out["dual_confirm"] = False
            return out
        roe = f.get("roe")
        if roe is not None and roe <= 0.05:
            out["dual_confirm"] = False
            return out
        if not fund_pass(f):
            out["dual_confirm"] = False
            return out
        pf = f.get("piotroski")
        sector = f.get("sector") or ""
        if pf is None or sector in _FINANCIAL_SECTORS:
            # cannot judge financial health mechanically here ->
            # unknown, never auto-fail
            out["dual_confirm"] = None
            return out
        if pf < 5:
            out["dual_confirm"] = False
            return out
        dte = f.get("days_to_earn")
        if dte is not None and 0 <= dte <= 14:
            out["dual_confirm"] = False
            return out
        out["dual_confirm"] = True
        return out
    except Exception:
        return out


def check_tickers(tickers, workers=6):
    """ticker list -> {ticker: fields dict}. Never raises; on total
    failure returns {} (caller writes null fields)."""
    try:
        if not tickers or not (_FUSE_OK and _YF_OK):
            return {}
        seen = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for t, r in zip(tickers, ex.map(_one, tickers)):
                seen[t] = r
        return seen
    except Exception:
        return {}
