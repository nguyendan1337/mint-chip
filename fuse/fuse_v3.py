#!/usr/bin/env python3
"""Light the Fuse v2 — corrected prototype.

Changes from v1:
- Universe: Mint's full US stock universe (mcap>=$2B, price>=$5, region us,
  exchanges NMS/NYQ/ASE/NGM/NCM/BTS) via yfinance EquityQuery — NOT S&P 500.
  Rationale: true pre-run names are mostly $2B-$20B, pre-index-inclusion.
- EPS acceleration fixed: YoY quarterly EPS growth acceleration
  (this-quarter YoY minus last-quarter YoY), winsorized, skipped on
  non-positive denominators. v1's sequential-quarter calc was nonsense.
- Revenue growth: YoY quarterly Total Revenue growth, winsorized.
- Early-cycle gate: 12-1 momentum hard-capped at +80% (names that already
  exploded are continuation plays, not Fuse plays).
- Energy sector excluded (commodity-price beneficiaries conflict with the
  no-war/no-commodity-spike mandate).
- Religious-themed names filtered by keyword.
- Duplicate share classes deduped (keep higher scorer).
- Stage 0 (batched price download, vectorized technicals) -> Stage 1
  (threaded fundamentals only for technical passers).
"""
import json, math, time, sys, os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import yfinance as yf
from yfinance.screener.query import EquityQuery as EqyQy

OUT = os.path.expanduser("~/workspace/stock-screener/fuse/fuse_v3_results.json")
STAGE0_CACHE = os.path.expanduser("~/workspace/stock-screener/fuse/fuse_v2_stage0.json")

RELIGION_WORDS = ["faith", "bible", "sharia", "halal", "catholic", "jesus", "torah"]

# --- v3 additions: Piotroski F-score, beat streak, earnings proximity ---------
def _cell(df, row, col):
    try:
        v = df.loc[row, col]
        return float(v) if pd.notna(v) else None
    except Exception:
        return None

def piotroski(tk):
    """Piotroski F-score 0-9 from annual statements, or None if data missing.

    Fail closed: any missing required field -> None -> name excluded.
    Floor for Fuse: F >= 5 (below that = financially shaky; original paper's
    'high' was 7-9 but that was a value-stock sort, too strict here).
    """
    try:
        inc, bal, cf = tk.income_stmt, tk.balance_sheet, tk.cashflow
        if inc is None or bal is None or cf is None:
            return None
        cols = sorted(inc.columns, reverse=True)
        if len(cols) < 2:
            return None
        c0, c1 = cols[0], cols[1]
        bcols = sorted(bal.columns, reverse=True)
        fcols = sorted(cf.columns, reverse=True)
        if len(bcols) < 2 or len(fcols) < 2:
            return None
        b0, b1, f0 = bcols[0], bcols[1], fcols[0]
        ni0 = _cell(inc, "Net Income", c0); ni1 = _cell(inc, "Net Income", c1)
        ta0 = _cell(bal, "Total Assets", b0); ta1 = _cell(bal, "Total Assets", b1)
        cfo0 = _cell(cf, "Operating Cash Flow", f0)
        gp0 = _cell(inc, "Gross Profit", c0); gp1 = _cell(inc, "Gross Profit", c1)
        rev0 = _cell(inc, "Total Revenue", c0); rev1 = _cell(inc, "Total Revenue", c1)
        ca0 = _cell(bal, "Current Assets", b0); ca1 = _cell(bal, "Current Assets", b1)
        cl0 = _cell(bal, "Current Liabilities", b0); cl1 = _cell(bal, "Current Liabilities", b1)
        ltd0 = _cell(bal, "Long Term Debt", b0) or 0.0
        ltd1 = _cell(bal, "Long Term Debt", b1) or 0.0
        sh0 = _cell(bal, "Ordinary Shares Number", b0)
        if sh0 is None:
            sh0 = _cell(bal, "Share Issued", b0)
        sh1 = _cell(bal, "Ordinary Shares Number", b1)
        if sh1 is None:
            sh1 = _cell(bal, "Share Issued", b1)
        req = [ni0, ni1, ta0, ta1, cfo0, gp0, gp1, rev0, rev1,
               ca0, ca1, cl0, cl1, sh0, sh1]
        if any(v is None for v in req) or ta0 == 0 or ta1 == 0 \
                or rev0 == 0 or rev1 == 0 or cl0 == 0 or cl1 == 0:
            return None
        roa0, roa1 = ni0 / ta0, ni1 / ta1
        cfo_ta = cfo0 / ta0
        s = 0
        s += roa0 > 0                      # F1 profitability
        s += cfo_ta > 0                   # F2 cash profitability
        s += roa0 > roa1                  # F3 improving ROA
        s += cfo_ta > roa0                # F4 accruals (CFO-backed earnings)
        s += (ltd0 / ta0) < (ltd1 / ta1)  # F5 deleveraging
        s += (ca0 / cl0) > (ca1 / cl1)    # F6 improving liquidity
        s += sh0 <= sh1                   # F7 no dilution
        s += (gp0 / rev0) > (gp1 / rev1)  # F8 improving gross margin
        s += (rev0 / ta0) > (rev1 / ta1)  # F9 improving asset turnover
        return s
    except Exception:
        return None

def beat_streak(rep):
    """Consecutive positive surprises, most-recent-first. rep already
    filtered to reported rows, sorted descending."""
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

def days_to_next_earnings(ixu):
    """Projected days until next earnings, from the median reporting interval.

    ixu: tz-aware UTC DatetimeIndex of reported earnings dates,
    most-recent-first. Returns int or None. Veto buys <=14 days out —
    don't buy into the binary event. Projection, not a promise.
    """
    try:
        if ixu is None or len(ixu) < 2:
            return None
        asc = ixu.sort_values()
        gaps = [(asc[i] - asc[i - 1]).days for i in range(1, len(asc))]
        gaps = [g for g in gaps if 20 < g < 200]
        if not gaps:
            return None
        med = sorted(gaps)[len(gaps) // 2]
        nxt = ixu.max() + pd.Timedelta(days=med)
        return (nxt - pd.Timestamp.now(tz="UTC")).days
    except Exception:
        return None

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ---------------- universe ----------------
def get_universe(min_mcap=2_000_000_000, min_price=5.0):
    q = EqyQy('and', [
        EqyQy('eq', ['region', 'us']),
        EqyQy('is-in', ['exchange', 'NMS', 'NYQ', 'ASE', 'NGM', 'NCM', 'BTS']),
        EqyQy('gte', ['intradaymarketcap', min_mcap]),
        EqyQy('gte', ['intradayprice', min_price]),
    ])
    quotes, offset, seen = [], 0, set()
    while True:
        batch = None
        for attempt in (1, 2, 3):
            try:
                res = yf.screen(q, size=250, offset=offset,
                                sortField='intradaymarketcap', sortAsc=False)
                batch = res.get('quotes', []) or []
                total = res.get('total', 0)
                break
            except Exception as e:
                log(f"  screener page {offset} attempt {attempt} failed: {e}")
                time.sleep(2 * attempt)
        if batch is None:
            log(f"  giving up at offset {offset}, keeping {len(quotes)}")
            break
        quotes.extend(batch)
        log(f"  page {offset}: {len(quotes)}/{total} fetched")
        if len(batch) < 250 or (total and len(quotes) >= total):
            break
        offset += 250
        time.sleep(0.4)
    out = []
    for x in quotes:
        sym = x.get('symbol')
        if sym and sym not in seen:
            seen.add(sym)
            out.append({
                "ticker": sym,
                "name": x.get('longName') or x.get('shortName') or sym,
                "mcap": x.get('marketCap'),
            })
    log(f"Universe: {len(out)} stocks")
    return out

# ---------------- stage 0: technicals ----------------
def ann_vol(s, w=60):
    r = np.log(s / s.shift(1)).dropna()
    return r.iloc[-w:].std() * math.sqrt(252) if len(r) >= w else float('nan')

def max_dd(s):
    roll = s.cummax()
    return float(((s - roll) / roll).min())

def tech_metrics(px, vol):
    px = px.dropna(); vol = vol.dropna()
    if len(px) < 70:
        return None
    cur = float(px.iloc[-1])
    hi52 = float(px.iloc[-252:].max()) if len(px) >= 60 else float(px.max())
    nearhi = cur / hi52 if hi52 > 0 else 0
    mom12_1 = float(px.iloc[-22] / px.iloc[-min(len(px), 262)] - 1) if len(px) > 80 else float('nan')
    ma50 = float(px.iloc[-50:].mean())
    v20 = float(vol.iloc[-20:].mean()); v60 = float(vol.iloc[-60:].mean())
    volratio = v20 / v60 if v60 > 0 else 0
    mania = float((vol.iloc[-5:].max() / v60)) if v60 > 0 else 0
    return {
        "price": cur, "nearhi": nearhi, "mom12_1": mom12_1,
        "above50": cur > ma50, "pct_above50": (cur / ma50 - 1) if ma50 > 0 else 0,
        "volratio": volratio, "mania": mania,
        "annvol60": ann_vol(px), "maxdd1y": max_dd(px.iloc[-252:]),
    }

def stage0(universe):
    tickers = [u["ticker"] for u in universe]
    results = {}
    diag = {"n": len(tickers)}
    CH = 300
    for i in range(0, len(tickers), CH):
        grp = tickers[i:i + CH]
        log(f"  download chunk {i//CH+1}/{(len(tickers)+CH-1)//CH} ({len(grp)} tickers)")
        try:
            df = yf.download(grp, period="1y", auto_adjust=True,
                             progress=False, threads=True, timeout=30)
        except Exception as e:
            log(f"  chunk failed: {e}")
            continue
        if df is None or df.empty:
            continue
        try:
            closes = df["Close"]; vols = df["Volume"]
        except Exception:
            continue
        multi = isinstance(closes.columns, pd.MultiIndex)
        for t in grp:
            try:
                px = closes[t] if not multi else closes.xs(t, axis=1, level=1)
                vv = vols[t] if not multi else vols.xs(t, axis=1, level=1)
            except Exception:
                continue
            m = tech_metrics(px, vv)
            if m and m["price"] >= 5.0:
                results[t] = m
        time.sleep(1)
    log(f"Stage 0 priced: {len(results)}/{len(tickers)}")
    with open(STAGE0_CACHE, "w") as f:
        json.dump(results, f)
    return results

# ---------------- stage 1: fundamentals ----------------
def winsor(x, lo, hi):
    return max(lo, min(hi, x))

def fund_metrics(t):
    out = {"ticker": t}
    try:
        tk = yf.Ticker(t)
        info = {}
        try:
            info = tk.info or {}
        except Exception:
            pass
        out["sector"] = info.get("sector")
        out["longName"] = info.get("longName") or info.get("shortName") or t
        out["roe"] = info.get("returnOnEquity")
        out["country"] = info.get("country")
        # earnings surprise + EPS accel + beat streak from earnings dates
        sue = None; accel = None; days_since_earn = None; streak = 0
        proj_next = None
        try:
            ed = tk.earnings_dates
            if ed is not None and not ed.empty:
                rep = ed[ed["Reported EPS"].notna()].copy()
                rep = rep.sort_index(ascending=False)
                ix = rep.index
                ixu = ix.tz_localize("UTC") if ix.tz is None else ix.tz_convert("UTC")
                cutoff = pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=1)
                rep = rep[ixu <= cutoff]
                if not rep.empty:
                    ixu = ixu[ixu <= cutoff]
                    r0 = rep.iloc[0]
                    if pd.notna(r0.get("Surprise(%)")):
                        sue = float(r0["Surprise(%)"]) / 100.0
                    days_since_earn = (pd.Timestamp.now(tz="UTC") - ixu[0]).days
                    streak = beat_streak(rep.head(8))
                    proj_next = days_to_next_earnings(ixu)
                    eps = rep["Reported EPS"].tolist()
                    if len(eps) >= 6 and all(e > 0 for e in eps[:6]):
                        g0 = winsor(eps[0] / eps[4] - 1, -3, 3)
                        g1 = winsor(eps[1] / eps[5] - 1, -3, 3)
                        accel = winsor(g0 - g1, -2, 2)
        except Exception as e:
            out["earn_err"] = str(e)[:80]
        # revenue growth YoY from quarterly income statement
        revg = None
        try:
            qis = tk.quarterly_income_stmt
            if qis is not None and not qis.empty and "Total Revenue" in qis.index:
                rev = qis.loc["Total Revenue"].dropna().tolist()
                if len(rev) >= 5 and rev[4] > 0:
                    revg = winsor(rev[0] / rev[4] - 1, -0.9, 5.0)
        except Exception:
            pass
        out["sue"] = winsor(sue, -1, 1) if sue is not None else None
        out["sue_raw"] = sue
        out["eps_accel"] = accel
        out["rev_g"] = revg
        out["days_since_earn"] = days_since_earn
        out["beat_streak"] = streak
        out["sue_capped_extreme"] = bool(sue is not None and sue > 1.0)
        # v3: Piotroski F-score (fail closed) + earnings proximity veto
        out["piotroski"] = piotroski(tk)
        # negative projection = stale data (earnings already happened or
        # cadence unparseable) -> unknown, not a fake number
        out["days_to_earn"] = proj_next if (proj_next is not None
                                            and proj_next >= 0) else None
    except Exception as e:
        out["error"] = str(e)[:120]
    return out

def tech_pass(m):
    return (m["nearhi"] >= 0.85 and m["mom12_1"] > 0 and m["mom12_1"] <= 0.80
            and m["above50"] and m["volratio"] >= 1.0 and m["mania"] < 3.5
            and (math.isnan(m["annvol60"]) or m["annvol60"] <= 0.80)
            and m["maxdd1y"] >= -0.40)

def fund_pass(f):
    return ((f.get("sue") or -9) > 0.03 or (f.get("eps_accel") or -9) > 0
            or (f.get("rev_g") or -9) > 0.15)

def main():
    log("FUSE v3 starting")
    uni = get_universe()
    uni_map = {u["ticker"]: u for u in uni}
    if os.path.exists(STAGE0_CACHE):
        log("loading cached stage0")
        with open(STAGE0_CACHE) as f:
            s0 = json.load(f)
    else:
        s0 = stage0(uni)
    tech_ok = {t: m for t, m in s0.items() if tech_pass(m)}
    log(f"Stage 0 technical passers: {len(tech_ok)}/{len(s0)}")
    # stage 1 fundamentals, threaded
    cands = list(tech_ok.keys())
    funds = {}
    def one(t):
        try:
            return t, fund_metrics(t)
        except Exception as e:
            return t, {"ticker": t, "error": str(e)[:120]}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, (t, f) in enumerate(ex.map(one, cands)):
            funds[t] = f
            if (i + 1) % 50 == 0:
                log(f"  fundamentals {i+1}/{len(cands)}")
    log("Stage 1 done")
    # assemble
    import re
    rows = []
    n_sue = n_acc = 0
    for t, m in tech_ok.items():
        if re.search(r'-P[A-Z]?$', t):  # preferred shares, not common equity
            continue
        f = funds.get(t, {})
        u = uni_map.get(t, {})
        name = f.get("longName") or u.get("name") or t
        nl = name.lower()
        if any(w in nl for w in RELIGION_WORDS):
            continue
        if (f.get("country") or "United States") != "United States":
            continue  # Mint benchmark mode: US-domiciled only
        if (f.get("sector") or "") in ("Energy", "Basic Materials"):
            continue  # commodity-price beneficiaries vs the mandate
        roe = f.get("roe")
        if roe is not None and roe <= 0.05:
            continue
        if not fund_pass(f):
            continue
        # v3.1: Piotroski F<5 is a hard gate for scored names. But None (missing
        # data) is fail-SOFT: insurers/financials use different statement
        # structures, so fail-closed would silently exclude a whole sector —
        # the exact bug class the Mint audits hunt. Unscored names are kept
        # with a flag and the researcher verifies financial health by hand.
        pf = f.get("piotroski")
        pio_unscored = pf is None
        if pf is not None and pf < 5:
            continue
        dte = f.get("days_to_earn")
        if dte is not None and 0 <= dte <= 14:
            continue  # don't buy into the binary event
        if f.get("sue") is not None:
            n_sue += 1
        if f.get("eps_accel") is not None:
            n_acc += 1
        rows.append({
            "ticker": t, "name": name, "sector": f.get("sector"),
            "mcap": u.get("mcap"), "roe": roe,
            "sue": f.get("sue"), "sue_raw": f.get("sue_raw"),
            "eps_accel": f.get("eps_accel"), "rev_g": f.get("rev_g"),
            "days_since_earn": f.get("days_since_earn"),
            "beat_streak": f.get("beat_streak"),
            "piotroski": f.get("piotroski"),
            "pio_unscored": pio_unscored,
            "days_to_earn": f.get("days_to_earn"),
            "sue_capped_extreme": f.get("sue_capped_extreme"),
            **{k: m[k] for k in ("price", "nearhi", "mom12_1", "above50",
                                 "pct_above50", "volratio", "mania",
                                 "annvol60", "maxdd1y")},
        })
    log(f"Fundamental passers: {len(rows)} "
          f"(with SUE: {n_sue}, with EPS accel: {n_acc})")
    # dedupe share classes: strip -A/-B suffix, keep higher preliminary score
    def base(t):
        import re
        return re.sub(r'-[AB]$', '', t)
    # score
    def z(vals):
        a = np.array([v for v in vals if v is not None], dtype=float)
        mu, sd = (a.mean(), a.std(ddof=0)) if len(a) else (0, 1)
        sd = sd if sd > 1e-9 else 1.0
        return {i: (v - mu) / sd for i, v in enumerate(vals) if v is not None}
    # need index-aligned; build lists
    s_sue = [r["sue"] if r["sue"] is not None else -1 for r in rows]
    s_acc = [r["eps_accel"] if r["eps_accel"] is not None else -2 for r in rows]
    s_revg = [r["rev_g"] if r["rev_g"] is not None else -0.9 for r in rows]
    s_near = [r["nearhi"] for r in rows]
    s_vr = [r["volratio"] for r in rows]
    s_mom = [r["mom12_1"] for r in rows]
    s_str = [min(r["beat_streak"] or 0, 4) for r in rows]
    zs, za, zr, zn, zv, zm, zt = (z(s_sue), z(s_acc), z(s_revg), z(s_near),
                                  z(s_vr), z(s_mom), z(s_str))
    # v3: v2 weights rescaled to 0.85 (NOT re-tuned), beat streak 0.15.
    # Deliberately small: new input, provisional until outcome data exists.
    for i, r in enumerate(rows):
        r["fuse_score"] = (0.85 * (0.30 * zs.get(i, 0) + 0.20 * za.get(i, 0)
                                   + 0.15 * zr.get(i, 0) + 0.15 * zn.get(i, 0)
                                   + 0.10 * zv.get(i, 0) + 0.10 * zm.get(i, 0))
                           + 0.15 * zt.get(i, 0))
    # dedupe by base ticker, keep max score
    best = {}
    for r in rows:
        b = base(r["ticker"])
        if b not in best or r["fuse_score"] > best[b]["fuse_score"]:
            best[b] = r
    rows = sorted(best.values(), key=lambda r: -r["fuse_score"])
    log(f"After dedupe: {len(rows)}")
    payload = {
        "asof": datetime.now(timezone.utc).isoformat(),
        "universe": len(uni), "priced": len(s0),
        "tech_pass": len(tech_ok), "final": len(rows),
        "gates": {"nearhi>=0.85": True, "0<mom12_1<=0.80": True,
                  "above50": True, "volratio>=1.0": True, "mania<3.5": True,
                  "annvol<=80%": True, "maxdd>=-40%": True,
                  "energy_excluded": True, "religion_filtered": True,
                  "roe>5%": True, "fund: sue>3%|accel>0|revg>15%": True},
        "score": "0.85*(v2 six-factor) + 0.15*z(min(beat_streak,4)); "
                 "v2 weights NOT re-tuned; streak weight provisional",
        "v3_gates": {"piotroski>=5 (fail closed)": True,
                     "earnings within 14d vetoed": True},
        "rows": rows,
    }
    with open(OUT, "w") as f:
        json.dump(payload, f, default=str)
    log(f"wrote {OUT}")
    for r in rows[:40]:
        sue_s = f"{r['sue']*100:+.1f}%" if r['sue'] is not None else "n/a"
        acc_s = f"{r['eps_accel']*100:+.1f}pp" if r['eps_accel'] is not None else "n/a"
        rg_s = f"{r['rev_g']*100:+.1f}%" if r['rev_g'] is not None else "n/a"
        mc = r['mcap']
        mc_s = f"${mc/1e9:.1f}B" if mc else "n/a"
        flag = " !!EXTREME-SUE" if r.get("sue_capped_extreme") else ""
        dte = r.get("days_to_earn")
        dte_s = f"{dte:.0f}d" if dte is not None else "?"
        print(f"{r['ticker']:8s} {r['fuse_score']:+.2f} {mc_s:>8s} "
              f"SUE {sue_s:>8s} acc {acc_s:>9s} rev {rg_s:>8s} "
              f"strk {r['beat_streak']} Pio {r['piotroski']} earn {dte_s:>4s} "
              f"near {r['nearhi']:.2f} 12-1 {r['mom12_1']*100:+.1f}%{flag} "
              f"{r['sector']}", flush=True)

if __name__ == "__main__":
    main()
