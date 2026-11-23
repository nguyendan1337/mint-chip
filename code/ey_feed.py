#!/usr/bin/env python3
"""Mechanical SEC earnings-yield feed for the Mint screener.

Supplies the point-in-time earnings yield (operating income /
enterprise value) behind the stock eligibility gate's EY band leg
(shipped 2026-11-21, Dan's directive after the frozen test in
nightshade/mint_ey_band_2026-11-21: in-band picks blew up at 44.6%
vs 27.5% out-of-band on Mint's mechanical-core bed, and excluding
the band was DOMINANT — pooled return +71.73% vs +67.30%, blowups
24.52% vs 27.96%, negative slates 8.60% vs 11.83%).

The construction replicates, field for field, the research store's
PIT fundamentals tables the test was computed from
(fundamentals_annual_pit.csv + fundamentals_balance_sheet_pit.csv,
both built from SEC companyfacts):

- Operating income: us-gaap OperatingIncomeLoss (USD), 10-K/10-K-A
  instances only, fiscal-year durations of 300-400 days.
- Shares: us-gaap CommonStockSharesOutstanding (instant, 10-K /
  10-K-A, fp FY or unmarked), falling back to
  WeightedAverageNumberOfDilutedSharesOutstanding (FY, 300-400d)
  when no shares-outstanding version is usable — the test's own
  fallback order.
- Total debt: the balance-sheet build's composite — within a
  single filing version, one current part (first present of
  DebtCurrent, LongTermDebtCurrent, LongtermBorrowingsCurrent,
  ShortTermBorrowings) plus one noncurrent part (LongTermDebt-
  Noncurrent, LongtermBorrowingsNoncurrent); otherwise the first
  present fallback (LongTermDebt, Debt). Missing debt is treated
  as 0 (and flagged), exactly as the test treats it.
- Per (period_end, filed_date), duplicate facts resolve the way
  the store builds resolve them (amendments beat originals, then
  the later accession wins). Versions with filed_date before
  period_end are dropped (the builds' PIT hygiene). Each field is
  then selected per-field latest-non-null as-filed: the version
  with the greatest filed_date on or before the as-of date (ties:
  greatest period_end) governs that field alone.
- Enterprise value: shares restated to today's units x price +
  total debt. The as-filed share count is in its filing period's
  units; it is multiplied by the product of split ratios with
  ex-date after the share count's own period end, which is the
  exact restatement for a price quoted in today's units (the
  test's panels are split-adjusted closes in today's units; its
  split_factor_after(pick date) is the same product whenever no
  split falls between the share period end and the pick date).
- EY = operating income / EV. Not computable (ey=None) when the
  CIK is unknown, operating income or shares are unavailable,
  shares <= 0, EV <= 0, or |EY| > 1 (the test's data-sanity
  guard: a full year of operating income exceeding the entire
  enterprise value is an as-filed share-units artifact).

Balance-sheet ratios (added 2026-11-21, Dan's directive to ship
the decomposition scan's two independent risk pockets after the
frozen arm tests in nightshade/scan_arms_2026-11-21 and the
combined-pair verification recorded there): :func:`balance_sheet_ratios`
also supplies, from the same companyfacts extraction,

- cash_share = cash & equivalents / total assets, replicating the
  accruals build (fundamentals_accruals_pit.csv) field for field:
  cash is the first present of CashAndCashEquivalentsAtCarryingValue
  then Cash (instant, 10-K/10-K-A, fp FY or unmarked, period ends
  on/after 2015-01-01 — that build's coverage window); total
  assets is us-gaap Assets under the same filters. The gate bars
  picks with cash_share >= 0.3519357079857501 (scan cell blowups
  46.1%; arm vs the EY-band baseline +73.06% / 21.61%).
- leverage = total debt / total assets, replicating the
  balance-sheet build (fundamentals_balance_sheet_pit.csv): the
  same composite debt this module already extracts for EV, over
  us-gaap Assets (instant, same filters, no period window). The
  gate bars picks with leverage < 0.0407764726456614 (strict; the
  scan's rank rule placed at-bound names outside the pocket; scan
  cell blowups 40.4%; arm DOMINANT vs the EY-band baseline,
  +74.77% / 24.19%). A name with no debt version on file has NO
  leverage (not zero) and is never barred by the leg.
- Net income / enterprise value (added 2026-11-22, Dan's
  directive to ship worst-slate Candidate 1 after the frozen
  arm test in nightshade/worst_slate_scan_2026-11-22):
  :func:`net_income_ev` supplies ni_ev = net income / EV, the
  gate's fourth filing-fact leg. Net income replicates the
  balance-sheet build (fundamentals_balance_sheet_pit.csv)
  field for field: the first present of NetIncomeLoss then
  ProfitLoss (USD, 10-K/10-K-A instances only, fp FY, fiscal-
  year durations of 300-400 days), selected per-field
  latest-non-null as-filed. EV is the EY computation's own
  enterprise value (same shares restatement, same narrow
  debt composite, missing debt treated as 0), so the two
  yields' denominators are identical by construction. The
  frozen test applied NO |ni_ev| > 1 sanity guard (unlike
  the EY path), and neither does this one: the shipped band
  sits near zero, far inside any share-units artifact's
  reach. The gate bars picks with ni_ev in
  [-0.039048959167093514, +0.003104453524704764] (the scan's
  recorded middle-quintile edges of the pick set, frozen
  verbatim): in-band picks blew up at 40.0% vs a 21.9%
  covered base on the clean bed, and excluding the band was
  DOMINANT there (return +54.90% vs +50.62%, blowups 21.40%
  vs 22.98%, negative slates unchanged, 70 of 570 picks
  displaced); the same cell passed the outcome bar on the
  93-date bed (blowup gap +16.1pp).
- leverage construction WIDENED (shipped 2026-11-22, Dan's
  directive after the measurement-upgrades retest in
  nightshade/measurement_upgrades_2026-11-22): the debt
  composite above misses debt that sits in commercial paper
  and lease liabilities, so the narrow construction could
  measure leverage for only 59.1% of clean-bed picks (48.7%
  in 2022-23). The wide construction — the shipped borrowings
  composite, plus commercial paper (components route only),
  plus per-type lease liabilities (operating and finance;
  the filer's own total lease tag wins when present and at
  least the current+noncurrent split sum, otherwise the
  split sum; capital-lease tags substitute slot-by-slot for
  the finance type), with LongTermDebtAndCapitalLease-
  Obligations added as the last-priority noncurrent fallback
  and finance leases skipped when that combined concept is
  the noncurrent pick (its name already covers them) — was
  built into the research store (fundamentals_balance_sheet_
  debt_wide_pit.csv, hand-verified against AAPL/MSFT/XOM
  10-Ks) and re-tested: coverage on clean-bed picks rose to
  75.8% (67.1% in 2022-23) and the pair step REMAINED
  DOMINANT at wide measurement (+50.62% vs +49.38%, blowups
  22.98% vs 26.32%), though its 2022-23 return edge shrank
  (+6.33pp narrow -> +0.40pp wide; the blowup gain was
  identical). The cutoff is unchanged. The narrow
  construction remains selectable (debt_construction=
  "narrow") for A/B comparison; "wide" is the default.
  The EY path's own debt (enterprise value) is NOT widened:
  it keeps the shipped narrow composite the EY test used.

The pair together (never run as an arm before the ship) verified
DOMINANT vs the EY-band baseline on the same bed: pooled return
+75.29% vs +71.73%, blowups 21.18% vs 24.52% (better in all four
eras), negative slates 5.38% vs 8.60%.

Public entry points: :func:`earnings_yield` (one ticker) and
:func:`earnings_yields` (batch), :func:`balance_sheet_ratios`,
and :func:`net_income_ev` / :func:`net_income_evs`. They never raise: any SEC,
network, parsing, or cache failure returns ey=None so the caller
treats the name as uncovered — and uncovered names are NEVER
barred by the gate (fail-soft, matching the test).

Caching and staleness: the SEC ticker->CIK map is disk-cached for
30 days (a stale map is used if a refresh fails). Each ticker's
extracted as-filed version lists are disk-cached; a cache entry is
reused while it is at most 7 days old (a new 10-K is the only
event that changes the extraction, and the as-of selection is
recomputed from the cached versions on every call, so any as-of
date is answered exactly from a fresh-enough cache).
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

USER_AGENT = "MintResearch test@example.com"
# Environment overrides exist so sandbox rehearsals can prove the
# pipeline's failure behaviour (bad UA / isolated cache) without
# editing production code. In production neither is set.
_USER_AGENT = os.environ.get("EY_USER_AGENT", USER_AGENT)
_HERE = os.path.dirname(os.path.abspath(__file__))
_CACHE_PATH = os.environ.get(
    "EY_CACHE_PATH", os.path.join(_HERE, "ey_cache.json"))
_CIK_CACHE_PATH = os.environ.get(
    "EY_CIK_CACHE_PATH", os.path.join(_HERE, "ey_cik_cache.json"))
_MIN_REQUEST_INTERVAL = 0.21  # a shade over 5 requests/second
_FACTS_TTL_DAYS = 7
_last_request_at = 0.0
_request_lock = threading.Lock()
_thread_local = threading.local()


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


def _get(url):
    """One paced SEC GET with the declared user agent (form4 pattern:
    request starts reserved under a lock at <=5/second, responses
    awaited outside the lock)."""
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


# CIK LINEAGE (mirrors form4_feed, 2026-11-12 cik diagnostic): the
# SEC ticker map sometimes points at a SUCCESSOR registrant whose
# filing history starts at the succession, while the operating
# company's history sits under the legacy CIK. successor -> legacy.
_CIK_LINEAGE = {
    "0002115436": "0000034088",  # XOM successor <- Exxon Mobil Corp
    "0002082866": "0001115055",  # PNFP successor <- Pinnacle Fin.
    "0002074176": "0001602065",  # VNOM successor <- Viper Energy
    "0002115119": "0001723596",  # CLBK successor <- Columbia Fin.
    "0002152623": "0001593538",  # NAVI successor <- Navient Corp
}


def _lineage_ciks(cik):
    """[cik] plus its legacy predecessor when cik is a known successor."""
    legacy = _CIK_LINEAGE.get(cik)
    return [cik, legacy] if legacy else [cik]


def _fill_from_snapshot(mapping):
    """Fill tickers missing from the live SEC map out of the
    research store's snapshot of the same file (see _cik_map)."""
    try:
        snap_path = (Path(__file__).resolve().parent / "research_data"
                     / "fundamentals" / "company_tickers.json")
        snap = _read_json(snap_path)
        srows = snap.values() if isinstance(snap, dict) else (snap or [])
        for row in srows:
            if not isinstance(row, dict):
                continue
            ticker = _ticker_key(row.get("ticker"))
            cik = row.get("cik_str")
            if ticker and cik is not None and ticker not in mapping:
                mapping[ticker] = str(int(cik)).zfill(10)
    except Exception:
        pass
    return mapping


def _cik_map():
    """Ticker -> zero-padded CIK, disk-cached with a 30-day TTL.
    A stale cache is used if the refresh fails (fail-safe)."""
    cached = _read_json(_CIK_CACHE_PATH)
    mapping = cached.get("tickers") if isinstance(cached, dict) else None
    if isinstance(mapping, dict) and mapping:
        fetched = _parse_date(cached.get("fetched_date"))
        if fetched is not None and (_today() - fetched).days < 30:
            return _fill_from_snapshot(mapping)
    try:
        response = _get("https://www.sec.gov/files/company_tickers.json")
        raw = response.json()
    except Exception:
        if isinstance(mapping, dict) and mapping:
            return _fill_from_snapshot(mapping)
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
            return _fill_from_snapshot(mapping)
        raise ValueError("SEC company ticker map was empty")
    # Coverage fill from the research store's snapshot of the same
    # SEC file: the live company_tickers.json occasionally drops a
    # listed name outright (observed 2026-11-21: ROKU / CIK 1428439
    # absent from the live file, present in the store snapshot the
    # historical test resolved CIKs from). CIKs are stable
    # identifiers, so an older snapshot is a safe fallback for names
    # the live file misses; live entries always win.
    fresh = _fill_from_snapshot(fresh)
    _write_json(_CIK_CACHE_PATH,
                {"fetched_date": _today().isoformat(), "tickers": fresh})
    return fresh


# ---------------------------------------------------------------- extraction
# Field rules replicate the research store builds exactly:
#   OI     fundamental_acceleration_2026-11-14 (fundamentals_annual_pit)
#   shares/debt  balance_sheet_build_2026-11-19 (fundamentals_balance_sheet_pit)

_FORMS_K = ("10-K", "10-K/A")
# Cash concepts in the accruals build's priority order.
_CASH_CONCEPTS = ["CashAndCashEquivalentsAtCarryingValue", "Cash"]
# Net income concepts in the balance-sheet build's priority order.
_NI_CONCEPTS = ["NetIncomeLoss", "ProfitLoss"]
# The accruals build's coverage window: it stores no period ending
# before 2015-01-01, so the cash_share as-of selection must not
# reach back past it either (the balance-sheet build has no such
# window, and leverage does not apply it).
_MIN_ACCRUALS_PERIOD_END = date(2015, 1, 1)
_DEBT_CURRENT = ["DebtCurrent", "LongTermDebtCurrent",
                 "LongtermBorrowingsCurrent", "ShortTermBorrowings"]
_DEBT_NONCURRENT = ["LongTermDebtNoncurrent",
                    "LongtermBorrowingsNoncurrent"]
_DEBT_FALLBACK = ["LongTermDebt", "Debt"]
# Wide leverage construction (2026-11-22 ship): the research
# build's concept sets (build1_wide_debt.py, SPEC Build 1 as
# amended). Commercial paper enters only through the components
# route; LongTermDebtAndCapitalLeaseObligations is the
# last-priority noncurrent borrowings candidate; leases are
# taken per type (total tag vs splits), with capital-lease tags
# as slot-by-slot stragglers for the finance type.
_CP_CONCEPTS = ["CommercialPaper"]
_COMBINED_NONCURRENT = "LongTermDebtAndCapitalLeaseObligations"
_DEBT_NONCURRENT_WIDE = _DEBT_NONCURRENT + [_COMBINED_NONCURRENT]
_LEASE_OP = ("OperatingLeaseLiability",
             "OperatingLeaseLiabilityCurrent",
             "OperatingLeaseLiabilityNoncurrent")
_LEASE_FIN = ("FinanceLeaseLiability",
              "FinanceLeaseLiabilityCurrent",
              "FinanceLeaseLiabilityNoncurrent")
_LEASE_CAP = ("CapitalLeaseObligations",
               "CapitalLeaseObligationsCurrent",
               "CapitalLeaseObligationsNoncurrent")
_DEBT_WIDE_ALL = (_DEBT_CURRENT + _DEBT_NONCURRENT_WIDE
                  + _DEBT_FALLBACK + _CP_CONCEPTS
                  + list(_LEASE_OP) + list(_LEASE_FIN)
                  + list(_LEASE_CAP))


def _facts(cf, concept, unit):
    try:
        return cf["facts"]["us-gaap"][concept]["units"].get(unit, [])
    except (KeyError, TypeError):
        return []


def _form_rank(form):
    return 1 if str(form or "").endswith("/A") else 0


def _duration_days(fact):
    start = _parse_date(fact.get("start"))
    end = _parse_date(fact.get("end"))
    if start is None or end is None:
        return None
    return (end - start).days


def _version_pick(facts):
    """One fact per (period_end, filed_date): amendments beat
    originals, then the later accession wins (the builds' rule)."""
    return max(facts, key=lambda f: (_form_rank(f.get("form", "")),
                                     str(f.get("accn", ""))))


def _collect_versions(cf, concept, unit, kind):
    """{(period_end, filed): value} for one field, annual 10-K only.

    kind "duration_fy": fp == 'FY' and 300-400 day duration (OI,
    diluted shares). kind "instant": fp in (FY, None, '') (shares
    outstanding). kind "oi": the annual fundamentals build's OI
    rule — form + duration only, no fp filter."""
    grouped = {}
    for fact in _facts(cf, concept, unit):
        form = fact.get("form", "")
        if form not in _FORMS_K:
            continue
        end = _parse_date(fact.get("end"))
        filed = _parse_date(fact.get("filed"))
        if end is None or filed is None or filed < end:
            continue
        fp = fact.get("fp")
        if kind in ("duration_fy", "oi"):
            dur = _duration_days(fact)
            if dur is None or not (300 <= dur <= 400):
                continue
            if kind == "duration_fy" and fp != "FY":
                continue
        else:  # instant
            if fp not in ("FY", None, ""):
                continue
        val = fact.get("val")
        if val is None:
            continue
        grouped.setdefault((end, filed), []).append(fact)
    out = {}
    for (end, filed), cands in grouped.items():
        out[(end, filed)] = float(_version_pick(cands).get("val"))
    return out


def _collect_versions_priority(cf, concepts, unit, kind):
    """{(period_end, filed): (value, concept)} for a field defined
    by a priority list of concepts — the store builds' rule (the
    accruals build's _collect_simple, the balance-sheet build's
    _collect): at each (period_end, filed) the highest-priority
    concept present wins; within that concept, amendments beat
    originals, then the later accession wins."""
    grouped = {}
    for concept in concepts:
        for fact in _facts(cf, concept, unit):
            form = fact.get("form", "")
            if form not in _FORMS_K:
                continue
            end = _parse_date(fact.get("end"))
            filed = _parse_date(fact.get("filed"))
            if end is None or filed is None or filed < end:
                continue
            fp = fact.get("fp")
            if kind == "instant":
                if fp not in ("FY", None, ""):
                    continue
            else:  # duration_fy
                if fp != "FY":
                    continue
                dur = _duration_days(fact)
                if dur is None or not (300 <= dur <= 400):
                    continue
            if fact.get("val") is None:
                continue
            grouped.setdefault((end, filed), []).append((concept, fact))
    out = {}
    for key, cands in grouped.items():
        for concept in concepts:
            mine = [f for c, f in cands if c == concept]
            if mine:
                out[key] = (float(_version_pick(mine).get("val")), concept)
                break
    return out


def _collect_debt(cf):
    """{(period_end, filed): (value, source)} — the balance-sheet
    build's composite: components summed only inside a single
    filing version (same accession)."""
    versions = {}  # (end, filed, accn, form) -> {concept: val}
    for concept in _DEBT_CURRENT + _DEBT_NONCURRENT + _DEBT_FALLBACK:
        for fact in _facts(cf, concept, "USD"):
            form = fact.get("form", "")
            if form not in _FORMS_K:
                continue
            if fact.get("fp") not in ("FY", None, ""):
                continue
            end = _parse_date(fact.get("end"))
            filed = _parse_date(fact.get("filed"))
            if end is None or filed is None or filed < end:
                continue
            if fact.get("val") is None:
                continue
            versions.setdefault(
                (end, filed, str(fact.get("accn", "")), form),
                {})[concept] = float(fact.get("val"))
    per_key = {}
    for (end, filed, accn, form), vals in versions.items():
        cur = next((vals[c] for c in _DEBT_CURRENT if c in vals), None)
        cur_name = next((c for c in _DEBT_CURRENT if c in vals), None)
        non = next((vals[c] for c in _DEBT_NONCURRENT if c in vals),
                   None)
        non_name = next((c for c in _DEBT_NONCURRENT if c in vals),
                        None)
        if cur is not None or non is not None:
            total = (cur or 0) + (non or 0)
            source = "+".join(n for n in (cur_name, non_name) if n)
        else:
            fb = next((c for c in _DEBT_FALLBACK if c in vals), None)
            if fb is None:
                continue
            total, source = vals[fb], fb
        per_key.setdefault((end, filed), []).append(
            (total, source, _form_rank(form), accn))
    out = {}
    for key, cands in per_key.items():
        total, source, _, _ = max(cands, key=lambda c: (c[2], c[3]))
        out[key] = (total, source)
    return out


def _collect_debt_wide(cf):
    """{(period_end, filed): (value, source)} — the wide
    leverage construction of the 2026-11-22 measurement build
    (fundamentals_balance_sheet_debt_wide_pit.csv), applied to
    the same single-filing-version grouping as _collect_debt:
    the shipped borrowings composite, plus commercial paper
    (components route only), plus per-type lease liabilities.

    Per lease type, the filer's own total tag wins whenever
    present and at least the current+noncurrent split sum;
    otherwise the split sum governs. Capital-lease tags
    substitute slot-by-slot for the finance type. When the
    combined LongTermDebtAndCapitalLeaseObligations concept is
    the winning noncurrent borrowings pick, finance leases are
    skipped (the combined line's name already covers them).
    A version carrying only leases still yields a (leases-only)
    value; a version with no wide component yields nothing."""
    versions = {}  # (end, filed, accn, form) -> {concept: val}
    for concept in _DEBT_WIDE_ALL:
        for fact in _facts(cf, concept, "USD"):
            form = fact.get("form", "")
            if form not in _FORMS_K:
                continue
            if fact.get("fp") not in ("FY", None, ""):
                continue
            end = _parse_date(fact.get("end"))
            filed = _parse_date(fact.get("filed"))
            if end is None or filed is None or filed < end:
                continue
            if fact.get("val") is None:
                continue
            versions.setdefault(
                (end, filed, str(fact.get("accn", "")), form),
                {})[concept] = float(fact.get("val"))

    def first(vals, clist):
        for c in clist:
            if c in vals:
                return vals[c], c
        return None, None

    def slot(vals, concept, straggler=None):
        if concept in vals:
            return vals[concept], concept
        if straggler and straggler in vals:
            return vals[straggler], straggler
        return None, None

    def lease_type(vals, tags, stragglers=(None, None, None)):
        tot_c, cur_c, non_c = tags
        s_tot, s_cur, s_non = stragglers
        tot, tot_n = slot(vals, tot_c, s_tot)
        cur, cur_n = slot(vals, cur_c, s_cur)
        non, non_n = slot(vals, non_c, s_non)
        split = None
        if cur is not None or non is not None:
            split = (cur or 0) + (non or 0)
        if tot is not None and (split is None or tot >= split):
            return tot, [tot_n]
        if split is not None:
            return split, [n for n in (cur_n, non_n) if n]
        return None, []

    per_key = {}
    for (end, filed, accn, form), vals in versions.items():
        borrow_cur, bc_name = first(vals, _DEBT_CURRENT)
        borrow_non, bn_name = first(vals, _DEBT_NONCURRENT_WIDE)
        borrow_fb, bf_name = first(vals, _DEBT_FALLBACK)
        cp, _cp_name = first(vals, _CP_CONCEPTS)
        combined_fired = bn_name == _COMBINED_NONCURRENT
        lease_op, op_names = lease_type(vals, _LEASE_OP)
        if combined_fired:
            lease_fin, fin_names = None, []
        else:
            lease_fin, fin_names = lease_type(
                vals, _LEASE_FIN,
                (_LEASE_CAP[0], _LEASE_CAP[1], _LEASE_CAP[2]))
        lease_part, lease_names = None, []
        if lease_op is not None or lease_fin is not None:
            lease_part = (lease_op or 0) + (lease_fin or 0)
            lease_names = op_names + fin_names
        wide, wnames = None, []
        if borrow_cur is not None or borrow_non is not None:
            wide = (borrow_cur or 0) + (borrow_non or 0) \
                + (cp or 0) + (lease_part or 0)
            wnames = [n for n in (bc_name, bn_name) if n] \
                + (["CommercialPaper"] if cp is not None else []) \
                + lease_names
        elif borrow_fb is not None:
            wide = borrow_fb + (lease_part or 0)
            wnames = ([bf_name] if bf_name else []) + lease_names
        elif lease_part is not None:
            wide = lease_part
            wnames = lease_names + ["leases-only"]
        if wide is None:
            continue
        per_key.setdefault((end, filed), []).append(
            (wide, "+".join(wnames), _form_rank(form), accn))
    out = {}
    for key, cands in per_key.items():
        total, source, _, _ = max(cands, key=lambda c: (c[2], c[3]))
        out[key] = (total, source)
    return out


def _extract(cf):
    """All as-filed version lists for one companyfacts payload."""
    oi = _collect_versions(cf, "OperatingIncomeLoss", "USD", "oi")
    sh = _collect_versions(cf, "CommonStockSharesOutstanding",
                           "shares", "instant")
    dil = _collect_versions(
        cf, "WeightedAverageNumberOfDilutedSharesOutstanding",
        "shares", "duration_fy")
    debt = _collect_debt(cf)
    debt_wide = _collect_debt_wide(cf)
    cash = _collect_versions_priority(cf, _CASH_CONCEPTS, "USD",
                                      "instant")
    assets = _collect_versions(cf, "Assets", "USD", "instant")
    ni = _collect_versions_priority(cf, _NI_CONCEPTS, "USD",
                                    "duration_fy")

    def ser(versions):
        return [{"period_end": e.isoformat(), "filed": f.isoformat(),
                 "val": v} for (e, f), v in sorted(versions.items())]

    return {
        "oi": ser(oi),
        "shares": ser(sh),
        "diluted": ser(dil),
        "debt": [{"period_end": e.isoformat(), "filed": f.isoformat(),
                  "val": v, "source": s}
                 for (e, f), (v, s) in sorted(debt.items())],
        "debt_wide": [{"period_end": e.isoformat(),
                       "filed": f.isoformat(), "val": v, "source": s}
                      for (e, f), (v, s) in
                      sorted(debt_wide.items())],
        "cash": [{"period_end": e.isoformat(), "filed": f.isoformat(),
                  "val": v, "source": s}
                 for (e, f), (v, s) in sorted(cash.items())],
        "assets": ser(assets),
        "ni": [{"period_end": e.isoformat(), "filed": f.isoformat(),
                "val": v, "source": s}
               for (e, f), (v, s) in sorted(ni.items())],
    }


def _splits(ticker):
    """[(ex_date, ratio)] from yfinance split history; [] on failure.
    Cached inside the facts cache entry by the caller."""
    try:
        import yfinance as yf
        series = yf.Ticker(str(ticker)).splits
        out = []
        if series is not None and len(series):
            for ts, ratio in series.items():
                d = _parse_date(ts)
                r = float(ratio)
                if d is not None and r > 0:
                    out.append((d, r))
        return out
    except Exception:
        return []


def _fetch_extraction(ticker):
    """Fetch companyfacts for the ticker's CIK (+ legacy lineage)
    and extract the version lists + split events. Raises on total
    failure so the caller can leave the name uncovered."""
    cik = _cik_map().get(_ticker_key(ticker))
    if not cik:
        raise ValueError(f"no CIK for {ticker}")
    merged = {"oi": [], "shares": [], "diluted": [], "debt": [],
              "debt_wide": [], "cash": [], "assets": [], "ni": []}
    got = False
    for use_cik in _lineage_ciks(cik):
        try:
            cf = _get("https://data.sec.gov/api/xbrl/companyfacts/"
                      f"CIK{use_cik}.json").json()
        except Exception:
            continue
        part = _extract(cf)
        for field in merged:
            merged[field].extend(part[field])
        got = True
    if not got:
        raise ValueError(f"companyfacts fetch failed for {ticker}")
    merged["splits"] = [{"date": d.isoformat(), "ratio": r}
                        for d, r in _splits(ticker)]
    merged["cik"] = cik
    return merged


def _extraction(ticker):
    """Cached extraction for a ticker (7-day TTL on the fetch)."""
    key = _ticker_key(ticker)
    cache = _read_json(_CACHE_PATH)
    entry = cache.get(key) if isinstance(cache, dict) else None
    if isinstance(entry, dict) and isinstance(entry.get("data"), dict):
        # Entries written before the balance-sheet ratios shipped
        # (2026-11-21) lack the cash/assets version lists,
        # entries written before the wide leverage construction
        # shipped (2026-11-22) lack the debt_wide list, and
        # entries written before the net-income yield shipped
        # (2026-11-22) lack the ni list; treat them as stale so
        # the new fields are never silently empty.
        if not all(k in entry["data"]
                   for k in ("cash", "assets", "debt_wide", "ni")):
            entry = None
        else:
            fetched = _parse_date(entry.get("fetched_date"))
            if fetched is not None and (_today() - fetched).days < _FACTS_TTL_DAYS:
                return entry["data"]
    data = _fetch_extraction(ticker)
    if not isinstance(cache, dict):
        cache = {}
    cache[key] = {"fetched_date": _today().isoformat(), "data": data}
    _write_json(_CACHE_PATH, cache)
    return data


def _asof(versions, as_of):
    """Per-field latest-non-null as-filed: the version with the
    greatest filed_date on or before as_of (ties: greatest
    period_end) — the store tables' as-of rule."""
    best = None
    for v in versions or []:
        filed = _parse_date(v.get("filed"))
        end = _parse_date(v.get("period_end"))
        if filed is None or end is None or filed > as_of:
            continue
        if best is None or (filed, end) > (best[0], best[1]):
            best = (filed, end, v)
    if best is None:
        return None
    return best[2]


def _compute(ticker, price, as_of):
    if price is None or not (float(price) > 0):
        return {"ey": None}
    data = _extraction(ticker)
    oi_v = _asof(data.get("oi"), as_of)
    if oi_v is None:
        return {"ey": None}
    sh_v = _asof(data.get("shares"), as_of)
    sh_source = "shares_outstanding"
    if sh_v is None:
        sh_v = _asof(data.get("diluted"), as_of)
        sh_source = "shares_diluted_wavg"
    if sh_v is None:
        return {"ey": None}
    shares = float(sh_v["val"])
    if not (shares > 0):
        return {"ey": None}
    sh_end = _parse_date(sh_v["period_end"])
    factor = 1.0
    for s in data.get("splits") or []:
        d = _parse_date(s.get("date"))
        if d is not None and sh_end is not None and d > sh_end:
            factor *= float(s["ratio"])
    shares_today = shares * factor
    debt_v = _asof(data.get("debt"), as_of)
    debt_missing = debt_v is None
    debt = 0.0 if debt_missing else float(debt_v["val"])
    mcap = shares_today * float(price)
    ev = mcap + debt
    if not (ev > 0):
        return {"ey": None}
    ey = float(oi_v["val"]) / ev
    if abs(ey) > 1.0:
        return {"ey": None}
    return {
        "ey": ey,
        "oi": float(oi_v["val"]),
        "oi_filed": oi_v["filed"],
        "oi_period_end": oi_v["period_end"],
        "shares": shares,
        "shares_today": shares_today,
        "split_factor": factor,
        "shares_source": sh_source,
        "shares_filed": sh_v["filed"],
        "shares_period_end": sh_v["period_end"],
        "debt": debt,
        "debt_missing": bool(debt_missing),
        "debt_filed": (debt_v["filed"] if debt_v else None),
        "debt_source": (debt_v.get("source") if debt_v else None),
        "market_cap": mcap,
        "ev": ev,
        "price": float(price),
        "as_of": as_of.isoformat(),
        "cik": data.get("cik"),
    }


def _compute_ni(ticker, price, as_of):
    """ni_ev = net income / enterprise value, the worst-slate
    test's construction: net income per-field latest-non-null
    as-filed (balance-sheet build rules), EV exactly the EY
    computation's (shares restated to today's units x price +
    the narrow debt composite, missing debt treated as 0).
    Deliberately NO |ni_ev| > 1 guard — the frozen test applied
    none. Not computable (ni_ev=None) when the CIK is unknown,
    net income or shares are unavailable, shares <= 0, or
    EV <= 0."""
    if price is None or not (float(price) > 0):
        return {"ni_ev": None}
    data = _extraction(ticker)
    ni_v = _asof(data.get("ni"), as_of)
    if ni_v is None:
        return {"ni_ev": None}
    sh_v = _asof(data.get("shares"), as_of)
    sh_source = "shares_outstanding"
    if sh_v is None:
        sh_v = _asof(data.get("diluted"), as_of)
        sh_source = "shares_diluted_wavg"
    if sh_v is None:
        return {"ni_ev": None}
    shares = float(sh_v["val"])
    if not (shares > 0):
        return {"ni_ev": None}
    sh_end = _parse_date(sh_v["period_end"])
    factor = 1.0
    for s in data.get("splits") or []:
        d = _parse_date(s.get("date"))
        if d is not None and sh_end is not None and d > sh_end:
            factor *= float(s["ratio"])
    shares_today = shares * factor
    debt_v = _asof(data.get("debt"), as_of)
    debt_missing = debt_v is None
    debt = 0.0 if debt_missing else float(debt_v["val"])
    mcap = shares_today * float(price)
    ev = mcap + debt
    if not (ev > 0):
        return {"ni_ev": None}
    return {
        "ni_ev": float(ni_v["val"]) / ev,
        "ni": float(ni_v["val"]),
        "ni_filed": ni_v["filed"],
        "ni_period_end": ni_v["period_end"],
        "ni_source": ni_v.get("source"),
        "shares": shares,
        "shares_today": shares_today,
        "split_factor": factor,
        "shares_source": sh_source,
        "shares_filed": sh_v["filed"],
        "shares_period_end": sh_v["period_end"],
        "debt": debt,
        "debt_missing": bool(debt_missing),
        "debt_filed": (debt_v["filed"] if debt_v else None),
        "debt_source": (debt_v.get("source") if debt_v else None),
        "market_cap": mcap,
        "ev": ev,
        "price": float(price),
        "as_of": as_of.isoformat(),
        "cik": data.get("cik"),
    }


def _asof_min_period(versions, as_of, min_period_end):
    """_asof over the versions whose period_end is on/after
    min_period_end (the accruals build's coverage window)."""
    kept = []
    for v in versions or []:
        end = _parse_date(v.get("period_end"))
        if end is not None and end >= min_period_end:
            kept.append(v)
    return _asof(kept, as_of)


def _compute_bs_ratios(ticker, as_of, debt_construction="wide"):
    if debt_construction not in ("wide", "narrow"):
        debt_construction = "wide"
    data = _extraction(ticker)
    out = {"cash_share": None, "leverage": None,
           "debt_construction": debt_construction,
           "as_of": as_of.isoformat(), "cik": data.get("cik")}
    # cash_share — the accruals build's constructions: cash and
    # total assets each selected per-field as-of from that file's
    # versions (period ends inside its 2015+ coverage window).
    cash_v = _asof_min_period(data.get("cash"), as_of,
                              _MIN_ACCRUALS_PERIOD_END)
    a_acc_v = _asof_min_period(data.get("assets"), as_of,
                               _MIN_ACCRUALS_PERIOD_END)
    if cash_v is not None and a_acc_v is not None:
        assets = float(a_acc_v["val"])
        if assets > 0:
            out["cash_share"] = float(cash_v["val"]) / assets
            out["cash"] = float(cash_v["val"])
            out["cash_filed"] = cash_v["filed"]
            out["cash_period_end"] = cash_v["period_end"]
            out["cash_source"] = cash_v.get("source")
            out["cash_assets"] = assets
            out["cash_assets_filed"] = a_acc_v["filed"]
            out["cash_assets_period_end"] = a_acc_v["period_end"]
    # leverage — total debt over total assets, each per-field
    # as-of, no period window. The debt composite is the wide
    # construction by default (2026-11-22 ship: borrowings +
    # commercial paper + leases, per the measurement build);
    # debt_construction="narrow" selects the shipped borrowings
    # composite for A/B comparison. No debt version on file
    # under the selected construction means NO leverage (the
    # test's NaN), never zero.
    debt_versions = (data.get("debt_wide")
                     if debt_construction == "wide"
                     else data.get("debt"))
    debt_v = _asof(debt_versions, as_of)
    a_bs_v = _asof(data.get("assets"), as_of)
    if debt_v is not None and a_bs_v is not None:
        assets = float(a_bs_v["val"])
        if assets > 0:
            out["leverage"] = float(debt_v["val"]) / assets
            out["debt"] = float(debt_v["val"])
            out["debt_filed"] = debt_v["filed"]
            out["debt_period_end"] = debt_v["period_end"]
            out["debt_source"] = debt_v.get("source")
            out["lev_assets"] = assets
            out["lev_assets_filed"] = a_bs_v["filed"]
            out["lev_assets_period_end"] = a_bs_v["period_end"]
    return out


def balance_sheet_ratio(ticker, as_of=None, debt_construction="wide"):
    """Point-in-time cash_share and leverage for ``ticker``.

    Returns a dict with ``cash_share`` and ``leverage`` (floats)
    plus the audit detail (component values and filing vintages);
    each ratio is None whenever it could not be determined, and a
    None ratio never bars (fail-soft, matching the tests). The
    leverage debt composite is the wide construction unless
    debt_construction="narrow" (the pre-2026-11-22 shipped
    composite, kept for A/B comparison). Never raises."""
    try:
        as_of_date = _parse_date(as_of) if as_of is not None else _today()
        if as_of_date is None or not str(ticker or "").strip():
            return {"cash_share": None, "leverage": None}
        return _compute_bs_ratios(str(ticker).strip(), as_of_date,
                                  debt_construction=debt_construction)
    except Exception:
        return {"cash_share": None, "leverage": None}


def balance_sheet_ratios(tickers, as_of=None, debt_construction="wide"):
    """Batch form: {ticker: balance_sheet_ratio(...)}. Shares the
    extraction cache with the earnings-yield calls, so a name
    already fetched for its EY costs no extra SEC request. Never
    raises; failed names map to both ratios None."""
    tickers = [str(t) for t in tickers]
    if not tickers:
        return {}
    as_of_date = _parse_date(as_of) if as_of is not None else _today()

    def one(t):
        return t, balance_sheet_ratio(
            t, as_of=as_of_date, debt_construction=debt_construction)

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, tickers))


def earnings_yield(ticker, price, as_of=None):
    """Point-in-time earnings yield for ``ticker`` at ``price``.

    Returns a dict with ``ey`` (float) plus the full audit detail
    (operating income and its filing vintage, the balance-sheet
    vintage, shares, debt, market cap, EV), or ``{"ey": None}``
    whenever the yield could not be determined. Never raises."""
    try:
        as_of_date = _parse_date(as_of) if as_of is not None else _today()
        if as_of_date is None or not str(ticker or "").strip():
            return {"ey": None}
        return _compute(str(ticker).strip(), price, as_of_date)
    except Exception:
        return {"ey": None}


def earnings_yields(tickers, prices, as_of=None):
    """Batch form: {ticker: earnings_yield(...)} for a price map.
    Fetches run on a small thread pool under the shared SEC pace
    cap. Never raises; failed names map to {"ey": None}."""
    tickers = [str(t) for t in tickers]
    if not tickers:
        return {}
    as_of_date = _parse_date(as_of) if as_of is not None else _today()

    def one(t):
        return t, earnings_yield(t, (prices or {}).get(t),
                                 as_of=as_of_date)

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, tickers))


def net_income_ev(ticker, price, as_of=None):
    """Point-in-time net income / enterprise value for ``ticker``
    at ``price`` (the gate's ni_ev band leg, shipped 2026-11-22).

    Returns a dict with ``ni_ev`` (float) plus the full audit
    detail (net income and its filing vintage, shares, debt,
    market cap, EV), or ``{"ni_ev": None}`` whenever the value
    could not be determined — and an undetermined value never
    bars (fail-soft, matching the test). Never raises."""
    try:
        as_of_date = _parse_date(as_of) if as_of is not None else _today()
        if as_of_date is None or not str(ticker or "").strip():
            return {"ni_ev": None}
        return _compute_ni(str(ticker).strip(), price, as_of_date)
    except Exception:
        return {"ni_ev": None}


def net_income_evs(tickers, prices, as_of=None):
    """Batch form: {ticker: net_income_ev(...)} for a price map.
    Shares the extraction cache with the earnings-yield calls,
    so a name already fetched for its EY costs no extra SEC
    request. Never raises; failed names map to {"ni_ev": None}."""
    tickers = [str(t) for t in tickers]
    if not tickers:
        return {}
    as_of_date = _parse_date(as_of) if as_of is not None else _today()

    def one(t):
        return t, net_income_ev(t, (prices or {}).get(t),
                                as_of=as_of_date)

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, tickers))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SEC earnings-yield feed")
    parser.add_argument("ticker")
    parser.add_argument("price", type=float)
    parser.add_argument("--as-of")
    args = parser.parse_args()
    print(json.dumps(earnings_yield(args.ticker, args.price,
                                    args.as_of), indent=1))
