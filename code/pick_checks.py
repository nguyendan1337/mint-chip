"""Per-ticker pick evidence checks for Mint (ledger fields + gate inputs).

For a small list of tickers (final picks), computes audit-instrumentation
fields recorded on each pick's ledger event:

  - sue: most recent reported earnings surprise (fraction, unwinsorized)
  - beat_streak: consecutive positive surprises, trailing 8 quarters
  - implied_upside: (analyst mean target - price) / price, None if
    unavailable

Also provides implied_upside_map / implied_upside_detail_map, the light
single-info-call versions used BEFORE selection: the stock eligibility
gate bars a name whose recorded implied_upside is negative, and the
two-strike risk gate counts a valuation strike from the same figure.
Those two functions are selection inputs; their math must not change
casually.

This module must never raise and must never materially slow the ledger
append: every yfinance call is best-effort with timeouts, per-ticker
work is threaded, and any failure yields None fields. A failure here
must never break record_picks_ledger — the caller wraps this in
try/except as a second layer.

History: created 2026-10-05 alongside a side project, when it also
stamped a second screen's pass/fail verdict on each pick. Dan retired
that project on 2026-11-20 (focus only on Mint); the verdict field was
removed and the module renamed pick_checks. The earnings-surprise
computation below is ported verbatim from the retired screen's
fund_metrics, and the implied-upside functions are carried over
unchanged, so every surviving field keeps its exact prior values.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date

try:
    import yfinance as yf
    import pandas as pd
    _YF_OK = True
except Exception:
    _YF_OK = False


def _beat_streak(rep):
    """Consecutive positive surprises, most-recent-first. rep already
    filtered to reported rows, sorted descending. (Ported verbatim.)"""
    s = 0
    for _, r in rep.iterrows():
        try:
            sp = r.get("Surprise(%)")
        except Exception:
            sp = None
        if sp is not None and pd.notna(sp) and float(sp) > 0:
            s += 1
        else:
            break
    return s


def _earnings_surprise(ticker):
    """(sue_raw, beat_streak) from the earnings-dates table; (None, 0)
    when the table is missing or carries no reported rows. Ported
    verbatim from the retired screen's fund_metrics earnings block,
    which is exactly what produced the ledger's sue / beat_streak
    values through 2026-11-19."""
    sue = None
    streak = 0
    try:
        tk = yf.Ticker(ticker)
        ed = tk.earnings_dates
        if ed is not None and not ed.empty:
            rep = ed[ed["Reported EPS"].notna()].copy()
            rep = rep.sort_index(ascending=False)
            ix = rep.index
            ixu = ix.tz_localize("UTC") if ix.tz is None else ix.tz_convert("UTC")
            cutoff = pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=1)
            rep = rep[ixu <= cutoff]
            if not rep.empty:
                r0 = rep.iloc[0]
                if pd.notna(r0.get("Surprise(%)")):
                    sue = float(r0["Surprise(%)"]) / 100.0
                streak = _beat_streak(rep.head(8))
    except Exception:
        pass
    return sue, streak


def _one(ticker):
    """Returns dict of fields for one ticker; never raises."""
    out = {"ticker": ticker, "sue": None, "beat_streak": None,
           "implied_upside": None}
    if not _YF_OK:
        return out
    try:
        # --- price from 1y daily bars (the retired screen's price:
        # last close of the dropna'd close series; its <70-bar and
        # price<$5 early returns are preserved so the fields below
        # stay unavailable for exactly the same names) ---
        df = yf.download(ticker, period="1y", auto_adjust=True,
                         progress=False, threads=False, timeout=25)
        if df is None or df.empty or len(df) < 70:
            return out
        close = df["Close"]
        if hasattr(close, "columns") and not isinstance(close, pd.Series):
            # single-ticker download should be a Series; be defensive
            close = close.iloc[:, 0]
        close = close.dropna()
        if len(close) < 70:
            return out
        price = float(close.iloc[-1])
        if price < 5.0:
            return out

        # --- earnings surprise + beat streak ---
        sue, streak = _earnings_surprise(ticker)
        out["sue"] = sue
        out["beat_streak"] = streak

        # --- implied upside vs the bars price (as before) ---
        try:
            info = yf.Ticker(ticker).info or {}
            tgt = info.get("targetMeanPrice")
            if tgt and price > 0:
                out["implied_upside"] = float(tgt) / price - 1.0
        except Exception:
            pass
        return out
    except Exception:
        return out


def check_tickers(tickers, workers=6):
    """ticker list -> {ticker: fields dict}. Never raises; on total
    failure returns {} (caller writes null fields)."""
    try:
        if not tickers or not _YF_OK:
            return {}
        seen = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for t, r in zip(tickers, ex.map(_one, tickers)):
                seen[t] = r
        return seen
    except Exception:
        return {}


def _iu_one(ticker):
    """(ticker, implied_upside or None, detail or None) from a single
    info call.

    Light version of the _one() computation above: the two-strike risk
    gate (2026-10-30) needs implied_upside for every selection candidate
    BEFORE selection, not just for final picks, so this skips the 1y
    download and fundamental pass entirely — one info call, best-effort.

    detail (logging package, 2026-11-13) carries the inputs behind the
    percentage so frames can record them: the analyst mean target
    (iu_target), the analyst count behind it (iu_analysts, from the
    info field numberOfAnalystOpinions), and iu_date — the date these
    inputs were observed. yfinance's info payload carries NO as-of
    date for the target itself, so the observation (fetch) date is the
    only honest date available; it is stamped, never backdated.
    """
    try:
        info = yf.Ticker(ticker).info or {}
        tgt = info.get("targetMeanPrice")
        price = (info.get("currentPrice") or info.get("regularMarketPrice")
                 or info.get("previousClose"))
        if tgt and price and float(price) > 0:
            detail = {"iu_target": float(tgt), "iu_analysts": None,
                      "iu_date": date.today().isoformat()}
            try:
                _na = info.get("numberOfAnalystOpinions")
                if _na is not None:
                    detail["iu_analysts"] = int(_na)
            except Exception:
                pass
            return ticker, float(tgt) / float(price) - 1.0, detail
    except Exception:
        pass
    return ticker, None, None


def implied_upside_map(tickers, workers=8):
    """ticker list -> {ticker: implied_upside or None}. Never raises.

    None means unavailable (no analyst target — common for ETFs — or a
    failed fetch); callers must treat None as 'strike not counted',
    never as a negative reading.
    """
    try:
        if not tickers or not _YF_OK:
            return {}
        out = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for t, v, _d in ex.map(_iu_one, list(dict.fromkeys(tickers))):
                out[t] = v
        return out
    except Exception:
        return {}


def implied_upside_detail_map(tickers, workers=8):
    """ticker list -> {ticker: {"implied_upside": v, "iu_target": t,
    "iu_analysts": n, "iu_date": d} or None}. Never raises.

    Same single info call per ticker as implied_upside_map; the detail
    record exists so the frame can show its work for the implied-upside
    strike (logging package, 2026-11-13). None means unavailable, with
    the same semantics as implied_upside_map.
    """
    try:
        if not tickers or not _YF_OK:
            return {}
        out = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for t, v, d in ex.map(_iu_one, list(dict.fromkeys(tickers))):
                if v is None or d is None:
                    out[t] = None
                else:
                    rec = dict(d)
                    rec["implied_upside"] = v
                    out[t] = rec
        return out
    except Exception:
        return {}
