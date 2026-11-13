#!/usr/bin/env python3
"""Mechanical SEC buyback-execution feed for the Mint screener.

The ``exec_only`` marker graduated in
``buyback_execution_2026-11-11``: a company that spent cash
repurchasing shares over the trailing twelve months while its
weighted-average share count still rose year over year (the buyback
entirely absorbed by issuance, mostly equity compensation) was
associated with a -17.0pp pooled next-year return gap on the
2022-2025 research panel, same-sign in both halves.  Dan adopted it
2026-11-11 at the conservative coefficient -0.050 (the pooled gap
would support roughly -8.5pp at the house half-gap convention, but
the effect is validation-heavy, so the Nov 30 audit re-sizes it).

The marker cannot be coded from headlines (execution dollars and
share counts live in cash-flow statements), so it is stamped
mechanically from SEC companyfacts, mirroring form4_feed's
discipline: point-in-time strictly by filed date, a per-ticker cache
reused within seven days, and a hard never-raise contract.

Construction (identical to the study's registered build):
``exec_ttm`` sums PaymentsForRepurchaseOfCommonStock over the four
latest consecutive reported quarters (standalone quarters plus
YTD-chain differencing; the reporting calendar comes from the
operating-cash-flow quarters, and a calendar quarter the filer
reported without a repurchase line counts as zero execution — the
study's disclosed zero-fill amendment).  A filer whose cash-flow
statements never carry the repurchase line by ``as_of`` is a
non-repurchaser (exec_ttm = 0).  Share direction is the
weighted-average share count (basic, then diluted) latest reported
period average versus the same measure one year earlier — the
fundamentals study's latest-period-value rule.  A company counts as
dilutive when the share count did NOT fall (share_dilution >= 0;
the study's registered rule is shrank iff share_dilution < 0).

Public entry point: :func:`exec_only_fact`.  It never raises: any
SEC, network, parsing, or cache failure returns ``exec_only=None``
so the caller leaves the EV term at zero.
"""

import json
import os
import threading
import time
from datetime import date, datetime, timedelta

import requests

USER_AGENT = "MintResearch test@example.com"
# The two environment overrides exist so sandbox rehearsals can prove
# the pipeline's failure behaviour (bad UA / isolated cache) without
# editing production code.  In production neither is set and the UA is
# exactly the declared string above.
_USER_AGENT = os.environ.get("EXEC_USER_AGENT", USER_AGENT)
_HERE = os.path.dirname(os.path.abspath(__file__))
_CACHE_PATH = os.environ.get(
    "EXEC_CACHE_PATH", os.path.join(_HERE, "exec_cache.json"))
_CIK_CACHE_PATH = os.environ.get(
    "EXEC_CIK_CACHE_PATH", os.path.join(_HERE, "exec_cik_cache.json"))
_MIN_REQUEST_INTERVAL = 0.21  # a shade over 5 requests/second
_last_request_at = 0.0
_request_lock = threading.Lock()
_thread_local = threading.local()
_facts_memo = {}
_FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A"}
_REPURCHASE_CONCEPT = "PaymentsForRepurchaseOfCommonStock"
_OCF_CONCEPT = "NetCashProvidedByUsedInOperatingActivities"
_SHARE_CONCEPTS = ("WeightedAverageNumberOfSharesOutstandingBasic",
                   "WeightedAverageNumberOfDilutedSharesOutstanding")
_STALE_DAYS = 200  # the study excludes windows anchored >200d out


def _today():
    return date.today()


def _parse_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _years_ago(day, years):
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # Feb 29 -> Feb 28
        return day.replace(year=day.year - years, day=28)


def _get(url):
    """One paced SEC GET with the declared user agent.

    Request *starts* are reserved under a lock at <=5/second, mirroring
    form4_feed (this feed issues one companyfacts call per ticker, so
    contention is minimal, but the pace cap is the same).
    """
    global _last_request_at
    with _request_lock:
        wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    response = session.get(
        url,
        headers={"User-Agent": _USER_AGENT,
                 "Accept": "application/json, */*"},
        timeout=60,
    )
    response.raise_for_status()
    return response


def _read_json(path):
    try:
        with open(path) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path, data):
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        pass


def _ticker_key(ticker):
    return str(ticker or "").strip().upper().replace(".", "-")


# CIK LINEAGE (2026-11-12, cik_diagnostic_2026-11-12): the SEC's
# ticker map sometimes points a ticker at a SUCCESSOR registrant (a
# merger vehicle, second-step conversion, or holdco reorganization)
# whose filing history starts at the succession, while the operating
# company's history sits under the legacy CIK. successor -> legacy.
_CIK_LINEAGE = {
    "0002115436": "0000034088",  # XOM: ExxonMobil Holdings <- Exxon Mobil Corp (2026-07-01)
    "0002082866": "0001115055",  # PNFP: merger successor <- Pinnacle Financial Partners (2025)
    "0002074176": "0001602065",  # VNOM: merger successor <- Viper Energy (2025)
    "0002115119": "0001723596",  # CLBK: Columbia Financial /MD/ <- Columbia Financial (2026)
    "0002152623": "0001593538",  # NAVI/JSM: Navient Holdings <- Navient Corp (2026-10-29)
}


def _lineage_ciks(cik):
    """[cik] plus its legacy predecessor when cik is a known successor."""
    legacy = _CIK_LINEAGE.get(cik)
    return [cik, legacy] if legacy else [cik]


def _cik_map():
    """Ticker -> zero-padded CIK, disk-cached with a 30-day TTL.

    CIK CACHE TTL (2026-11-12, cik_diagnostic_2026-11-12): the cache
    previously never expired, which let SEC successor remappings
    enter silently and stay forever. A stale cache is refreshed from
    the SEC; if the refresh fails, the stale cache is used rather
    than failing the feed (the feed's fail-safe contract)."""
    cached = _read_json(_CIK_CACHE_PATH)
    mapping = cached.get("tickers") if isinstance(cached, dict) else None
    if isinstance(mapping, dict) and mapping:
        fetched = _parse_date(cached.get("fetched_date"))
        if fetched is not None and (_today() - fetched).days < 30:
            return mapping
    try:
        response = _get("https://www.sec.gov/files/company_tickers.json")
        raw = response.json()
    except Exception:
        if isinstance(mapping, dict) and mapping:
            return mapping
        raise
    rows = raw.values() if isinstance(raw, dict) else raw
    fresh = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = _ticker_key(row.get("ticker"))
        cik = row.get("cik_str")
        if ticker and cik is not None:
            fresh[ticker] = str(int(cik)).zfill(10)
    if not fresh:
        if isinstance(mapping, dict) and mapping:
            return mapping
        raise ValueError("SEC company ticker map was empty")
    _write_json(_CIK_CACHE_PATH,
                {"fetched_date": _today().isoformat(), "tickers": fresh})
    return fresh


def _companyfacts(cik):
    """The ticker's companyfacts payload, memoised per process."""
    if cik in _facts_memo:
        return _facts_memo[cik]
    response = _get(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    payload = response.json()
    if not isinstance(payload, dict) or "facts" not in payload:
        raise ValueError("companyfacts payload malformed")
    _facts_memo[cik] = payload
    return payload


def _merge_facts(payloads):
    """Union companyfacts payloads (legacy + successor CIKs): every
    fact instance from every payload, exact duplicates dropped."""
    merged = {"facts": {}}
    for payload in payloads:
        for taxonomy, concepts in (payload.get("facts") or {}).items():
            t_out = merged["facts"].setdefault(taxonomy, {})
            for concept, body in (concepts or {}).items():
                c_out = t_out.setdefault(concept, {"units": {}})
                for unit, rows in ((body or {}).get("units")
                                   or {}).items():
                    u_out = c_out["units"].setdefault(unit, [])
                    seen = {(r.get("start"), r.get("end"), r.get("val"),
                             r.get("filed"), r.get("form"))
                            for r in u_out}
                    for r in rows or []:
                        key = (r.get("start"), r.get("end"),
                               r.get("val"), r.get("filed"),
                               r.get("form"))
                        if key not in seen:
                            u_out.append(r)
                            seen.add(key)
    return merged


def _companyfacts_for(cik):
    """Companyfacts for a resolved CIK; when the CIK is a known
    successor, merged with its legacy predecessor so trailing
    windows straddling the succession see the full history
    (2026-11-12, cik_diagnostic_2026-11-12)."""
    ciks = _lineage_ciks(cik)
    if len(ciks) == 1:
        return _companyfacts(cik)
    return _merge_facts([_companyfacts(c) for c in ciks])


def _duration_facts(cf, taxonomy, concept, unit):
    """Duration facts for one concept as plain dicts (start/end/val/
    filed/form), mirroring the study's extraction: entries without a
    start are skipped."""
    node = (cf.get("facts") or {}).get(taxonomy, {}).get(concept)
    if not node:
        return []
    rows = []
    for u, entries in (node.get("units") or {}).items():
        if u != unit:
            continue
        for e in entries:
            if e.get("start") is None:
                continue
            start = _parse_date(e.get("start"))
            end = _parse_date(e.get("end"))
            filed = _parse_date(e.get("filed"))
            if start is None or end is None or filed is None:
                continue
            rows.append({"start": start, "end": end,
                         "val": float(e["val"]), "filed": filed,
                         "form": str(e.get("form", ""))})
    return rows


def _build_quarters(rows, as_of):
    """Standalone quarters (oldest first) from facts filed <= as_of.

    Faithful port of the study's build_quarters: dedup per
    (start, end) with the latest filed winning; direct ~90-day facts;
    YTD-chain differencing per fiscal-year start (cash-flow lines are
    reported year-to-date in 10-Qs, so differencing is the main path).
    Returns None when no facts survive the filter, [] when facts
    survive but no quarter can be formed.
    """
    d = [r for r in rows if r["filed"] <= as_of and r["form"] in _FORMS]
    if not d:
        return None
    d.sort(key=lambda r: r["filed"])
    dedup = {}
    for r in d:
        dedup[(r["start"], r["end"])] = r
    d = list(dedup.values())

    def dur(r):
        return (r["end"] - r["start"]).days

    quarters = {}  # end -> (start, val)
    for r in d:
        if 70 <= dur(r) <= 110:
            quarters[r["end"]] = (r["start"], r["val"])
    by_start = {}
    for r in d:
        by_start.setdefault(r["start"], []).append(r)
    for start, g in by_start.items():
        def pick(lo, hi):
            gg = [r for r in g if lo <= dur(r) <= hi]
            if not gg:
                return None
            return sorted(gg, key=lambda r: r["end"])[-1]

        q1 = pick(70, 110)
        half = pick(150, 210)
        nine = pick(240, 300)
        fy = pick(320, 400)
        scale = None
        for cand in (fy, nine, half, q1):
            if cand is not None:
                scale = cand["val"]
                break
        if scale is None or scale <= 0:
            continue
        tol = 0.005 * scale

        def derive(val, end, qstart):
            if val is None or val < -tol:
                return
            if end not in quarters:  # direct facts take precedence
                quarters[end] = (qstart, max(val, 0.0))

        if half is not None and q1 is not None:
            derive(half["val"] - q1["val"], half["end"], q1["end"])
        if nine is not None and half is not None:
            derive(nine["val"] - half["val"], nine["end"], half["end"])
        if fy is not None and nine is not None:
            derive(fy["val"] - nine["val"], fy["end"], nine["end"])
    if not quarters:
        return []
    return sorted([(s, e, v) for e, (s, v) in quarters.items()])


def _window_from_calendar(rep_q, ocf_q):
    """The study's amended window: the reporting calendar comes from
    the union of operating-cash-flow and repurchase quarter ends; for
    each of the four latest consecutive calendar ends the repurchase
    value is the reported value if present, else 0 (the filer
    reported its cash-flow statement for that period without a
    repurchase line).  Returns (vals_oldest_first, anchor_end,
    n_filled_zero) or None."""
    if not ocf_q:
        return None
    cal = sorted({e for _, e, _ in ocf_q}
                 | {e for _, e, _ in (rep_q or [])}, reverse=True)
    run_ends = [cal[0]]
    for prev, cur in zip(cal, cal[1:]):
        if 70 <= (prev - cur).days <= 120:
            run_ends.append(cur)
        else:
            break
    if len(run_ends) < 4:
        return None
    ends = list(reversed(run_ends[:4]))
    rep_map = {e: v for _, e, v in (rep_q or [])}
    vals, filled = [], 0
    for e in ends:
        if e in rep_map:
            vals.append(rep_map[e])
        else:
            vals.append(0.0)
            filled += 1
    return vals, ends[-1], filled


def _share_facts(cf):
    """Weighted-average share facts with concept priority (basic 0,
    diluted 1), mirroring the fundamentals study's shares slot."""
    gaap = (cf.get("facts") or {}).get("us-gaap", {})
    rows = []
    for prio, concept in enumerate(_SHARE_CONCEPTS):
        node = gaap.get(concept)
        if not node:
            continue
        for u, entries in (node.get("units") or {}).items():
            if u != "shares":
                continue
            for e in entries:
                end = _parse_date(e.get("end"))
                filed = _parse_date(e.get("filed"))
                if end is None or filed is None:
                    continue
                rows.append({"start": _parse_date(e.get("start")),
                             "end": end, "val": float(e["val"]),
                             "filed": filed, "prio": prio})
    return rows


def _latest_period_value(rows, as_of, cutoff):
    """Most recent period value for the average-type share concept:
    the fact with the greatest end <= cutoff among facts filed by
    as_of with duration 70-400 days; ties on end break to the longest
    duration, then concept priority, then latest filed.  (The
    fundamentals study's rule, used ONLY for shares.)"""
    d = [r for r in rows if r["filed"] <= as_of]
    if not d:
        return None
    # _dedup: highest-priority concept wins per (start, end); within a
    # concept the latest filed wins.  Successive stable sorts apply
    # the keys in reverse priority order.
    d.sort(key=lambda r: r["filed"], reverse=True)
    d.sort(key=lambda r: r["prio"])
    seen = set()
    ded = []
    for r in d:
        key = (r["start"], r["end"])
        if key in seen:
            continue
        seen.add(key)
        ded.append(r)
    d = [r for r in ded if r["end"] <= cutoff and r["start"] is not None
         and 70 <= (r["end"] - r["start"]).days <= 400]
    if not d:
        return None
    d.sort(key=lambda r: r["filed"], reverse=True)
    d.sort(key=lambda r: r["prio"])
    d.sort(key=lambda r: (r["end"] - r["start"]).days, reverse=True)
    d.sort(key=lambda r: r["end"], reverse=True)
    return float(d[0]["val"])


def _share_dilution(cf, as_of):
    """Weighted-share growth YoY (negative = shrink), or None."""
    rows = _share_facts(cf)
    if not rows:
        return None
    sh = _latest_period_value(rows, as_of, as_of)
    sh_p = _latest_period_value(rows, as_of, _years_ago(as_of, 1))
    if sh is None or sh_p is None or sh_p <= 0:
        return None
    return sh / sh_p - 1.0


def _undetermined(exec_ttm=None, share_chg=None, evidence=None):
    return {"exec_only": None, "exec_ttm": exec_ttm,
            "share_chg_yoy": share_chg, "evidence": evidence or []}


def _compute(ticker, as_of):
    cik = _cik_map().get(_ticker_key(ticker))
    if not cik:
        return _undetermined()
    cf = _companyfacts_for(cik)
    rep = _duration_facts(cf, "us-gaap", _REPURCHASE_CONCEPT, "USD")
    ocf = _duration_facts(cf, "us-gaap", _OCF_CONCEPT, "USD")
    concept_ever = any(r["filed"] <= as_of for r in rep)
    has_ocf_filing = any(r["filed"] <= as_of for r in ocf)
    rep_q = _build_quarters(rep, as_of)
    ocf_q = _build_quarters(ocf, as_of)

    exec_ttm = None
    anchor_end = None
    if concept_ever:
        win = _window_from_calendar(rep_q, ocf_q)
        if win is not None:
            vals, anchor_end, _filled = win
            if (as_of - anchor_end).days > _STALE_DAYS:
                return _undetermined()  # stale: the study excludes it
            exec_ttm = float(sum(vals))
    elif has_ocf_filing:
        # The filer reports cash flow but has never carried a
        # repurchase line by as_of: a non-repurchaser.
        exec_ttm = 0.0
    if exec_ttm is None:
        return _undetermined()

    share_chg = _share_dilution(cf, as_of)
    anchor_txt = (f" through {anchor_end.isoformat()}" if anchor_end
                  else "")
    if exec_ttm <= 0:
        return {"exec_only": 0, "exec_ttm": 0.0,
                "share_chg_yoy": share_chg,
                "evidence": [f"no share repurchases in the trailing "
                             f"12 months{anchor_txt} "
                             f"(SEC companyfacts)"]}
    if share_chg is None:
        # Execution is known but the share direction is not: the
        # marker cannot be determined.
        return _undetermined(
            exec_ttm=exec_ttm,
            evidence=[f"repurchased ${exec_ttm:,.0f} in the trailing "
                      f"12 months{anchor_txt}; weighted share change "
                      f"unavailable (SEC companyfacts)"])
    dilutive = share_chg >= 0  # study rule: shrank iff change < 0
    direction = "rose" if share_chg > 0 else (
        "was flat" if share_chg == 0 else "fell")
    pct = f"{abs(share_chg):.1%}"
    if dilutive:
        evidence = [f"repurchased ${exec_ttm:,.0f} in the trailing 12 "
                    f"months{anchor_txt} while weighted shares "
                    f"{direction} {pct} year over year: buyback "
                    f"absorbed by issuance (SEC companyfacts)"]
    else:
        evidence = [f"repurchased ${exec_ttm:,.0f} in the trailing 12 "
                    f"months{anchor_txt} and weighted shares fell "
                    f"{pct} year over year (SEC companyfacts)"]
    return {"exec_only": 1 if dilutive else 0,
            "exec_ttm": float(exec_ttm),
            "share_chg_yoy": float(share_chg),
            "evidence": evidence}


def exec_only_fact(ticker, as_of=None):
    """Return the filing-based buyback-execution marker for ``ticker``.

    ``exec_only`` is 1 when the company repurchased shares over the
    trailing twelve months (by filings public as of ``as_of``) while
    its weighted-average share count did not fall year over year; 0
    when the marker is determined and does not hold; None whenever it
    could not be determined.  This function never raises into the
    screener pipeline.
    """
    try:
        as_of_date = _parse_date(as_of) if as_of is not None else _today()
        if as_of_date is None:
            return _undetermined()
        key = _ticker_key(ticker)
        if not key:
            return _undetermined()

        cache = _read_json(_CACHE_PATH)
        cached = cache.get(key) if isinstance(cache, dict) else None
        if isinstance(cached, dict):
            cached_as_of = _parse_date(cached.get("as_of"))
            result = cached.get("result")
            if (cached_as_of is not None and isinstance(result, dict)
                    and abs((as_of_date - cached_as_of).days) <= 7
                    and result.get("exec_only") in (0, 1)):
                return {
                    "exec_only": int(result["exec_only"]),
                    "exec_ttm": (float(result["exec_ttm"])
                                 if result.get("exec_ttm") is not None
                                 else None),
                    "share_chg_yoy": (
                        float(result["share_chg_yoy"])
                        if result.get("share_chg_yoy") is not None
                        else None),
                    "evidence": list(result.get("evidence") or []),
                }

        result = _compute(key, as_of_date)
        if result.get("exec_only") in (0, 1):
            if not isinstance(cache, dict):
                cache = {}
            cache[key] = {
                "as_of": as_of_date.isoformat(),
                "fetched_date": _today().isoformat(),
                "result": result,
            }
            _write_json(_CACHE_PATH, cache)
        return result
    except Exception:
        return _undetermined()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="SEC buyback-execution (exec_only) feed")
    parser.add_argument("ticker")
    parser.add_argument("--as-of")
    args = parser.parse_args()
    print(json.dumps(exec_only_fact(args.ticker, args.as_of), indent=1))
