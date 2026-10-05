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
# Pin the process timezone to the user's timezone (America/Los_Angeles).
# datetime.now() follows the ambient TZ, which varies between execution
# contexts (interactive shells, cron workers, subagents) — without this pin,
# evening runs produced UTC-dated cache partitions, log files, and bundle
# names (e.g. cache/20261004 for an Oct 3 run), splitting the warm cache
# and mislabeling research provenance. All pipeline dates are user-local.
os.environ["TZ"] = "America/Los_Angeles"
time.tzset()
from collections import Counter
from datetime import datetime, timedelta, timezone


def pipeline_today():
    """Cache-partition date: user-local, rewound to Friday on weekends.

    Markets are closed Sat/Sun, so a weekend run would refetch Friday's
    closes into a new partition. Rewinding to Friday lets weekend runs reuse
    the warm partition instead. Log/bundle/chart filenames keep the true
    date — only the cache partition rewinds."""
    d = datetime.now().date()
    if d.weekday() == 5:      # Saturday -> Friday
        d -= timedelta(days=1)
    elif d.weekday() == 6:    # Sunday -> Friday
        d -= timedelta(days=2)
    return d.strftime("%Y%m%d")

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

# Fundamental good-news words only: earnings/contract/approval milestones.
# Deliberately EXCLUDES analyst upgrades, price targets, and social-media
# chatter — those are noise, not thesis-changing developments.
POSITIVE_WORDS = {
    "raised guidance", "raises guidance", "beat estimates", "beats estimates",
    "topped estimates", "record revenue", "record earnings", "record profit",
    "contract win", "contract awarded", "wins contract", "deal signed",
    "fda approval", "approved by fda", "regulatory approval",
    "dividend increase", "raises dividend", "dividend raised",
    "buyback", "share repurchase",
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
            EqyQy('is-in', ['exchange', 'NMS', 'NYQ', 'ASE', 'NGM', 'NCM', 'BTS']),
            # NMS/NYQ/ASE + NGM (Nasdaq Global Market) + NCM (Nasdaq Capital
            # Market) + BTS (CBOE BZX). 2026-10-03 audit: NGM/NCM/BTS were
            # missing, silently dropping ~105 US-listed stocks >=$2B including
            # AAOI (+242% 52w%), SITM (+126%), PTGX (+118%) — same bug class as
            # the ETF PCX/BTS fixes. OTC (PNK/OQX/OQB) stays excluded by design
            # (not exchange-listed).
            EqyQy('gte', ['intradaymarketcap', min_mcap]),
            EqyQy('gte', ['intradayprice', min_price]),
        ])
        quotes = []
        offset = 0
        while True:
            # Per-page retry: a 429 on page 6 must not discard pages 1-5.
            # 3 attempts with backoff; accumulated quotes are kept either way.
            batch, total = None, 0
            for attempt in (1, 2, 3):
                try:
                    res = yf.screen(q, size=250, offset=offset,
                                    sortField='intradaymarketcap', sortAsc=False)
                    batch = res.get('quotes', []) or []
                    total = res.get('total', 0)
                    break
                except Exception as e:
                    log(f"  screener page offset {offset}: attempt {attempt} "
                        f"failed ({e})")
                    time.sleep(2 * attempt)
            if batch is None:
                log(f"  screener page offset {offset}: giving up after 3 "
                    f"attempts — keeping {len(quotes)} quotes fetched so far")
                break
            quotes.extend(batch)
            total = total or 0
            log(f"  page offset {offset}: {len(quotes)}/{total} fetched")
            if len(batch) < 250 or (total and len(quotes) >= total):
                break
            offset += 250
            time.sleep(0.4)
        out, seen = [], set()
        _exchanges = {}
        for x in quotes:
            sym = x.get('symbol')
            pct = x.get('fiftyTwoWeekChangePercent')
            if sym and pct is not None and sym not in seen:
                seen.add(sym)
                _exchanges[x.get('exchange')] = \
                    _exchanges.get(x.get('exchange'), 0) + 1
                out.append({
                    "ticker": sym,
                    "name": x.get('longName') or x.get('shortName') or sym,
                    "pct_52w": float(pct),
                })
        log(f"Universe: {len(out)} stocks from yfinance screener")
        log(f"Universe exchanges: {dict(sorted(_exchanges.items(), key=lambda kv: -kv[1]))}")
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


def build_scores(closes, infos, min_rows=120):
    """Score for: continue to do well + minimize chance of going negative after buying.

    Heavily weights: persistent momentum + trend + quality + LOW risk
    (low vol, shallow drawdown, low leverage, reasonable valuation).

    min_rows: benchmark mode needs 120d+ of history; watchlist mode passes 60
    so short-history names (recent IPOs) are scored, not silently dropped.
    """
    rows = []
    for t, info in infos.items():
        px = closes.get(t)
        if px is None or len(px) < min_rows:
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
    log(f"research pool: top {args.n_stock_research} stocks + top {args.n_etf_research} ETFs by base_score")
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
        f"overlap cap for ETFs, enforced on the SELECTED portfolio: when two "
        f"picks overlap above the cap the lower-EV one is evicted and the slot "
        f"refilled (redundancy is not diversification)")
    log(f"score floor --min-score={args.min_score}")
    log(f"final slots: --n-stocks={args.n_stocks} + --n-etfs={args.n_etfs}")
    log("--- end levers ---")


def apply_risk_gates(df, max_vol=0.80, min_dd=-0.40):
    """Absolute risk gates for the 'don't go negative' goal.

    Relative z-scores can't do this: in a parabolic universe, -40% DD can
    look 'average'. Gate absolutely before research/final selection.

    Fail-closed on missing data: a ticker whose volatility or drawdown can't
    be measured doesn't get the benefit of the doubt (NaN comparisons are
    False in pandas, which would otherwise let unmeasurable names through).
    """
    gated_vol = df["vol60"].fillna(float("inf")) > max_vol
    gated_dd = df["maxdd"].fillna(float("-inf")) < min_dd
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
    n_fresh, n_carried, n_unknown, n_stale, n_missing = 0, 0, 0, 0, 0
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
        why = str(r.get("llm_rationale", "") or "").replace("\n", " ").strip()
        if ad == today:
            prov, n_fresh = "fresh", n_fresh + 1
        elif ad:
            n_carried += 1
            try:
                age = (__import__("datetime").date.today() -
                       __import__("datetime").date.fromisoformat(ad)).days
            except Exception:
                age = -1
            if age > 7:
                # stale: the 7-day refresh rule says this should have been
                # re-researched — flag it loudly rather than silently carrying
                prov, n_stale = f"carried({ad}) STALE>{age}d", n_stale + 1
            else:
                prov = f"carried({ad})"
        elif why:
            prov, n_unknown = "carried(?)", n_unknown + 1
        else:
            # no assessment at all: neutral defaults (dep 0.3/cont 0.5/conf
            # 0.7) were used silently by apply_llm_outputs — make it visible
            prov, n_missing = "NO-ASSESSMENT (neutral defaults)", n_missing + 1
        log(f"  {r['ticker']:6s} dep={dep_s} cont={cont_s} conf={conf_s} [{prov}]")
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
        f"{n_unknown} unknown (pre-dates date stamping), "
        f"{n_stale} STALE (>7d, should have been re-researched), "
        f"{n_missing} with NO assessment (neutral defaults used)")


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


def watchlist_quality_summary(ratings, evs, tickers=None):
    """One-sentence verdict on the watchlist as a whole — computed, not
    written. Keys the quality word off the average expected value, names the
    drag when sells pull it below the +3% bar, and calls out the leader and
    the biggest drag by name."""
    n = len(ratings)
    if not n:
        return ""
    b = sum(1 for x in ratings if x == "BUY")
    h = sum(1 for x in ratings if x == "HOLD")
    s = n - b - h
    try:
        evf = [float(e) for e in evs]
        avg = sum(evf) / n
    except Exception:
        evf, avg = [], 0.0
    if avg >= 0.08:
        q = "strong"
    elif avg >= 0.05:
        q = "solid"
    elif avg >= 0.03:
        q = "decent"
    elif avg >= 0.0:
        q = "mixed"
    else:
        q = "weak"

    def _pl(k, w):
        return f"{k} {w}" if k == 1 else f"{k} {w}s"
    out = (f"A {q} list — {_pl(b, 'buy')}, {_pl(h, 'hold')}, {_pl(s, 'sell')}, "
           f"averaging {avg:+.1%} expected value")
    if s and avg < 0.03:
        out += ("; the sell pulls the average below the +3% bar" if s == 1
                else "; the sells pull the average below the +3% bar")
    if tickers and evf and n > 1:
        try:
            order = sorted(range(n), key=lambda i: evf[i], reverse=True)
            lead, drag = order[0], order[-1]
            out += (f"; {tickers[lead]} ({evf[lead]:+.1%}) leads, "
                    f"{tickers[drag]} ({evf[drag]:+.1%}) drags")
        except Exception:
            pass
    return out + "."


def add_ratings(df):
    """Attach deterministic Buy/Hold/Sell columns ('what Mint would do').

    Uses compute_rating() on the pipeline outputs already present
    (est_next_1y, llm_event_dependence, llm_confidence, vol60, maxdd).
    Logs each rating for the audit trail.
    """
    sub = df.copy()
    _rats = [compute_rating(r.get("est_next_1y"), r.get("llm_event_dependence"),
                            r.get("llm_confidence"), r.get("vol60"),
                            r.get("maxdd")) for _, r in sub.iterrows()]
    sub["rating"] = [x[0] for x in _rats]
    sub["rating_reason"] = [x[1] for x in _rats]
    for _, r in sub.iterrows():
        log(f"  rating {r['ticker']:6s}: {r['rating']} — {r['rating_reason']}")
    return sub


def compute_rating(ev, dep, conf, vol, maxdd):
    """Buy/Hold/Sell from pipeline outputs — 'what Mint would do'.

    Deterministic and auditable (no LLM vibes). Philosophy mapping:
    - SELL: doesn't earn its place (EV below the +3% floor) or fails the
      event-driven veto (dep >= 0.7) — the two ways the main pipeline
      rejects a name outright.
    - HOLD: earns its place (EV >= +3%) but Mint wouldn't buy it as-is:
      fails a risk gate (too volatile / too deeply scarred — downside
      protection first), leans on outside events (dep >= 0.5), or the
      read isn't trusted (conf < 0.6).
    - BUY: clears the bar with margin (EV >= +8%), durable thesis
      (dep < 0.5), trusted read (conf >= 0.6), passes the risk gates.
    Returns (rating, reason) with the reason in plain words.
    """
    import math
    try:
        ev = float(ev)
    except Exception:
        return ("HOLD", "no estimate available")
    try:
        dep = float(dep)
    except Exception:
        dep = 0.3
    try:
        conf = float(conf)
    except Exception:
        conf = 0.7
    if math.isnan(ev):
        return ("HOLD", "no estimate available")
    if ev < 0.03:
        return ("SELL", "expected value below the +3% bar — doesn't earn its place")
    if dep >= 0.7:
        return ("SELL", "event-driven — the thesis depends on things outside its control")

    def _missing(x):
        if x is None:
            return True
        try:
            return math.isnan(float(x))
        except Exception:
            return True

    # Fail-closed on missing risk data: an unverifiable name can't be a BUY.
    if _missing(vol) or _missing(maxdd):
        return ("HOLD", "clears the bar, but risk couldn't be verified")
    try:
        if float(vol) > 0.80:
            return ("HOLD", "clears the bar, but too volatile for a buy")
    except Exception:
        pass
    try:
        if float(maxdd) < -0.40:
            return ("HOLD", "clears the bar, but too deeply scarred for a buy")
    except Exception:
        pass
    if dep >= 0.5:
        return ("HOLD", "clears the bar, but the thesis leans on outside events")
    if conf < 0.6:
        return ("HOLD", "clears the bar, but confidence in the read is low")
    if ev >= 0.08:
        return ("BUY", "clears the bar with margin — durable thesis, trusted read")
    return ("HOLD", "earns its place, but not with conviction")


# ---------------- ETF pipeline ----------------
# ETFs have no ROE/margins/P-E; score on momentum + risk + structure/cost.
# Same goal: likely to continue, minimize going negative after buying.

US_ETF_EXCHANGES = {"NMS", "NYQ", "ASE", "ARC", "BAT", "NGM", "PCX", "BTS"}
# NOTE 2026-10-03: "PCX" is how Yahoo's ETF screener labels NYSE Arca (it never
# emits "ARC" there). It was missing, silently excluding every Arca-listed ETF
# — including SPMO — from the candidate universe. Arca is a US exchange, so
# this is a bug fix, not a methodology change.
# 2026-10-03, same bug class again: "BTS" is Yahoo's code for CBOE BZX (BATS),
# also a US exchange. It was missing too, silently dropping ~90 hot-band ETFs
# including VLUE (+57% 52w%) and DIVB. Exchange skips are now counted+logged
# below so a missing code can never go silent again.
LEVERAGE_PAT = None  # compiled lazily (re module import at top)


def _leverage_pat():
    global LEVERAGE_PAT
    if LEVERAGE_PAT is None:
        import re as _re
        LEVERAGE_PAT = _re.compile(
            r"2X|3X|LEVERAG|ULTRA|DIREXION|MICROSECTORS|INVERSE|\bSHORT\b|HEDGE"
            r"|BEAR\s*\d|BULL\s*\d|DAILY\s*(BULL|BEAR)", _re.IGNORECASE)
    return LEVERAGE_PAT


def get_etf_universe(target=2000, min_price=5.0, min_52w=None):
    """Top 52-week gaining US-listed, unleveraged ETFs via yfinance ETF screener.

    Tiles 52w% bands top-down (API caps page size). Leveraged/inverse and
    non-US listings are excluded up front — structural decay and closure
    risk are the opposite of 'don't go negative'.

    When min_52w is given (benchmark-relative mode), bands are tiled until
    the band range sits entirely below min_52w — the cutoff is 'better than
    the benchmark', not an arbitrary headcount. `target` remains only as a
    hard work bound (raised 2026-10-04: 500 silently truncated the universe
    mid-band in hot years, dropping 57 eligible ETFs — the same bug class as
    the old fixed-200 cap). If the bound ever binds before the bar is
    reached, it logs a WARNING so the truncation is visible, not silent.
    """
    from yfinance import ETFQuery, screen
    bands = [(150, None), (100, 150), (70, 100), (50, 70), (35, 50),
             (25, 35), (15, 25), (8, 15), (0, 8)]
    pat = _leverage_pat()
    seen, out = set(), []
    _ex_skipped = {}
    for lo, hi in bands:
        if min_52w is not None and hi is not None and hi <= min_52w:
            break  # band entirely below the bar; lower bands are too
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
                    _ex_skipped[x.get("exchange")] = \
                        _ex_skipped.get(x.get("exchange"), 0) + 1
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
    log(f"ETF universe final: {len(out)} US unleveraged ETFs"
        + (f" (52w% > {min_52w:.0f}%)" if min_52w is not None else ""))
    if len(out) >= target and min_52w is not None:
        log(f"WARNING: ETF work bound ({target}) hit before reaching the "
            f"{min_52w:.0f}% bar — universe is TRUNCATED, not complete. "
            f"Raise target.")
    if _ex_skipped:
        log(f"ETF universe: skipped exchanges (not in allow-list): "
            f"{dict(sorted(_ex_skipped.items(), key=lambda kv: -kv[1]))}")
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
    Returns (kept_rows, report) with dropped ticker names per reason (audit:
    every silent exclusion must be named, same as the stock filter)."""
    kept = []
    dropped = {"short_history": [], "illiquid": [], "tiny_aum": [],
               "blown_up": [], "leveraged_name": [], "foreign_focus": [],
               "religious_theme": []}
    pat = _leverage_pat()
    for r in rows:
        t = r["ticker"]
        px = closes.get(t)
        if px is None or len(px) < 120:
            dropped["short_history"].append(t)
            continue
        info = infos.get(t, {})
        name = str(info.get("longName") or r["name"] or "")
        if pat.search(name) or pat.search(t):
            dropped["leveraged_name"].append(t)
            continue
        if _religious_theme(name, t):
            dropped["religious_theme"].append(t)
            continue
        if us_only and _foreign_focus(info, name):
            dropped["foreign_focus"].append(t)
            continue
        try:
            adv = (info.get("averageDailyVolume10Day")
                   or info.get("averageVolume")
                   or r.get("screener_vol10") or 0)
            lastpx = info.get("regularMarketPrice") or px.iloc[-1]
            if float(adv) * float(lastpx) < 2_000_000:
                dropped["illiquid"].append(t)
                continue
        except Exception as e:
            # Fail-closed: unverifiable liquidity doesn't pass (was: pass).
            log(f"etf trash: {t} liquidity check failed ({e}) — dropped")
            dropped["illiquid"].append(t)
            continue
        aum = (etf_facts(info)["aum"] or r.get("screener_aum") or 0)
        try:
            aum_f = float(aum) if aum else 0.0
        except Exception:
            aum_f = 0.0
        # Fail-closed on unknown AUM: scale can't be verified (was: kept).
        if aum_f < 100_000_000:
            dropped["tiny_aum"].append(t)
            continue
        dd = max_drawdown(px)
        if dd < -0.70:
            dropped["blown_up"].append(t)
            continue
        kept.append(r)
    report = {"kept": len(kept),
              "dropped_total": sum(len(v) for v in dropped.values())}
    for reason, names in dropped.items():
        report[reason] = len(names)
        if names:
            log(f"etf trash[{reason}]: {names}")
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


def make_chart_html(stocks_df, etfs_df, path, meta, titles=None,
                  honorable=None, market_chat=None, watchlist_chat=None):
    """HTML chart: stocks + ETFs with 1y return, expected value, confidence.

    Columns: 1-year performance %, sector, and expected value %
    (continuation-vs-downside expected value heuristic). `titles` optionally
    overrides the two section headings. `honorable` is a list of dicts (ticker, kind, name, sector,
    ret_1y, est_next_1y, confidence, reason) rendered as the Honorable
    mentions table. Rows carrying a non-US `country`
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

    # --- Blurb maps: ticker -> story, rendered INSIDE the pick row and
    # revealed by clicking it (expand, not flip — a flip would hide the
    # stats the row exists to show). Main page: market_chat sector tickers
    # (what/why). Watchlist: blurb + rating chip. The market-chat section
    # below keeps only the sector paragraphs; the watchlist's "About these
    # names" section is gone (its summary stays).
    _bmap = {}
    if market_chat and market_chat.get("sectors"):
        for _sec in market_chat["sectors"]:
            for _t in _sec.get("tickers", []):
                _bmap[str(_t.get("ticker", "")).upper()] = (
                    "mc", str(_t.get("what", "")), str(_t.get("why", "")))
    if watchlist_chat and watchlist_chat.get("tickers"):
        for _t in watchlist_chat["tickers"]:
            _bmap[str(_t.get("ticker", "")).upper()] = (
                "wl", str(_t.get("blurb", "")),
                str(_t.get("rating", "HOLD")).upper(),
                str(_t.get("rating_reason", "")))
    if market_chat and market_chat.get("honorable"):
        for _t in market_chat["honorable"]:
            _bmap[str(_t.get("ticker", "")).upper()] = (
                "hm", str(_t.get("what", "")), str(_t.get("why", "")))
    header = """
<div class="row head">
  <div># / Ticker / Name</div><div>Sector / Category</div>
  <div>1-year return</div><div>Expected value</div>
  <div>Confidence</div><div>Stability</div>
</div>
"""
    rows_html = ""
    for si, (title, df) in enumerate(sections):
        rows_html += f'<h2>{_html.escape(title)}</h2>\n'
        rows_html += (
            f'<div class="sortctl" data-pl="pl{si}"><span>Sort by:</span> '
            f'<button class="sbtn" data-k="r1y">1-year return</button>'
            f'<button class="sbtn on" data-k="ev">Expected value</button></div>\n')
        rows_html += header + f'<div class="picklist" id="pl{si}">\n'
        for i, (_, r) in enumerate(df.iterrows(), 1):
            est = r.get("est_next_1y")
            ctry = str(r.get("country") or "")
            nonus = (f' <span class="nonus" title="Domiciled in {_html.escape(ctry)}">'
                     f"non-US</span>" if ctry and ctry != "United States" else "")
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
            _b = _bmap.get(str(r["ticker"]).upper())
            _blurb_html, _chev, _hasb, _wchip = "", "", "", ""
            if _b:
                _hasb = " has-blurb"
                _chev = '<span class="chev" title="Tap to expand">▸</span>'
                if _b[0] in ("mc", "hm"):
                    _blurb_html = (
                        f'<div class="blurb"><div class="mc-what">'
                        f'{_html.escape(_b[1])}</div>'
                        f'<div class="mc-why">{_html.escape(_b[2])}</div></div>')
                else:
                    # watchlist: the rating chip lives on the row itself, not
                    # hidden in the expansion — the call should be visible
                    _rcls = {"BUY": "buy", "HOLD": "hold",
                             "SELL": "sell"}.get(_b[2], "hold")
                    _wchip = f' <span class="rchip {_rcls}">{_b[2]}</span>'
                    _blurb_html = (
                        f'<div class="blurb">'
                        f'<span class="mc-why">{_html.escape(_b[1])}</span>'
                        + (f'<br><span class="wl-rate-why">{_b[2].title()}: '
                           f'{_html.escape(_b[3])}</span>' if _b[3] else '')
                        + '</div>')
            rows_html += f"""
<div class="row k-{_kk}{_hasb}" data-ev="{_evf}" data-r1y="{_r1yf}">
  <div class="id"><span class="rank">{i}</span>
    <span class="tick">{_html.escape(str(r['ticker']))}</span>
    <span class="nm">{_html.escape(str(r['name'])[:38])}{nonus}</span>{_wchip}{_chev}</div>
  <div class="sec">{_html.escape(str(r['sector'])[:26])}</div>
  <div class="cell" data-cap="1-year return"><div class="{lbl(r['ret_1y'], 'r1y')}">{pct(r['ret_1y'])}</div>{bar(r['ret_1y'])}</div>
  <div class="cell" data-cap="Expected value"><div class="{lbl(est, 'est')}">{pct(est)}</div>{bar(est, gold=True)}</div>
  <div class="cf" data-cap="Confidence">{conf_pct(r.get('llm_confidence'))}</div>
  <div class="stab" data-cap="Stability">{_html.escape(str(r.get('stability', '?')))}</div>
  {_blurb_html}
</div>
"""
        rows_html += '</div>\n'  # close .picklist
    now = meta.get("asof", "")
    bench = meta.get("benchmark", "")
    etf_bench = meta.get("etf_benchmark", bench)
    bench_label = (f"{bench} (stocks) / {etf_bench} (ETFs)"
                   if etf_bench != bench else bench)
    # --- Honorable mentions: cleared the EV floor, didn't make the cut.
    # The near-miss table Dan asked for — alternatives worth a look, with the
    # reason each missed (cap, overlap, or final-score order).
    hm_html = ""
    # --- Market today: single reader-facing section (themed blocks with
    # colored bold leads). Content is written by the researcher into
    # market_chat.json as "market_today": [{"tone": "good"|"bad"|"verdict",
    # "lead": "...", "body": "..."}]; the chart only renders it.
    _TONE_COLORS = {"good": "#4ade80", "bad": "#f87171",
                    "verdict": "#ffd54f"}
    mc_html = ""
    _mt = market_chat.get("market_today") if market_chat else None
    if _mt:
        _parts = ['<h2>Market today</h2>', '<div class="note mc-today">']
        for _b in _mt:
            _tone = str(_b.get("tone", "")).strip().lower()
            _color = _TONE_COLORS.get(_tone, "#cfd6cf")
            _lead = str(_b.get("lead", "")).strip()
            _body = str(_b.get("body", "")).strip()
            if not _lead and not _body:
                continue
            _parts.append(
                f'<p><b style="color:{_color}">{_html.escape(_lead)}</b>'
                + (f' {_html.escape(_body)}' if _body else '') + '</p>')
        _parts.append(
            '<p class="fineprint">As of ' +
            _html.escape(str(market_chat.get("asof", ""))) + '.</p></div>')
        mc_html = "\n".join(_parts)
    elif market_chat and market_chat.get("sectors"):
        # legacy fallback: overview + per-sector expandables
        _parts = ['<h2>Market chat</h2>']
        _ov = str(market_chat.get("overview") or "").strip()
        if _ov:
            _parts.append(
                '<div class="note mc-overview"><p>' + _html.escape(_ov) + '</p>'
                '<p class="fineprint">As of ' +
                _html.escape(str(market_chat.get("asof", ""))) + '.</p></div>')
        for _sec in market_chat["sectors"]:
            # per-ticker what/why now lives inside the pick rows (click to
            # expand); the sector block keeps only its "why it's doing well"
            _parts.append(
                f'<details class="mc-sec"><summary>'
                f'{_html.escape(str(_sec.get("name", "")))}</summary>'
                f'<div class="mc-body"><p>'
                f'{_html.escape(str(_sec.get("blurb", "")))}</p>'
                f'</div></details>')
        mc_html = "\n".join(_parts)
    # --- Watchlist verdict: the computed one-liner plus the researcher's
    # daily roast (summary_color). Ticker blurbs now live inside the pick
    # rows (click to expand) — this paragraph is all that stays below.
    wl_html = ""
    if watchlist_chat:
        _wsum = str(watchlist_chat.get("summary") or "").strip()
        _wcolor = str(watchlist_chat.get("summary_color") or "").strip()
        if _wsum or _wcolor:
            _wtxt = _wsum + (" " + _wcolor if _wcolor else "")
            wl_html = (f'<p class="note wl-summary">{_html.escape(_wtxt)}</p>')
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
            _extra = " hm-extra" if i > 5 else ""
            _hb = _bmap.get(str(h.get("ticker", "")).upper())
            _hblurb_html, _hchev, _hhasb = "", "", ""
            if _hb and _hb[0] == "hm" and (_hb[1] or _hb[2]):
                _hhasb = " has-blurb"
                _hchev = '<span class="chev" title="Tap to expand">▸</span>'
                _hblurb_html = (
                    f'<div class="blurb"><div class="mc-what">'
                    f'{_html.escape(_hb[1])}</div>'
                    f'<div class="mc-why">{_html.escape(_hb[2])}</div></div>')
            hm_rows.append(f"""
<div class="row hm k-{_hk}{_extra}{_hhasb}" data-ev="{_hev}" data-r1y="{_hr1y}">
  <div class="id"><span class="rank">{i}</span>
    <span class="tick">{_html.escape(str(h.get('ticker', '')))}</span>
    <span class="kchip {_hk}">{_hk.upper()}</span>
    <span class="nm">{_html.escape(str(h.get('name', ''))[:38])}</span>{_hchev}</div>
  <div class="sec">{_html.escape(str(h.get('sector', ''))[:26])}</div>
  <div class="cell" data-cap="1-year return"><div class="{lbl(h.get('ret_1y'), 'r1y')}">{pct(h.get('ret_1y'))}</div>{bar(h.get('ret_1y'))}</div>
  <div class="cell" data-cap="Expected value"><div class="{lbl(h.get('est_next_1y'), 'est')}">{pct(h.get('est_next_1y'))}</div>{bar(h.get('est_next_1y'), gold=True)}</div>
  <div class="cf" data-cap="Confidence">{conf_pct(h.get('confidence'))}</div>
  <div class="why" data-cap="Why not picked">{_html.escape(str(h.get('reason', '')))}</div>
{_hblurb_html}</div>""")
        hm_html = (
            '<h2>Honorable mentions — cleared the bar, didn\u2019t make the cut</h2>\n'
            '<div class="sortctl" data-pl="pl2"><span>Sort by:</span> '
            '<button class="sbtn" data-k="r1y">1-year return</button>'
            '<button class="sbtn on" data-k="ev">Expected value</button></div>\n'
            '<div class="row head"><div># / Ticker / Name</div><div>Sector / Category</div>'
            '<div>1-year return</div><div>Expected value</div>'
            '<div>Confidence</div><div>Why not picked</div></div>\n'
            '<div class="picklist" id="pl2">\n'
            + "\n".join(hm_rows) + "\n</div>\n"
            + (f'<div class="hm-more"><button class="sbtn" id="hm-toggle" '
                f'data-n="{len(honorable)}">Show all {len(honorable)} \u2193</button></div>\n'
                if len(honorable) > 5 else ""))
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
    radial-gradient(1000px 420px at 50% -8%, rgba(125,211,252,0.10), transparent 60%),
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
  position: relative; overflow: hidden;
  background: rgba(255,255,255,0.028);
  -webkit-backdrop-filter: blur(8px) saturate(1.5);
  backdrop-filter: blur(8px) saturate(1.5);
  border: 1px solid rgba(255,255,255,0.14); border-radius: 16px;
  box-shadow: 0 8px 28px rgba(0,0,0,0.38), inset 0 1px 0 rgba(255,255,255,0.18); }}
.row::before {{ content: ""; position: absolute; inset: 0; border-radius: inherit;
  pointer-events: none;
  background: linear-gradient(115deg, var(--sheen, rgba(255,255,255,0.08)) 0%,
    var(--sheen2, rgba(255,255,255,0.03)) 30%, transparent 58%); }}
.row.head::before {{ display: none; }}
.row.k-stock {{ --sheen: rgba(125,211,252,0.22); --sheen2: rgba(125,211,252,0.07); }}
.row.k-etf {{ --sheen: rgba(196,181,253,0.22); --sheen2: rgba(196,181,253,0.07); }}
.row.head {{ background: none; border: none; box-shadow: none;
  -webkit-backdrop-filter: none; backdrop-filter: none;
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.10em;
  color: #93a093; padding: 4px 16px 8px; margin-bottom: 2px; }}
.row > div {{ min-width: 0; overflow: hidden; }}
.row.head > div {{ overflow: visible; }}
@media (hover: hover) {{
  .row {{ transition: border-color 0.25s ease, box-shadow 0.25s ease; }}
  .row:hover {{ border-color: rgba(255,255,255,0.22);
    box-shadow: 0 8px 28px rgba(0,0,0,0.38), inset 0 1px 0 rgba(255,255,255,0.24); }}
}}
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
.bar {{ height: 10px; border-radius: 6px; position: relative; overflow: hidden; }}
.bar::after {{ content: ""; position: absolute; inset: 0; border-radius: inherit;
  background: linear-gradient(180deg, rgba(255,255,255,0.55) 0%,
    rgba(255,255,255,0.14) 42%, rgba(255,255,255,0) 62%);
  pointer-events: none; }}
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
.note {{ position: relative; overflow: hidden; margin-top: 26px; font-size: 12px;
  color: #9aa79a; line-height: 1.6; padding: 16px 20px;
  background: rgba(255,255,255,0.025);
  -webkit-backdrop-filter: blur(8px) saturate(1.5);
  backdrop-filter: blur(8px) saturate(1.5);
  border: 1px solid rgba(255,255,255,0.14); border-radius: 16px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.18), 0 8px 28px rgba(0,0,0,0.38); }}
.note::before {{ content: ""; position: absolute; inset: 0; pointer-events: none;
  background: linear-gradient(115deg, rgba(255,213,79,0.16) 0%,
    rgba(255,213,79,0.05) 30%, transparent 58%); }}
.note b {{ color: var(--gold); }}
.note p {{ margin: 0 0 13px; }}
.note p:last-child {{ margin-bottom: 0; }}
.note .stages-head {{ font-weight: 700; color: #cfd6cf;
  margin-top: 18px; }}
.note .fineprint {{ font-size: 11px; color: #7d887d; }}

.tledger a {{ color: #9aa79a; font-size: 12px;
  text-decoration: underline; text-underline-offset: 2px; }}
.sortctl {{ display: flex; align-items: center; gap: 8px; margin: 2px 0 10px;
  font-size: 12px; color: #93a093; letter-spacing: 0.06em; }}
.sbtn {{ font-family: inherit; font-size: 12px; letter-spacing: 0.04em;
  color: #cfd6cf;
  background:
    linear-gradient(180deg, rgba(255,255,255,0.24) 0%, rgba(255,255,255,0.06) 52%,
      rgba(255,255,255,0) 100%),
    rgba(255,255,255,0.06);
  border: 1px solid rgba(255,255,255,0.16); border-radius: 999px;
  padding: 4px 14px; cursor: pointer;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.22), 0 2px 10px rgba(0,0,0,0.4); }}
.sbtn.on {{ color: #0a0f0c; font-weight: 700;
  background:
    linear-gradient(180deg, rgba(255,255,255,0.5) 0%, rgba(255,255,255,0.10) 52%,
      rgba(0,0,0,0.14) 100%),
    linear-gradient(90deg, var(--gold-deep), var(--gold));
  border-color: rgba(255,213,79,0.8);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.5), 0 0 14px rgba(255,213,79,0.45); }}
.why {{ font-size: 12px; color: #a8b3a8; text-align: right; line-height: 1.4; }}
.row.hm {{ opacity: 0.88; }}
.hm-extra {{ display: none; }}
#pl2.showall .hm-extra {{ display: grid; }}
.hm-more {{ margin: 4px 0 6px; }}
.row.k-stock .tick {{ color: #7dd3fc; }}
.row.k-etf .tick {{ color: #c4b5fd; }}
.kchip {{ display: inline-block; font-size: 10px; font-weight: 700;
  letter-spacing: 0.08em; border-radius: 8px; padding: 1px 7px;
  margin-left: 6px; vertical-align: 1px; white-space: nowrap; }}
.kchip.stock {{ color: #7dd3fc; background: rgba(125,211,252,0.12);
  border: 1px solid rgba(125,211,252,0.35); }}
.kchip.etf {{ color: #c4b5fd; background: rgba(196,181,253,0.12);
  border: 1px solid rgba(196,181,253,0.35); }}
.mc-sec {{ position: relative; overflow: hidden; margin-bottom: 10px;
  border: 1px solid rgba(255,255,255,0.14); border-radius: 16px;
  background: rgba(255,255,255,0.028);
  -webkit-backdrop-filter: blur(8px) saturate(1.5);
  backdrop-filter: blur(8px) saturate(1.5);
  box-shadow: 0 8px 28px rgba(0,0,0,0.38), inset 0 1px 0 rgba(255,255,255,0.18); }}
.mc-sec::before {{ content: ""; position: absolute; inset: 0; border-radius: inherit;
  pointer-events: none;
  background: linear-gradient(115deg, rgba(255,255,255,0.07) 0%, transparent 48%); }}
.mc-sec > summary {{ position: relative; cursor: pointer; padding: 13px 18px;
  font-size: 14px; font-weight: 600; letter-spacing: 0.08em; color: #cfd6cf;
  list-style: none; }}
.mc-sec > summary::-webkit-details-marker {{ display: none; }}
.mc-sec > summary::before {{ content: "▸  "; color: var(--gold); }}
.mc-sec[open] > summary::before {{ content: "▾  "; }}
.mc-body {{ position: relative; padding: 0 18px 14px; font-size: 13px;
  color: #a8b3a8; line-height: 1.65; }}
.mc-body p {{ margin: 0 0 12px; }}
.mc-item {{ margin: 12px 0; }}
.mc-tick {{ font-weight: 700; letter-spacing: 0.03em; }}
.mc-tick.stock {{ color: #7dd3fc; }}
.mc-tick.etf {{ color: #c4b5fd; }}
.note.mc-overview {{ font-size: 13px; margin-bottom: 14px; }}
.rchip {{ display: inline-block; font-size: 10px; font-weight: 700;
  letter-spacing: 0.08em; padding: 2px 9px; border-radius: 999px;
  border: 1px solid; vertical-align: 2px; margin-right: 7px; }}
.rchip.buy {{ color: #4ade80; border-color: rgba(74,222,128,0.40);
  background: rgba(74,222,128,0.10); }}
.row.has-blurb {{ cursor: pointer; }}
.row .blurb {{ display: none; grid-column: 1 / -1;
  border-top: 1px solid rgba(255,255,255,0.07);
  margin-top: 8px; padding: 8px 4px 6px 34px; }}
.row.open .blurb {{ display: block; }}
.row .blurb .mc-what {{ margin-bottom: 5px; }}
.chev {{ display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; font-size: 13px; line-height: 1; color: #cfd6cf;
  border: 1px solid rgba(255,255,255,0.22); border-radius: 50%;
  margin-left: 9px; vertical-align: 1px;
  transition: transform 0.18s ease, background 0.18s ease; }}
.row.open .chev {{ transform: rotate(90deg);
  background: rgba(255,255,255,0.09); }}
.rchip.hold {{ color: #ffd54f; border-color: rgba(255,213,79,0.40);
  background: rgba(255,213,79,0.10); }}
.rchip.sell {{ color: #f87171; border-color: rgba(248,113,113,0.40);
  background: rgba(248,113,113,0.10); }}
.wl-blurb {{ margin: 14px 0; }}
.wl-rate-why {{ color: #8a938a; font-style: italic; }}
.navwrap {{ text-align: center; margin: 2px 0 14px; }}
a.navbtn {{ display: inline-block; font-size: 12px; letter-spacing: 0.05em;
  font-weight: 600; color: #ffd54f; text-decoration: none;
  background:
    linear-gradient(180deg, rgba(255,255,255,0.24) 0%, rgba(255,255,255,0.06) 52%,
      rgba(255,255,255,0) 100%),
    rgba(255,213,79,0.08);
  border: 1px solid rgba(255,213,79,0.35); border-radius: 999px;
  padding: 6px 18px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.22), 0 2px 10px rgba(0,0,0,0.4); }}
a.navbtn:hover {{ background:
    linear-gradient(180deg, rgba(255,255,255,0.32) 0%, rgba(255,255,255,0.10) 52%,
      rgba(255,255,255,0) 100%),
    rgba(255,213,79,0.14); }}
.mc-nm {{ color: #cfd6cf; font-weight: 600; }}
.mc-what {{ color: #8a938a; }}
.mc-why {{ color: #a8b3a8; }}
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
{ '<div class="navwrap"><a class="navbtn" href="index.html">&larr; Mint picks</a></div>' if meta.get("mode") == "watchlist" else '<div class="navwrap"><a class="navbtn" href="watchlist.html">Dan&apos;s watchlist &rarr;</a></div>' }
{rows_html}
{wl_html}
{mc_html}
{hm_html}
<div class="note">
<p><b>About Mint.</b> Mint looks for American stocks and ETFs that have already
proven themselves &mdash; names that beat their benchmark over the past year.
For stocks that benchmark is VGT, a technology index; for ETFs it&apos;s VOO,
which tracks the S&amp;P 500. Then Mint asks the harder question: which are most
likely to keep winning without stumbling right after you buy?</p>
{ "<p>Watchlist mode: every ticker you supplied is researched and scored, sorted by expected value &mdash; no benchmark, no picking.</p>" if meta.get("mode") == "watchlist" else "" }
<p>The philosophy is simple: avoid losses first. We look past recent gains to the
business behind each name, the risks that could derail it, and whether its
success looks durable or depends on hype, headlines, or a one-time event.
Strong recent performance gets a name noticed. Strong evidence earns it a spot.
If nothing clears the bar, we leave the slot empty &mdash; every pick earns its
place.</p>
<p class="stages-head">How it works, in three stages:</p>
<p><b>Find strength.</b> Every stock and ETF is screened for beating its benchmark
over the past year, then scored on the quality of the business, the timing of
entry, and the steadiness of the ride. Hard risk gates throw out anything too
volatile or too deeply scarred.</p>
<p><b>Question the story.</b> Survivors get researched &mdash; we read the news
behind each name, not just the numbers. How much does the story depend on things
outside the company&apos;s control: wars, hype, one-time windfalls? Too much, and
it&apos;s out on the spot. For the rest: how likely is the strength to continue,
and how much do we trust our own read?</p>
<p><b>Weigh the odds.</b> Will the strength continue, or reverse? Those
judgments become one number &mdash; expected value: the likely gains if it holds,
minus the likely pain if it turns, adjusted for how much we trust our own
read.</p>
<p>Mint is built around a simple idea: find strength, question the story, weigh
the odds, and only make room for what earns it.</p>
<p class="fineprint">Not financial advice. Past performance doesn&apos;t predict
future returns. Also: there are gremlins that have control over the markets &mdash;
they hate you personally, and they do the exact opposite of your buys and sells
purely to spite you. Invest accordingly.</p>
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
      if(pl.id === 'pl2'){{ applyHmLimit(); }}
    }});
  }});
}});
function applyHmLimit(){{
  var pl2 = document.getElementById('pl2');
  if(!pl2) return;
  var open = pl2.classList.contains('showall');
  Array.prototype.forEach.call(pl2.querySelectorAll('.row'), function(r, i){{
    r.classList.toggle('hm-extra', !open && i >= 5);
  }});
}}
applyHmLimit();
var hmt = document.getElementById('hm-toggle');
if(hmt){{
  hmt.addEventListener('click', function(){{
    var pl2 = document.getElementById('pl2');
    var open = pl2.classList.toggle('showall');
    applyHmLimit();
    hmt.textContent = open ? 'Show fewer \u2191'
      : 'Show all ' + hmt.getAttribute('data-n') + ' \u2193';
  }});
}}
// Expandable pick rows: tap a row to reveal its blurb (delegated, so it
// survives re-sorting). Only rows with a story get the affordance.
document.querySelectorAll('.picklist').forEach(function(pl){{
  pl.addEventListener('click', function(e){{
    var row = e.target.closest ? e.target.closest('.row.has-blurb') : null;
    if(!row || !pl.contains(row)) return;
    row.classList.toggle('open');
  }});
}});
</script>
</body></html>
"""
    with open(path, "w") as f:
        f.write(html_doc)
    log(f"Chart written: {path}")


# ---------------- News layer: dynamic risk discovery ----------------

def fetch_rss_headlines(query, max_items=30, max_age_days=7):
    """Google News RSS titles for a query, dropping items older than max_age_days.

    Stale items (e.g. a 2025 headline in a 2026 bundle) are worse than useless
    for the world layer — they read as current context."""
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        # requests with a timeout first: feedparser.parse(url) hangs forever
        # on a stalled connection (a hang is not an exception), which can
        # burn the whole cron budget on one query.
        resp = requests.get(url, timeout=15,
                            headers={"User-Agent": "Mozilla/5.0"})
        feed = feedparser.parse(resp.content)
    except Exception:
        return []
    now = datetime.now(timezone.utc)
    out = []
    for e in feed.entries[:max_items]:
        if not hasattr(e, "title"):
            continue
        try:
            pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            if (now - pub).days > max_age_days:
                continue
        except Exception:
            pass  # keep items with unparseable dates
        out.append(e.title)
    return out


def fetch_market_headlines():
    """Recent market headlines from free RSS (shared by rules + LLM layers).

    Generic queries catch whatever is loudest; topical queries guarantee
    coverage of geopolitics, energy, and rates — the things that move markets
    even when they aren't the top story (a 7-month war once surfaced as 4
    vague mentions out of 80 and was missed entirely)."""
    queries = ["stock market", "global economy markets", "wall street",
               "middle east war oil", "oil prices opec",
               "federal reserve interest rates", "inflation cpi report",
               "treasury yields bonds"]
    headlines = []
    for q in queries:
        headlines += fetch_rss_headlines(q, 20)
        time.sleep(0.5)
    # dedupe, keep order
    seen, out = set(), []
    for h in headlines:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out[:120]


def fetch_market_internals(cache=None):
    """Fear/rate gauges for the world layer: VIX and 10Y yield, latest values.

    Gives the researcher a numeric read on market fear and borrowing costs
    every run, independent of what the headlines happen to emphasize.
    Date-partitioned cache: same-day reruns reuse, a new day refetches."""
    if cache is not None:
        hit = cache.get("market_internals")
        if hit:
            return hit
    out = {}
    try:
        for sym, key in [("^VIX", "vix"), ("^TNX", "tnx_10y")]:
            hist = yf.Ticker(sym).history(period="5d")
            if len(hist):
                out[key] = round(float(hist["Close"].iloc[-1]), 2)
        if out:
            log(f"market internals: {out}")
    except Exception as e:
        log(f"market internals unavailable ({e})")
    if out and cache is not None:
        cache.put("market_internals", out)
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


def positive_news_score(headlines):
    """0..1 score: higher = more fundamental good-news in today's headlines.

    Mirrors news_risk_penalty but for upgrades: only counts POSITIVE_WORDS
    (earnings beats, raised guidance, contract wins, approvals) — not
    analyst chatter. Used to invalidate a stale carried assessment when
    real good news breaks, so the researcher re-evaluates promptly.
    """
    if not headlines:
        return 0.0
    pos_hits = sum(1 for h in headlines
                   if any(w in h.lower() for w in POSITIVE_WORDS))
    pos_ratio = pos_hits / max(len(headlines), 1)
    return float(min(pos_ratio * 2, 1.0))


def fetch_headlines_threaded(tickers_names, cache=None, workers=6,
                             label="headlines"):
    """Fetch headlines for (ticker, name) pairs concurrently.

    Cache hits skip the network entirely. 6 workers keeps us friendly to
    Yahoo/RSS (each RSS call has its own 15s timeout; a hung feed can't
    stall the pool). Cache puts are atomic per-key, so concurrent writes
    are safe. Returns dict ticker -> headlines list.
    """
    from concurrent.futures import ThreadPoolExecutor
    out, todo = {}, []
    for t, name in tickers_names:
        h = cache.get(f"news_{t}") if cache is not None else None
        if h is not None:
            out[t] = h
        else:
            todo.append((t, name))
    if not todo:
        return out
    log(f"Fetching {label} for {len(todo)} tickers "
        f"({workers} threads, {len(tickers_names) - len(todo)} cached)...")

    def _fetch(tn):
        t, name = tn
        try:
            h = fetch_stock_headlines(t, name)
        except Exception as e:
            log(f"headlines: {t} fetch failed ({e})")
            h = []
        if cache is not None:
            try:
                cache.put(f"news_{t}", h)
            except Exception as e:
                log(f"headlines: cache put {t} failed ({e})")
        return t, h

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for t, h in ex.map(_fetch, todo):
            done += 1
            if done % 25 == 0:
                log(f"  {label} {done}/{len(todo)}...")
            out[t] = h
    return out


def apply_news_adjustment(df, risk_themes, top_k=50, cache=None, w_outlier=1.0):
    log(f"Fetching stock news for top {min(top_k, len(df))} scorers...")
    candidates = df.head(top_k)
    _hl = fetch_headlines_threaded(
        [(r["ticker"], r["name"]) for _, r in candidates.iterrows()],
        cache=cache, label="news")
    penalties, news_counts, samples = [], [], []
    for _, r in candidates.iterrows():
        headlines = _hl.get(r["ticker"], [])
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


def repair_etf_overlap(selected, pool, max_overlap=0.30, cap=2,
                       min_score=0.0, veto_dep=None):
    """Enforce the holdings-overlap cap on the SELECTED ETF portfolio.

    While any two picks overlap above max_overlap (top-10 holdings), evict
    the lower-EV one and refill the slot from eligible candidates that
    neither breach the category cap nor overlap a survivor. A pre-selection
    filter can't do this correctly: it drops funds for overlapping a
    'winner' that may never make the chart (e.g. SOXQ/TTEQ were excluded
    for overlapping SMH, which then lost the Technology cap).
    """
    global _LAST_OVERLAP_DROPS
    _LAST_OVERLAP_DROPS = {}
    sel = selected.copy().reset_index(drop=True)
    if len(sel) < 2:
        return sel

    def _ev(r):
        try:
            return float(r["est_next_1y"])
        except Exception:
            return float("-inf")

    def _dep_ok(v):
        if veto_dep is None:
            return True
        try:
            return not (float(v) > veto_dep)
        except Exception:
            return True

    evicted = set()
    while len(sel) >= 2:
        tickers = list(sel["ticker"])
        holds = {t: etf_top_holdings(t) for t in tickers}
        ev = {t: _ev(sel.loc[sel["ticker"] == t].iloc[0]) for t in tickers}
        pairs = []
        for i in range(len(tickers)):
            for j in range(i + 1, len(tickers)):
                a, b = tickers[i], tickers[j]
                if not holds[a] or not holds[b]:
                    continue
                ov = holdings_overlap(holds[a], holds[b])
                if ov > max_overlap:
                    pairs.append((ov, a, b))
        if not pairs:
            break
        pairs.sort(reverse=True)
        ov, a, b = pairs[0]
        loser = a if ev[a] < ev[b] else b
        winner = b if loser == a else a
        lrow = sel.loc[sel["ticker"] == loser].iloc[0]
        _LAST_OVERLAP_DROPS[loser] = (winner, ov)
        log(f"etf overlap repair: {loser} overlaps selected {winner} {ov:.0%} "
            f"(top-10 holdings) > {max_overlap:.0%} — evicting lower EV "
            f"({ev[loser]:+.1%} vs {ev[winner]:+.1%})")
        # idempotent: the two-pass Phase B reruns this repair, so skip
        # losers already recorded today
        if loser not in _ledger_seen_today(REJECTED_LEDGER, "rejected"):
            ledger_append(REJECTED_LEDGER, {
                "event": "rejected", "ticker": loser, "kind": "etf",
                "name": str(lrow.get("name", "")),
                "price": None,
                "reason": (f"etf holdings overlap {ov:.0%} with selected {winner} "
                           f"> {max_overlap:.0%} (lower EV)"),
                "est_next_1y": round(ev[loser], 4),
                "dep": _safe(lrow.get("llm_event_dependence")),
                "cont": _safe(lrow.get("llm_continuation")),
                "conf": _safe(lrow.get("llm_confidence")),
            })
        evicted.add(loser)
        sel = sel[sel["ticker"] != loser].reset_index(drop=True)
        # refill the freed slot: same hard gates as selection, plus the
        # category cap against survivors and no overlap with survivors
        counts = Counter(sel["sector"]) if len(sel) else Counter()
        cands = pool[~pool["ticker"].isin(set(sel["ticker"]) | evicted)].copy()
        cands = cands[cands["final_score"] >= min_score]
        if "llm_event_dependence" in cands.columns:
            cands = cands[cands["llm_event_dependence"].map(_dep_ok)]
        cands = cands[cands["sector"].map(lambda s: counts.get(s, 0) < cap)]
        surv_holds = {t: etf_top_holdings(t) for t in sel["ticker"]}

        def _clear(r):
            hh = etf_top_holdings(str(r["ticker"]))
            if not hh:
                return True
            return all(holdings_overlap(hh, surv_holds[t]) <= max_overlap
                       for t in surv_holds if surv_holds[t])

        if not cands.empty:
            cands = cands[cands.apply(_clear, axis=1)]
        if cands.empty:
            log("etf overlap repair: no eligible refill — slot left empty")
            continue
        cands = cands.copy()
        cands["_newcat"] = (~cands["sector"].isin(set(counts))).astype(int)
        cands = cands.sort_values(["_newcat", "est_next_1y"],
                                  ascending=[False, False])
        add = cands.iloc[0:1].copy()
        add["pick_pass"] = 3
        log(f"etf overlap repair: refill +{add['ticker'].tolist()} "
            f"(ev={_ev(add.iloc[0]):+.1%})")
        sel = pd.concat([sel, add.drop(columns=["_newcat"], errors="ignore")],
                        ignore_index=True)
    return sel


def fetch_pick_summaries(tickers, path="business_summaries.json", workers=6):
    """One-line business descriptions for picks (market-chat grounding).

    Fetches longBusinessSummary via yfinance (threaded); the researcher
    condenses each to one plain line. Failures leave that ticker blank
    (never fatal). Already-cached tickers are skipped, so reruns are free."""
    import json as _json
    from concurrent.futures import ThreadPoolExecutor
    out = {}
    try:
        out = _json.load(open(path))
    except Exception:
        out = {}
    missing = [t for t in tickers if not out.get(t, {}).get("summary")]
    if not missing:
        return

    def fetch(t):
        try:
            info = yf.Ticker(str(t)).info or {}
            s = info.get("longBusinessSummary") or ""
            return t, {"name": info.get("longName") or info.get("shortName") or t,
                       "summary": s[:700]}
        except Exception as e:
            log(f"pick summaries: {t} unavailable ({e})")
            return t, {"name": t, "summary": ""}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for t, entry in ex.map(fetch, missing):
            out[t] = entry
    # Locked read-merge-write with atomic replace: the daily and watchlist
    # runs can overlap (14:16 vs 14:45 crons) and both update this file —
    # without a lock the last writer silently drops the other's additions;
    # without atomic replace a crash mid-write corrupts it for everyone.
    try:
        import fcntl
        lock_path = path + ".lock"
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                with open(path) as f:
                    current = _json.load(f)
            except Exception:
                current = {}
            current.update(out)
            tmp = path + f".tmp.{os.getpid()}"
            with open(tmp, "w") as f:
                _json.dump(current, f, indent=1)
            os.replace(tmp, path)
        log(f"pick summaries: {path} ({len(current)} tickers)")
    except Exception as e:
        log(f"pick summaries: could not write ({e})")


def honorable_mentions(ranked, final, args, top_k=30):
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
            reason = "lower expected value than picks"
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
    check("nonempty_final", len(final) > 0, f"{len(final)} picks")
    if len(final):
        check("no_dupes", not final["ticker"].duplicated().any(),
              f"{final['ticker'].duplicated().sum()} duplicate tickers")
        # research coverage: every pick must have an actual assessment —
        # a pick on neutral defaults (no rationale) is a research miss
        if "llm_rationale" in final.columns:
            n_cov = int(final["llm_rationale"].astype(str).str.strip().ne("").sum())
            check("research_coverage", n_cov == len(final),
                  f"{n_cov}/{len(final)} picks with assessments")
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
    etf_universe = cache.get("etf_universe_v5")
    if etf_universe is None:
        # benchmark-relative universe: everything the bench-5 filter below
        # would consider; 2000 is only a work bound, not a cutoff
        etf_universe = get_etf_universe(target=2000, min_price=args.min_price,
                                        min_52w=bench_ret * 100 - 5)
        if etf_universe:
            cache.put("etf_universe_v5", etf_universe)
        # Never cache an empty universe (outage would poison same-day retries).
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
        srank = srank.copy()
        srank["_ckey"] = srank["name"].str.lower().str.replace(
            r"\b(class [a-c]|inc\.?|corp\.?|corporation|company|co\.?|ltd\.?|plc|holdings?|group)\b",
            "", regex=True).str.replace(r"[^a-z0-9]", "", regex=True)
        srank = srank.sort_values("final_score", ascending=False)
        _dup_mask = srank.duplicated("_ckey", keep="first")
        _dropped_names = list(srank.loc[_dup_mask, "ticker"]) if _dup_mask.any() else []
        srank = srank[~_dup_mask].drop(columns=["_ckey"])
        if _dropped_names:
            log(f"share-class dedupe: {len(_dropped_names)} duplicate listings "
                f"removed: {_dropped_names}")

    # ETF portfolio dedupe: never hold two wrappers of the same portfolio
    # (ETF analogue of the GOOG/GOOGL rule, e.g. QQQ vs QQQM). Keyed on a
    # small alias map for known identical-portfolio pairs surfaced by
    # research, falling back to normalized fund name.
    if not erank.empty and "name" in erank.columns:
        erank = erank.copy()
        _noname = (erank["name"].str.lower()
                   .str.replace(r"\b(etf|trust|fund|index|shares?)\b", "", regex=True)
                   .str.replace(r"[^a-z0-9]", "", regex=True))
        erank["_pkey"] = [_ETF_PORTFOLIO_ALIASES.get(str(t).lower(), n)
                          for t, n in zip(erank["ticker"], _noname)]
        erank = erank.sort_values("final_score", ascending=False)
        _dup_mask = erank.duplicated("_pkey", keep="first")
        _dropped_names = list(erank.loc[_dup_mask, "ticker"]) if _dup_mask.any() else []
        erank = erank[~_dup_mask].drop(columns=["_pkey"])
        if _dropped_names:
            log(f"ETF portfolio dedupe: {len(_dropped_names)} duplicate "
                f"wrappers removed: {_dropped_names}")

    def pick_group(ranked, n, cap, label):
        # expected-value floor first: never pick a negative-EV name
        cut = ranked[ranked["est_next_1y"] < args.min_est]
        if len(cut):
            log(f"est floor ({args.min_est:+.1%}): {len(cut)} {label} excluded: "
                f"{cut['ticker'].tolist()}")
        est_ok = ranked[ranked["est_next_1y"] >= args.min_est]
        # EV shortlist: final_score's job is the quality floor (min_score);
        # among floor-passers, expected value ranks and selects. The number
        # the chart shows is the number that awards places.
        est_ok = est_ok.sort_values("est_next_1y", ascending=False)
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
                rest = rest.sort_values(["_newcat", "est_next_1y"],
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
        if label == "ETFs" and len(first):
            # redundancy is not diversification: enforce the overlap cap on
            # the selected portfolio (post-selection repair, not pre-filter)
            first = repair_etf_overlap(first, est_ok,
                                       max_overlap=args.max_etf_overlap,
                                       cap=cap, min_score=args.min_score,
                                       veto_dep=veto)
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
    cache = StepCache(args.cache_dir, enabled=not args.no_cache, log=log,
                      date_str=pipeline_today())
    tickers = [t.strip().upper() for t in tickers if t.strip()]
    log(f"Watchlist Phase A: {tickers}")
    print(f"Watchlist: {', '.join(tickers)}")

    closes = cache.get("watchlist_prices_" + StepCache.tickers_key(tickers))
    if closes is None:
        closes = download_prices(tickers, period="1y")
        cache.put("watchlist_prices_" + StepCache.tickers_key(tickers), closes)
    else:
        log(f"watchlist: {len(closes)} tickers' 1y prices from cache")
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
                           {t: sinfos[t] for t in kt if t in sinfos},
                           min_rows=60)
        _dropped = [t for t in kt if t not in set(sdf["ticker"])] if not sdf.empty else list(kt)
        for t in _dropped:
            log(f"watchlist: {t} dropped by scorer (<60 trading days of history)")
            print(f"  {t}: insufficient history for scoring (<60 trading days), skipped")
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
    _hl = fetch_headlines_threaded(
        [(r["ticker"], r["name"]) for _, r in df.iterrows()],
        cache=cache, label="watchlist news")
    for _, r in df.iterrows():
        t = r["ticker"]
        headlines = _hl.get(t, [])
        cands.append(_watchlist_cand(r, headlines))

    bundle = build_research_bundle(
        cands, market_headlines,
        {"mode": "watchlist", "tickers": tickers,
         "note": "user-supplied tickers; no benchmark comparison. "
                 "LLM decides event_dependence/continuation/confidence; "
                 "output sorted by est_next_1y, no floors."},
        fetch_market_internals(cache))
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


def load_json_arg(path, what):
    """Load a worker-written JSON file with a clean error, not a traceback.

    The research/market-chat files are hand-written by an agent; a malformed
    file should explain itself (which file, what's wrong, how to recover)
    instead of dumping a json.decoder traceback. Exits nonzero so crons
    report the failure instead of publishing a half-built page.
    """
    import json as _json
    try:
        with open(path) as f:
            return _json.load(f)
    except FileNotFoundError:
        log(f"ERROR: {what} file not found: {path}")
    except _json.JSONDecodeError as e:
        log(f"ERROR: {what} file is not valid JSON: {path}: {e}. "
            f"Fix the JSON (common cause: trailing comma, unclosed brace) "
            f"and re-run.")
    except OSError as e:
        log(f"ERROR: cannot read {what} file {path}: {e}")
    raise SystemExit(f"Phase B aborted: bad {what} file ({path})")


def validate_outputs(outputs, what="research outputs"):
    """Light schema check on the research outputs before merging.

    apply_llm_outputs() is defensive (clamps ranges, fills neutral defaults),
    so this only verifies the top-level shape and warns on suspicious
    entries — it never crashes a run that could proceed.
    """
    if not isinstance(outputs, dict):
        log(f"WARNING: {what} is not a JSON object — research merge skipped")
        return {}
    stocks = outputs.get("stocks")
    if not isinstance(stocks, dict):
        log(f"WARNING: {what} has no 'stocks' object — research merge skipped")
        return {}
    n_bad = 0
    for t, s in stocks.items():
        if not isinstance(s, dict):
            log(f"WARNING: {what}['stocks']['{t}'] is not an object — skipped")
            n_bad += 1
            continue
        for f in ("event_dependence", "continuation", "confidence"):
            try:
                v = float(s.get(f, 0.5))
                if not (0.0 <= v <= 1.0):
                    log(f"WARNING: {t}.{f}={s.get(f)} out of [0,1] — clamped")
            except (TypeError, ValueError):
                log(f"WARNING: {t}.{f}={s.get(f)!r} not numeric — default used")
                n_bad += 1
        if not s.get("assessed_date"):
            log(f"WARNING: {t} has no assessed_date — provenance unknown")
    if n_bad == 0:
        log(f"{what}: {len(stocks)} assessments, schema OK")
    return outputs


def run_watchlist_apply(args):
    """Phase B for --watchlist-apply: apply research, sort by est, chart.

    No floors, no caps, no picking — every researched ticker is shown,
    sorted by confidence-weighted est_next_1y, highest first.
    """
    import json as _json
    from llm_research import apply_llm_outputs
    setup_logging()
    log("Watchlist Phase B: applying research")
    bundle = load_json_arg(args.watchlist_apply[0], "watchlist bundle")
    outputs = validate_outputs(
        load_json_arg(args.watchlist_apply[1], "watchlist research outputs"),
        "watchlist research outputs")
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

    # Buy/Hold/Sell: what Mint would do, from pipeline outputs (auditable)
    final = add_ratings(final)
    out = f"watchlist_results_{datetime.now().strftime('%Y%m%d')}.csv"
    final.to_csv(out, index=False)
    print(f"Saved: {out}")
    # business summaries for blurb grounding + researcher-written blurbs
    try:
        _picks = list(final["ticker"])
        if _picks:
            fetch_pick_summaries(_picks)
    except Exception as e:
        log(f"pick summaries: skipped ({e})")
    wl_chat = None
    try:
        import json as _json2
        with open("watchlist_chat.json") as _f:
            wl_chat = _json2.load(_f)
        # valid JSON but wrong type (e.g. a list) would crash .get() later
        if not isinstance(wl_chat, dict):
            log(f"WARNING: watchlist_chat.json is {type(wl_chat).__name__}, "
                f"not an object — blurbs skipped")
            wl_chat = None
    except Exception:
        wl_chat = None
    if wl_chat and wl_chat.get("tickers"):
        _rmap = {str(r["ticker"]): (r["rating"], r["rating_reason"])
                 for _, r in final.iterrows()}
        for _t in wl_chat["tickers"]:
            _rt, _rr = _rmap.get(str(_t.get("ticker")), ("HOLD", ""))
            _t["rating"] = _t.get("rating") or _rt
            _t["rating_reason"] = _t.get("rating_reason") or _rr
    if wl_chat is not None:
        # overall quality verdict — computed from the ratings, stays honest
        wl_chat["summary"] = watchlist_quality_summary(
            list(final["rating"]), list(final["est_next_1y"]),
            list(final["ticker"]))
        log(f"  watchlist summary: {wl_chat['summary']}")
    sfinal = final[final["kind"] == "stock"]
    efinal = final[final["kind"] == "etf"]
    chart_path = f"watchlist_chart_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    make_chart_html(sfinal, efinal, chart_path,
                    {"benchmark": "", "mode": "watchlist",
                     "heading": "Watchlist — by est. next 1y",
                     "asof": datetime.now().strftime("%Y-%m-%d")},
                    titles=("Stocks — by est. next 1y", "ETFs — by est. next 1y"),
                    watchlist_chat=wl_chat)
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

_RUN_DATE = None


def run_date():
    """Process-wide run date, pinned on first call. A run crossing midnight
    must not split its ledger events across two dates (idempotency keys and
    the audit both assume one date per run)."""
    global _RUN_DATE
    if _RUN_DATE is None:
        _RUN_DATE = datetime.now().strftime("%Y-%m-%d")
    return _RUN_DATE


def ledger_append(path, event):
    import json as _json
    event = dict(event)
    event.setdefault("date", run_date())
    event.setdefault("ts", datetime.now().isoformat())
    with open(path, "a") as f:
        f.write(_json.dumps(event, default=str) + "\n")


def _ledger_seen_today(path, event="picked"):
    """Tickers already recorded with `event` today — keeps Phase B idempotent."""
    import json as _json
    seen = set()
    try:
        today = run_date()
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


def fetch_pick_prices(tickers, cache=None):
    """Robust pick-price fetch: batch attempts, then per-ticker fallback.

    Returns (prices, completeness dict). A run with missing prices is
    INCOMPLETE_DATA — the structural self-checks can pass while price data
    is absent, so completeness gets its own explicit status, recorded in the
    log and the run summary.

    Date-partitioned cache (keyed by ticker set): same-day reruns — including
    the two-pass Phase B pattern — reuse prices instead of re-downloading.
    """
    from cache import StepCache
    tickers = list(dict.fromkeys(tickers))
    lkey = "ledger_prices_" + StepCache.tickers_key(tickers)
    if cache is not None:
        hit = cache.get(lkey)
        if hit and isinstance(hit, dict) and "prices" in hit:
            log(f"ledger: pick prices from cache "
                f"({hit['completeness'].get('got')}/{len(tickers)})")
            return hit["prices"], hit["completeness"]
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
            if not prices:
                # Attempt 1 returned nothing at all — attempt 2 is the same
                # call and won't help; skip straight to the per-ticker
                # fallback instead of burning another 10s + full batch.
                log("ledger: attempt 1 got zero prices — skipping attempt 2")
                break
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
    completeness = {"status": status, "got": len(prices),
                    "total": len(tickers), "missing": missing}
    if cache is not None and status == "COMPLETE":
        # Only complete fetches are cached: an INCOMPLETE result must not
        # poison later reruns — they should retry Yahoo fresh.
        cache.put(lkey, {"prices": prices, "completeness": completeness})
    return prices, completeness


def backfill_ledger_prices(cache=None):
    """Fill pick_price=None on today's picked events (completeness recovery).

    The cron's thesis step can call this when a run was marked INCOMPLETE_DATA.
    Returns the number of entries fixed.
    """
    import json as _json
    today = run_date()
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
    prices, _ = fetch_pick_prices(need, cache)
    fixed, out = 0, []
    for line in lines:
        try:
            d = _json.loads(line)
        except Exception:
            # torn line (crash mid-append) — keep it verbatim, don't crash
            out.append(line.rstrip("\n"))
            continue
        if (d.get("date") == today and d.get("event") == "picked"
                and d.get("pick_price") is None and d.get("ticker") in prices):
            d["pick_price"] = prices[d["ticker"]]
            fixed += 1
        out.append(_json.dumps(d, default=str))
    # atomic rewrite: a crash mid-write must never truncate the ledger
    tmp = THESIS_LEDGER + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, THESIS_LEDGER)
    log(f"ledger: backfilled {fixed} pick prices")
    return fixed


def record_picks_ledger(final, ranked, args, cache=None):
    """Append pick events for today's finals + rejected events for the
    audit-interesting near-misses (vetoes, exclusions, EV-floor fails)."""
    import json as _json  # noqa: F401 (kept local like the rest of this file)
    global _LAST_DATA_COMPLETENESS
    today = run_date()
    tickers = list(final["ticker"])
    prices, _LAST_DATA_COMPLETENESS = fetch_pick_prices(tickers, cache)
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


def thesis_check(track_days=THESIS_TRACK_DAYS, cache=None):
    """Price-based thesis check for picks made within the last `track_days`.

    Appends 'check' events (intact/watch/broken) to the ledger and returns
    the status list. The daily agent does the news-based re-verification on
    top and amends reasons; this function only measures price truth.

    The 3mo price fetch is date-partitioned cached (keyed by ticker set), so
    repeated same-day checks don't re-download.
    """
    import json as _json
    from cache import StepCache
    if cache is None:
        # standalone invocation (e.g. python3 -c): use the default cache dir
        cache = StepCache("cache", log=log, date_str=pipeline_today())
    picks = {}
    try:
        with open(THESIS_LEDGER) as f:
            for line in f:
                try:
                    e = _json.loads(line)
                except Exception:
                    continue
                if e.get("event") == "picked":
                    # Key by (ticker, date): a re-picked ticker's earlier
                    # prediction must not be shadowed — the audit measures
                    # every prediction, not just the latest.
                    picks[(e["ticker"], e.get("date"))] = e
    except FileNotFoundError:
        log("thesis_check: no ledger yet — 0 tracked")
        return []
    cutoff = (datetime.now() - timedelta(days=track_days)).strftime("%Y-%m-%d")
    tracked = {k: e for k, e in picks.items()
               if e.get("date", "") >= cutoff and e.get("pick_price")}
    if not tracked:
        log("thesis_check: 0 tracked (no picks with prices in window) — "
            "not a fetch failure")
        return []
    out = []
    try:
        _track_tickers = sorted({t for t, _d in tracked})
        tkey = "thesis_prices_" + StepCache.tickers_key(_track_tickers)
        px = cache.get(tkey)
        if px is None:
            px = download_prices(_track_tickers, period="3mo")
            cache.put(tkey, px)
        else:
            log(f"thesis_check: {len(px)} tickers' 3mo prices from cache")
    except Exception as e:
        log(f"thesis_check: price fetch failed ({e})")
        return []
    today = run_date()
    # Today's existing check events, so reruns don't duplicate identical
    # results (a status CHANGE still records — that's the timeline).
    seen_checks = set()
    try:
        with open(THESIS_LEDGER) as f:
            for line in f:
                try:
                    e = _json.loads(line)
                except Exception:
                    continue
                if (e.get("event") == "check" and e.get("date") == today):
                    seen_checks.add((e.get("ticker"), e.get("status"),
                                     e.get("reason")))
    except FileNotFoundError:
        pass
    for (t, _pick_date), e in tracked.items():
        if t not in px:
            continue
        s = px[t].dropna()
        # Anchor to the last available bar on or before the pick date, so
        # weekend picks (dated Sat/Sun with no price bars) measure from
        # Friday's close instead of tracking nothing.
        _anchor = s.index[s.index <= pd.Timestamp(e["date"])]
        if len(_anchor) == 0:
            continue
        s = s.loc[_anchor[-1]:]
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
        if (t, status, reason) in seen_checks:
            continue  # identical check already recorded today
        rec = {"event": "check", "ticker": t, "date": today, "status": status,
               "days_held": days, "ret_since_pick": round(ret, 4),
               "dd_since_pick": round(dd, 4), "pick_price": pp, "reason": reason,
               "pick_date": e["date"]}
        ledger_append(THESIS_LEDGER, rec)
        out.append(rec)
    nb = sum(1 for r in out if r["status"] == "broken")
    nw = sum(1 for r in out if r["status"] == "watch")
    log(f"thesis_check: {len(out)} tracked, {nb} broken, {nw} watch")
    return out


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
    ap.add_argument("--n-etf-research", type=int, default=100,
                    help="top ETF quant candidates entering research (default 100)")
    ap.add_argument("--n-stock-research", type=int, default=100,
                    help="top stock quant candidates entering research (default 100)")
    ap.add_argument("--news-invalidate-threshold", type=float, default=0.65,
                    help="news score (bad or good) at/above which a carried assessment "
                         "is invalidated, forcing fresh research (default 0.65)")
    ap.add_argument("--n-surge", type=int, default=10,
                    help="max fast-movers (10d return >= 10%%) outside the research pool "
                         "pulled in for fresh assessment (default 10)")
    ap.add_argument("--surge-min-10d", type=float, default=0.10,
                    help="min 10-day return for a surge candidate (default 0.10)")
    ap.add_argument("--max-etf-overlap", type=float, default=0.30,
                    help="max pairwise top-10 holdings overlap between picked ETFs (default 0.30; lower-EV member of over-threshold pairs is excluded)")
    ap.add_argument("--max-per-etf-category", type=int, default=2,
                    help="max final ETFs per fund category (default 4)")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore the on-disk cache; make all calls fresh")
    ap.add_argument("--cache-dir", default="cache",
                    help="cache directory (default: cache/)")
    args = ap.parse_args()

    # Shared cache, constructed once: every branch (Phase A, Phase B,
    # watchlist) uses this. Previously Phase B referenced `cache` before it
    # was bound, raising UnboundLocalError (swallowed) and silently skipping
    # ledger recording.
    from cache import StepCache
    cache = StepCache(args.cache_dir, enabled=not args.no_cache, log=log,
                      date_str=pipeline_today())

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
        bundle = load_json_arg(args.llm_apply[0], "research bundle")
        phase_a_log = (bundle.get("meta") or {}).get("phase_a_log")
        if phase_a_log and os.path.exists(phase_a_log):
            continue_logging(phase_a_log)
            log("=== PHASE B: applying LLM research (same log as Phase A) ===")
        else:
            setup_logging()
            log("Phase B: applying LLM research (no Phase A log found; new file)")
        outputs = validate_outputs(
            load_json_arg(args.llm_apply[1], "research outputs"))
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
        out = (f"screener_results_{datetime.now().strftime('%Y%m%d')}_"
               f"{args.benchmark}_llm.csv")
        final.to_csv(out, index=False)
        print(f"Saved: {out}")
        # Self-check gates the run: a failure aborts BEFORE ledger appends
        # and chart write, so a broken run never records picks or produces
        # a publishable page.
        checks, fails = run_self_check(final, args, out)
        if fails:
            print("\n!!! SELF-CHECK FAILURES:")
            for c in fails:
                print(f"    FAIL: {c['name']} {c['detail']}")
            log_error(f"SELF-CHECK failed: {[c['name'] for c in fails]} — "
                      f"aborting before ledger/chart")
            sys.exit(1)
        # thesis ledger: record picks + rejected control group (audit trail)
        try:
            record_picks_ledger(final, ranked, args, cache)
        except Exception as e:
            log(f"ledger: record_picks_ledger failed ({e})")
        chart_path = (f"chart_{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
                      f"{args.benchmark}_llm.html")
        hm = honorable_mentions(ranked, final, args)
        # market chat (researcher-written, plain language) + business
        # summaries for tomorrow's market chat — both optional files
        mc = None
        try:
            import json as _json
            with open("market_chat.json") as _f:
                mc = _json.load(_f)
            if not isinstance(mc, dict):
                log(f"WARNING: market_chat.json is {type(mc).__name__}, "
                    f"not an object — market chat skipped")
                mc = None
        except Exception:
            mc = None
        try:
            _picks = list(final["ticker"]) if len(final) else []
            _hm_t = [h.get("ticker") for h in (hm or []) if h.get("ticker")]
            _all = list(dict.fromkeys(list(_picks) + _hm_t))
            if _all:
                fetch_pick_summaries(_all)
        except Exception as e:
            log(f"pick summaries: skipped ({e})")
        make_chart_html(final_stocks, final_etfs, chart_path,
                        {"benchmark": args.benchmark,
                         "etf_benchmark": etf_benchmark(args),
                         "asof": datetime.now().strftime("%Y-%m-%d")},
                        honorable=hm, market_chat=mc)
        print(f"Chart: {chart_path}")
        # (self-check already gated above, before ledger/chart)
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

    # cache was constructed once at the top of main() so Phase B can use it;
    # just report it here.
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
    universe = cache.get("universe_v3")
    if universe is None:
        universe = get_all_screener_stocks(min_mcap=args.min_mcap, min_price=args.min_price)
        if universe:
            cache.put("universe_v3", universe)
        # Never cache an empty universe: a 429/outage would otherwise poison
        # every same-day retry (COMPLETE-only caching, same as ledger prices).
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
    n_priced = 0
    for c in cands:
        t = c["ticker"]
        s = closes.get(t)
        r = calc_return(s) if s is not None else np.nan
        if not pd.isna(r):
            n_priced += 1
        if not pd.isna(r) and r > bench_ret:
            c["ret_1y"] = r
            outperformers.append(c)
    # Outage gate: a 429 storm that yields few closes must not degrade into a
    # false methodological conclusion ("just buy the benchmark"). If we
    # couldn't price most candidates, the data is broken, not the market.
    if cands:
        price_cov = n_priced / len(cands)
        log(f"price coverage: {price_cov:.0%} ({n_priced}/{len(cands)} candidates priced)")
        if price_cov < 0.5:
            print(f"ABORT: only {price_cov:.0%} of candidates priced — likely "
                  f"a Yahoo outage. Not publishing a false 'buy the benchmark' run.")
            sys.exit(2)
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

    # Outage gate: if Yahoo's .info endpoint is down, every candidate looks
    # like it has $0 volume and gets trash-filtered as "illiquid" — a total
    # outage masquerading as a clean "no picks" day. Fail loud instead.
    if out_tickers:
        cov = len([t for t in out_tickers if infos.get(t)]) / len(out_tickers)
        log(f"fundamentals coverage: {cov:.0%} ({len([t for t in out_tickers if infos.get(t)])}/{len(out_tickers)})")
        if cov < 0.5:
            print(f"ABORT: fundamentals coverage only {cov:.0%} — likely a "
                  f"Yahoo outage, not an empty market. Not publishing a "
                  f"false 'no picks' run.")
            sys.exit(2)

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
    log_score_breakdown(df.head(args.n_stock_research), f"stocks (research pool: top {args.n_stock_research})",
                        _STOCK_SCORE_GROUPS)
    log_character_breakdown(df.head(args.n_stock_research), f"stocks (research pool: top {args.n_stock_research})",
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

    # research pool: top N stocks + top N ETFs
    _pool_stocks = df.head(args.n_stock_research).copy()
    _pool_etfs = edf.head(args.n_etf_research).copy() if not edf.empty else edf
    pool = pd.concat([_pool_stocks, _pool_etfs], ignore_index=True)
    log(f"research pool: {len(_pool_stocks)} stocks + {len(_pool_etfs)} ETFs")

    # Surge candidates: fast movers (10d return >= threshold) outside the
    # quant pool get pulled in for fresh assessment. This is the "good news,
    # buy quickly" path — the surge only buys an EVALUATION. Risk gates
    # already applied above; the dep>=0.7 event veto and EV floor still
    # decide. A meme spike gets researched and vetoed; a genuine repricing
    # gets a fair EV within 24h instead of waiting for its base_score rank
    # to climb.
    if args.n_surge > 0:
        _pool_tickers = set(pool["ticker"])
        _surge_frames = []
        for _sdf in (df, edf):
            if _sdf is not None and not _sdf.empty and "ret_2w" in _sdf.columns:
                _surge_frames.append(_sdf)
        if _surge_frames:
            _sall = pd.concat(_surge_frames, ignore_index=True)
            _surg = _sall[(_sall["ret_2w"].fillna(0) >= args.surge_min_10d) &
                          (~_sall["ticker"].isin(_pool_tickers))] \
                .sort_values("ret_2w", ascending=False).head(args.n_surge)
            if len(_surg):
                for _, _r in _surg.iterrows():
                    log(f"SURGE: {_r['ticker']} (+{_r['ret_2w']:.1%} 10d) — "
                        f"added to research pool for fresh assessment")
                _surg = _surg.copy()
                _surg["_surge"] = True
                pool = pd.concat([pool, _surg], ignore_index=True)
                log(f"research pool after surge: {len(pool)} "
                    f"({len(_surg)} surge added)")

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
        # risk themes needed for news-triggered invalidation scoring
        risk_themes = discover_risk_themes(cache)
        cands = []
        _hl = fetch_headlines_threaded(
            [(r["ticker"], r["name"]) for _, r in top.iterrows()],
            cache=cache, label="research headlines")
        for _, r in top.iterrows():
            t = r["ticker"]
            headlines = _hl.get(t, [])
            # News-triggered invalidation: if today's headlines carry
            # significant bad news OR significant fundamental good news,
            # any carried assessment is stale — flag it so the researcher
            # does fresh research instead of carrying forward. The 7-day
            # rule assumes "no significant news"; a score spike violates
            # that assumption in either direction.
            _bad = news_risk_penalty(headlines, risk_themes)
            _good = positive_news_score(headlines)
            _trig = None
            if _bad >= args.news_invalidate_threshold:
                _trig = f"bad:{_bad:.2f}"
            elif _good >= args.news_invalidate_threshold:
                _trig = f"good:{_good:.2f}"
            if _trig:
                log(f"NEWS INVALIDATION: {t} ({_trig}) — forcing fresh assessment")
            _sv = r.get("_surge")
            _is_surge = bool(_sv) and not (isinstance(_sv, float) and _sv != _sv)  # not NaN
            cand = {
                "ticker": r["ticker"], "name": r["name"], "sector": r["sector"],
                "kind": r.get("kind", "stock"),
                "ret_1y": float(r["ret_1y"]), "ret_6m": _safe(r.get("ret_6m")),
                "ret_3m": _safe(r.get("ret_3m")), "ret_2w": _safe(r.get("ret_2w")),
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
                "news_trigger": _trig,
                "surge": _is_surge,
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
                         "phase_a_log: Phase B must append to this file so one run = one log."},
                fetch_market_internals(cache))
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
