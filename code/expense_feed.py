#!/usr/bin/env python3
"""Mechanical SEC prospectus expense-ratio feed for the Mint screener.

Phase A takes expense ratios from yfinance, which returns None for
essentially every researched fund (0 of 120 on 2026-11-11), so
``build_etf_scores`` z-scores a filled constant and the expense leg of
the live base_score and character is decorative.  Phase 2 of the ETF
rank audit (``etf_rank_audit_2026-11-11``) built point-in-time expense
from SEC filings instead — 90.7% coverage on 7,055 eligible fund-dates,
and z_expense was the first leg in the audit positive in both halves —
but that table never reached the daily bundle.  This feed
productionizes the audited construction so Phase A can consume it the
way it consumes form4_feed and exec_feed: a per-ticker cache, values
stamped mechanically from filings, and a hard never-raise contract
(any failure returns ``expense=None`` and the caller falls back to
today's behaviour — the yfinance value if any, else the constant
fill).

Construction (identical to the audited build — EXP F's machinery,
reused unchanged by Phase 2; do not redesign):

- Ticker -> (CIK, seriesId, classId) from EDGAR's own
  ``company_tickers_mf.json``, plus EXP F's documented manual
  additions (PVAL; SPY / DIA / MDY as CIK-level unit trusts; QQQ at
  CIK level, pre-conversion; HACK's post-reorganization series).
- Per identity, EDGAR browse-edgar atom feeds over forms 497K,
  485BPOS, 497, 485APOS, walked in the audited source-priority order
  (class 497K, series 497K, CIK 497K, class 485BPOS, series 485BPOS,
  CIK 485BPOS, class 497, CIK 497, class 485APOS, CIK 485APOS).  A
  class feed lists only that class's filings; series / CIK feeds are
  guarded by a ticker-identity check on the document.
- The fee table is parsed from the filing's largest .htm documents:
  "Total Annual Fund Operating Expenses" — the net-after-waiver figure
  when the prospectus prints one, else a net-annual figure, else the
  gross total.  Candidate values are accepted only in
  [0.005%, 2.50%].
- Strict point-in-time: the ratio comes from the most recent filing
  accepted strictly before ``as_of``.  EXP F's post-date fallback
  route is NOT used (Phase 2 dropped it, and a live feed has no need
  of it).

Known holes inherited from the audited build: SPY, QQQ, DIA and MDY
return None — unit-investment-trust prospectuses do not carry the
fee-table phrasing the parser keys on (EXP F failed on the same four,
Phase 2 confirmed).  Tickers absent from company_tickers_mf.json
(~128 funds in the audit population, overwhelmingly since delisted)
also return None.

Cache TTL is 30 days (form4_feed / exec_feed use 7).  Their values are
event-window counts that shift daily; an expense ratio is a step
function republished on the prospectus's annual cycle (497K /
485BPOS), and a mid-year fee change arrives via a supplement filing —
so a 30-day TTL bounds staleness at 30 days while cutting SEC load
roughly fourfold, with no plausible path to a materially stale value:
funds must file the new figure before they may charge it.

SEC discipline mirrors the sibling feeds: the declared user agent,
request starts paced at <=5/second, and a 403/429 is a hard stop for
the batch (the stop is latched; remaining tickers return None).
"""

import html as htmlmod
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import requests

USER_AGENT = "MintResearch test@example.com"
# The environment overrides exist so sandbox rehearsals can prove the
# pipeline's failure behaviour (bad UA / isolated caches) without
# editing production code.  In production none is set and the UA is
# exactly the declared string above.
_USER_AGENT = os.environ.get("EXPENSE_USER_AGENT", USER_AGENT)
_HERE = os.path.dirname(os.path.abspath(__file__))
_CACHE_PATH = os.environ.get(
    "EXPENSE_CACHE_PATH", os.path.join(_HERE, "expense_cache.json"))
_MAP_CACHE_PATH = os.environ.get(
    "EXPENSE_MAP_CACHE_PATH",
    os.path.join(_HERE, "expense_map_cache.json"))
_MIN_REQUEST_INTERVAL = 0.21  # a shade over 5 requests/second
_TTL_DAYS = 30                # see module docstring for the argument
_MAP_TTL_DAYS = 30
_last_request_at = 0.0
_request_lock = threading.Lock()
_thread_local = threading.local()
_cache_write_lock = threading.Lock()
_hard_stop = threading.Event()
_atom_memo = {}
_parse_memo = {}
_memo_lock = threading.Lock()


class _HardStop(Exception):
    """SEC answered 403/429: stop the batch (audit discipline)."""


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


def _get(url, max_bytes=10_000_000):
    """One paced SEC GET with the declared user agent.

    Mirrors the sibling feeds (request *starts* reserved under a lock
    at <=5/second, responses awaited outside it), with EXP F's
    additions: streamed reads capped at ``max_bytes``, non-200 returns
    None, and a 403/429 latches the batch hard stop and raises
    _HardStop.
    """
    global _last_request_at
    if _hard_stop.is_set():
        raise _HardStop("SEC hard stop already latched for this batch")
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
        headers={"User-Agent": _USER_AGENT},
        timeout=90,
        stream=True,
    )
    if response.status_code in (403, 429):
        response.close()
        _hard_stop.set()
        raise _HardStop(
            f"HTTP {response.status_code} at {url} — SEC hard stop")
    if response.status_code != 200:
        response.close()
        return None
    try:
        buf = b""
        for chunk in response.iter_content(chunk_size=65536):
            buf += chunk
            if len(buf) >= max_bytes:
                break
    finally:
        response.close()
    return buf


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


# ------------------------------------------------------------ fund map
def _fund_map():
    """Ticker -> {cik, seriesId, classId, map_src}, disk-cached.

    The map is EDGAR's company_tickers_mf.json plus EXP F's manual
    additions, each grounded in an SEC source (see build_expense.py in
    etf_expense_2026-11-11 for the provenance notes).
    """
    cached = _read_json(_MAP_CACHE_PATH)
    fetched = _parse_date(cached.get("fetched_date"))
    mapping = cached.get("tickers") if isinstance(cached, dict) else None
    if (isinstance(mapping, dict) and mapping and fetched is not None
            and abs((_today() - fetched).days) <= _MAP_TTL_DAYS):
        return mapping
    raw = _get("https://www.sec.gov/files/company_tickers_mf.json")
    if raw is None:
        raise ValueError("company_tickers_mf.json unavailable")
    mf = json.loads(raw)
    mapping = {}
    for entry in mf["data"]:
        ticker = (entry[3] or "").upper()
        if ticker and ticker not in mapping:
            mapping[ticker] = {
                "cik": int(entry[0]), "seriesId": entry[1],
                "classId": entry[2], "map_src": "company_tickers_mf.json"}
    raw2 = _get("https://www.sec.gov/files/company_tickers.json")
    ct = {}
    if raw2 is not None:
        ct = {v["ticker"]: int(v["cik_str"])
              for v in json.loads(raw2).values()}
    # Manual / UIT additions (EXP F, grounded in its results.md).
    mapping["PVAL"] = {
        "cik": 1845809, "seriesId": "S000071710", "classId": None,
        "map_src": "manual: prior sprint EDGAR header verification"}
    # PBPH (Portfolio Building Block World Pharma and Biotech Index
    # ETF, Tidal Trust IV) is likewise absent from EDGAR's published
    # map; the series identity was verified against a live NPORT-P
    # filing header (accession 0002000324-26-004430, period
    # 2026-07-31, filed 2026-09-21) whose series name matches the
    # fund's bundle name exactly.  Added 2026-11-12 alongside the
    # aum_feed ship (PBPH's expense leg was the only one of the
    # seven unmapped Nov 12 funds reachable this way; the other six
    # are commodity pools/trusts that file 10-K/424B3, not N-PORT).
    mapping["PBPH"] = {
        "cik": 2043390, "seriesId": "S000096308", "classId": None,
        "map_src": "manual: EDGAR NPORT-P header verification "
                   "(Tidal Trust IV; accession 0002000324-26-004430)"}
    for ticker in ("SPY", "DIA", "MDY"):
        if ticker in ct:
            mapping[ticker] = {
                "cik": ct[ticker], "seriesId": None, "classId": None,
                "map_src": "company_tickers.json (unit trust, CIK-level)"}
    mapping["QQQ"] = {
        "cik": 1067839, "seriesId": None, "classId": None,
        "map_src": "CIK-level: pre-2026 Invesco QQQ Trust, Series 1"}
    mapping["HACK"] = {
        "cik": 1633061, "seriesId": "S000082278",
        "classId": "C000245564",
        "map_src": "sec.gov series list for Amplify ETF Trust "
                   "(reorganized 2024-01-29 from ETF Managers Trust)"}
    if not mapping:
        raise ValueError("SEC mutual-fund ticker map was empty")
    _write_json(_MAP_CACHE_PATH,
                {"fetched_date": _today().isoformat(), "tickers": mapping})
    return mapping


# ------------------------------------------------------------ atom feeds
def _atom_entries(ident, form):
    key = f"{ident}|{form}"
    with _memo_lock:
        if key in _atom_memo:
            return _atom_memo[key]
    url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
           f"&CIK={ident}&type={form}&dateb=&owner=include&count=100"
           "&output=atom")
    raw = _get(url)
    out = []
    if raw:
        txt = raw.decode("utf-8", "ignore")
        for entry in txt.split("<entry>")[1:]:
            acc = re.search(
                r"<accession-number>([^<]+)</accession-number>", entry)
            filed = re.search(r"<filing-date>([^<]+)</filing-date>", entry)
            if acc and filed:
                out.append({"accession": acc.group(1),
                            "date": filed.group(1), "form": form})
    out.sort(key=lambda e: (e["date"], e["accession"]), reverse=True)
    with _memo_lock:
        _atom_memo[key] = out
    return out


def _candidate_filings(fm, form, ident_kind):
    ident = {"class": fm.get("classId"), "series": fm.get("seriesId"),
             "cik": str(fm["cik"])}.get(ident_kind)
    if not ident:
        return []
    return _atom_entries(ident, form)


# ------------------------------------------------------------ fee parsing
_NUM = re.compile(r"(\d+\.\d+|\.\d+)\s*%?")


def _strip_html(raw):
    txt = raw.decode("utf-8", "ignore")
    txt = re.sub(r"(?is)<(script|style).*?</\1>", " ", txt)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = htmlmod.unescape(txt)
    txt = txt.replace("\xa0", " ")
    return re.sub(r"\s+", " ", txt)


def _first_num_after(txt, start):
    seg = txt[start:start + 260]
    match = _NUM.search(seg)
    if not match:
        return None
    value = float(match.group(1))
    return value if 0.005 <= value <= 2.50 else None


def _parse_expense(txt):
    """(expense_pct, basis) or (None, None).  Net-after-waiver wins."""
    low = txt.lower()
    net = None
    for match in re.finditer(
            r"total\s+annual\s+(?:fund\s+)?operating\s+expenses\s+after",
            low):
        value = _first_num_after(txt, match.end())
        if value is not None:
            net = value
            break
    if net is None:
        for match in re.finditer(
                r"net\s+annual\s+(?:fund\s+)?operating\s+expenses"
                r"(?!\s+after)", low):
            value = _first_num_after(txt, match.end())
            if value is not None:
                net = value
                break
    if net is not None:
        return net, "net_after_waiver"
    for match in re.finditer(
            r"total\s+annual\s+(?:fund\s+)?operating\s+expenses"
            r"(?!\s+after)", low):
        value = _first_num_after(txt, match.end())
        if value is not None:
            return value, "gross_total"
    return None, None


def _doc_matches(txt, ticker, name):
    head = txt[:20000]
    pat = (rf"(?:\(|—|–|-|\|)\s*{re.escape(ticker)}\b"
           rf"|\b{re.escape(ticker)}\s*\)")
    if re.search(pat, head):
        return True
    if name and name.upper()[:25] in txt[:8000].upper():
        return True
    return False


def _parse_filing(cik, filing, require_ticker=None, fund_name=None):
    accession = filing["accession"]
    ckey = accession + (f"|{require_ticker}" if require_ticker else "")
    with _memo_lock:
        if ckey in _parse_memo:
            return _parse_memo[ckey]
        if accession in _parse_memo and not require_ticker:
            return _parse_memo[accession]
    accn = accession.replace("-", "")
    result = {"expense_pct": None, "basis": None, "doc": None,
              "fail": None}
    raw = _get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/"
               f"index.json")
    if not raw:
        result["fail"] = "index_unavailable"
    else:
        directory = json.loads(raw)
        candidates = []
        for item in directory["directory"]["item"]:
            name = item["name"]
            lowered = name.lower()
            if not lowered.endswith((".htm", ".html", ".txt")):
                continue
            if re.match(r"r\d+\.htm", lowered) or "index" in lowered:
                continue
            try:
                size = int(item.get("size") or 0)
            except Exception:
                size = 0
            candidates.append((size, name))
        candidates.sort(reverse=True)
        if not candidates:
            result["fail"] = "no_htm_doc"
        for _size, doc in candidates[:3]:
            raw_doc = _get(
                f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/"
                f"{doc}")
            if not raw_doc:
                continue
            txt = _strip_html(raw_doc)
            if require_ticker and not _doc_matches(
                    txt, require_ticker, fund_name):
                result["fail"] = "doc_identity_mismatch"
                continue
            value, basis = _parse_expense(txt)
            if value is not None:
                result = {"expense_pct": value, "basis": basis,
                          "doc": doc, "fail": None}
                break
        if result["expense_pct"] is None and result["fail"] is None:
            result["fail"] = "fee_table_not_parsed"
    with _memo_lock:
        _parse_memo[ckey] = result
    return result


# ------------------------------------------------------------ per-ticker PIT
# Source priority (audited): the class's own 497K first (a class atom
# feed lists only that class's filings), then series / CIK levels —
# those two are guarded by a ticker-identity check on the document,
# because a series feed mixes share classes and a CIK feed mixes funds.
_SOURCES = [("class", "497K"), ("series", "497K"), ("cik", "497K"),
            ("class", "485BPOS"), ("series", "485BPOS"),
            ("cik", "485BPOS"), ("class", "497"), ("cik", "497"),
            ("class", "485APOS"), ("cik", "485APOS")]


def _undetermined(fail=None):
    return {"expense": None, "expense_pct": None, "basis": None,
            "filing_date": None, "accession": None, "form": None,
            "source": None, "fail": fail}


def _compute(ticker, as_of, name=None):
    fm = _fund_map().get(_ticker_key(ticker))
    if fm is None:
        return _undetermined(fail="ticker_not_mapped")
    as_of_iso = as_of.isoformat()
    saw_any = False
    for kind, form in _SOURCES:
        if kind == "class" and not fm.get("classId"):
            continue
        if kind == "series" and not fm.get("seriesId"):
            continue
        entries = _candidate_filings(fm, form, kind)
        before = [f for f in entries if f["date"] < as_of_iso]
        if before:
            saw_any = True
        for filing in before[:4]:
            require = ticker if kind in ("series", "cik") else None
            parsed = _parse_filing(fm["cik"], filing,
                                   require_ticker=require,
                                   fund_name=name)
            if parsed["expense_pct"] is not None:
                return {
                    "expense": parsed["expense_pct"] / 100.0,
                    "expense_pct": parsed["expense_pct"],
                    "basis": parsed["basis"],
                    "filing_date": filing["date"],
                    "accession": filing["accession"],
                    "form": filing["form"],
                    "source": kind,
                    "fail": None,
                }
    return _undetermined(
        fail="no_parseable_pre_asof_filing" if saw_any
        else "no_pre_asof_filing")


def _cache_lookup(cache, key, as_of_date):
    cached = cache.get(key) if isinstance(cache, dict) else None
    if not isinstance(cached, dict):
        return None
    cached_as_of = _parse_date(cached.get("as_of"))
    result = cached.get("result")
    if (cached_as_of is not None and isinstance(result, dict)
            and abs((as_of_date - cached_as_of).days) <= _TTL_DAYS
            and result.get("expense") is not None):
        return {
            "expense": float(result["expense"]),
            "expense_pct": (float(result["expense_pct"])
                            if result.get("expense_pct") is not None
                            else None),
            "basis": result.get("basis"),
            "filing_date": result.get("filing_date"),
            "accession": result.get("accession"),
            "form": result.get("form"),
            "source": result.get("source"),
            "fail": None,
        }
    return None


def _cache_store(key, as_of_date, result):
    with _cache_write_lock:
        cache = _read_json(_CACHE_PATH)
        if not isinstance(cache, dict):
            cache = {}
        cache[key] = {
            "as_of": as_of_date.isoformat(),
            "fetched_date": _today().isoformat(),
            "result": result,
        }
        _write_json(_CACHE_PATH, cache)


def expense_ratio(ticker, as_of=None, name=None):
    """Return the filings-based expense ratio for ``ticker``.

    ``expense`` is the decimal ratio (0.0059 = 0.59%) from the most
    recent prospectus filing accepted strictly before ``as_of``
    (default: today), or None whenever it could not be determined.
    This function never raises into the screener pipeline.
    """
    try:
        as_of_date = _parse_date(as_of) if as_of is not None else _today()
        if as_of_date is None:
            return _undetermined()
        key = _ticker_key(ticker)
        if not key:
            return _undetermined()
        hit = _cache_lookup(_read_json(_CACHE_PATH), key, as_of_date)
        if hit is not None:
            return hit
        if _hard_stop.is_set():
            return _undetermined(fail="sec_hard_stop")
        result = _compute(key, as_of_date, name=name)
        if result.get("expense") is not None:
            _cache_store(key, as_of_date, result)
        return result
    except _HardStop:
        return _undetermined(fail="sec_hard_stop")
    except Exception:
        return _undetermined()


def expense_ratios(tickers, as_of=None, names=None):
    """Batch form for the Phase A wiring: {TICKER: decimal-or-None}.

    One feed call per distinct ticker (cache-served when fresh),
    computed on a small thread pool over the paced getter.  Never
    raises; once a 403/429 latches the hard stop, the remaining
    tickers return None without further SEC traffic.
    """
    try:
        as_of_date = _parse_date(as_of) if as_of is not None else _today()
        if as_of_date is None:
            return {}
        names = names or {}
        keys = []
        seen = set()
        for ticker in tickers or []:
            key = _ticker_key(ticker)
            if key and key not in seen:
                seen.add(key)
                keys.append(key)
        if not keys:
            return {}
        cache = _read_json(_CACHE_PATH)
        out = {}
        todo = []
        for key in keys:
            hit = _cache_lookup(cache, key, as_of_date)
            if hit is not None:
                out[key] = hit["expense"]
            else:
                todo.append(key)
        if todo and not _hard_stop.is_set():
            def _one(key):
                try:
                    if _hard_stop.is_set():
                        return key, None
                    result = _compute(key, as_of_date,
                                      name=names.get(key))
                    if result.get("expense") is not None:
                        _cache_store(key, as_of_date, result)
                        return key, float(result["expense"])
                    return key, None
                except Exception:
                    return key, None

            with ThreadPoolExecutor(max_workers=8) as pool:
                for key, value in pool.map(_one, todo):
                    out[key] = value
        else:
            for key in todo:
                out[key] = None
        return {key: out.get(key) for key in keys}
    except Exception:
        return {}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="SEC prospectus expense-ratio feed")
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--as-of")
    args = parser.parse_args()
    print(json.dumps(
        {t: expense_ratio(t, args.as_of) for t in args.tickers},
        indent=1))
