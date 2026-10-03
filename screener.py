#!/usr/bin/env python3
"""
Stock screener: EVERY stock beating your benchmark (full yfinance universe),
fundamentals pulled for all outperformers, trash filtered, scored for
continuation + low drawdown risk, top scorers researched via dynamic news
risk layer. Presents top 10 (max 2 per sector).

Free APIs only: yfinance (screener + prices + fundamentals + news),
Google News RSS. No API keys required.

Universe source: yfinance's screener (Yahoo Finance data), paged in full —
US-listed (NYSE/Nasdaq), market cap >= $2B, price >= $5 by default.

Usage:
  python3 screener.py --benchmark SPMO
  python3 screener.py --benchmark VGT --test   (quick test, 60 outperformers max)
  python3 screener.py --compare-benchmarks     (show SPMO vs VGT vs DIVB vs VFLO)
  python3 screener.py --min-mcap 1000000000    (include smaller caps)

Stored in: ~/workspace/stock-screener/
Run whenever you want fresh results.
"""
import argparse
import os
import re
import sys
import time
import warnings
from collections import Counter
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
    import requests
    import feedparser
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Run: pip3 install --break-system-packages yfinance pandas numpy requests feedparser")
    sys.exit(1)

CACHE_DIR = "cache"
BENCHMARK_CHOICES = ["SPMO", "VGT", "DIVB", "VFLO", "VOO"]

# ETF portfolio dedupe aliases: tickers mapping to the same underlying
# portfolio (different wrappers). Surfaced by research; extend as found.
_ETF_PORTFOLIO_ALIASES = {
    "qqq": "nasdaq100",
    "qqqm": "nasdaq100",
}

# Generic negative sentiment words (not event-specific, just tone)
NEGATIVE_WORDS = {
    "downgrade", "downgraded", "miss", "misses", "warning", "lawsuit",
    "investigation", "probe", "cut", "cuts", "layoff", "fraud",
    "default", "bankruptcy", "recall", "fine", "penalty", "concern",
    "fear", "risk", "fall", "falls", "drop", "drops", "plunge",
    "slump", "weak", "disappoint", "scandal", "resign"
}

STOPWORDS = set("""
a an the and or but if then else when at by for with about into through during
before after above below to from up down in out on off over under again further
once here there all any both each few more most other some such no nor not only
own same so than too very can will just don should now of as is are was were be
been being have has had having do does did doing would could ought i'm you he she
it we they them his her its our their this that these those i me my we us what
which who whom whose where why how stock market stocks shares price trading
global economy economic economies market markets wall street finance financial
reuters yahoo bloomberg cnbc wsj barron's investing business news today
""".split())


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a") as f:
                f.write(f"{datetime.now().isoformat()} [INFO] {msg}\n")
        except Exception:
            pass


def log_error(msg):
    """Log an error with traceback to console and log file."""
    import traceback
    tb = traceback.format_exc()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: {msg}", flush=True)
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a") as f:
                f.write(f"{datetime.now().isoformat()} [ERROR] {msg}\n")
                if tb and "NoneType: None" not in tb:
                    f.write(tb + "\n")
        except Exception:
            pass


def _load_philosophy():
    """Investor philosophy text, stamped into every log and run summary so any
    reviewer (human or LLM) gets the values the mechanism serves."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "PHILOSOPHY.md")) as f:
            return f.read().strip()
    except Exception:
        return ""


def setup_logging():
    """Create logs/ dir and a timestamped log file for this run."""
    global LOG_FILE
    try:
        os.makedirs("logs", exist_ok=True)
        LOG_FILE = os.path.join(
            "logs", f"screener_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
        with open(LOG_FILE, "w") as f:
            f.write(f"=== screener run started {datetime.now().isoformat()} ===\n")
            f.write(f"args: {' '.join(sys.argv)}\n")
            f.write(f"cwd: {os.getcwd()}\n")
            phil = _load_philosophy()
            if phil:
                f.write("--- investor philosophy ---\n" + phil + "\n")
                f.write("--- end philosophy ---\n")
    except Exception as e:
        print(f"WARNING: could not set up log file: {e}", flush=True)
        LOG_FILE = None
    return LOG_FILE


def continue_logging(path):
    """Continue a previous phase's log file so one pipeline run = one log.

    Phase A writes its log path into the research bundle meta; Phase B picks
    it up and appends. An auditor (human or LLM) reads a single file and sees
    every stage: universe -> cuts -> research -> selection."""
    global LOG_FILE
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        LOG_FILE = path
        with open(LOG_FILE, "a") as f:
            f.write(f"\n=== continuing log {datetime.now().isoformat()} ===\n")
            f.write(f"args: {' '.join(sys.argv)}\n")
    except Exception as e:
        print(f"WARNING: could not continue log file {path}: {e}", flush=True)
        LOG_FILE = None
    return LOG_FILE


LOG_FILE = None


def get_all_screener_stocks(min_mcap=2_000_000_000, min_price=5.0):
    """Pull the FULL US-listed universe from yfinance's screener (paged).

    Returns list of dicts: ticker / name / pct_52w (52-week % change).
    No cap — this is the broad pool we then filter down to outperformers.
    """
    log("Paging yfinance screener for full US universe (mcap>=${:.0f}B, price>=${:.0f})...".format(
        min_mcap / 1e9, min_price))
    try:
        from yfinance.screener.query import EquityQuery as EqyQy
        q = EqyQy('and', [
            EqyQy('eq', ['region', 'us']),
            EqyQy('is-in', ['exchange', 'NMS', 'NYQ']),
            EqyQy('gte', ['intradaymarketcap', min_mcap]),
            EqyQy('gte', ['intradayprice', min_price]),
        ])
        quotes = []
        offset = 0
        while True:
            res = yf.screen(q, size=250, offset=offset,
                            sortField='intradaymarketcap', sortAsc=False)
            batch = res.get('quotes', []) or []
            quotes.extend(batch)
            total = res.get('total', 0)
            log(f"  page offset {offset}: {len(quotes)}/{total} fetched")
            if len(batch) < 250 or len(quotes) >= total:
                break
            offset += 250
            time.sleep(0.4)
        out, seen = [], set()
        for x in quotes:
            sym = x.get('symbol')
            pct = x.get('fiftyTwoWeekChangePercent')
            if sym and pct is not None and sym not in seen:
                seen.add(sym)
                out.append({
                    "ticker": sym,
                    "name": x.get('longName') or x.get('shortName') or sym,
                    "pct_52w": float(pct),
                })
        log(f"Universe: {len(out)} stocks from yfinance screener")
        return out
    except Exception as e:
        log(f"yfinance screener failed ({e})")
        return []


def get_fundamentals(tickers, workers=6):
    """Fetch fundamentals via yfinance info for ALL tickers (threaded)."""
    log(f"Fetching fundamentals for {len(tickers)} tickers ({workers} threads)...")
    infos = {}
    from concurrent.futures import ThreadPoolExecutor

    def fetch(t):
        try:
            info = yf.Ticker(t).info
            if info and info.get("regularMarketPrice") is not None:
                return t, info
        except Exception:
            pass
        return t, None

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for t, info in ex.map(fetch, tickers):
            done += 1
            if done % 50 == 0:
                log(f"  fundamentals {done}/{len(tickers)}...")
            if info:
                infos[t] = info
            time.sleep(0.05)
    log(f"Got fundamentals for {len(infos)}/{len(tickers)} tickers")
    return infos


def apply_trash_filters(rows, closes, infos, us_only=True):
    """Hard filters to drop obvious trash BEFORE scoring, using fundamentals.

    Transparent rules (tune as needed):
    - price history >= 120 trading days
    - 10-day avg dollar volume >= $2M (illiquid = trash)
    - 1y max drawdown not worse than -70% (blown-up behavior)
    - market cap >= $2B and price >= $5 already enforced at screener level
    - us_only: drop companies not domiciled in the United States (yfinance
      'country'; names with no country data are kept, not dropped)
    Everything subtler is handled by the scorer's z-scores.
    Returns (kept_rows, report_dict).
    """
    report = {}
    dropped_names = {}
    def _drop(reason, t):
        report[reason] = report.get(reason, 0) + 1
        dropped_names.setdefault(reason, []).append(t)
    kept = []
    for r in rows:
        t = r["ticker"]
        s = closes.get(t)
        if s is None or len(s) < 120:
            _drop("no_price_history", t)
            continue
        if _religious_theme(r.get("name", ""), t):
            _drop("religious_theme", t)
            continue
        dd = max_drawdown(s)
        if not pd.isna(dd) and dd < -0.70:
            _drop("drawdown_worse_than_-70%", t)
            continue
        info = infos.get(t, {})
        if us_only:
            ctry = info.get("country")
            if ctry and ctry != "United States":
                _drop("foreign_domicile", t)
                continue
        try:
            adv = info.get("averageDailyVolume10Day") or info.get("averageVolume") or 0
            px = info.get("regularMarketPrice") or s.iloc[-1]
            if float(adv) * float(px) < 2_000_000:
                _drop("dollar_volume_lt_$2M", t)
                continue
        except Exception:
            pass
        kept.append(r)
    report["kept"] = len(kept)
    report["dropped_total"] = len(rows) - len(kept)
    report["dropped_names"] = dropped_names
    return kept, report


def get_sp500_tickers(limit=None):
    """Fetch S&P 500 list from Wikipedia, fallback to datahub, then static."""
    tickers = None
    try:
        log("Fetching S&P 500 ticker list from Wikipedia...")
        # Use requests with user-agent to avoid 403
        headers = {"User-Agent": "Mozilla/5.0 (compatible; stock-screener/1.0)"}
        req = requests.Request("GET", "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=headers)
        prepped = req.prepare()
        s = requests.Session()
        resp = s.send(prepped, timeout=15)
        tables = pd.read_html(resp.text)
        tickers = tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
        tickers = [t.strip() for t in tickers if isinstance(t, str)]
        log(f"Got {len(tickers)} tickers from Wikipedia")
    except Exception as e:
        log(f"Wikipedia fetch failed ({e}), trying datahub...")
    if not tickers:
        try:
            df = pd.read_csv("https://datahub.io/core/s-and-p-500-companies/r/constituents.csv")
            col = "Symbol" if "Symbol" in df.columns else df.columns[0]
            tickers = df[col].str.replace(".", "-", regex=False).tolist()
            log(f"Got {len(tickers)} tickers from datahub")
        except Exception as e:
            log(f"Datahub fetch failed ({e}), using fallback list")
    if not tickers:
        tickers = [
            "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "BRK-B", "LLY",
            "AVGO", "JPM", "XOM", "UNH", "V", "MA", "TSLA", "COST", "JNJ",
            "HD", "PG", "WMT", "BAC", "ABBV", "CRM", "ORCL", "NFLX",
            "KO", "AMD", "PEP", "TMO", "LIN", "DIS", "ABT", "ACN",
            "ADBE", "MRK", "CSCO", "VZ", "INTC", "PFE", "CMCSA",
            "NEE", "XEL", "F", "GM", "CAT", "BA", "GE", "IBM", "T",
        ]
    if limit:
        return tickers[:limit]
    return tickers


def download_prices(tickers, period="1y", batch_size=80, min_rows=50):
    """Batch download adjusted closes, in chunks with retry.

    Yahoo rate-limits big batches (shows up as bogus 'possibly delisted'
    errors), so we go chunk by chunk and retry misses individually.

    min_rows is the sanity floor on returned history length. It defaults to
    50 (the 1y use case); short-window callers (e.g. the 5d ledger fetch) must
    pass a smaller floor, or every result is discarded — 5 rows never clears
    a 50-row bar. (This exact bug silently zeroed the ledger price fetch.)
    """
    tickers = list(dict.fromkeys(tickers))  # dedupe, keep order
    log(f"Downloading {period} prices for {len(tickers)} tickers (this takes a bit)...")
    closes = {}

    def extract(data, batch):
        got = 0
        if isinstance(data.columns, pd.MultiIndex):
            for t in batch:
                try:
                    s = data[t]["Close"].dropna()
                    if len(s) >= min_rows and t not in closes:
                        closes[t] = s
                        got += 1
                except Exception:
                    continue
        else:
            try:
                s = data["Close"].dropna()
                if len(s) >= min_rows and batch[0] not in closes:
                    closes[batch[0]] = s
                    got += 1
            except Exception:
                pass
        return got

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        try:
            data = yf.download(
                batch, period=period, auto_adjust=True, progress=False,
                threads=True, group_by="ticker"
            )
            extract(data, batch)
        except Exception as e:
            log(f"  batch {i}-{i + len(batch)} failed ({e}), will retry individually")
        missing = [t for t in batch if t not in closes]
        if missing:
            time.sleep(2)
            for t in missing:
                try:
                    d = yf.download(t, period=period, auto_adjust=True,
                                    progress=False, threads=False)
                    close = d["Close"].dropna() if "Close" in d else pd.Series(dtype=float)
                    # handle single-ticker multiindex quirk
                    if isinstance(close, pd.DataFrame):
                        close = close.iloc[:, 0].dropna()
                    if len(close) >= min_rows:
                        closes[t] = close
                except Exception:
                    continue
                time.sleep(0.3)
        log(f"  progress: {len(closes)}/{len(tickers)}")
        time.sleep(1)
    log(f"Got price history for {len(closes)}/{len(tickers)} tickers")
    return closes


def calc_return(series, days=None):
    if series is None or len(series) < 2:
        return np.nan
    s = series.dropna()
    if days:
        s = s.iloc[-days:] if len(s) >= days else s
    if len(s) < 2:
        return np.nan
    return float(s.iloc[-1] / s.iloc[0] - 1)


def max_drawdown(series):
    s = series.dropna()
    if len(s) < 2:
        return np.nan
    roll_max = s.cummax()
    dd = (s - roll_max) / roll_max
    return float(dd.min())  # negative number, e.g. -0.25


def drawdown_frequency(series, window=21, thresh=-0.10):
    """Fraction of rolling 1-month windows that lost more than 10%.

    Depth-only drawdown misses 'death by a thousand cuts': a name that dips
    15% every other month has the same maxdd as one with a single 15% dip,
    but very different post-buy pain risk. Used to scale the EV downside.
    """
    s = series.dropna()
    if len(s) < window + 1:
        return 0.0
    rets = s.pct_change(window).dropna()
    if len(rets) == 0:
        return 0.0
    return float((rets < thresh).mean())


def zscore(series):
    s = pd.Series(series, dtype=float)
    mu = s.mean(skipna=True)
    sd = s.std(skipna=True)
    if pd.isna(sd) or sd == 0:
        return pd.Series([0.0] * len(s), index=s.index)
    # missing values -> neutral 0, not NaN (NaN would poison the whole score)
    return ((s - mu) / sd).fillna(0)


def build_scores(closes, infos):
    """Score for: continue to do well + minimize chance of going negative after buying.

    Heavily weights: persistent momentum + trend + quality + LOW risk
    (low vol, shallow drawdown, low leverage, reasonable valuation).
    """
    rows = []
    for t, info in infos.items():
        px = closes.get(t)
        if px is None or len(px) < 120:
            continue
        daily_ret = px.pct_change().dropna()
        row = {"ticker": t}
        row["sector"] = info.get("sector", "Unknown")
        row["name"] = info.get("longName", t) or t

        # --- momentum / trend ---
        row["ret_1y"] = calc_return(px)
        row["ret_6m"] = calc_return(px, 126)
        row["ret_3m"] = calc_return(px, 63)
        row["ret_1m"] = calc_return(px, 21)
        row["ret_2w"] = calc_return(px, 10)
        sma50 = px.rolling(50).mean().iloc[-1] if len(px) >= 50 else np.nan
        sma200 = px.rolling(200).mean().iloc[-1] if len(px) >= 200 else np.nan
        sma20 = px.rolling(20).mean().iloc[-1] if len(px) >= 20 else np.nan
        last = px.iloc[-1]
        row["above50"] = 1.0 if not pd.isna(sma50) and last > sma50 else 0.0
        row["above200"] = 1.0 if not pd.isna(sma200) and last > sma200 else 0.0
        row["dist_20dma"] = float(last / sma20 - 1) if sma20 and not pd.isna(sma20) and sma20 > 0 else 0.0
        hi252 = px.rolling(252).max().iloc[-1] if len(px) >= 60 else np.nan
        # avoid buying extremely extended into 52w high (reversal risk)
        row["dist_high"] = float(last / hi252) if hi252 and hi252 > 0 else np.nan

        # --- quality ---
        row["roe"] = info.get("returnOnEquity", np.nan)
        row["margin"] = info.get("profitMargins", np.nan)
        row["earn_growth"] = info.get("earningsGrowth", np.nan)
        row["rev_growth"] = info.get("revenueGrowth", np.nan)
        fcf = info.get("freeCashflow", np.nan)
        row["fcf_pos"] = 1.0 if isinstance(fcf, (int, float)) and fcf > 0 else 0.0
        dte = info.get("debtToEquity", np.nan)
        row["dte"] = float(dte) if isinstance(dte, (int, float)) else np.nan

        # --- value (avoid overpaying for momentum) ---
        fpe = info.get("forwardPE", np.nan)
        row["fpe"] = float(fpe) if isinstance(fpe, (int, float)) and fpe > 0 else np.nan
        ptb = info.get("priceToBook", np.nan)
        row["ptb"] = float(ptb) if isinstance(ptb, (int, float)) and ptb > 0 else np.nan

        # --- risk (the "don't go negative right after I buy" part) ---
        row["vol60"] = float(daily_ret.iloc[-60:].std() * np.sqrt(252)) if len(daily_ret) >= 60 else np.nan
        row["maxdd"] = max_drawdown(px)  # negative
        row["dd_freq"] = drawdown_frequency(px)  # how often it hurts, not just how deep
        beta = info.get("beta", np.nan)
        row["beta"] = float(beta) if isinstance(beta, (int, float)) else np.nan

        rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Z-scores (higher = better). Invert bad metrics.
    df["z_6m"] = zscore(df["ret_6m"].fillna(0))
    df["z_3m"] = zscore(df["ret_3m"].fillna(0))
    # blowoff-top penalty: vertical 2-week spike or extended far above 20DMA
    # (buying the spike = the classic "goes negative right after buying")
    spike = (df["ret_2w"].fillna(0) - 0.15).clip(lower=0) + \
            (df["dist_20dma"].fillna(0) - 0.10).clip(lower=0)
    df["z_blowoff"] = zscore(-spike)
    # penalize extremely extended (>0.98 of high) slightly
    df["z_ext"] = zscore((-(df["dist_high"].fillna(0.9) - 0.9).abs()))
    df["z_trend"] = zscore(df["above50"] + df["above200"])

    df["z_roe"] = zscore(df["roe"])
    df["z_margin"] = zscore(df["margin"])
    df["z_earn"] = zscore(df["earn_growth"])
    df["z_fcf"] = zscore(df["fcf_pos"])
    df["z_dte"] = zscore(-df["dte"].fillna(df["dte"].median()))

    df["z_fpe"] = zscore(-df["fpe"].fillna(df["fpe"].median()))
    df["z_ptb"] = zscore(-df["ptb"].fillna(df["ptb"].median()))

    df["z_vol"] = zscore(-df["vol60"].fillna(df["vol60"].median()))
    df["z_dd"] = zscore(df["maxdd"].fillna(df["maxdd"].median()))  # less negative = better
    # penalize high beta (>1.5)
    beta_pen = df["beta"].fillna(1.0).apply(lambda b: -abs(b - 1.0))
    df["z_beta"] = zscore(beta_pen)

    # Weighted composite: momentum 22%, blowoff-avoidance 8%, quality 30%,
    # low-risk 30%, insider/valuation/value 10%. Built for: keep doing well,
    # minimize post-buy drawdown.
    df["base_score"] = (
        0.08 * df["z_6m"] + 0.06 * df["z_3m"] + 0.04 * df["z_ext"] + 0.04 * df["z_trend"]
        + 0.08 * df["z_blowoff"]
        + 0.10 * df["z_roe"] + 0.08 * df["z_margin"] + 0.07 * df["z_earn"] + 0.05 * df["z_fcf"]
        + 0.12 * df["z_vol"] + 0.12 * df["z_dd"] + 0.06 * df["z_beta"]
        + 0.05 * df["z_dte"]
        + 0.03 * df["z_fpe"] + 0.02 * df["z_ptb"]
    )
    # character: the Composure parts the expected-value estimate doesn't
    # already see — quality + entry timing + leverage/value. est uses ret_6m
    # and maxdd directly, so momentum/vol/drawdown z-scores are deliberately
    # excluded here (no double-counting). Used as a multiplier on the EV.
    df["character"] = (
        0.10 * df["z_roe"] + 0.08 * df["z_margin"] + 0.07 * df["z_earn"] + 0.05 * df["z_fcf"]
        + 0.08 * df["z_blowoff"] + 0.04 * df["z_ext"] + 0.04 * df["z_trend"]
        + 0.05 * df["z_dte"] + 0.03 * df["z_fpe"] + 0.02 * df["z_ptb"]
    ).fillna(0)
    return df.sort_values("base_score", ascending=False)


def _enrich_one(t):
    """Insider net selling / market cap (90d) and days to next earnings.
    Returns (ticker, insider_ratio, earnings_in_days); None when unknown."""
    insider_ratio, earn_days = None, None
    try:
        tk = yf.Ticker(t)
        try:
            it = tk.insider_transactions
            if it is not None and not it.empty:
                d = it.copy()
                d["dt"] = pd.to_datetime(d["Start Date"], errors="coerce", utc=True)
                cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=90)
                rec = d[d["dt"] >= cutoff]
                sells = rec[rec["Text"].str.startswith("Sale", na=False)]["Value"].fillna(0).sum()
                buys = rec[rec["Text"].str.contains("Purchase", na=False)]["Value"].fillna(0).sum()
                mcap = tk.info.get("marketCap") if isinstance(tk.info, dict) else None
                if mcap:
                    insider_ratio = float((sells - buys) / mcap)
        except Exception:
            pass
        try:
            ed = tk.earnings_dates
            if ed is not None and len(ed):
                idx = ed.index
                try:
                    idx_utc = idx.tz_convert("UTC")
                except Exception:
                    try:
                        idx_utc = idx.tz_localize("UTC")
                    except Exception:
                        idx_utc = idx
                now = pd.Timestamp.now(tz="UTC")
                fut = idx_utc[idx_utc > now]
                if len(fut):
                    earn_days = int((fut[0] - now).total_seconds() // 86400)
        except Exception:
            pass
    except Exception:
        pass
    return t, insider_ratio, earn_days


def enrich_candidates(tickers, workers=6):
    """Threaded insider + earnings enrichment for top candidates."""
    from concurrent.futures import ThreadPoolExecutor
    log(f"Enriching {len(tickers)} candidates (insider + earnings)...")
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        done = 0
        for t, ir, ed in ex.map(_enrich_one, tickers):
            done += 1
            if done % 25 == 0:
                log(f"  enrichment {done}/{len(tickers)}...")
            out[t] = {"insider_ratio": ir, "earnings_in_days": ed}
    return out


def log_levers(args):
    """Every tunable of the pipeline with its current value and what it does.

    This is the lever panel for an auditor (human or LLM): to change what the
    pipeline selects, push/pull these. Values are read from the live args so
    this block can never go stale."""
    _eb = etf_benchmark(args)
    log("--- pipeline levers (tunables: push/pull these to change selection) ---")
    log(f"universe: yfinance screener, mcap>=${args.min_mcap:.0f}B, price>=${args.min_price:.0f}")
    log(f"benchmark={args.benchmark} (stocks must beat its 1y; 5pp slack on screener 52w%); "
        f"etf_benchmark={_eb} (ETFs must beat its 1y)")
    log("trash filters (hard, pre-scoring): history>=120 trading days; "
        "1y maxDD>=-70%; 10d avg dollar volume>=$2M; US-domiciled only; no religious themes")
    log(f"risk gates (hard, pre-research): ann. 60d vol<={args.max_vol:.0%}; "
        f"maxDD>={args.min_dd:.0%}  (--max-vol, --min-dd)")
    log(f"research pool: top 50 stocks + top {args.n_etf_research} ETFs by base_score")
    log(f"final_score = base_w - {args.w_down}*dep + {args.w_up}*cont "
        f"- {args.w_outlier}*excess*dep  (--w-down, --w-up, --w-outlier; "
        "base_w = base_score winsorized at p95, excess = amount above cap)")
    log("base_score(stocks) = 0.08*z_6m + 0.06*z_3m + 0.04*z_ext + 0.04*z_trend "
        "+ 0.08*z_blowoff + 0.10*z_roe + 0.08*z_margin + 0.07*z_earn + 0.05*z_fcf "
        "+ 0.12*z_vol + 0.12*z_dd + 0.06*z_beta + 0.05*z_dte + 0.03*z_fpe + 0.02*z_ptb")
    log("base_score(ETFs)   = 0.12*z_6m + 0.08*z_3m + 0.10*z_1m + 0.05*z_trend "
        "+ 0.05*z_blowoff + 0.15*z_vol + 0.15*z_dd + 0.10*z_dvol + 0.10*z_expense + 0.10*z_aum")
    log("  z glossary: z_6m/3m/1m = trailing returns; z_ext = not extended far below high; "
        "z_trend = above 50/200DMA; z_blowoff = penalizes 2-week vertical spikes (don't buy the top); "
        "z_roe/margin = profitability; z_earn = earnings growth; z_fcf = positive FCF; "
        "z_vol = low 60d vol; z_dd = shallow maxDD; z_beta = beta near 1.0; z_dte = low debt/equity; "
        "z_fpe/ptb = cheapness; z_expense = low fees; z_aum = large AUM (closure safety); "
        "z_dvol = liquidity. Higher z = better; bad metrics inverted.")
    log("expected value = conf * (1+0.25*character) * "
        "(cont*upside - (1-cont)*downside - dep*20%)")
    log("  upside = 6m run: first 25% at full weight, excess at half, input capped at 50%")
    log("  downside = max(|maxDD|*50%, 10%) * (1 + dd_freq); "
        "dd_freq = fraction of rolling 1m windows losing >10%")
    log("  character = Composure z-sum of quality/entry-timing/structure "
        "(excludes momentum+volatility, no double-count), clamped [-2,2]")
    log("  character(stocks) = 0.10*z_roe + 0.08*z_margin + 0.07*z_earn + 0.05*z_fcf "
        "+ 0.08*z_blowoff + 0.04*z_ext + 0.04*z_trend + 0.05*z_dte + 0.03*z_fpe + 0.02*z_ptb")
    log("  character(ETFs)   = 0.05*z_blowoff + 0.05*z_trend + 0.10*z_expense "
        "+ 0.10*z_aum + 0.10*z_dvol")
    log("  (z_* are cross-sectional z-scores across the scored outperformer set; "
        "per-ticker character parts are in the 'character breakdown' section)")
    log(f"EV floor --min-est={args.min_est:+.1%}: every pick must earn its place; "
        "empty slots beat filler")
    log(f"event veto --veto-dep={args.veto_dep}: dep above this is excluded outright")
    log(f"sector cap --max-per-sector={args.max_per_sector} (stocks); "
        f"--max-per-etf-category={args.max_per_etf_category} (ETFs); hard across both "
        "pick passes; cap ties broken by EV (highest-EV names survive)")
    log(f"--max-etf-overlap={args.max_etf_overlap:.0%}: pairwise top-10 holdings "
        f"overlap cap for ETFs; lower-EV member of over-threshold pairs is "
        f"excluded (redundancy is not diversification)")
    log(f"score floor --min-score={args.min_score}")
    log(f"final slots: --n-stocks={args.n_stocks} + --n-etfs={args.n_etfs}")
    log("--- end levers ---")


def apply_risk_gates(df, max_vol=0.80, min_dd=-0.40):
    """Absolute risk gates for the 'don't go negative' goal.

    Relative z-scores can't do this: in a parabolic universe, -40% DD can
    look 'average'. Gate absolutely before research/final selection.
    """
    gated_vol = df["vol60"] > max_vol
    gated_dd = df["maxdd"] < min_dd
    gated = df[gated_vol | gated_dd]
    kept = df[~(gated_vol | gated_dd)].copy()
    report = {"gated_vol_gt": f"{max_vol:.0%}", "n_gated_vol": int(gated_vol.sum()),
              "gated_dd_lt": f"{min_dd:.0%}", "n_gated_dd": int(gated_dd.sum()),
              "gated_total": len(gated), "kept": len(kept),
              "gated_tickers": gated["ticker"].tolist()}
    log(f"Risk gates: {report['gated_total']} excluded "
        f"(vol>{max_vol:.0%}: {report['n_gated_vol']}, dd<{min_dd:.0%}: {report['n_gated_dd']})")
    log(f"  risk-gated tickers: {report['gated_tickers']}")
    return kept, report


def stability_grade(row):
    """A/B/C/D stability grade from vol, drawdown, event dependence."""
    try:
        vol = float(row.get("vol60", 1))
    except Exception:
        vol = 1.0
    try:
        dd = float(row.get("maxdd", -1))
    except Exception:
        dd = -1.0
    dep = row.get("llm_event_dependence", row.get("news_penalty", 0.3))
    try:
        dep = float(dep)
    except Exception:
        dep = 0.3
    if vol <= 0.35 and dd >= -0.20 and dep <= 0.35:
        return "A"
    if vol <= 0.55 and dd >= -0.30 and dep <= 0.55:
        return "B"
    if vol <= 0.80 and dd >= -0.45:
        return "C"
    return "D"


    if vol <= 0.80 and dd >= -0.45:
        return "C"
    return "D"


_STOCK_SCORE_GROUPS = {
    "mom": [(0.08, "z_6m"), (0.06, "z_3m"), (0.04, "z_ext"), (0.04, "z_trend")],
    "blowoff": [(0.08, "z_blowoff")],
    "qual": [(0.10, "z_roe"), (0.08, "z_margin"), (0.07, "z_earn"), (0.05, "z_fcf")],
    "risk": [(0.12, "z_vol"), (0.12, "z_dd"), (0.06, "z_beta")],
    "lev": [(0.05, "z_dte")],
    "val": [(0.03, "z_fpe"), (0.02, "z_ptb")],
}
_ETF_SCORE_GROUPS = {
    "mom": [(0.12, "z_6m"), (0.08, "z_3m"), (0.10, "z_1m"),
            (0.05, "z_trend"), (0.05, "z_blowoff")],
    "risk": [(0.15, "z_vol"), (0.15, "z_dd")],
    "liq": [(0.10, "z_dvol")],
    "struct": [(0.10, "z_expense"), (0.10, "z_aum")],
}


_CHAR_PARTS_STOCK = [("z_roe", 0.10), ("z_margin", 0.08), ("z_earn", 0.07),
                      ("z_fcf", 0.05), ("z_blowoff", 0.08), ("z_ext", 0.04),
                      ("z_trend", 0.04), ("z_dte", 0.05), ("z_fpe", 0.03),
                      ("z_ptb", 0.02)]
_CHAR_PARTS_ETF = [("z_blowoff", 0.05), ("z_trend", 0.05), ("z_expense", 0.10),
                   ("z_aum", 0.10), ("z_dvol", 0.10)]


def log_character_breakdown(df, label, parts):
    """Per-ticker Composure decomposition: which sub-components drive character.

    Lets an independent reviewer judge the scorer's construction, not just its
    output — e.g. whether z_earn is doing real work or just adding noise.
    """
    log(f"--- character breakdown: {label} ({len(df)} tickers) ---")
    for _, r in df.iterrows():
        try:
            comps = []
            for z, w in parts:
                v = r.get(z, float("nan"))
                try:
                    v = float(v)
                except Exception:
                    v = float("nan")
                if v != v:  # NaN -> 0, mirroring the .fillna(0) in scoring
                    v = 0.0
                comps.append(f"{z[2:]} {w * v:+.2f}")
            log(f"  {r['ticker']:6s} char={float(r['character']):+.2f} "
                f"({' | '.join(comps)})")
        except Exception:
            continue


def log_score_breakdown(df, label, groups):
    """Log per-ticker grouped score contributions, e.g.
    DAC base=+0.17 (mom +0.42 | qual -0.10 | risk +0.05 | ...).
    This is the scorer stage, fully exposed: each group's weighted
    contribution to base_score."""
    if df is None or df.empty:
        return
    log(f"--- score breakdown: {label} ({len(df)} tickers) ---")
    for _, r in df.sort_values("base_score", ascending=False).iterrows():
        parts = []
        for gname, terms in groups.items():
            v = 0.0
            for w, col in terms:
                try:
                    x = r.get(col)
                    v += w * float(x) if pd.notna(x) else 0.0
                except Exception:
                    pass
            parts.append(f"{gname} {v:+.2f}")
        try:
            bs = f"{float(r['base_score']):+.2f}"
        except Exception:
            bs = "n/a"
        log(f"  {r['ticker']:6s} base={bs} ({' | '.join(parts)})")


def log_research_assessments(ranked):
    """Log each ticker's research-stage assessment: verdict + full rationale +
    risks + the news the researcher based it on.

    This is the research stage, fully exposed: what the researcher decided
    about each name, why, what could go wrong, and what news it read — so an
    independent reviewer can re-judge every call from the log alone.
    """
    log("--- research assessments ---")
    today = __import__("datetime").date.today().isoformat()
    n_fresh, n_carried, n_unknown = 0, 0, 0
    for _, r in ranked.iterrows():
        try:
            dep = float(r.get("llm_event_dependence", float("nan")))
            cont = float(r.get("llm_continuation", float("nan")))
            conf = float(r.get("llm_confidence", float("nan")))
            dep_s, cont_s, conf_s = f"{dep:.2f}", f"{cont:.2f}", f"{conf:.2f}"
        except Exception:
            dep_s = cont_s = conf_s = "n/a"
        # provenance: fresh research vs carried-over from an earlier run
        ad = str(r.get("llm_assessed_date", "") or "")
        if ad == today:
            prov, n_fresh = "fresh", n_fresh + 1
        elif ad:
            prov, n_carried = f"carried({ad})", n_carried + 1
        else:
            prov, n_unknown = "carried(?)", n_unknown + 1
        log(f"  {r['ticker']:6s} dep={dep_s} cont={cont_s} conf={conf_s} [{prov}]")
        why = str(r.get("llm_rationale", "") or "").replace("\n", " ").strip()
        if why:
            log(f"    rationale: {why}")
        risks = str(r.get("llm_risks", "") or "").strip()
        if risks:
            log(f"    risks: {risks}")
        try:
            headlines = [str(h).strip() for h in (r.get("headlines", []) or [])
                         if str(h).strip()]
        except Exception:
            headlines = []
        if headlines:
            log(f"    news seen ({len(headlines)}):")
            for h in headlines:
                log(f"      - {h[:220]}")
    log(f"research provenance: {n_fresh} fresh, {n_carried} carried-over, "
        f"{n_unknown} unknown (pre-dates date stamping)")


def log_world_layer(outputs):
    """Log the researcher's world/market context: drivers + risk events.

    This is the macro layer every per-ticker assessment was conditioned on.
    An independent reviewer needs it to judge whether the researcher read
    the market right (e.g. did it register a freight-rate spike before
    judging a tanker stock?). If the researcher skipped it, the log says so
    — a missing world layer is itself audit signal.
    """
    w = (outputs or {}).get("world") or {}
    drivers = w.get("drivers") or []
    risk_events = w.get("risk_events") or []
    log("--- world/market context (researcher's macro layer) ---")
    if not drivers and not risk_events:
        log("  world layer: NOT PROVIDED by researcher "
            "(outputs.json has no 'world' key)")
        return
    for d in drivers:
        log(f"  driver: {str(d.get('title', ''))[:200]} | "
            f"{str(d.get('summary', ''))[:300]} | "
            f"sectors={d.get('affected_sectors', [])} | "
            f"direction={d.get('direction', '')}")
    for e in risk_events:
        log(f"  risk event: {str(e.get('title', ''))[:200]} | "
            f"{str(e.get('summary', ''))[:300]} | "
            f"sectors={e.get('affected_sectors', [])} | "
            f"severity={e.get('severity', '')}")


def winsorize_base(df):
    """Cap base_score at the 95th percentile for reranking.

    Addresses the SBLK problem: a statistical outlier (base +1.62 vs +0.6
    for #2) built on trailing stats can dominate the final ordering even
    when half its thesis is event-driven. The raw score stays for display;
    reranking uses base_w. Returns (df, cap)."""
    df = df.copy()
    if df.empty or "base_score" not in df.columns:
        df["base_w"] = df.get("base_score", pd.Series(dtype=float))
        df.attrs["base_cap"] = float("inf")
        return df, float("inf")
    cap = float(df["base_score"].quantile(0.95))
    df["base_w"] = df["base_score"].clip(upper=cap)
    df.attrs["base_cap"] = cap
    clipped = df[df["base_score"] > cap]["ticker"].tolist()
    log(f"winsorize: base_score p95 cap={cap:+.2f}"
        + (f"; clipped: {clipped}" if clipped else "; nothing clipped"))
    return df, cap


def est_parts(row, dep, cont, conf=1.0):
    """Decompose the expected-value heuristic into auditable components.

    Returns dict(r6, upside, dd, dd_freq, downside, character, dep, cont, conf, est).
    estimate_next_year() is a thin wrapper over this; the log prints the
    components per ticker so any reviewer can re-derive every estimate
    from logged inputs alone.
    """
    try:
        r6 = float(row.get("ret_6m", 0) or 0)
    except Exception:
        r6 = 0.0
    try:
        dd = float(row.get("maxdd", -0.2) or -0.2)
    except Exception:
        dd = -0.2
    try:
        dep = float(dep)
    except Exception:
        dep = 0.3
    try:
        cont = float(cont)
    except Exception:
        cont = 0.5
    try:
        conf = max(0.0, min(1.0, float(conf)))
    except Exception:
        conf = 1.0
    try:
        ddf = max(0.0, min(1.0, float(row.get("dd_freq", 0) or 0)))
    except Exception:
        ddf = 0.0
    try:
        char = max(-2.0, min(2.0, float(row.get("character", 0) or 0)))
    except Exception:
        char = 0.0
    r6c = min(max(r6, 0.0), 0.50)
    upside = min(r6c, 0.25) + 0.5 * max(r6c - 0.25, 0.0)
    # downside: half the worst 1y drawdown, floored at 10%, scaled by how
    # OFTEN pain arrives (dd_freq): frequent dippers hurt more than rare ones.
    downside = max(abs(dd) * 0.5, 0.10) * (1 + ddf)
    # character: the Composure parts est doesn't already see (quality, entry
    # timing, structure/cost) adjust the whole estimate up/down by up to ~50%.
    est = conf * (1 + 0.25 * char) * (cont * upside - (1 - cont) * downside - dep * 0.20)
    return {"r6": r6, "upside": upside, "dd": dd, "dd_freq": ddf,
            "downside": downside, "character": char,
            "dep": dep, "cont": cont, "conf": conf, "est": est}


def estimate_next_year(row, dep, cont, conf=1.0):
    """Heuristic expected value, for display and ranking — NOT a prediction.

    EV = conf x (1 + 0.25 x character) x
         (cont x upside - (1 - cont) x downside - dep x event_unwind)
      upside      = last-6m run with a mean-reversion dampener: the first 25%
                    counts in full, anything beyond counts at half weight
                    (capped at 50%): monster half-years fade, so the estimate
                    must not let a historic run repeat at full weight;
      downside    = half the historical 1y max-drawdown magnitude (floor 10%),
                    scaled by (1 + drawdown frequency): names that dip 10%+
                    often hurt more than names with one deep dip;
      character   = the Composure parts this formula doesn't already see
                    (quality, entry timing, structure/cost), as a z-sum:
                    high-character names get their estimate lifted, low-
                    character names get it cut. Momentum and volatility are
                    deliberately excluded (ret_6m/maxdd already cover them);
      event_unwind= 20% haircut scaled by event_dependence;
      conf        = researcher's confidence in the assessment (0..1): low
                    confidence shrinks the estimate toward zero (neutral).
    Strong, stable continuation -> clearly positive; unstable -> near zero;
    obvious decliners -> negative.
    """
    return est_parts(row, dep, cont, conf)["est"]
    raw = cont * upside - (1 - cont) * downside - dep * 0.20
    return conf * raw


# ---------------- ETF pipeline ----------------
# ETFs have no ROE/margins/P-E; score on momentum + risk + structure/cost.
# Same goal: likely to continue, minimize going negative after buying.

US_ETF_EXCHANGES = {"NMS", "NYQ", "ASE", "ARC", "BAT", "NGM"}
LEVERAGE_PAT = None  # compiled lazily (re module import at top)


def _leverage_pat():
    global LEVERAGE_PAT
    if LEVERAGE_PAT is None:
        import re as _re
        LEVERAGE_PAT = _re.compile(
            r"2X|3X|LEVERAG|ULTRA|DIREXION|MICROSECTORS|INVERSE|\bSHORT\b|HEDGE"
            r"|BEAR\s*\d|BULL\s*\d|DAILY\s*(BULL|BEAR)", _re.IGNORECASE)
    return LEVERAGE_PAT


def get_etf_universe(target=120, min_price=5.0):
    """Top 52-week gaining US-listed, unleveraged ETFs via yfinance ETF screener.

    Tiles 52w% bands top-down (API caps page size). Leveraged/inverse and
    non-US listings are excluded up front — structural decay and closure
    risk are the opposite of 'don't go negative'.
    """
    from yfinance import ETFQuery, screen
    bands = [(150, None), (100, 150), (70, 100), (50, 70), (35, 50),
             (25, 35), (15, 25), (8, 15), (0, 8)]
    pat = _leverage_pat()
    seen, out = set(), []
    for lo, hi in bands:
        ops = [ETFQuery("gt", ["fiftytwowkpercentchange", lo]),
               ETFQuery("gt", ["intradayprice", min_price])]
        if hi is not None:
            ops.append(ETFQuery("lt", ["fiftytwowkpercentchange", hi]))
        qq = ETFQuery("and", ops)
        off = 0
        while True:
            try:
                r = screen(qq, sortField="fiftytwowkpercentchange",
                           sortAsc=False, size=250, offset=off)
            except Exception as e:
                log(f"ETF screener band {lo}-{hi} offset {off} failed: {e}")
                break
            qs = r.get("quotes", []) or []
            if not qs:
                break
            for x in qs:
                sym = x.get("symbol")
                if not sym or sym in seen:
                    continue
                seen.add(sym)
                if x.get("exchange") not in US_ETF_EXCHANGES:
                    continue
                name = str(x.get("shortName") or x.get("longName") or "")
                if pat.search(name) or pat.search(sym):
                    continue
                out.append({
                    "ticker": sym, "name": name,
                    "pct_52w": x.get("fiftyTwoWeekChangePercent"),
                    "screener_aum": x.get("netAssets"),
                    "screener_vol10": x.get("averageDailyVolume10Day"),
                })
                if len(out) >= target:
                    break
            if len(out) >= target or len(qs) < 250:
                break
            off += 250
            time.sleep(0.3)
        log(f"ETF universe: {len(out)} so far (band {lo}-{hi})")
        if len(out) >= target:
            break
        time.sleep(0.3)
    log(f"ETF universe final: {len(out)} US unleveraged ETFs")
    return out


def etf_facts(info):
    """Expense ratio, AUM, category from yfinance info (defensive)."""
    info = info or {}
    exp = info.get("expenseRatio")
    if exp is None:
        exp = info.get("annualReportNetExpenseRatio")
    aum = info.get("totalAssets")
    if aum is None:
        aum = info.get("netAssets")
    return {"expense_ratio": exp, "aum": aum,
            "category": info.get("category") or "Unknown",
            "family": info.get("fundFamily") or ""}


# Religious-themed securities (faith-based screens, religious mandates or
# issuers). Dan's rule: no religious stocks, ETFs, or similar, ever.
# Word-boundary matching; "church" deliberately excluded (Church & Dwight).
_RELIGIOUS_THEME_PAT = re.compile(
    r"\b(shariah|sharia|islamic|halal|wahed|biblical|bible|christian|"
    r"catholic|gospel|torah|kosher|faith)\b", re.IGNORECASE)
_RELIGIOUS_TICKERS = {"hlal", "spus", "bibl"}  # known faith-based ETFs whose
# names don't carry keywords (e.g. Inspire 100)


def _religious_theme(name, ticker=""):
    """True if a stock/ETF name or ticker is religious-themed."""
    if _RELIGIOUS_THEME_PAT.search(str(name or "")):
        return True
    return str(ticker or "").lower() in _RELIGIOUS_TICKERS


def _foreign_focus(info, name):
    """True if an ETF's focus is non-US (foreign/emerging/global mandate).

    Checks the Morningstar-style category first (reliable), falling back to
    the fund name for strong foreign keywords when the category is missing.
    """
    cat = str(etf_facts(info)["category"] or "")
    foreign_words = ("foreign", "emerging", "international", "global", "world",
                     "europe", "european", "asia", "asian", "pacific", "japan",
                     "japanese", "china", "chinese", "india", "latin", "canada",
                     "canadian", "frontier", "mexico", "brazil", "korea",
                     "taiwan", "ex-us")
    cl = cat.lower()
    if cat and cat.lower() != "unknown" and any(w in cl for w in foreign_words):
        return True
    # category missing/unknown: only strong name keywords count
    nl = str(name or "").lower()
    strong = ("emerging", "international", "foreign", "europe", "asia",
              "japan", "china", "msci eafe")
    return any(w in nl for w in strong)


def etf_trash_filter(rows, closes, infos, us_only=True):
    """Hard filters for ETFs: history, liquidity, scale, no blowups.
    Returns (kept_rows, report)."""
    kept, dropped = [], {"short_history": 0, "illiquid": 0, "tiny_aum": 0,
                         "blown_up": 0, "leveraged_name": 0, "foreign_focus": 0,
                         "religious_theme": 0}
    pat = _leverage_pat()
    for r in rows:
        t = r["ticker"]
        px = closes.get(t)
        if px is None or len(px) < 120:
            dropped["short_history"] += 1
            continue
        info = infos.get(t, {})
        name = str(info.get("longName") or r["name"] or "")
        if pat.search(name) or pat.search(t):
            dropped["leveraged_name"] += 1
            continue
        if _religious_theme(name, t):
            dropped["religious_theme"] += 1
            continue
        if us_only and _foreign_focus(info, name):
            dropped["foreign_focus"] += 1
            continue
        try:
            adv = (info.get("averageDailyVolume10Day")
                   or info.get("averageVolume")
                   or r.get("screener_vol10") or 0)
            lastpx = info.get("regularMarketPrice") or px.iloc[-1]
            if float(adv) * float(lastpx) < 2_000_000:
                dropped["illiquid"] += 1
                continue
        except Exception:
            pass
        aum = (etf_facts(info)["aum"] or r.get("screener_aum") or 0)
        try:
            if aum and float(aum) < 100_000_000:
                dropped["tiny_aum"] += 1
                continue
        except Exception:
            pass
        dd = max_drawdown(px)
        if dd < -0.70:
            dropped["blown_up"] += 1
            continue
        kept.append(r)
    report = {"kept": len(kept), "dropped_total": sum(dropped.values()), **dropped}
    return kept, report


def build_etf_scores(closes, infos):
    """Score ETFs: momentum 40%, low-risk/liquidity 40%, cost/scale 20%."""
    rows = []
    for t, px in closes.items():
        info = infos.get(t, {})
        if px is None or len(px) < 120:
            continue
        facts = etf_facts(info)
        row = {"ticker": t,
               "name": str(info.get("longName") or info.get("shortName") or t),
               "sector": facts["category"], "kind": "etf",
               "expense_ratio": facts["expense_ratio"], "aum": facts["aum"]}
        row["ret_1y"] = calc_return(px)
        row["ret_6m"] = calc_return(px, 126)
        row["ret_3m"] = calc_return(px, 63)
        row["ret_1m"] = calc_return(px, 21)
        row["ret_2w"] = calc_return(px, 10)
        sma50 = px.rolling(50).mean().iloc[-1] if len(px) >= 50 else np.nan
        sma200 = px.rolling(200).mean().iloc[-1] if len(px) >= 200 else np.nan
        sma20 = px.rolling(20).mean().iloc[-1] if len(px) >= 20 else np.nan
        last = px.iloc[-1]
        row["above50"] = 1.0 if not pd.isna(sma50) and last > sma50 else 0.0
        row["above200"] = 1.0 if not pd.isna(sma200) and last > sma200 else 0.0
        row["dist_20dma"] = float(last / sma20 - 1) if sma20 and not pd.isna(sma20) and sma20 > 0 else 0.0
        rets = px.pct_change().dropna()
        row["vol60"] = float(rets.tail(60).std() * np.sqrt(252)) if len(rets) >= 60 else np.nan
        row["maxdd"] = max_drawdown(px)
        row["dd_freq"] = drawdown_frequency(px)
        try:
            adv = info.get("averageDailyVolume10Day") or info.get("averageVolume") or 0
            lastpx = info.get("regularMarketPrice") or last
            row["dollar_vol10"] = float(adv) * float(lastpx)
        except Exception:
            row["dollar_vol10"] = 0.0
        rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["z_6m"] = zscore(df["ret_6m"].fillna(0))
    df["z_3m"] = zscore(df["ret_3m"].fillna(0))
    df["z_1m"] = zscore(df["ret_1m"].fillna(0))
    df["z_trend"] = zscore(df["above50"].fillna(0) + df["above200"].fillna(0))
    spike = (df["ret_2w"].fillna(0) - 0.15).clip(lower=0) + \
            (df["dist_20dma"].fillna(0) - 0.10).clip(lower=0)
    df["z_blowoff"] = zscore(-spike)
    df["z_vol"] = zscore(-df["vol60"].fillna(1.0))
    df["z_dd"] = zscore(-df["maxdd"].fillna(-0.5).abs())
    df["z_dvol"] = zscore(np.log1p(df["dollar_vol10"].fillna(0)))
    # lower expense ratio is better; missing -> neutral-ish (median)
    exp = pd.to_numeric(df["expense_ratio"], errors="coerce")
    df["z_expense"] = zscore(-exp.fillna(exp.median() if not exp.isna().all() else 0.005))
    aum = pd.to_numeric(df["aum"], errors="coerce")
    df["z_aum"] = zscore(np.log1p(aum.fillna(aum.median() if not aum.isna().all() else 1e8)))
    df["base_score"] = (
        0.12 * df["z_6m"] + 0.08 * df["z_3m"] + 0.10 * df["z_1m"]
        + 0.05 * df["z_trend"] + 0.05 * df["z_blowoff"]
        + 0.15 * df["z_vol"] + 0.15 * df["z_dd"] + 0.10 * df["z_dvol"]
        + 0.10 * df["z_expense"] + 0.10 * df["z_aum"]
    )
    # character: the ETF parts the EV doesn't already see — structure/cost/
    # liquidity + entry timing. Excludes momentum and risk z-scores (est uses
    # ret_6m/maxdd directly; no double-counting). EV multiplier.
    df["character"] = (
        0.05 * df["z_blowoff"] + 0.05 * df["z_trend"]
        + 0.10 * df["z_expense"] + 0.10 * df["z_aum"] + 0.10 * df["z_dvol"]
    ).fillna(0)
    return df.sort_values("base_score", ascending=False)


def make_chart_html(stocks_df, etfs_df, path, meta, titles=None, thesis=None,
                  honorable=None):
    """HTML chart: stocks + ETFs with 1y return, expected value, confidence.

    Columns: 1-year performance %, sector, and expected value %
    (continuation-vs-downside expected value heuristic). `titles` optionally
    overrides the two section headings. `thesis` is a list of status dicts
    for the Thesis watch section (ticker, days_held, ret_since_pick, status,
    reason). `honorable` is a list of dicts (ticker, kind, name, sector,
    ret_1y, est_next_1y, confidence, reason) rendered as the Honorable
    mentions table after the thesis section. Rows carrying a non-US `country`
    get a small flag. Pick tables are sortable client-side by expected value
    or 1-year return (descending).
    """
    import html as _html

    def pct(x):
        try:
            return f"{float(x):+.1%}"
        except Exception:
            return "n/a"

    def conf_pct(x):
        try:
            return f"{float(x):.0%}"
        except Exception:
            return "n/a"

    sections = [(titles[0] if titles else "Top 10 stocks — most confident", stocks_df),
                (titles[1] if titles else "Top 10 ETFs — most confident", etfs_df)]
    # Fixed reference scales, stated honestly in the note: a full bar means
    # something absolute, not "best of today's list." Full 1y bar = +150%
    # (a monster year); full EV bar = +20% (an exceptional expected value).
    # Per-column max scaling made the top pick look godly (100% fill) even
    # when its EV was merely good; fixed scales keep every bar an absolute
    # gauge — and comparable across days, since the scale never moves.
    REF_1Y, REF_EV = 1.50, 0.20

    def bar(val, gold=False):
        try:
            v = float(val)
        except Exception:
            return ""
        ref = REF_EV if gold else REF_1Y
        w = max(2, min(100, abs(v) / ref * 100))
        cls = "neg" if v < 0 else ("est" if gold else "pos")
        return (f'<div class="track"><div class="bar {cls}" style="width:{w:.1f}%">'
                f"</div></div>")

    def lbl(val, base):
        """Label div class: green 1y / gold est / red when negative."""
        try:
            neg = float(val) < 0
        except Exception:
            neg = False
        return f"lbl {base} neg" if neg else f"lbl {base}"

    header = """
<div class="row head">
  <div># / Ticker / Name</div><div>Sector / Category</div>
  <div>1-year return</div><div>Expected value</div>
  <div>Confidence</div><div>Stability</div>
</div>
"""
    # ticker -> latest thesis status, for watch/broken badges on pick rows
    thmap = {str(t.get("ticker")): t for t in (thesis or [])}
    rows_html = ""
    for si, (title, df) in enumerate(sections):
        rows_html += f'<h2>{_html.escape(title)}</h2>\n'
        rows_html += (
            f'<div class="sortctl" data-pl="pl{si}"><span>Sort by:</span> '
            f'<button class="sbtn on" data-k="ev">Expected value</button>'
            f'<button class="sbtn" data-k="r1y">1-year return</button></div>\n')
        rows_html += header + f'<div class="picklist" id="pl{si}">\n'
        for i, (_, r) in enumerate(df.iterrows(), 1):
            est = r.get("est_next_1y")
            ctry = str(r.get("country") or "")
            nonus = (f' <span class="nonus" title="Domiciled in {_html.escape(ctry)}">'
                     f"non-US</span>" if ctry and ctry != "United States" else "")
            th = thmap.get(str(r["ticker"]), {})
            tst = th.get("status", "intact")
            tbadge = (f' <span class="tbadge {tst}" '
                      f'title="{_html.escape(str(th.get("reason", "")))}">{tst}</span>'
                      if tst in ("watch", "broken") else "")
            try:
                _evf = float(est)
            except Exception:
                _evf = float("nan")
            try:
                _r1yf = float(r.get("ret_1y"))
            except Exception:
                _r1yf = float("nan")
            _kk = str(r.get("kind", "")).strip().lower()
            if _kk not in ("stock", "etf"):
                _kk = "stock" if si == 0 else "etf"
            rows_html += f"""
<div class="row k-{_kk}" data-ev="{_evf}" data-r1y="{_r1yf}">
  <div class="id"><span class="rank">{i}</span>
    <span class="tick">{_html.escape(str(r['ticker']))}</span>{tbadge}
    <span class="nm">{_html.escape(str(r['name'])[:38])}{nonus}</span></div>
  <div class="sec">{_html.escape(str(r['sector'])[:26])}</div>
  <div class="cell" data-cap="1-year return"><div class="{lbl(r['ret_1y'], 'r1y')}">{pct(r['ret_1y'])}</div>{bar(r['ret_1y'])}</div>
  <div class="cell" data-cap="Expected value"><div class="{lbl(est, 'est')}">{pct(est)}</div>{bar(est, gold=True)}</div>
  <div class="cf" data-cap="Confidence">{conf_pct(r.get('llm_confidence'))}</div>
  <div class="stab" data-cap="Stability">{_html.escape(str(r.get('stability', '?')))}</div>
</div>
"""
        rows_html += '</div>\n'  # close .picklist
    now = meta.get("asof", "")
    bench = meta.get("benchmark", "")
    etf_bench = meta.get("etf_benchmark", bench)
    bench_label = (f"{bench} (stocks) / {etf_bench} (ETFs)"
                   if etf_bench != bench else bench)
    # --- Thesis watch: one <details> whose summary IS the status line.
    # Boring state = a single collapsed line; the full tracked list expands
    # in place (no file download needed). Watch/broken chips ride in the
    # summary so the signal shows without expanding, and the section
    # auto-opens when something needs attention.
    thesis_html = ""
    if thesis:
        nb = sum(1 for t in thesis if t.get("status") == "broken")
        nw = sum(1 for t in thesis if t.get("status") == "watch")
        ni = len(thesis) - nb - nw
        _tsort = lambda t: ({"broken": 0, "watch": 1}.get(
            t.get("status"), 2), str(t.get("ticker", "")))
        chips = []
        for t in sorted(thesis, key=_tsort):
            if t.get("status") in ("watch", "broken"):
                try:
                    rsptxt = f" {float(t.get('ret_since_pick')):+.1%}"
                except Exception:
                    rsptxt = ""
                chips.append(
                    f'<span class="tbadge {t["status"]}" '
                    f'title="{_html.escape(str(t.get("reason", "")))}">'
                    f'{_html.escape(str(t.get("ticker", "")))} \u00b7 '
                    f'{t["status"]}{rsptxt}</span>')
        chips_html = (" " + " ".join(chips)) if chips else ""
        attn = " \u00b7 needs attention" if (nb or nw) else ""
        items = []
        for t in sorted(thesis, key=_tsort):
            st = str(t.get("status", "intact"))
            try:
                rsp = float(t.get("ret_since_pick"))
                if abs(rsp) < 0.0005:
                    rsp = 0.0
                rsptxt = f"{rsp:+.1%} since pick"
            except Exception:
                rsptxt = "\u2014"
            held = t.get("days_held", 0)
            items.append(
                f'<div class="titem"><b>{_html.escape(str(t.get("ticker", "")))}</b>'
                f'<span>held {held}d</span>'
                f'<span>{rsptxt}</span>'
                f'<span class="tstat {st}">{st}</span>'
                f'<span class="tnote">{_html.escape(str(t.get("reason", "")))}</span>'
                '</div>')
        open_attr = " open" if (nb or nw) else ""
        thesis_html = (
            f'<details class="twatch-det"{open_attr}>'
            f'<summary><b>Thesis watch</b> \u00b7 {len(thesis)} tracked \u00b7 {ni} intact \u00b7 '
            f'{nw} watch \u00b7 {nb} broken{attn}{chips_html}</summary>'
            '<div class="tlist">' + "\n".join(items) + '</div>'
            '<div class="tledger"><a href="thesis_ledger.jsonl">full ledger (JSONL)</a></div>'
            '</details>\n')
    # --- Honorable mentions: cleared the EV floor, didn't make the cut.
    # The near-miss table Dan asked for — alternatives worth a look, with the
    # reason each missed (cap, overlap, or final-score order).
    hm_html = ""
    if honorable:
        hm_rows = []
        for i, h in enumerate(honorable, 1):
            _hk = str(h.get("kind", "")).strip().lower()
            if _hk not in ("stock", "etf"):
                _hk = "stock"
            try:
                _hev = float(h.get("est_next_1y"))
            except Exception:
                _hev = float("nan")
            try:
                _hr1y = float(h.get("ret_1y"))
            except Exception:
                _hr1y = float("nan")
            hm_rows.append(f"""
<div class="row hm k-{_hk}" data-ev="{_hev}" data-r1y="{_hr1y}">
  <div class="id"><span class="rank">{i}</span>
    <span class="tick">{_html.escape(str(h.get('ticker', '')))}</span>
    <span class="kchip {_hk}">{_hk.upper()}</span>
    <span class="nm">{_html.escape(str(h.get('name', ''))[:38])}</span></div>
  <div class="sec">{_html.escape(str(h.get('sector', ''))[:26])}</div>
  <div class="cell" data-cap="1-year return"><div class="{lbl(h.get('ret_1y'), 'r1y')}">{pct(h.get('ret_1y'))}</div>{bar(h.get('ret_1y'))}</div>
  <div class="cell" data-cap="Expected value"><div class="{lbl(h.get('est_next_1y'), 'est')}">{pct(h.get('est_next_1y'))}</div>{bar(h.get('est_next_1y'), gold=True)}</div>
  <div class="cf" data-cap="Confidence">{conf_pct(h.get('confidence'))}</div>
  <div class="why" data-cap="Why not picked">{_html.escape(str(h.get('reason', '')))}</div>
</div>""")
        hm_html = (
            '<h2>Honorable mentions — cleared the bar, didn\u2019t make the cut</h2>\n'
            '<div class="sortctl" data-pl="pl2"><span>Sort by:</span> '
            '<button class="sbtn on" data-k="ev">Expected value</button>'
            '<button class="sbtn" data-k="r1y">1-year return</button></div>\n'
            '<div class="row head"><div># / Ticker / Name</div><div>Sector / Category</div>'
            '<div>1-year return</div><div>Expected value</div>'
            '<div>Confidence</div><div>Why not picked</div></div>\n'
            '<div class="picklist" id="pl2">\n'
            + "\n".join(hm_rows) + "\n</div>\n")
    html_doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Screener results vs { _html.escape(bench_label) } — { _html.escape(now) }</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Jost:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {{ color-scheme: dark;
  --green: #4ade80; --green-deep: #22c55e; --gold: #ffd54f; --gold-deep: #f59e0b;
  --red: #f87171; }}
body {{ font-family: 'Jost', -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif;
  max-width: 980px; margin: 24px auto; padding: 0 16px 40px; color: #f2f5f1;
  background:
    radial-gradient(900px 500px at 12% -4%, rgba(34,197,94,0.14), transparent 60%),
    radial-gradient(800px 520px at 88% 108%, rgba(245,158,11,0.12), transparent 60%),
    radial-gradient(600px 400px at 55% 45%, rgba(74,222,128,0.05), transparent 65%),
    #070b09; background-attachment: fixed; }}
h1 {{ font-size: 26px; font-weight: 700; letter-spacing: 0.05em;
  color: #a7f3c7;
  text-shadow: 0 2px 24px rgba(74,222,128,0.25); margin-bottom: 4px; }}
h1 span.chip {{ -webkit-text-fill-color: var(--gold); color: var(--gold); }}
.tagline {{ font-size: 15px; font-style: italic; letter-spacing: 0.06em;
  color: var(--gold); margin: 2px 0 2px; text-shadow: 0 0 14px rgba(255,213,79,0.3); }}
.subhead {{ font-size: 13px; color: #8a938a; margin-bottom: 18px; letter-spacing: 0.04em; }}
h1 span {{ -webkit-text-fill-color: #8a938a; color: #8a938a; }}
h2 {{ font-size: 15px; font-weight: 600; letter-spacing: 0.14em;
  text-transform: uppercase; margin: 30px 0 12px; color: var(--green);
  text-shadow: 0 0 18px rgba(74,222,128,0.35); }}
.row {{ display: grid; grid-template-columns: 290px 160px 1fr 1fr 80px 70px;
  gap: 10px; align-items: center; padding: 12px 16px; margin-bottom: 10px;
  background: rgba(255,255,255,0.045);
  -webkit-backdrop-filter: blur(14px) saturate(1.25); backdrop-filter: blur(14px) saturate(1.25);
  border: 1px solid rgba(255,255,255,0.09); border-radius: 16px;
  box-shadow: 0 8px 28px rgba(0,0,0,0.38), inset 0 1px 0 rgba(255,255,255,0.10); }}
.row.head {{ background: none; border: none; box-shadow: none;
  -webkit-backdrop-filter: none; backdrop-filter: none;
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.10em;
  color: #93a093; padding: 4px 16px 8px; margin-bottom: 2px; }}
.row > div {{ min-width: 0; overflow: hidden; }}
.row.head > div {{ overflow: visible; }}
.id {{ white-space: nowrap; text-overflow: ellipsis; }}
.id .nm {{ overflow: hidden; text-overflow: ellipsis; }}
.sec {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.id .rank {{ display:inline-block; width:22px; color: var(--gold); font-weight: 600; }}
.id .tick {{ font-weight: 700; margin-right: 8px; letter-spacing: 0.03em; color: #ffffff; }}
.id .nm {{ color:#a8b3a8; font-size: 12px; }}
.sec {{ font-size: 12px; color:#a8b3a8; }}
.lbl {{ font-size: 12px; margin-bottom: 4px; color: #93a093; }}
.lbl.r1y {{ color: var(--green); font-weight: 600; }}
.lbl.est {{ color: var(--gold); font-weight: 700; }}
.lbl.neg {{ color: var(--red) !important; }}
.track {{ background: rgba(255,255,255,0.07); height: 10px; border-radius: 6px;
  box-shadow: inset 0 1px 3px rgba(0,0,0,0.5); overflow: hidden; }}
.bar {{ height: 10px; border-radius: 6px; }}
.bar.pos {{ background: linear-gradient(90deg, var(--green-deep), var(--green));
  box-shadow: 0 0 12px rgba(74,222,128,0.55); }}
.bar.est {{ background: linear-gradient(90deg, var(--gold-deep), var(--gold));
  box-shadow: 0 0 12px rgba(255,213,79,0.55); }}
.bar.neg {{ background: linear-gradient(90deg, #dc2626, var(--red));
  box-shadow: 0 0 12px rgba(248,113,113,0.5); }}
.stab {{ font-size: 12px; color:#a8b3a8; text-align: right; }}
.cf {{ font-size: 13px; font-weight: 600; text-align: right; color: var(--gold); }}
.nonus {{ display: inline-block; font-size: 10px; font-weight: 600; color: #ffd54f;
  background: rgba(245,158,11,0.16); border: 1px solid rgba(255,213,79,0.35);
  border-radius: 8px; padding: 1px 7px; margin-left: 6px; vertical-align: 1px; }}
.note {{ margin-top: 26px; font-size: 12px; color: #9aa79a; line-height: 1.6;
  padding: 16px 20px; background: rgba(255,255,255,0.035);
  -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px);
  border: 1px solid rgba(255,255,255,0.08); border-radius: 16px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.08); }}
.note b {{ color: var(--gold); }}
.twatch-det {{ margin-top: 22px; border: 1px solid rgba(255,255,255,0.08);
  border-radius: 14px; background: rgba(255,255,255,0.02); }}
.twatch-det > summary {{ cursor: pointer; padding: 12px 18px; font-size: 13px;
  color: #9aa79a; list-style: none; line-height: 2; }}
.twatch-det > summary::-webkit-details-marker {{ display: none; }}
.twatch-det > summary::before {{ content: "▸  "; color: var(--gold); }}
.twatch-det[open] > summary::before {{ content: "▾  "; }}
.twatch-det > summary b {{ color: var(--gold); }}
.tlist {{ padding: 0 18px 6px; font-size: 12px; }}
.titem {{ display: flex; flex-wrap: wrap; gap: 4px 14px; align-items: baseline;
  padding: 5px 0; border-top: 1px solid rgba(255,255,255,0.05); color: #9aa79a; }}
.titem b {{ color: #e8ece8; }}
.tstat.intact {{ color: #4ade80; }} .tstat.watch {{ color: #fbbf24; }}
.tstat.broken {{ color: #f87171; }}
.tbadge {{ display: inline-block; font-size: 11px; font-weight: 700;
  border-radius: 999px; padding: 1px 9px; margin-left: 8px; white-space: nowrap; }}
.tbadge.watch {{ color: #fbbf24; background: rgba(251,191,36,0.12);
  border: 1px solid rgba(251,191,36,0.35); }}
.tbadge.broken {{ color: #f87171; background: rgba(248,113,113,0.12);
  border: 1px solid rgba(248,113,113,0.35); }}
.id .tbadge {{ margin-left: 6px; }}
.tledger {{ padding: 0 18px 12px; }}
.tledger a {{ color: #9aa79a; font-size: 12px;
  text-decoration: underline; text-underline-offset: 2px; }}
.sortctl {{ display: flex; align-items: center; gap: 8px; margin: 2px 0 10px;
  font-size: 12px; color: #93a093; letter-spacing: 0.06em; }}
.sbtn {{ font-family: inherit; font-size: 12px; letter-spacing: 0.04em;
  color: #a8b3a8; background: rgba(255,255,255,0.05);
  border: 1px solid rgba(255,255,255,0.12); border-radius: 999px;
  padding: 4px 14px; cursor: pointer; }}
.sbtn.on {{ color: #0a0f0c; font-weight: 700; background: var(--gold);
  border-color: var(--gold); box-shadow: 0 0 12px rgba(255,213,79,0.4); }}
.why {{ font-size: 12px; color: #a8b3a8; text-align: right; line-height: 1.4; }}
.row.hm {{ opacity: 0.88; }}
.row.k-stock .tick {{ color: #7dd3fc; }}
.row.k-etf .tick {{ color: #c4b5fd; }}
.kchip {{ display: inline-block; font-size: 10px; font-weight: 700;
  letter-spacing: 0.08em; border-radius: 8px; padding: 1px 7px;
  margin-left: 6px; vertical-align: 1px; white-space: nowrap; }}
.kchip.stock {{ color: #7dd3fc; background: rgba(125,211,252,0.12);
  border: 1px solid rgba(125,211,252,0.35); }}
.kchip.etf {{ color: #c4b5fd; background: rgba(196,181,253,0.12);
  border: 1px solid rgba(196,181,253,0.35); }}
@media (max-width: 700px) {{
  h1 {{ font-size: 20px; }}
  .row {{ grid-template-columns: 1fr 1fr; row-gap: 10px; padding: 14px; }}
  .row.head {{ display: none; }}
  .id, .sec {{ grid-column: 1 / -1; }}
  .cell::before, .cf::before, .stab::before {{
    content: attr(data-cap); display: block;
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em;
    color: #93a093; margin-bottom: 3px; }}
  .cf, .stab {{ text-align: left; font-size: 14px; }}
}}
</style></head>
<body>
<h1>Mint <span class="chip">(Chip)</span></h1>
<div class="tagline">Straight from the Mint.</div>
<div class="subhead">{ _html.escape(meta.get("heading") or f"Top picks vs {bench_label}") } — { _html.escape(now) }</div>
{rows_html}
{thesis_html}
{hm_html}
<div class="note">
<b>How to read this.</b> We look at the strongest American stocks and ETFs of
the past year &mdash; the ones that beat the market &mdash; and ask which are most
likely to <i>keep</i> doing well without dropping right after you buy.
Avoiding losses comes first. Every name here earned its place; we would rather
leave a slot empty than fill it with something we don&apos;t believe in.

"1y" is how much it gained over the past year
(stocks vs { _html.escape(bench) }, ETFs vs { _html.escape(etf_bench) }).
"Expected value" is our honest estimate of what could come next &mdash; think of
it as a weather forecast, not a promise. It starts with the recent run, then
asks the hard questions: how often does this name stumble, and how bad are the
stumbles? Is the story built on real business strength, or on hype, headlines,
and one-time events? The shakier the answers, the more we shrink the estimate
toward zero.

Confidence is how much we trust our research on the name. Stability
(A is the calmest) is about how bumpy the ride has been. { "All tickers you supplied are shown, sorted by expected value &mdash; red estimates are the warning, not a recommendation." if meta.get("mode") == "watchlist" else "Anything below +3% expected value does not make the list." }

The bars use fixed scales &mdash; a full bar always means +150% past-year gain or
+20% expected value &mdash; so you can compare one day against the next, honestly.
Thesis watch keeps score on every pick we have published; expand it to see how
they are doing. Honorable mentions are strong names that just missed the cut,
with the reason why. You can re-sort any table by past-year gain or expected
value.

Not financial advice. Past performance doesn&apos;t
predict future returns.
</div>
<script>
document.querySelectorAll('.sortctl').forEach(function(ctl){{
  var pl = document.getElementById(ctl.getAttribute('data-pl'));
  if(!pl) return;
  ctl.querySelectorAll('.sbtn').forEach(function(btn){{
    btn.addEventListener('click', function(){{
      ctl.querySelectorAll('.sbtn').forEach(function(b){{ b.classList.remove('on'); }});
      btn.classList.add('on');
      var k = btn.getAttribute('data-k');
      var rows = Array.prototype.slice.call(pl.querySelectorAll('.row'));
      rows.sort(function(a, b){{
        var va = parseFloat(a.getAttribute('data-' + k));
        var vb = parseFloat(b.getAttribute('data-' + k));
        va = isNaN(va) ? -Infinity : va;
        vb = isNaN(vb) ? -Infinity : vb;
        return vb - va;
      }});
      rows.forEach(function(r, i){{
        pl.appendChild(r);
        var rk = r.querySelector('.rank');
        if(rk) rk.textContent = i + 1;
      }});
    }});
  }});
}});
</script>
</body></html>
"""
    with open(path, "w") as f:
        f.write(html_doc)
    log(f"Chart written: {path}")


# ---------------- News layer: dynamic risk discovery ----------------

def fetch_rss_headlines(query, max_items=30):
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        feed = feedparser.parse(url)
        return [e.title for e in feed.entries[:max_items] if hasattr(e, "title")]
    except Exception:
        return []


def fetch_market_headlines():
    """Recent market headlines from free RSS (shared by rules + LLM layers)."""
    headlines = []
    for q in ["stock market", "global economy markets", "wall street"]:
        headlines += fetch_rss_headlines(q, 30)
        time.sleep(0.5)
    # dedupe, keep order
    seen, out = set(), []
    for h in headlines:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def discover_risk_themes(cache=None):
    """Dynamically discover what's spiking in market news right now.

    No hardcoded events. We look at frequent non-trivial words in recent
    market headlines — whatever is being talked about a lot *right now*
    becomes the risk theme set.
    """
    log("Discovering dynamic risk themes from recent market news...")
    headlines = None
    if cache is not None:
        headlines = cache.get("market_headlines")
    if headlines is None:
        headlines = fetch_market_headlines()
        if cache is not None:
            cache.put("market_headlines", headlines)
    if not headlines:
        log("  no market headlines fetched, skipping theme discovery")
        return []

    words = []
    for h in headlines:
        toks = re.findall(r"[a-z]{4,}", h.lower())
        words += [w for w in toks if w not in STOPWORDS]
    freq = Counter(words)
    # Top frequent unusual words = what's dominating news right now
    themes = [w for w, _ in freq.most_common(25)]
    log(f"  discovered themes: {', '.join(themes[:12])}...")
    return themes


# Foreign exchange tags used to spot ticker collisions across markets.
# IEX = IDEX Corp (NYSE) AND Indian Energy Exchange (NSEI) — headlines about
# the wrong company used to pollute the research evidence.
_FOREIGN_XTAGS = ("NSEI", "NSE", "BSE", "SGX", "HKEX", "LSE", "TSE", "ASX")


def _entity_mismatch(headline, ticker):
    """True if the headline's ticker tag points at a foreign listing.

    Tickers collide across exchanges, so a headline like 'Indian Energy
    Exchange (NSEI:IEX)' is evidence about a different company than the US
    stock with the same ticker. Only fires when the foreign tag actually
    references our ticker, so US headlines are never dropped.
    """
    if not headline or not ticker:
        return False
    hl = str(headline).upper()
    t = str(ticker).upper()
    return any(f"{x}:{t}" in hl for x in _FOREIGN_XTAGS)


def fetch_stock_headlines(ticker, name):
    headlines = []
    # yfinance news (free)
    try:
        news = yf.Ticker(ticker).news or []
        for n in news:
            t = n.get("title") if isinstance(n, dict) else None
            if t:
                headlines.append(t)
    except Exception:
        pass
    # Google News RSS: ticker query AND company-name query. The name query was
    # added because ticker-only search conflates same-ticker foreign companies
    # (e.g. IEX -> Indian Energy Exchange headlines).
    headlines += fetch_rss_headlines(f"{ticker} stock", 12)
    if name:
        headlines += fetch_rss_headlines(f'"{name}" stock', 12)
    # drop foreign-entity mismatches, then dedupe, keep order
    dropped = [h for h in headlines if _entity_mismatch(h, ticker)]
    if dropped:
        log(f"news: entity filter dropped {len(dropped)} foreign-ticker "
            f"headlines for {ticker}")
    seen, out = set(), []
    for h in headlines:
        if h not in seen and h not in dropped:
            seen.add(h)
            out.append(h)
    return out[:20]


def news_risk_penalty(headlines, risk_themes):
    """0..1 penalty: higher = more event/negative-news exposure right now."""
    if not headlines:
        return 0.3  # unknown = mild penalty, don't blindly trust
    text = " ".join(headlines).lower()
    # 1) overlap with dynamically discovered market themes
    overlap = sum(1 for th in risk_themes if th in text)
    theme_score = min(overlap / 5.0, 1.0)  # 5+ theme hits = max
    # 2) negative tone ratio (generic words, not event-specific)
    neg_hits = sum(1 for h in headlines if any(w in h.lower() for w in NEGATIVE_WORDS))
    neg_ratio = neg_hits / max(len(headlines), 1)
    return float(0.5 * theme_score + 0.5 * min(neg_ratio * 2, 1.0))


def apply_news_adjustment(df, risk_themes, top_k=50, cache=None, w_outlier=1.0):
    log(f"Fetching stock news for top {min(top_k, len(df))} scorers...")
    penalties, news_counts, samples = [], [], []
    candidates = df.head(top_k)
    for i, (_, r) in enumerate(candidates.iterrows()):
        t = r["ticker"]
        if i % 10 == 0:
            log(f"  news {i}/{len(candidates)}...")
        headlines = cache.get(f"news_{t}") if cache is not None else None
        if headlines is None:
            headlines = fetch_stock_headlines(t, r["name"])
            if cache is not None:
                cache.put(f"news_{t}", headlines)
            time.sleep(0.4)
        pen = news_risk_penalty(headlines, risk_themes)
        penalties.append(pen)
        news_counts.append(len(headlines))
        samples.append("; ".join(headlines[:2]))
    candidates = candidates.copy()
    candidates["news_penalty"] = penalties
    candidates["news_count"] = news_counts
    candidates["news_sample"] = samples
    # Adjust: penalize high news-risk names; winsorized base + outlier*event
    # interaction (same SBLK logic as the LLM path, news_penalty as dep proxy)
    base = candidates["base_w"] if "base_w" in candidates.columns else candidates["base_score"]
    if "base_cap" in candidates.columns:
        cap = pd.to_numeric(candidates["base_cap"], errors="coerce")
    else:
        cap = pd.Series(candidates.attrs.get("base_cap", float("inf")), index=candidates.index)
    excess = (candidates["base_score"] - cap).clip(lower=0).fillna(0)
    candidates["adj_score"] = (base - 1.2 * candidates["news_penalty"]
                               - w_outlier * excess * candidates["news_penalty"])
    return candidates.sort_values("adj_score", ascending=False)


def _ev_of(r):
    """Expected value as a float; NaN sorts as -inf so it never wins a tiebreak."""
    try:
        v = float(r.get("est_next_1y", float("nan")))
        return v if v == v else float("-inf")
    except Exception:
        return float("-inf")


# ---------------- ETF holdings-overlap dedupe ----------------
# A second wrapper on the same theme is redundancy, not diversification.
# The category cap can't see this (CIBR and BUG are both "Technology" but the
# real issue is they hold the same stocks). Pairwise top-10 holdings overlap
# above --max-etf-overlap keeps only the higher-EV fund.
_ETF_HOLDINGS_CACHE = "etf_holdings_cache.json"
_ETF_HOLDINGS_TTL_DAYS = 7


def _etf_holdings_cache_load():
    import json as _json
    try:
        return _json.load(open(_ETF_HOLDINGS_CACHE))
    except Exception:
        return {}


def etf_top_holdings(ticker):
    """Top-10 holdings {symbol: weight} for an ETF, 7-day file-cached."""
    import json as _json
    cache = _etf_holdings_cache_load()
    today = datetime.now().date().isoformat()
    hit = cache.get(ticker)
    if hit:
        try:
            age = (datetime.now().date() -
                   datetime.fromisoformat(hit["date"]).date()).days
            if age <= _ETF_HOLDINGS_TTL_DAYS:
                return {k: float(v) for k, v in hit["holdings"].items()}
        except Exception:
            pass
    try:
        th = yf.Ticker(ticker).funds_data.top_holdings
        if th is None or not len(th):
            log(f"etf overlap: holdings unavailable for {ticker} "
                f"(no data) — pair skipped")
            return {}
        col = ("Holding Percent" if "Holding Percent" in th.columns
               else th.columns[-1])
        holds = {str(s).upper(): float(w)
                 for s, w in zip(th.index, th[col])}
    except Exception as e:
        log(f"etf overlap: holdings unavailable for {ticker} ({e}) — "
            f"pair skipped")
        return {}
    cache[ticker] = {"date": today, "holdings": holds}
    try:
        _json.dump(cache, open(_ETF_HOLDINGS_CACHE, "w"))
    except Exception:
        pass
    return holds


def holdings_overlap(h1, h2):
    """Sum of min weights over common holdings (0..~1)."""
    return sum(min(h1[s], h2[s]) for s in set(h1) & set(h2))


def apply_etf_overlap_cap(edf, max_overlap=0.30):
    """Drop the lower-EV member of ETF pairs whose top-10 holdings overlap
    exceeds max_overlap. Runs on the EV-floor passers, before selection."""
    df = edf.sort_values("est_next_1y", ascending=False).copy()
    tickers = list(df["ticker"])
    if len(tickers) < 2:
        return df
    log(f"etf overlap: checking {len(tickers)} floor-passing ETFs "
        f"(max overlap {max_overlap:.0%}, top-10 holdings)")
    holds = {t: etf_top_holdings(t) for t in tickers}
    ev = {t: float(df.loc[df["ticker"] == t, "est_next_1y"].iloc[0])
          for t in tickers}
    pairs = []
    for i in range(len(tickers)):
        for j in range(i + 1, len(tickers)):
            a, b = tickers[i], tickers[j]
            if not holds[a] or not holds[b]:
                continue
            pairs.append((holdings_overlap(holds[a], holds[b]), a, b))
    pairs.sort(reverse=True)
    alive, dropped = set(tickers), []
    global _LAST_OVERLAP_DROPS
    _LAST_OVERLAP_DROPS = {}
    for ov, a, b in pairs:
        if ov <= max_overlap or a not in alive or b not in alive:
            continue
        loser = a if ev[a] < ev[b] else b
        winner = b if loser == a else a
        alive.discard(loser)
        dropped.append(loser)
        _LAST_OVERLAP_DROPS[loser] = (winner, ov)
        log(f"etf overlap: {loser} overlaps {winner} {ov:.0%} "
            f"(top-10 holdings) > {max_overlap:.0%} — lower EV "
            f"({ev[loser]:+.1%} vs {ev[winner]:+.1%}) excluded")
        ledger_append(REJECTED_LEDGER, {
            "event": "rejected", "ticker": loser, "kind": "etf",
            "name": str(df.loc[df['ticker'] == loser, 'name'].iloc[0]),
            "price": None,
            "reason": (f"etf holdings overlap {ov:.0%} with {winner} "
                       f"> {max_overlap:.0%} (lower EV)"),
            "est_next_1y": round(ev[loser], 4),
            "dep": _safe(df.loc[df['ticker'] == loser,
                                'llm_event_dependence'].iloc[0]),
            "cont": _safe(df.loc[df['ticker'] == loser,
                                 'llm_continuation'].iloc[0]),
            "conf": _safe(df.loc[df['ticker'] == loser,
                                 'llm_confidence'].iloc[0]),
        })
    if dropped:
        log(f"etf overlap: excluded {len(dropped)} redundant ETFs: {dropped}")
    return df[df["ticker"].isin(alive)]


def honorable_mentions(ranked, final, args, top_k=10):
    """Near-miss table: researched names that cleared the EV floor but didn't
    make the chart — alternatives worth a look, each with the reason it
    missed (overlap, cap, or final-score order). Sorted by EV, descending."""
    if ranked is None or ranked.empty or final is None:
        return []
    if "est_next_1y" not in ranked.columns:
        # pick_final enriches a copy via add_research_columns; the caller's
        # frame lacks est_next_1y — enrich here so near-misses can be ranked
        ranked = add_research_columns(ranked, llm_path=True)
    picked = set(final["ticker"]) if len(final) else set()
    veto = getattr(args, "veto_dep", 0.7) or 0.7
    floor = getattr(args, "min_est", 0.03)
    contenders = []
    for _, r in ranked.iterrows():
        t = str(r["ticker"])
        if t in picked or bool(r.get("llm_excluded")):
            continue
        try:
            ev = float(r["est_next_1y"])
        except Exception:
            continue
        if ev < floor:
            continue
        try:
            if (r.get("llm_event_dependence") is not None
                    and float(r["llm_event_dependence"]) > veto):
                continue
        except Exception:
            pass
        contenders.append(r)
    caps = {"stock": getattr(args, "max_per_sector", 2),
            "etf": getattr(args, "max_per_etf_category", 2)}
    sec_counts = {}
    for _, r in final.iterrows():
        k = (str(r.get("kind", "")), str(r.get("sector", "")))
        sec_counts[k] = sec_counts.get(k, 0) + 1
    out = []
    for r in sorted(contenders, key=lambda r: float(r["est_next_1y"]),
                    reverse=True)[:top_k]:
        t = str(r["ticker"])
        kind, sec = str(r.get("kind", "stock")), str(r.get("sector", ""))
        if t in _LAST_OVERLAP_DROPS:
            w, ov = _LAST_OVERLAP_DROPS[t]
            reason = f"overlaps {w} ({ov:.0%}) — kept the higher-EV fund"
        elif sec_counts.get((kind, sec), 0) >= caps.get(kind, 2):
            reason = f"{sec} cap full"
        else:
            reason = "edged out on final-score order"
        try:
            conf = float(r.get("llm_confidence"))
        except Exception:
            conf = None
        out.append({"ticker": t, "kind": kind, "name": str(r.get("name", "")),
                    "sector": sec, "ret_1y": r.get("ret_1y"),
                    "est_next_1y": float(r["est_next_1y"]), "confidence": conf,
                    "reason": reason})
    if out:
        log(f"honorable mentions: {len(out)} near-misses "
            f"({', '.join(h['ticker'] for h in out)})")
    return out


def pick_top_with_sector_cap(df, n=10, max_per_sector=2, min_score=0.0,
                             veto_dep=None, initial_counts=None):
    """Pick top n with sector cap. Never fills slots with sub-floor or
    vetoed stocks: fewer strong picks beats 10 diluted ones.
    EV tiebreak: when the cap forces a choice inside a sector, the
    highest-expected-value names survive — a hard cap should cut the
    lowest-EV name, not whichever final_score happened to order last.
    initial_counts seeds the per-sector tally (used by the fill pass so the
    cap stays hard across both passes)."""
    picked, skipped = [], Counter()
    skipped_names = {}
    sector_counts = Counter(initial_counts) if initial_counts else Counter()
    picked_by_sector = {}  # sec -> list of picked rows (enables the EV tiebreak)
    for _, r in df.iterrows():
        if r["final_score"] < min_score:
            skipped["below_floor"] += 1
            skipped_names.setdefault("below_floor", []).append(r["ticker"])
            continue
        if veto_dep is not None and "llm_event_dependence" in df.columns:
            try:
                if float(r["llm_event_dependence"]) > veto_dep:
                    skipped["vetoed_event_dep"] += 1
                    skipped_names.setdefault("vetoed_event_dep", []).append(r["ticker"])
                    continue
            except Exception:
                pass
        sec = r["sector"]
        if sector_counts[sec] >= max_per_sector:
            cur_ev = _ev_of(r)
            incumbents = picked_by_sector.get(sec, [])
            if incumbents:
                weakest = min(incumbents, key=_ev_of)
                if cur_ev > _ev_of(weakest):
                    # EV tiebreak: swap the weakest picked name in this sector out
                    picked = [x for x in picked if x is not weakest]
                    picked_by_sector[sec] = [x for x in incumbents if x is not weakest]
                    skipped["sector_cap"] += 1
                    skipped_names.setdefault("sector_cap", []).append(
                        f"{weakest['ticker']}({sec},ev={_ev_of(weakest):+.1%})")
                    log(f"  ev_tiebreak: {r['ticker']} ({cur_ev:+.1%}) replaces "
                        f"{weakest['ticker']} ({_ev_of(weakest):+.1%}) in {sec}")
                    picked.append(r)
                    picked_by_sector[sec].append(r)
                    # sector_counts unchanged: still at cap
                    if len(picked) >= n:
                        break
                    continue
            skipped["sector_cap"] += 1
            skipped_names.setdefault("sector_cap", []).append(
                f"{r['ticker']}({sec})")
            continue
        picked.append(r)
        picked_by_sector.setdefault(sec, []).append(r)
        sector_counts[sec] += 1
        if len(picked) >= n:
            break
    log(f"pick_top: {len(picked)} selected (floor={min_score}, veto_dep={veto_dep}), "
        f"skipped={dict(skipped)}")
    for reason, names in skipped_names.items():
        log(f"  skipped[{reason}]: {names}")
    return pd.DataFrame(picked)


def run_self_check(final, args, out_csv):
    """Automatic post-run validation. Returns list of failures (alert!)."""
    import os
    checks = []

    def check(name, ok, detail=""):
        ok = bool(ok)
        checks.append({"name": name, "ok": ok, "detail": str(detail)})
        log(f"SELF-CHECK {'PASS' if ok else 'FAIL'}: {name} {detail}")

    check("csv_written", os.path.exists(out_csv), out_csv)
    if len(final):
        check("no_nan_final_score", not final["final_score"].isna().any())
        check("no_nan_base_score", not final["base_score"].isna().any())
        check("floor_respected",
              bool((final["final_score"] >= args.min_score - 1e-9).all()),
              f"min_score={args.min_score}")
        if "kind" in final.columns:
            for kind, cap in (("stock", args.max_per_sector),
                              ("etf", args.max_per_etf_category)):
                sub = final[final["kind"] == kind]
                if len(sub):
                    # the category cap is hard across both pick passes
                    sc = sub["sector"].value_counts()
                    check(f"sector_cap_{kind}",
                          bool((sc <= cap).all()), f"max/{kind}={sc.max()}")
        else:
            sc = final["sector"].value_counts()
            check("sector_cap", bool((sc <= args.max_per_sector).all()),
                  f"max/sector={sc.max()}")
        if "vol60" in final.columns:
            check("risk_gates",
                  bool(((final["vol60"] <= args.max_vol + 1e-9) &
                        (final["maxdd"] >= args.min_dd - 1e-9)).all()))
        if "llm_event_dependence" in final.columns and args.veto_dep is not None:
            check("dep_veto",
                  bool((final["llm_event_dependence"] <= args.veto_dep + 1e-9).all()),
                  f"veto_dep={args.veto_dep}")
        if "est_next_1y" in final.columns:
            check("est_floor",
                  bool((final["est_next_1y"] >= args.min_est - 1e-9).all()),
                  f"min_est={args.min_est}")
        check("has_stability_grade", "stability" in final.columns)
        check("has_confidence", "llm_confidence" in final.columns)
    fails = [c for c in checks if not c["ok"]]
    log(f"SELF-CHECK: {len(checks) - len(fails)}/{len(checks)} passed")
    return checks, fails


def write_run_summary(path, payload):
    import json as _json
    try:
        if "philosophy" not in payload:
            phil = _load_philosophy()
            if phil:
                payload["philosophy"] = phil
        with open(path, "w") as f:
            _json.dump(payload, f, indent=1, default=str)
        log(f"Run summary: {path}")
    except Exception as e:
        log(f"Could not write run summary: {e}")


def _safe(x):
    try:
        f = float(x)
        return f if f == f else None  # NaN -> None
    except Exception:
        return None


def _print_llm_final(final_stocks, final_etfs, args):
    print("\n" + "=" * 70)
    print(f"TOP PICKS vs {args.benchmark} — {len(final_stocks)} stocks + "
          f"{len(final_etfs)} ETFs — LLM reranked")
    print("=" * 70)
    for title, grp in (("STOCKS", final_stocks), ("ETFs", final_etfs)):
        print(f"\n{title}")
        for i, (_, r) in enumerate(grp.iterrows(), 1):
            dep = r.get("llm_event_dependence", float("nan"))
            cont = r.get("llm_continuation", float("nan"))
            try:
                est = f"{float(r['est_next_1y']):+.1%}"
            except Exception:
                est = "n/a"
            try:
                cf = f"{float(r['llm_confidence']):.0%}"
            except Exception:
                cf = "n/a"
            print(
                f"{i:2d}. {r['ticker']:8s} | {r['sector'][:20]:20s} | "
                f"1y {r['ret_1y']:+.0%} | est next 1y {est} | conf {cf} | "
                f"dep {dep:.2f} | cont {cont:.2f} | final {r['final_score']:+.2f}"
            )
            print(f"    {r['name'][:60]}")
            if r.get("llm_rationale"):
                print(f"    why: {str(r['llm_rationale'])[:160]}")
            if r.get("llm_risks"):
                print(f"    risks: {str(r['llm_risks'])[:160]}")
    print("\nNote: past performance doesn't predict future returns. This is a")
    print("screening tool, not financial advice.")
    log(f"Phase B complete: {len(final_stocks)} stocks + {len(final_etfs)} ETFs")
    # final picks in the log too, so the log alone carries the full audit trail
    log("--- final picks ---")
    for title, grp in (("stocks", final_stocks), ("etfs", final_etfs)):
        for i, (_, r) in enumerate(grp.iterrows(), 1):
            try:
                est = f"{float(r['est_next_1y']):+.1%}"
            except Exception:
                est = "n/a"
            try:
                cf = f"{float(r['llm_confidence']):.0%}"
            except Exception:
                cf = "n/a"
            log(f"  {i:2d}. [{title}] {r['ticker']:6s} {str(r['sector'])[:22]:22s} "
                f"1y={r['ret_1y']:+.0%} est={est} conf={cf} "
                f"stab={r.get('stability', '?')} final={r['final_score']:+.2f}")


def etf_benchmark(args):
    """Benchmark ticker for the ETF leg (defaults to the stock benchmark)."""
    return args.etf_benchmark or args.benchmark


def run_etf_pipeline(args, cache, bench_ret, bench_name):
    """Full ETF leg: universe -> outperformers -> infos -> trash -> scores -> gates.

    Returns a scored, gated, winsorized DataFrame (may be empty).
    """
    from cache import StepCache
    etf_universe = cache.get("etf_universe_v2")
    if etf_universe is None:
        etf_universe = get_etf_universe(target=200, min_price=args.min_price)
        cache.put("etf_universe_v2", etf_universe)
    if not etf_universe:
        log("ETF universe empty; skipping ETF leg")
        return pd.DataFrame()
    bench_pct = bench_ret * 100
    ecands = [u for u in etf_universe
              if u.get("pct_52w") and u["pct_52w"] > bench_pct - 5]
    ecands.sort(key=lambda d: d["pct_52w"] or 0, reverse=True)
    print(f"\n{len(ecands)} candidate ETF outperformers "
          f"(screener 52w% > {bench_pct - 5:.0f}%)")
    etickers = [c["ticker"] for c in ecands]
    ekey = "etf_prices_" + StepCache.tickers_key(etickers)
    ecloses = cache.get(ekey)
    if ecloses is None:
        ecloses = download_prices(etickers, period="1y")
        cache.put(ekey, ecloses)
    eout = []
    for c in ecands:
        s = ecloses.get(c["ticker"])
        r = calc_return(s) if s is not None else np.nan
        if not pd.isna(r) and r > bench_ret:
            c["ret_1y"] = r
            eout.append(c)
    eout.sort(key=lambda d: d["ret_1y"], reverse=True)
    print(f"{len(eout)} confirmed ETF outperformers beat {bench_name} "
          f"({bench_ret:+.1%} 1y)")
    if not eout:
        return pd.DataFrame()
    fkey = "etf_fundamentals_" + StepCache.tickers_key([c["ticker"] for c in eout])
    einfos = cache.get(fkey) or {}
    emiss = [c["ticker"] for c in eout if c["ticker"] not in einfos]
    if emiss:
        log(f"ETF fundamentals: {len(einfos)} cached, fetching {len(emiss)} missing")
        einfos.update(get_fundamentals(emiss))
        cache.put(fkey, einfos)
    ekept, etrash = etf_trash_filter(eout, ecloses, einfos, us_only=args.us_only)
    log(f"ETF trash filter: {etrash['dropped_total']} dropped, "
        f"{etrash['kept']} kept ({etrash})")
    print(f"ETF trash filter: {etrash['dropped_total']} dropped, {etrash['kept']} kept")
    ekept_t = [c["ticker"] for c in ekept]
    einfos = {t: i for t, i in einfos.items() if t in ekept_t}
    ecloses = {t: s for t, s in ecloses.items() if t in ekept_t}
    edf = build_etf_scores(ecloses, einfos)
    if edf.empty:
        return edf
    print(f"Scored {len(edf)} ETFs. Top 5:")
    for _, r in edf.head(5).iterrows():
        print(f"  {r['ticker']:8s} {str(r['sector'])[:22]:22s} "
              f"base={r['base_score']:+.2f} 1y={r['ret_1y']:+.0%}")
    log(f"etf base_score stats: {edf['base_score'].describe().to_dict()}")
    log_score_breakdown(edf, "etfs", _ETF_SCORE_GROUPS)
    _etf_pool = edf.head(args.n_etf_research) if not edf.empty else edf
    log_character_breakdown(_etf_pool, "etfs (research pool)", _CHAR_PARTS_ETF)
    edf, egate = apply_risk_gates(edf, max_vol=args.max_vol, min_dd=args.min_dd)
    edf, ecap = winsorize_base(edf)
    edf["base_cap"] = ecap
    log(f"ETF gates: {egate['gated_total']} excluded; winsorize cap={ecap:+.2f}")
    return edf


def add_research_columns(df, llm_path=True):
    """Add dep/cont/conf/est/stability columns from the research layer.

    No picking — just enriches. llm_path=True uses the LLM research columns;
    False falls back to the rules-based news layer columns.
    """
    dep_col = "llm_event_dependence" if llm_path else "news_penalty"
    cont_col = "llm_continuation" if llm_path else None
    conf_col = "llm_confidence" if llm_path else None
    sub = df.copy()
    if dep_col in sub.columns:
        dep = pd.to_numeric(sub[dep_col], errors="coerce").fillna(0.3)
    else:
        dep = pd.Series(0.3, index=sub.index)
    if cont_col and cont_col in sub.columns:
        cont = pd.to_numeric(sub[cont_col], errors="coerce").fillna(0.5)
    else:
        cont = pd.Series(0.5, index=sub.index)
    if conf_col and conf_col in sub.columns:
        conf = pd.to_numeric(sub[conf_col], errors="coerce").fillna(0.7)
    else:
        conf = pd.Series(0.5 if not llm_path else 0.7, index=sub.index)
    sub["llm_confidence"] = conf
    recs = sub.to_dict("records")
    sub["est_next_1y"] = [estimate_next_year(r, d, c, f)
                          for r, d, c, f in zip(recs, dep, cont, conf)]
    sub["stability"] = sub.apply(stability_grade, axis=1)
    # --- est audit trail: formula + per-ticker components, so any reviewer
    # (human or LLM) can re-derive every estimate from the log alone ---
    log("--- expected-value audit: est = conf x (1 + 0.25 x character) x "
        "(cont x upside - (1-cont) x downside - dep x 0.20); "
        "upside = first 25% of 6m run at full weight, beyond at half weight "
        "(cap 50%); downside = max(|maxDD| x 0.5, 10%) x (1 + drawdown_freq); "
        "character = quality/entry-timing/structure z-sum, clamped [-2, 2]")
    for (_, r), d, c, f in zip(sub.iterrows(), dep, cont, conf):
        p = est_parts(r, d, c, f)
        log(f"  {r['ticker']:6s} est={p['est']:+.1%} = {p['conf']:.2f} x "
            f"{1 + 0.25 * p['character']:.2f}char x "
            f"({p['cont']:.2f} x {p['upside']:.3f} - {1 - p['cont']:.2f} x "
            f"{p['downside']:.3f} - {p['dep']:.2f} x 0.20) "
            f"[r6={p['r6']:+.1%} -> upside {p['upside']:.1%}; "
            f"dd={p['dd']:+.0%} x(1+{p['dd_freq']:.2f}freq) -> down {p['downside']:.1%}; "
            f"char={p['character']:+.2f}]")
    return sub


def pick_final(adj, args, llm_path):
    """Final picks: n_stocks most-confident stocks + n_etfs most-confident ETFs.

    Each group gets its own sector/category cap, quality floor, expected-value
    floor (no negative-EV picks -- the HGER lesson), and event-dependence veto.
    Adds confidence-weighted est_next_1y and stability grade to each pick.
    """
    veto = args.veto_dep if llm_path else None
    adj = add_research_columns(adj, llm_path=llm_path)

    # research-driven hard exclusions (rule violations, etc.)
    if "llm_excluded" in adj.columns and adj["llm_excluded"].any():
        _ex = adj[adj["llm_excluded"]]
        for _, r in _ex.iterrows():
            log(f"RESEARCH EXCLUSION: {r['ticker']} — {r.get('llm_exclude_reason', '')}")
        print(f"Research excluded {_ex['ticker'].tolist()}")
        adj = adj[~adj["llm_excluded"]].copy()

    if "kind" in adj.columns:
        srank = adj[adj["kind"] == "stock"]
        erank = adj[adj["kind"] == "etf"]
    else:
        srank, erank = adj, adj.iloc[0:0]

    # share-class dedupe (safety net for Phase B bundles built before the
    # Phase A dedupe): never hold two listings of the same company
    if not srank.empty and "name" in srank.columns:
        _n0 = len(srank)
        srank = srank.copy()
        srank["_ckey"] = srank["name"].str.lower().str.replace(
            r"\b(class [a-c]|inc\.?|corp\.?|corporation|company|co\.?|ltd\.?|plc|holdings?|group)\b",
            "", regex=True).str.replace(r"[^a-z0-9]", "", regex=True)
        srank = srank.sort_values("final_score", ascending=False).drop_duplicates("_ckey")
        srank = srank.drop(columns=["_ckey"])
        if len(srank) < _n0:
            log(f"share-class dedupe: {_n0 - len(srank)} duplicate listings removed")

    # ETF portfolio dedupe: never hold two wrappers of the same portfolio
    # (ETF analogue of the GOOG/GOOGL rule, e.g. QQQ vs QQQM). Keyed on a
    # small alias map for known identical-portfolio pairs surfaced by
    # research, falling back to normalized fund name.
    if not erank.empty and "name" in erank.columns:
        _n0 = len(erank)
        erank = erank.copy()
        _noname = (erank["name"].str.lower()
                   .str.replace(r"\b(etf|trust|fund|index|shares?)\b", "", regex=True)
                   .str.replace(r"[^a-z0-9]", "", regex=True))
        erank["_pkey"] = [_ETF_PORTFOLIO_ALIASES.get(str(t).lower(), n)
                          for t, n in zip(erank["ticker"], _noname)]
        erank = erank.sort_values("final_score", ascending=False).drop_duplicates("_pkey")
        erank = erank.drop(columns=["_pkey"])
        if len(erank) < _n0:
            log(f"ETF portfolio dedupe: {_n0 - len(erank)} duplicate listings removed")

    def pick_group(ranked, n, cap, label):
        # expected-value floor first: never pick a negative-EV name
        cut = ranked[ranked["est_next_1y"] < args.min_est]
        if len(cut):
            log(f"est floor ({args.min_est:+.1%}): {len(cut)} {label} excluded: "
                f"{cut['ticker'].tolist()}")
        est_ok = ranked[ranked["est_next_1y"] >= args.min_est]
        if label == "ETFs":
            # redundancy is not diversification: one wrapper per theme cluster
            est_ok = apply_etf_overlap_cap(est_ok,
                                           max_overlap=args.max_etf_overlap)
        first = pick_top_with_sector_cap(est_ok, n=n, max_per_sector=cap,
                                         min_score=args.min_score, veto_dep=veto)
        first = first.copy()
        first["pick_pass"] = 1
        # second pass: fill empty slots with the next-best floor-passers.
        # The category cap is HARD: the fill pass may not exceed it either.
        # Within the cap, prefer categories not yet represented before
        # doubling up. An empty slot beats a redundant third-of-theme.
        # (Hard floors — score, est, veto, gates — never bend.)
        if len(first) < n:
            taken = set(first["ticker"]) if len(first) else set()
            taken_counts = Counter(first["sector"]) if len(first) else Counter()
            rest = est_ok[~est_ok["ticker"].isin(taken)].copy()
            rest = rest[rest["sector"].map(lambda s: taken_counts.get(s, 0) < cap)]
            if not rest.empty:
                rest["_newcat"] = (~rest["sector"].isin(set(taken_counts))).astype(int)
                rest = rest.sort_values(["_newcat", "final_score"],
                                        ascending=[False, False])
                rest = rest.drop(columns=["_newcat"])
            fill = pick_top_with_sector_cap(rest, n=n - len(first),
                                            max_per_sector=cap,
                                            min_score=args.min_score,
                                            veto_dep=veto,
                                            initial_counts=taken_counts)
            if len(fill):
                fill = fill.copy()
                fill["pick_pass"] = 2
                log(f"fill pass ({label}): +{len(fill)} within category cap: "
                    f"{fill['ticker'].tolist()}")
                first = pd.concat([first, fill], ignore_index=True)
        # divergence self-check (informational, not a gate): how much does
        # final_score-ordered selection disagree with pure EV ordering?
        # Flags high-EV unpicked names (e.g. HPE) for the monthly audit.
        try:
            ev_rank = est_ok.sort_values("est_next_1y", ascending=False)
            ev_top = list(ev_rank["ticker"].head(n))
            sel = list(first["ticker"]) if len(first) else []

            def _fmt(t):
                v = float(ev_rank.loc[ev_rank["ticker"] == t,
                                     "est_next_1y"].iloc[0])
                return "%s(%+.1f%%)" % (t, 100 * v)

            hi_unpicked = [_fmt(t) for t in ev_top if t not in sel]
            lo_picked = [_fmt(t) for t in sel if t not in ev_top]
            log("selection/divergence check (%s): EV-top-%d overlap %d/%d; "
                "high-EV unpicked: %s; picked outside EV-top-%d: %s"
                % (label, n, n - len(hi_unpicked), n,
                   hi_unpicked or ["none"], n, lo_picked or ["none"]))
            # opportunity cost of each constraint-driven exclusion: state the
            # binding constraint and the EV gap to the lowest-EV pick — the
            # honest margin for "include X within N slots" is displacing the
            # weakest pick. Feeds the monthly audit's cap-vs-return question.
            if hi_unpicked and sel:
                sec_of, ev_of = {}, {}
                for t in set(ev_top) | set(sel):
                    r_ = ev_rank.loc[ev_rank["ticker"] == t].iloc[0]
                    ev_of[t] = float(r_["est_next_1y"])
                    sec_of[t] = str(r_.get("sector", ""))
                sec_counts = {}
                for t in sel:
                    sec_counts[sec_of[t]] = sec_counts.get(sec_of[t], 0) + 1
                min_pick = min(sel, key=lambda t: ev_of[t])
                for t in ev_top:
                    if t in sel:
                        continue
                    why = ("%s cap full" % sec_of[t]
                           if sec_counts.get(sec_of[t], 0) >= cap
                           else "final_score order")
                    gap = 100 * (ev_of[t] - ev_of[min_pick])
                    log("  opportunity cost (%s): %s(%+.1f%%) unpicked [%s]; "
                        "including it within %d slots displaces lowest-EV "
                        "pick %s(%+.1f%%) — EV gap %+.1fpp"
                        % (label, t, 100 * ev_of[t], why, n,
                           min_pick, 100 * ev_of[min_pick], gap))
        except Exception as e:
            log("selection/divergence check (%s): skipped (%s)" % (label, e))
        return first

    final_stocks = pick_group(srank, args.n_stocks, args.max_per_sector, "stocks")
    # for ETFs the 'sector' column holds the fund category; the cap is a hard
    # max of 2 per category (a third tech ETF is redundant, not diversifying)
    final_etfs = pick_group(erank, args.n_etfs, args.max_per_etf_category, "ETFs")
    # display order: highest confidence-weighted expected 1y return first
    if not final_stocks.empty:
        final_stocks = final_stocks.sort_values("est_next_1y", ascending=False)
    if not final_etfs.empty:
        final_etfs = final_etfs.sort_values("est_next_1y", ascending=False)
    return final_stocks, final_etfs


# ---------------- Watchlist mode: Dan's tickers, full pipeline ----------------

def _watchlist_cand(r, headlines):
    """Candidate dict for the research bundle (same shape as Phase A)."""
    cand = {
        "ticker": r["ticker"], "name": r["name"], "sector": r["sector"],
        "kind": r.get("kind", "stock"),
        "ret_1y": float(r["ret_1y"]), "ret_6m": _safe(r.get("ret_6m")),
        "ret_3m": _safe(r.get("ret_3m")),
        "vol60": float(r["vol60"]),
        "maxdd": float(r["maxdd"]),
        "dd_freq": _safe(r.get("dd_freq")),
        "character": _safe(r.get("character")),
        "beta": _safe(r.get("beta")), "roe": _safe(r.get("roe")),
        "margin": _safe(r.get("margin")), "fpe": _safe(r.get("fpe")),
        "base_score": float(r["base_score"]),
        "base_w": float(r["base_w"]) if "base_w" in r and pd.notna(r["base_w"]) else float(r["base_score"]),
        "base_cap": _safe(r.get("base_cap")),
        "earnings_in_days": _safe(r.get("earnings_in_days")),
        "earn_soon": bool(r.get("earn_soon", 0.0)),
        "insider_ratio": _safe(r.get("insider_ratio")),
        "headlines": headlines[:12],
    }
    if r.get("kind") == "etf":
        cand["expense_ratio"] = _safe(r.get("expense_ratio"))
        cand["aum"] = _safe(r.get("aum"))
    cand["country"] = r.get("country", "")
    return cand


def run_watchlist(args, tickers):
    """Phase A for --watchlist: Dan's tickers through quant + headlines.

    No benchmark, no picking, no trash filters — every ticker the user
    provided that has usable price history gets researched and charted,
    sorted by est_next_1y. Foreign-domiciled names are flagged, not dropped
    (they're his explicit list).
    """
    import json as _json
    from cache import StepCache
    from llm_research import build_research_bundle
    setup_logging()
    cache = StepCache(args.cache_dir, enabled=not args.no_cache, log=log)
    tickers = [t.strip().upper() for t in tickers if t.strip()]
    log(f"Watchlist Phase A: {tickers}")
    print(f"Watchlist: {', '.join(tickers)}")

    closes = download_prices(tickers, period="1y")
    ok = [t for t in tickers
          if t in closes and closes[t] is not None and len(closes[t]) >= 60]
    for t in tickers:
        if t not in ok:
            log(f"watchlist: no usable price history for {t}, skipped")
            print(f"  {t}: no usable price history, skipped")
    if not ok:
        print("No usable price data for any ticker. Aborting.")
        return
    infos = get_fundamentals(ok)
    is_etf = {t: (infos.get(t, {}).get("quoteType") == "ETF") for t in ok}
    st = [t for t in ok if not is_etf[t]]
    et = [t for t in ok if is_etf[t]]
    log(f"watchlist: {len(st)} stocks, {len(et)} ETFs")

    frames = []
    if st:
        srows = [{"ticker": t, "name": str(infos.get(t, {}).get("longName") or t)}
                 for t in st]
        scloses = {t: closes[t] for t in st}
        sinfos = {t: infos[t] for t in st if t in infos}
        # No trash filters in watchlist mode: every user-supplied ticker
        # with price data is scored, researched, and charted.
        kt = [r["ticker"] for r in srows]
        log(f"watchlist stocks: {len(kt)} (no trash filters)")
        sdf = build_scores({t: scloses[t] for t in kt},
                           {t: sinfos[t] for t in kt if t in sinfos})
        if not sdf.empty:
            sdf["kind"] = "stock"
            log_score_breakdown(sdf, "watchlist stocks", _STOCK_SCORE_GROUPS)
            frames.append(sdf)
    if et:
        erows = [{"ticker": t, "name": str(infos.get(t, {}).get("longName") or t)}
                 for t in et]
        ecloses = {t: closes[t] for t in et}
        einfos = {t: infos[t] for t in et if t in infos}
        kt = [r["ticker"] for r in erows]
        log(f"watchlist ETFs: {len(kt)} (no trash filters)")
        edf = build_etf_scores({t: ecloses[t] for t in kt},
                               {t: einfos[t] for t in kt if t in einfos})
        if not edf.empty:
            log_score_breakdown(edf, "watchlist etfs", _ETF_SCORE_GROUPS)
            frames.append(edf)
    if not frames:
        print("Watchlist: nothing to score.")
        return
    df = pd.concat(frames, ignore_index=True)
    df["country"] = df["ticker"].map(lambda t: infos.get(t, {}).get("country") or "")
    for _, r in df.iterrows():
        if r["country"] and r["country"] != "United States":
            log(f"watchlist: {r['ticker']} domiciled in {r['country']} (flagged, kept)")
    df, _cap = winsorize_base(df)

    log("Watchlist: fetching headlines...")
    market_headlines = cache.get("market_headlines")
    if market_headlines is None:
        market_headlines = fetch_market_headlines()
        cache.put("market_headlines", market_headlines)
    cands = []
    for _, r in df.iterrows():
        t = r["ticker"]
        headlines = cache.get(f"news_{t}")
        if headlines is None:
            headlines = fetch_stock_headlines(t, r["name"])
            cache.put(f"news_{t}", headlines)
            time.sleep(0.3)
        cands.append(_watchlist_cand(r, headlines))

    bundle = build_research_bundle(
        cands, market_headlines,
        {"mode": "watchlist", "tickers": tickers,
         "note": "user-supplied tickers; no benchmark comparison. "
                 "LLM decides event_dependence/continuation/confidence; "
                 "output sorted by est_next_1y, no floors."})
    bp = f"watchlist_bundle_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(bp, "w") as f:
        _json.dump(bundle, f, indent=1, default=str)
    log(f"Watchlist Phase A complete. Bundle: {bp}")
    print("\n" + "=" * 70)
    print("WATCHLIST BUNDLE WRITTEN (agent backend)")
    print("=" * 70)
    print(f"Bundle: {bp}")
    print("Next: research the web and write watchlist_outputs.json per the")
    print("schema inside the bundle. Then run:")
    print(f"  python3 screener.py --watchlist-apply {bp} watchlist_outputs.json")


def run_watchlist_apply(args):
    """Phase B for --watchlist-apply: apply research, sort by est, chart.

    No floors, no caps, no picking — every researched ticker is shown,
    sorted by confidence-weighted est_next_1y, highest first.
    """
    import json as _json
    from llm_research import apply_llm_outputs
    setup_logging()
    log("Watchlist Phase B: applying research")
    with open(args.watchlist_apply[0]) as f:
        bundle = _json.load(f)
    with open(args.watchlist_apply[1]) as f:
        outputs = _json.load(f)
    df = pd.DataFrame(bundle["candidates"])
    log(f"Loaded {len(df)} watchlist candidates")
    ranked = apply_llm_outputs(df, outputs,
                               w_down=args.w_down, w_up=args.w_up,
                               w_outlier=args.w_outlier)
    log_research_assessments(ranked)
    final = add_research_columns(ranked, llm_path=True)
    final = final.sort_values("est_next_1y", ascending=False).reset_index(drop=True)

    def _cf(x):
        try:
            return f"{float(x):.0%}"
        except Exception:
            return "n/a"
    print("\nWATCHLIST — by est. next 1y")
    print("=" * 70)
    for i, (_, r) in enumerate(final.iterrows(), 1):
        try:
            est = f"{float(r['est_next_1y']):+.1%}"
        except Exception:
            est = "n/a"
        flag = " [non-US]" if r.get("country") and r["country"] != "United States" else ""
        print(f"{i:2d}. {r['ticker']:6s} | {r['sector'][:22]:22s} | "
              f"1y {r['ret_1y']:+.0%} | est next 1y {est} | conf {_cf(r.get('llm_confidence'))} | "
              f"stab {r.get('stability', '?')}{flag}")
        log(f"  {i:2d}. {r['ticker']:6s} {r['sector'][:22]:22s} "
            f"1y={r['ret_1y']:+.0%} est={est} conf={_cf(r.get('llm_confidence'))} "
            f"stab={r.get('stability', '?')}{flag}")
        print(f"    {r['name'][:60]}")
        if r.get("llm_rationale"):
            print(f"    why: {str(r['llm_rationale'])[:150]}")
    print("\nNote: past performance doesn't predict future returns. This is a")
    print("screening tool, not financial advice.")

    out = f"watchlist_results_{datetime.now().strftime('%Y%m%d')}.csv"
    final.to_csv(out, index=False)
    print(f"Saved: {out}")
    sfinal = final[final["kind"] == "stock"]
    efinal = final[final["kind"] == "etf"]
    chart_path = f"watchlist_chart_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    make_chart_html(sfinal, efinal, chart_path,
                    {"benchmark": "", "mode": "watchlist",
                     "heading": "Watchlist — by est. next 1y",
                     "asof": datetime.now().strftime("%Y-%m-%d")},
                    titles=("Stocks — by est. next 1y", "ETFs — by est. next 1y"))
    print(f"Chart: {chart_path}")
    checks = []
    checks.append(("no_nan_est", not final["est_next_1y"].isna().any()))
    checks.append(("has_confidence", "llm_confidence" in final.columns))
    checks.append(("sorted_by_est",
                   bool((final["est_next_1y"].values[:-1] >=
                         final["est_next_1y"].values[1:]).all())))
    for name, ok in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    write_run_summary(
        f"watchlist_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        {"timestamp": datetime.now().isoformat(), "mode": "watchlist",
         "tickers": bundle["meta"].get("tickers"),
         "checks": [{"name": n, "ok": bool(o)} for n, o in checks],
         "picks": [
             {"ticker": r["ticker"], "kind": r.get("kind"),
              "sector": r["sector"], "country": r.get("country", ""),
              "ret_1y": round(float(r["ret_1y"]), 4),
              "est_next_1y": round(float(r["est_next_1y"]), 4),
              "confidence": _safe(r.get("llm_confidence")),
              "stability": r.get("stability"),
              "event_dependence": _safe(r.get("llm_event_dependence")),
              "continuation": _safe(r.get("llm_continuation")),
              "rationale": r.get("llm_rationale", "")}
             for _, r in final.iterrows()],
         "chart": chart_path})


# ---------------- Thesis ledger: post-buy accountability ----------------
# The system used to forget its picks the moment it published them. The ledger
# records every pick (with the prediction it was picked on) and every rejected
# near-miss (the control group), append-only, so a later audit can measure
# predictions against outcomes and the formula can be improved from evidence.
THESIS_LEDGER = "thesis_ledger.jsonl"
REJECTED_LEDGER = "rejected_ledger.jsonl"
THESIS_TRACK_DAYS = 90  # price-based checks cover picks this fresh or newer


def ledger_append(path, event):
    import json as _json
    event = dict(event)
    event.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
    event.setdefault("ts", datetime.now().isoformat())
    with open(path, "a") as f:
        f.write(_json.dumps(event, default=str) + "\n")


def _ledger_seen_today(path, event="picked"):
    """Tickers already recorded with `event` today — keeps Phase B idempotent."""
    import json as _json
    seen = set()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        with open(path) as f:
            for line in f:
                try:
                    e = _json.loads(line)
                except Exception:
                    continue
                if e.get("event") == event and e.get("date") == today:
                    seen.add(e.get("ticker"))
    except FileNotFoundError:
        pass
    return seen


_LAST_DATA_COMPLETENESS = {}
_LAST_OVERLAP_DROPS = {}  # loser -> (winner, overlap); set by apply_etf_overlap_cap


def fetch_pick_prices(tickers):
    """Robust pick-price fetch: batch attempts, then per-ticker fallback.

    Returns (prices, completeness dict). A run with missing prices is
    INCOMPLETE_DATA — the structural self-checks can pass while price data
    is absent, so completeness gets its own explicit status, recorded in the
    log and the run summary.
    """
    tickers = list(dict.fromkeys(tickers))
    prices = {}
    for attempt in (1, 2):
        try:
            px = download_prices(tickers, period="5d", min_rows=3)
            for t in tickers:
                s = px[t].dropna() if t in px else None
                if s is not None and len(s):
                    prices[t] = float(s.iloc[-1])
            missing = [t for t in tickers if t not in prices]
            if not missing:
                break
            log(f"ledger: price fetch attempt {attempt} missed {missing}")
        except Exception as e:
            log(f"ledger: pick-price fetch failed (attempt {attempt}): {e}")
        time.sleep(10)
    missing = [t for t in tickers if t not in prices]
    if missing:
        # per-ticker fallback: single-ticker downloads often succeed when the
        # batched call is rate-limited into returning nothing
        log(f"ledger: per-ticker fallback for {missing}")
        for t in missing:
            try:
                d = yf.download(t, period="5d", auto_adjust=True,
                                progress=False, threads=False)
                s = d["Close"].dropna() if "Close" in d else pd.Series(dtype=float)
                if isinstance(s, pd.DataFrame):
                    s = s.iloc[:, 0].dropna()
                if len(s):
                    prices[t] = float(s.iloc[-1])
                    log(f"ledger: fallback got {t}={prices[t]:.2f}")
            except Exception as e:
                log(f"ledger: fallback failed for {t} ({e})")
            time.sleep(1)
    missing = [t for t in tickers if t not in prices]
    status = "COMPLETE" if not missing else "INCOMPLETE_DATA"
    log(f"data_completeness: {status} ({len(prices)}/{len(tickers)} pick prices)"
        + (f" — missing: {missing}; ledger entries lack pick_price; "
           "backfill before thesis_check" if missing else ""))
    return prices, {"status": status, "got": len(prices),
                    "total": len(tickers), "missing": missing}


def backfill_ledger_prices():
    """Fill pick_price=None on today's picked events (completeness recovery).

    The cron's thesis step can call this when a run was marked INCOMPLETE_DATA.
    Returns the number of entries fixed.
    """
    import json as _json
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        lines = open(THESIS_LEDGER).read().splitlines()
    except FileNotFoundError:
        return 0
    need = []
    for line in lines:
        try:
            d = _json.loads(line)
        except Exception:
            continue
        if (d.get("date") == today and d.get("event") == "picked"
                and d.get("pick_price") is None and d.get("ticker")):
            need.append(d["ticker"])
    need = list(dict.fromkeys(need))
    if not need:
        return 0
    prices, _ = fetch_pick_prices(need)
    fixed, out = 0, []
    for line in lines:
        d = _json.loads(line)
        if (d.get("date") == today and d.get("event") == "picked"
                and d.get("pick_price") is None and d.get("ticker") in prices):
            d["pick_price"] = prices[d["ticker"]]
            fixed += 1
        out.append(_json.dumps(d, default=str))
    open(THESIS_LEDGER, "w").write("\n".join(out) + "\n")
    log(f"ledger: backfilled {fixed} pick prices")
    return fixed


def record_picks_ledger(final, ranked, args):
    """Append pick events for today's finals + rejected events for the
    audit-interesting near-misses (vetoes, exclusions, EV-floor fails)."""
    import json as _json  # noqa: F401 (kept local like the rest of this file)
    global _LAST_DATA_COMPLETENESS
    tickers = list(final["ticker"])
    prices, _LAST_DATA_COMPLETENESS = fetch_pick_prices(tickers)
    if _LAST_DATA_COMPLETENESS["missing"]:
        log(f"ledger: recording {len(_LAST_DATA_COMPLETENESS['missing'])} picks "
            f"without prices; backfill before thesis_check")
    already = _ledger_seen_today(THESIS_LEDGER, "picked")
    npick = 0
    for _, r in final.iterrows():
        if r["ticker"] in already:
            continue
        ledger_append(THESIS_LEDGER, {
            "event": "picked",
            "ticker": r["ticker"], "kind": r.get("kind", ""),
            "name": r.get("name", ""), "sector": r.get("sector", ""),
            "pick_price": prices.get(r["ticker"]),
            "ret_1y": _safe(r.get("ret_1y")),
            "est_next_1y": _safe(r.get("est_next_1y")),
            "dep": _safe(r.get("llm_event_dependence")),
            "cont": _safe(r.get("llm_continuation")),
            "conf": _safe(r.get("llm_confidence")),
            "character": _safe(r.get("character")),
            "dd_freq": _safe(r.get("dd_freq")),
            "base_score": _safe(r.get("base_score")),
            "final_score": _safe(r.get("final_score")),
            "rationale": str(r.get("llm_rationale", "") or "")[:500],
            "benchmark": args.benchmark,
            "etf_benchmark": etf_benchmark(args),
        })
        npick += 1
    # rejected control group: names the system said no to, so the audit can
    # ask whether the vetoes/exclusions/floors were right
    final_set = set(tickers)
    rseen = _ledger_seen_today(REJECTED_LEDGER, "rejected")
    veto = getattr(args, "veto_dep", 0.7) or 0.7
    recorded = 0
    for _, r in ranked.iterrows():
        t = r["ticker"]
        if t in final_set or t in rseen or recorded >= 15:
            continue
        d = _safe(r.get("llm_event_dependence"))
        c = _safe(r.get("llm_continuation"))
        f_ = _safe(r.get("llm_confidence"))
        p = est_parts(r, d if d is not None else 0.3,
                      c if c is not None else 0.5, f_ if f_ is not None else 1.0)
        reason = None
        if r.get("llm_excluded"):
            reason = f"research exclusion: {str(r.get('llm_exclude_reason', ''))[:200]}"
        elif d is not None and d > veto:
            reason = f"event-dependence veto (dep={d:.2f} > {veto})"
        elif p["est"] < args.min_est:
            reason = f"expected-value floor (est={p['est']:+.1%} < {args.min_est:+.0%})"
        else:
            continue  # sector cap / score rank — not audit-interesting
        ledger_append(REJECTED_LEDGER, {
            "event": "rejected", "ticker": t, "kind": r.get("kind", ""),
            "name": r.get("name", ""), "price": prices.get(t),
            "reason": reason, "est_next_1y": round(p["est"], 4),
            "dep": d, "cont": c, "conf": f_,
        })
        recorded += 1
    log(f"ledger: recorded {npick} new picks ({len(final)} final), "
        f"{recorded} rejected (control group)")


def thesis_check(track_days=THESIS_TRACK_DAYS):
    """Price-based thesis check for picks made within the last `track_days`.

    Appends 'check' events (intact/watch/broken) to the ledger and returns
    the status list. The daily agent does the news-based re-verification on
    top and amends reasons; this function only measures price truth.
    """
    import json as _json
    picks = {}
    try:
        with open(THESIS_LEDGER) as f:
            for line in f:
                try:
                    e = _json.loads(line)
                except Exception:
                    continue
                if e.get("event") == "picked":
                    picks[e["ticker"]] = e
    except FileNotFoundError:
        return []
    cutoff = (datetime.now() - timedelta(days=track_days)).strftime("%Y-%m-%d")
    tracked = {t: e for t, e in picks.items()
               if e.get("date", "") >= cutoff and e.get("pick_price")}
    if not tracked:
        return []
    out = []
    try:
        px = download_prices(list(tracked), period="3mo")
    except Exception as e:
        log(f"thesis_check: price fetch failed ({e})")
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    for t, e in tracked.items():
        if t not in px:
            continue
        s = px[t].dropna()
        s = s[s.index >= pd.Timestamp(e["date"])]
        if len(s) == 0:
            continue
        pp = float(e["pick_price"])
        ret = float(s.iloc[-1] / pp - 1)
        dd = float(((s - s.cummax()) / s.cummax()).min())
        days = (datetime.now() - datetime.fromisoformat(e["date"])).days
        status = "intact"
        reason = "price-based: within tolerance"
        if dd <= -0.15 or ret <= -0.15:
            status, reason = "broken", f"price-based: {dd:+.0%} max dip since pick"
        elif dd <= -0.08 or ret <= -0.08:
            status, reason = "watch", f"price-based: {dd:+.0%} dip since pick"
        rec = {"event": "check", "ticker": t, "date": today, "status": status,
               "days_held": days, "ret_since_pick": round(ret, 4),
               "dd_since_pick": round(dd, 4), "pick_price": pp, "reason": reason}
        ledger_append(THESIS_LEDGER, rec)
        out.append(rec)
    nb = sum(1 for r in out if r["status"] == "broken")
    nw = sum(1 for r in out if r["status"] == "watch")
    log(f"thesis_check: {len(out)} tracked, {nb} broken, {nw} watch")
    return out


def thesis_status_for_chart(track_days=THESIS_TRACK_DAYS):
    """Latest status per tracked ticker for the chart's Thesis watch section."""
    import json as _json
    latest = {}
    try:
        with open(THESIS_LEDGER) as f:
            for line in f:
                try:
                    e = _json.loads(line)
                except Exception:
                    continue
                if e.get("event") in ("picked", "check") and e.get("ticker"):
                    latest[e["ticker"]] = e
    except FileNotFoundError:
        return []
    cutoff = (datetime.now() - timedelta(days=track_days)).strftime("%Y-%m-%d")
    rows = []
    for t, e in sorted(latest.items()):
        if e.get("date", "") < cutoff:
            continue
        if e["event"] == "picked":
            days = (datetime.now() - datetime.fromisoformat(e["date"])).days
            rows.append({"ticker": t, "kind": e.get("kind", ""),
                         "days_held": days, "ret_since_pick": None,
                         "status": "intact", "reason": "freshly picked"})
        else:
            rows.append({"ticker": t, "kind": e.get("kind", ""),
                         "days_held": e.get("days_held", 0),
                         "ret_since_pick": e.get("ret_since_pick"),
                         "status": e.get("status", "intact"),
                         "reason": e.get("reason", "")})
    return rows


def main():
    ap = argparse.ArgumentParser(description="Free stock screener with dynamic news risk")
    ap.add_argument("--benchmark", default="SPMO", choices=BENCHMARK_CHOICES)
    ap.add_argument("--etf-benchmark", default=None, choices=BENCHMARK_CHOICES,
                    help="separate benchmark for the ETF leg "
                         "(default: same as --benchmark)")
    ap.add_argument("--compare-benchmarks", action="store_true")
    ap.add_argument("--test", action="store_true", help="quick test (caps outperformers at 60)")
    ap.add_argument("--min-mcap", type=float, default=2e9,
                    help="minimum market cap in USD for the screener universe (default 2B)")
    ap.add_argument("--min-price", type=float, default=5.0,
                    help="minimum share price in USD (default 5)")
    ap.add_argument("--max-outperformers", type=int, default=800,
                    help="safety cap on outperformers to score (default 800)")
    ap.add_argument("--top-n", type=int, default=10)
    ap.add_argument("--max-per-sector", type=int, default=2)
    ap.add_argument("--with-llm", action="store_true",
                    help="use the 3-layer LLM research stack instead of the rules news layer")
    ap.add_argument("--llm-backend", default="agent", choices=["agent", "auto"],
                    help="agent=file handoff (no keys); auto=free API failover chain")
    ap.add_argument("--llm-apply", nargs=2, metavar=("BUNDLE", "OUTPUTS"),
                    help="phase B: rerank from a research bundle + filled llm outputs")
    ap.add_argument("--watchlist",
                    help="comma-separated tickers to run through the pipeline "
                         "(Phase A: quant + headlines, writes watchlist bundle)")
    ap.add_argument("--watchlist-apply", nargs=2, metavar=("BUNDLE", "OUTPUTS"),
                    help="watchlist Phase B: apply research, sort by est, chart")
    ap.add_argument("--max-vol", type=float, default=0.80,
                    help="absolute risk gate: exclude vol60 above this (default 0.80)")
    ap.add_argument("--min-dd", type=float, default=-0.40,
                    help="absolute risk gate: exclude max drawdown below this (default -0.40)")
    ap.add_argument("--min-score", type=float, default=0.0,
                    help="quality floor for final picks (default 0.0)")
    ap.add_argument("--veto-dep", type=float, default=0.7,
                    help="LLM event-dependence above this is vetoed from final picks (default 0.7)")
    ap.add_argument("--w-down", type=float, default=1.5,
                    help="rerank weight: penalty per unit event_dependence (default 1.5)")
    ap.add_argument("--w-up", type=float, default=1.0,
                    help="rerank weight: reward per unit continuation (default 1.0)")
    ap.add_argument("--w-outlier", type=float, default=0.5,
                    help="rerank weight: extra penalty for outlier base_score * event_dependence (default 0.5)")
    ap.add_argument("--n-stocks", type=int, default=10,
                    help="final stock picks (default 10)")
    ap.add_argument("--n-etfs", type=int, default=10,
                    help="final ETF picks (default 10)")
    ap.add_argument("--min-est", type=float, default=0.03,
                    help="expected-value floor: picks must have est_next_1y >= this (default 0.03; every pick earns its place)")
    ap.add_argument("--us-only", dest="us_only", action="store_true", default=True,
                    help="US-domiciled stocks and US-focused ETFs only (default on)")
    ap.add_argument("--no-us-only", dest="us_only", action="store_false",
                    help="disable the US-only filter")
    ap.add_argument("--n-etf-research", type=int, default=30,
                    help="top ETF quant candidates entering research (default 30)")
    ap.add_argument("--max-etf-overlap", type=float, default=0.30,
                    help="max pairwise top-10 holdings overlap between picked ETFs (default 0.30; lower-EV member of over-threshold pairs is excluded)")
    ap.add_argument("--max-per-etf-category", type=int, default=2,
                    help="max final ETFs per fund category (default 4)")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore the on-disk cache; make all calls fresh")
    ap.add_argument("--cache-dir", default="cache",
                    help="cache directory (default: cache/)")
    args = ap.parse_args()

    # ---- Phase B: apply already-done LLM research, no re-fetching ----
    if args.watchlist:
        run_watchlist(args, args.watchlist.split(","))
        return

    if args.watchlist_apply:
        run_watchlist_apply(args)
        return

    if args.llm_apply:
        import json as _json
        from llm_research import apply_llm_outputs
        with open(args.llm_apply[0]) as f:
            bundle = _json.load(f)
        phase_a_log = (bundle.get("meta") or {}).get("phase_a_log")
        if phase_a_log and os.path.exists(phase_a_log):
            continue_logging(phase_a_log)
            log("=== PHASE B: applying LLM research (same log as Phase A) ===")
        else:
            setup_logging()
            log("Phase B: applying LLM research (no Phase A log found; new file)")
        with open(args.llm_apply[1]) as f:
            outputs = _json.load(f)
        df = pd.DataFrame(bundle["candidates"])
        log(f"Loaded {len(df)} candidates from bundle")
        kinds = df["kind"].value_counts().to_dict() if "kind" in df.columns else {}
        log(f"candidate kinds: {kinds}")
        ranked = apply_llm_outputs(df, outputs,
                                   w_down=args.w_down, w_up=args.w_up,
                                   w_outlier=args.w_outlier)
        log_world_layer(outputs)
        log_research_assessments(ranked)
        # absolute risk gates also apply on the Phase-B path
        ranked, gate_report = apply_risk_gates(ranked, max_vol=args.max_vol,
                                               min_dd=args.min_dd)
        final_stocks, final_etfs = pick_final(ranked, args, llm_path=True)
        final = pd.concat([final_stocks, final_etfs], ignore_index=True)
        _print_llm_final(final_stocks, final_etfs, args)
        # thesis ledger: record picks + rejected control group (audit trail)
        try:
            record_picks_ledger(final, ranked, args)
        except Exception as e:
            log(f"ledger: record_picks_ledger failed ({e})")
        thesis_rows = thesis_status_for_chart()
        out = (f"screener_results_{datetime.now().strftime('%Y%m%d')}_"
               f"{args.benchmark}_llm.csv")
        final.to_csv(out, index=False)
        print(f"Saved: {out}")
        chart_path = (f"chart_{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
                      f"{args.benchmark}_llm.html")
        hm = honorable_mentions(ranked, final, args)
        make_chart_html(final_stocks, final_etfs, chart_path,
                        {"benchmark": args.benchmark,
                         "etf_benchmark": etf_benchmark(args),
                         "asof": datetime.now().strftime("%Y-%m-%d")},
                        thesis=thesis_rows, honorable=hm)
        print(f"Chart: {chart_path}")
        checks, fails = run_self_check(final, args, out)
        if fails:
            print("\n!!! SELF-CHECK FAILURES:")
            for c in fails:
                print(f"    FAIL: {c['name']} {c['detail']}")
            log_error(f"SELF-CHECK failed: {[c['name'] for c in fails]}")
        write_run_summary(
            f"run_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{args.benchmark}_llm.json",
            {"timestamp": datetime.now().isoformat(),
             "args": {k: v for k, v in vars(args).items()},
             "log_file": LOG_FILE, "phase": "B",
             "stages": {"candidates": len(df), "gated": gate_report.get("gated_total"),
                        "final_stocks": len(final_stocks),
                        "final_etfs": len(final_etfs)},
             "gate_report": gate_report,
             "self_check": checks,
             "data_completeness": _LAST_DATA_COMPLETENESS or {},
             "final_picks": [
                 {"ticker": r["ticker"], "kind": r.get("kind"),
                  "sector": r["sector"],
                  "ret_1y": round(float(r["ret_1y"]), 4),
                  "vol60": round(float(r["vol60"]), 4),
                  "maxdd": round(float(r["maxdd"]), 4),
                  "base_score": round(float(r["base_score"]), 3),
                  "final_score": round(float(r["final_score"]), 3),
                  "est_next_1y": round(float(r["est_next_1y"]), 4),
                  "confidence": _safe(r.get("llm_confidence")),
                  "stability": r.get("stability"),
                  "pick_pass": int(r.get("pick_pass", 1)),
                  "event_dependence": _safe(r.get("llm_event_dependence")),
                  "continuation": _safe(r.get("llm_continuation"))}
                 for _, r in final.iterrows()],
             "chart": chart_path})
        return

    log_path = setup_logging()
    log(f"Logging to {log_path}" if log_path else "File logging unavailable")
    log_levers(args)

    from cache import StepCache
    cache = StepCache(args.cache_dir, enabled=not args.no_cache, log=log)
    if args.no_cache:
        log("cache disabled (--no-cache): all calls fresh")
    else:
        log(f"cache dir: {cache.report()['dir']} (date-partitioned; --no-cache for fresh)")

    print("=" * 70)
    print("STOCK SCREENER — beat the benchmark, minimize post-buy drawdown")
    print("Free APIs only. Not financial advice.")
    print("=" * 70)

    # 1) Benchmark 1y return
    bench_tickers = BENCHMARK_CHOICES if args.compare_benchmarks else [args.benchmark]
    _eb = etf_benchmark(args)
    if _eb not in bench_tickers:
        bench_tickers = bench_tickers + [_eb]
    bench_closes = cache.get("bench_" + "_".join(bench_tickers))
    if bench_closes is None:
        bench_closes = download_prices(bench_tickers, period="1y")
        cache.put("bench_" + "_".join(bench_tickers), bench_closes)
    bench_rets = {t: calc_return(s) for t, s in bench_closes.items()}
    for t, r in bench_rets.items():
        print(f"Benchmark {t}: 1y return = {r:+.1%}" if not pd.isna(r) else f"Benchmark {t}: n/a")
    if not args.compare_benchmarks:
        bench_ret = bench_rets.get(args.benchmark, np.nan)
        if pd.isna(bench_ret):
            print(f"Could not get benchmark {args.benchmark} return. Exiting.")
            sys.exit(1)
        etf_bench_ret = bench_rets.get(_eb, np.nan)
        if pd.isna(etf_bench_ret):
            print(f"Could not get ETF benchmark {_eb} return. Exiting.")
            sys.exit(1)
    else:
        # if comparing, use the best benchmark as the hurdle? No — use the user's chosen default SPMO
        bench_ret = bench_rets.get("SPMO", np.nan)
        etf_bench_ret = bench_rets.get(_eb, bench_ret)

    # 2) Universe: FULL yfinance screener pull (paged), then outperformers only.
    # No top-N cap: every stock beating the benchmark goes through fundamentals.
    universe = cache.get("universe")
    if universe is None:
        universe = get_all_screener_stocks(min_mcap=args.min_mcap, min_price=args.min_price)
        cache.put("universe", universe)
    if not universe:
        print("Could not fetch screener universe. Try again later.")
        sys.exit(1)

    # 3) Beat-the-benchmark filter (your rule: else just buy the benchmark).
    # Pre-filter on the screener's 52w % with a little slack (methodology differs
    # slightly from our adjusted-close calc), then verify precisely from prices.
    bench_pct = bench_ret * 100
    cands = [u for u in universe if u["pct_52w"] > bench_pct - 5]
    cands.sort(key=lambda d: d["pct_52w"], reverse=True)
    if args.test:
        cands = cands[:60]
    elif len(cands) > args.max_outperformers:
        log(f"Capping outperformers at {args.max_outperformers} (safety cap)")
        cands = cands[:args.max_outperformers]
    print(f"\n{len(cands)} candidate outperformers from {len(universe)}-stock universe "
          f"(screener 52w% > {bench_pct - 5:.0f}%)")
    log(f"universe: {len(universe)} stocks pulled from yfinance screener "
        f"(mcap>=${args.min_mcap:.0f}B, price>=${args.min_price:.0f})")
    log(f"benchmark {args.benchmark}: 1y={bench_ret:+.1%} -> 52w% cutoff "
        f">{bench_pct - 5:.0f}% (5pp slack for methodology drift)")
    log(f"candidate outperformers: {len(cands)} (sorted by screener 52w%)")
    log("raw input top 20 by screener 52w%: " +
        ", ".join(f"{c['ticker']}({c['pct_52w']:.0f}%)" for c in cands[:20]))
    log(f"all candidates by screener 52w% ({len(cands)}): " +
        ", ".join(f"{c['ticker']}({c['pct_52w']:.0f}%)" for c in cands))

    cand_tickers = [c["ticker"] for c in cands if c["ticker"] not in BENCHMARK_CHOICES]
    prices_key = "prices_" + StepCache.tickers_key(cand_tickers)
    closes = cache.get(prices_key)
    if closes is None:
        closes = download_prices(cand_tickers, period="1y")
        cache.put(prices_key, closes)

    # Precise 1y return check vs benchmark, same methodology
    outperformers = []
    for c in cands:
        t = c["ticker"]
        s = closes.get(t)
        r = calc_return(s) if s is not None else np.nan
        if not pd.isna(r) and r > bench_ret:
            c["ret_1y"] = r
            outperformers.append(c)
    outperformers.sort(key=lambda d: d["ret_1y"], reverse=True)
    print(f"{len(outperformers)} confirmed outperformers beat {args.benchmark} ({bench_ret:+.1%} 1y)")
    log(f"confirmed outperformers (precise 1y from prices) beat {args.benchmark} "
        f"({bench_ret:+.1%} 1y): {len(outperformers)}")
    log("confirmed outperformer tickers: " +
        ", ".join(f"{c['ticker']}({c['ret_1y']:+.0%})" for c in outperformers))
    if not outperformers:
        print(f"No stocks beat the benchmark. Per your rule: just buy {args.benchmark}.")
        sys.exit(0)
    print("Top 5 outperformers:")
    for c in outperformers[:5]:
        print(f"  {c['ticker']:8s} {c['ret_1y']:+.0%}  {c['name'][:45]}")
    log("confirmed top 10 by precise 1y return: " +
        ", ".join(f"{c['ticker']}({c['ret_1y']:+.0%})" for c in outperformers[:10]))

    # 4) Fundamentals for ALL outperformers (this is where trash gets identified)
    out_tickers = [c["ticker"] for c in outperformers]
    fund_key = "fundamentals_" + StepCache.tickers_key(out_tickers)
    infos = cache.get(fund_key) or {}
    missing = [t for t in out_tickers if t not in infos]
    if missing:
        log(f"fundamentals: {len(infos)} cached, fetching {len(missing)} missing")
        fresh = get_fundamentals(missing)
        infos.update(fresh)
        cache.put(fund_key, infos)
    else:
        log(f"fundamentals: all {len(infos)} from cache")
    infos = {t: i for t, i in infos.items() if t in closes}

    # 5) Hard trash filters (transparent rules, reported below)
    kept, trash_report = apply_trash_filters(outperformers, closes, infos,
                                               us_only=args.us_only)
    log(f"Trash filter: {trash_report['dropped_total']} dropped, {trash_report['kept']} kept")
    for k, v in trash_report.items():
        if k not in ("kept", "dropped_total", "dropped_names") and v:
            log(f"  trash reason - {k}: {v}")
    for k, names in trash_report.get("dropped_names", {}).items():
        # full name lists for the interesting (small) reasons; foreign_domicile
        # is large and uninteresting, so cap it
        shown = names if k != "foreign_domicile" else names[:20]
        suffix = f" (+{len(names) - 20} more)" if k == "foreign_domicile" and len(names) > 20 else ""
        log(f"  trash dropped[{k}]: {shown}{suffix}")
    print(f"\nTrash filter: {trash_report['dropped_total']} dropped, {trash_report['kept']} kept")
    for k, v in trash_report.items():
        if k not in ("kept", "dropped_total", "dropped_names") and v:
            print(f"  - {k}: {v}")
    if not kept:
        print("Everything was filtered as trash. Loosen filters and retry.")
        sys.exit(0)

    # 6) Score all survivors for your goal: continuation + minimum post-buy drawdown risk
    keep_tickers = [c["ticker"] for c in kept]
    infos = {t: i for t, i in infos.items() if t in keep_tickers}
    df = build_scores(closes, infos)
    if df.empty:
        print("Scoring produced no results (data issues). Try again later.")
        sys.exit(1)
    print(f"Scored {len(df)} stocks. Top 5 pre-news:")
    for _, r in df.head(5).iterrows():
        print(f"  {r['ticker']:8s} {r['sector'][:22]:22s} base={r['base_score']:+.2f} 1y={r['ret_1y']:+.0%}")
    log(f"base_score stats: {df['base_score'].describe().to_dict()}")
    log_score_breakdown(df.head(50), "stocks (research pool: top 50)",
                        _STOCK_SCORE_GROUPS)
    log_character_breakdown(df.head(50), "stocks (research pool: top 50)",
                            _CHAR_PARTS_STOCK)

    # 6b) Enrich top 100 with insider + earnings signals, adjust scores
    enrich_n = min(100, len(df))
    enr_tickers = df.head(enrich_n)["ticker"].tolist()
    enr_key = "enrich_" + StepCache.tickers_key(enr_tickers)
    enr = cache.get(enr_key) or {}
    enr_missing = [t for t in enr_tickers if t not in enr]
    if enr_missing:
        log(f"enrichment: {len(enr)} cached, fetching {len(enr_missing)} missing")
        enr.update(enrich_candidates(enr_missing))
        cache.put(enr_key, enr)
    else:
        log(f"enrichment: all {len(enr)} from cache")
    df["insider_ratio"] = df["ticker"].map(lambda t: enr.get(t, {}).get("insider_ratio"))
    df["earnings_in_days"] = df["ticker"].map(lambda t: enr.get(t, {}).get("earnings_in_days"))
    mask = df["ticker"].isin(enr.keys())
    df.loc[mask, "z_insider"] = zscore(
        -pd.to_numeric(df.loc[mask, "insider_ratio"], errors="coerce").fillna(0)).values
    df["z_insider"] = df["z_insider"].fillna(0)
    df["earn_soon"] = df["earnings_in_days"].apply(
        lambda d: 1.0 if d is not None and 0 <= d <= 14 else 0.0)
    n_earn_soon = int(df["earn_soon"].sum())
    log(f"earnings within 14d: {n_earn_soon} of top {enrich_n} "
        f"({df[df['earn_soon'] == 1.0]['ticker'].tolist()[:10]})")
    df["base_score"] = df["base_score"] + 0.06 * df["z_insider"] - 0.25 * df["earn_soon"]
    df = df.sort_values("base_score", ascending=False)
    log("base_score adjusted for insider/earnings; re-sorted")

    # 6c) Absolute risk gates before research/final selection
    df, gate_report = apply_risk_gates(df, max_vol=args.max_vol, min_dd=args.min_dd)
    df, stock_cap = winsorize_base(df)
    df["kind"] = "stock"
    df["base_cap"] = stock_cap
    log(f"stocks winsorize cap={stock_cap:+.2f}")
    # dedupe share classes: keep the best-scoring listing per company
    # (e.g. GOOG vs GOOGL) so the final chart never holds the same firm twice
    _n0 = len(df)
    df["_ckey"] = df["name"].str.lower().str.replace(
        r"\b(class [a-c]|inc\.?|corp\.?|corporation|company|co\.?|ltd\.?|plc|holdings?|group)\b",
        "", regex=True).str.replace(r"[^a-z0-9]", "", regex=True)
    df = df.sort_values("base_score", ascending=False).drop_duplicates("_ckey")
    df = df.drop(columns=["_ckey"])
    if len(df) < _n0:
        log(f"share-class dedupe: {_n0 - len(df)} duplicate listings removed")

    # 6d) ETF leg: separate universe + scorer (different fundamentals)
    edf = run_etf_pipeline(args, cache, etf_bench_ret, _eb)

    # research pool: top 50 stocks + top N ETFs
    _pool_etfs = edf.head(args.n_etf_research).copy() if not edf.empty else edf
    pool = pd.concat([df.head(50).copy(), _pool_etfs], ignore_index=True)
    log(f"research pool: {len(df.head(50))} stocks + {len(_pool_etfs)} ETFs")

    # 7) News/research layer on top scorers (stocks + ETFs)
    if args.with_llm:
        # ---- LLM research stack (replaces the rules-based news layer) ----
        import json as _json
        from llm_research import build_research_bundle, run_auto_layers, \
            apply_llm_outputs, NoLLMBackend
        top = pool
        log("LLM layer: fetching headlines for research pool...")
        market_headlines = cache.get("market_headlines")
        if market_headlines is None:
            market_headlines = fetch_market_headlines()
            cache.put("market_headlines", market_headlines)
        cands = []
        for i, (_, r) in enumerate(top.iterrows()):
            if i % 10 == 0:
                log(f"  headlines {i}/{len(top)}...")
            t = r["ticker"]
            headlines = cache.get(f"news_{t}")
            if headlines is None:
                headlines = fetch_stock_headlines(t, r["name"])
                cache.put(f"news_{t}", headlines)
                time.sleep(0.3)
            cand = {
                "ticker": r["ticker"], "name": r["name"], "sector": r["sector"],
                "kind": r.get("kind", "stock"),
                "ret_1y": float(r["ret_1y"]), "ret_6m": _safe(r.get("ret_6m")),
                "ret_3m": _safe(r.get("ret_3m")),
                "vol60": float(r["vol60"]),
                "maxdd": float(r["maxdd"]),
                "dd_freq": _safe(r.get("dd_freq")),
                "character": _safe(r.get("character")),
                "beta": _safe(r.get("beta")), "roe": _safe(r.get("roe")),
                "margin": _safe(r.get("margin")), "fpe": _safe(r.get("fpe")),
                "base_score": float(r["base_score"]),
                "base_w": float(r["base_w"]) if "base_w" in r and pd.notna(r["base_w"]) else float(r["base_score"]),
                "base_cap": _safe(r.get("base_cap")),
                "earnings_in_days": _safe(r.get("earnings_in_days")),
                "earn_soon": bool(r.get("earn_soon", 0.0)),
                "insider_ratio": _safe(r.get("insider_ratio")),
                "headlines": headlines[:12],
            }
            if r.get("kind") == "etf":
                cand["expense_ratio"] = _safe(r.get("expense_ratio"))
                cand["aum"] = _safe(r.get("aum"))
            cands.append(cand)

        if args.llm_backend == "agent":
            # Phase A: write bundle, stop. An agent fills llm_outputs.json,
            # then: python3 screener.py --llm-apply <bundle> <outputs>
            bundle = build_research_bundle(
                cands, market_headlines,
                {"benchmark": args.benchmark,
                 "etf_benchmark": _eb,
                 "phase_a_log": os.path.abspath(LOG_FILE) if LOG_FILE else None,
                 "note": "quant base scores included; LLM decides event_dependence/continuation. "
                         "Stocks were filtered vs the stock benchmark; ETFs vs the ETF benchmark. "
                         "phase_a_log: Phase B must append to this file so one run = one log."})
            bp = f"research_bundle_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{args.benchmark}.json"
            with open(bp, "w") as f:
                _json.dump(bundle, f, indent=1, default=str)
            log(f"Phase A complete. Bundle: {bp}")
            print("\n" + "=" * 70)
            print("LLM RESEARCH BUNDLE WRITTEN (agent backend)")
            print("=" * 70)
            print(f"Bundle: {bp}")
            print("Next: have your research agent (e.g. Muse) read the bundle,")
            print("research the web, and write llm_outputs.json per the schema")
            print("inside the bundle. Then run:")
            _eb_flag = f" --etf-benchmark {_eb}" if _eb != args.benchmark else ""
            print(f"  python3 screener.py --llm-apply {bp} llm_outputs.json "
                  f"--benchmark {args.benchmark}{_eb_flag}")
            return

        # auto backend: API failover chain, graceful fallback to rules
        try:
            outputs = run_auto_layers(cands, market_headlines, log=log)
            adj = apply_llm_outputs(top, outputs,
                                    w_down=args.w_down, w_up=args.w_up,
                                    w_outlier=args.w_outlier)
            llm_path = True
        except NoLLMBackend as e:
            log(f"No LLM backend available ({e}) — falling back to rules-based news layer")
            risk_themes = discover_risk_themes(cache)
            adj = apply_news_adjustment(pool, risk_themes, top_k=len(pool),
                                        cache=cache, w_outlier=args.w_outlier)
            adj = adj.rename(columns={"adj_score": "final_score"})
            llm_path = False
    else:
        # ---- default: rules-based dynamic news risk layer ----
        risk_themes = discover_risk_themes(cache)
        adj = apply_news_adjustment(pool, risk_themes, top_k=len(pool),
                                    cache=cache, w_outlier=args.w_outlier)
        adj = adj.rename(columns={"adj_score": "final_score"})
        llm_path = False

    # 7b) Log biggest rank movers (quant base -> news/LLM adjusted)
    if llm_path:
        adj = adj.copy()
        adj["base_rank"] = adj["base_score"].rank(ascending=False, method="min").astype(int)
        adj["final_rank"] = adj["final_score"].rank(ascending=False, method="min").astype(int)
        adj["rank_delta"] = adj["base_rank"] - adj["final_rank"]  # + = moved up
        movers = adj.sort_values("rank_delta")
        log("Biggest DOWN-movers (research downgraded):")
        for _, r in movers.head(5).iterrows():
            log(f"  {r['ticker']:8s} base_rank={r['base_rank']} -> final_rank={r['final_rank']} "
                f"(dep={r.get('llm_event_dependence', '?')}, cont={r.get('llm_continuation', '?')})")
        log("Biggest UP-movers (research upgraded):")
        for _, r in movers.tail(5).iterrows():
            log(f"  {r['ticker']:8s} base_rank={r['base_rank']} -> final_rank={r['final_rank']} "
                f"(dep={r.get('llm_event_dependence', '?')}, cont={r.get('llm_continuation', '?')})")

    # 8) Final picks: most-confident stocks + most-confident ETFs
    final_stocks, final_etfs = pick_final(adj, args, llm_path)
    final = pd.concat([final_stocks, final_etfs], ignore_index=True)

    def _print_group(title, g):
        print(f"\n{title}")
        print("-" * 70)
        lines = []
        for i, (_, r) in enumerate(g.iterrows(), 1):
            try:
                cf = f"{float(r['llm_confidence']):.0%}"
            except Exception:
                cf = "n/a"
            line = (
                f"{i:2d}. {r['ticker']:8s} | {r['sector'][:20]:20s} | "
                f"1y {r['ret_1y']:+.0%} | est next 1y {r['est_next_1y']:+.1%} | "
                f"conf {cf} | vol {r['vol60']:.0%} | dd {r['maxdd']:.0%} | "
                f"stab {r.get('stability', '?')} | score {r['final_score']:+.2f}"
            )
            print(line)
            lines.append(line)
            print(f"    {r['name'][:60]}")
            extra = r.get("llm_rationale") or r.get("news_sample", "")
            if extra:
                print(f"    note: {str(extra)[:140]}")
        return lines

    print("\n" + "=" * 70)
    print(f"TOP PICKS vs {args.benchmark} — {args.n_stocks} stocks + {args.n_etfs} ETFs "
          f"(floor {args.min_score:+.1f})")
    print("=" * 70)
    final_lines = _print_group(f"STOCKS (max {args.max_per_sector} per sector)", final_stocks)
    final_lines += _print_group(f"ETFs (max {args.max_per_etf_category} per category)", final_etfs)
    log("FINAL PICKS:\n" + "\n".join(final_lines))
    if len(final_stocks) < args.n_stocks or len(final_etfs) < args.n_etfs:
        log("NOTE: some slots intentionally left empty (floor/veto)")
        print("\nNOTE: some slots intentionally left empty (quality floor / veto).")
    print("\nNote: past performance doesn't predict future returns. This is a")
    print("screening tool, not financial advice. Consider position sizing,")
    print("diversification, and your own risk tolerance.")

    # Save CSV (stocks + ETFs, kind column)
    tag = "_llm" if args.with_llm else ""
    out = f"screener_results_{datetime.now().strftime('%Y%m%d')}_{args.benchmark}{tag}.csv"
    final.to_csv(out, index=False)
    print(f"\nSaved: {out}")

    # Chart: 5+5 with 1y bars + estimated next-1y bars
    chart_path = f"chart_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{args.benchmark}{tag}.html"
    make_chart_html(final_stocks, final_etfs, chart_path,
                    {"benchmark": args.benchmark,
                     "etf_benchmark": etf_benchmark(args),
                     "asof": datetime.now().strftime("%Y-%m-%d")})
    print(f"Chart: {chart_path}")

    # ---- automatic post-run validation (the "analyze" half of self-correction) ----
    checks, fails = run_self_check(final, args, out)
    if fails:
        print("\n!!! SELF-CHECK FAILURES — review the log before trusting results:")
        for c in fails:
            print(f"    FAIL: {c['name']} {c['detail']}")
        log_error(f"SELF-CHECK failed: {[c['name'] for c in fails]}")

    # ---- machine-readable run summary for analysis ----
    summary = {
        "timestamp": datetime.now().isoformat(),
        "args": {k: v for k, v in vars(args).items()},
        "log_file": LOG_FILE,
        "stages": {
            "universe": len(universe),
            "candidates": len(cands),
            "outperformers": len(outperformers),
            "trash_kept": trash_report.get("kept"),
            "trash_dropped": trash_report.get("dropped"),
            "gated": gate_report.get("gated_total"),
            "final_stocks": len(final_stocks),
            "final_etfs": len(final_etfs),
        },
        "trash_breakdown": {k: v for k, v in trash_report.items()
                            if k not in ("kept", "dropped")},
        "gate_report": gate_report,
        "cache": cache.report(),
        "base_score_stats": df["base_score"].describe().to_dict(),
        "self_check": checks,
        "final_picks": [
            {"ticker": r["ticker"], "kind": r.get("kind"), "name": r["name"],
             "sector": r["sector"],
             "ret_1y": round(float(r["ret_1y"]), 4),
             "vol60": round(float(r["vol60"]), 4),
             "maxdd": round(float(r["maxdd"]), 4),
             "base_score": round(float(r["base_score"]), 3),
             "final_score": round(float(r["final_score"]), 3),
             "est_next_1y": round(float(r["est_next_1y"]), 4),
             "confidence": _safe(r.get("llm_confidence")),
             "stability": r.get("stability"),
             "event_dependence": _safe(r.get("llm_event_dependence")),
             "continuation": _safe(r.get("llm_continuation"))}
            for _, r in final.iterrows()
        ],
        "chart": chart_path,
    }
    summary_path = f"run_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{args.benchmark}{tag}.json"
    write_run_summary(summary_path, summary)

    log(f"Run complete. Results: {out} | Log: {LOG_FILE} | Summary: {summary_path}")
    log(f"cache: {cache.report()}")
    log(f"STATS universe={len(universe)} candidates={len(cands)} "
        f"outperformers={len(outperformers)} kept={trash_report['kept']} "
        f"gated={gate_report['gated_total']} final_picks={len(final)}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log_error(f"FATAL unhandled exception: {e}")
        raise
