#!/usr/bin/env python3
"""Mechanical SEC N-PORT fund-size (net assets) feed for the Mint screener.

The ETF Score's size leg was graded from yfinance's ``totalAssets``,
which on 2026-11-12 returned None for VO and VTI (a transient
whole-info fetch miss that day) and is elsewhere stale or simply the
wrong fund's number (Yahoo carries a similarly named DFA mutual
fund's $45.05B for DCOR, whose own N-PORT reports $3.34B).  The
leg-backfill investigation (``etf_upgrade_plan_2026-11-12/
leg_backfill/REPORT.md``, prototype validated against the research
store's N-PORT spine — 8 of 10 samples reproducing the spine's
accession and value to the cent) built the filings construction;
this feed productionizes it so the score's size leg can consume it
the way it consumes expense_feed: a per-ticker cache, values stamped
mechanically from filings, and a hard never-raise contract (any
failure returns ``aum=None`` and the caller falls back to the row's
own values — exactly today's behaviour).

Construction (the investigated prototype's, unchanged in substance):

- Ticker -> (CIK, seriesId) from expense_feed's fund map (EDGAR's
  ``company_tickers_mf.json`` plus its documented manual additions),
  so there is exactly one ticker-identity source for both feeds.
- Per registrant CIK, walk the registrant's NPORT-P / NPORT-P/A
  filings newest-first from data.sec.gov submissions; fetch the raw
  filing XML at the accession root (submissions' ``primaryDocument``
  carries the ``xslFormNPORT-P_X01/`` rendering prefix — the
  basename is the raw document); accept a filing only if its header
  ``seriesId`` equals the ticker's mapped series — identity is
  never negotiable, and there is deliberately NO CIK-level fallback
  (a trust's filings mix series).  The value is ``fundInfo``'s
  ``netAssets``: series-level total net assets, all share classes —
  the same definition the audited PIT table
  ``research_data/filings/etf_expense_aum_pit_2022-2025.csv`` used.
- If a wanted series is not found in the submissions ``recent``
  window (1,000 filings — high-volume registrants can slide past
  it), fall back to the submissions ``filings.files`` index files,
  newest first, until the series is found or the index budget is
  spent.
- Strict point-in-time: a filing supplies the value only when its
  filingDate is on or before ``as_of`` — a value is usable only
  once filed (the expense_feed stance).

Stamping: every determination carries the value's ``period_end``,
``filing_date``, ``accession`` and ``form`` alongside it, so any
grade the score prints from it can be re-derived by hand.

Cache TTL is 30 days (as in expense_feed): N-PORT values change
quarterly (period end plus the ~60-day filing lag), so a 30-day
TTL bounds staleness well inside one reporting cycle while keeping
SEC load low.  Submissions JSONs are cached per CIK alongside
(``aum_submissions/``) on the same TTL.

SEC discipline mirrors the sibling feeds: the declared user agent,
request starts paced at <=5/second, and a 403/429 is a hard stop
for the batch (the stop is latched; remaining tickers return None).
"""

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import requests

USER_AGENT = "MintResearch test@example.com"
# The environment overrides exist so sandbox rehearsals can prove
# the pipeline's failure behaviour (bad UA / isolated caches)
# without editing production code.  In production none is set and
# the UA is exactly the declared string above.
_USER_AGENT = os.environ.get("AUM_USER_AGENT", USER_AGENT)
_HERE = os.path.dirname(os.path.abspath(__file__))
_CACHE_PATH = os.environ.get(
    "AUM_CACHE_PATH", os.path.join(_HERE, "aum_cache.json"))
_SUBMISSIONS_DIR = os.environ.get(
    "AUM_SUBMISSIONS_DIR", os.path.join(_HERE, "aum_submissions"))
_MIN_REQUEST_INTERVAL = 0.21  # a shade over 5 requests/second
_TTL_DAYS = 30                # see module docstring for the argument
_SUBMISSIONS_TTL_DAYS = 30
_XML_PREFIX_BYTES = 500_000      # header + genInfo ride at the top
_XML_REFETCH_BYTES = 3_000_000   # only if fundInfo was cut off
_MAX_WALK_PER_CIK = 400
_MAX_INDEX_FILES = 6
_last_request_at = 0.0
_request_lock = threading.Lock()
_thread_local = threading.local()
_cache_write_lock = threading.Lock()
_hard_stop = threading.Event()


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

    Mirrors the sibling feeds (request *starts* reserved under a
    lock at <=5/second, responses awaited outside it): streamed
    reads capped at ``max_bytes``, non-200 returns None, and a
    403/429 latches the batch hard stop and raises _HardStop.
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
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
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
    """Ticker -> {cik, seriesId, ...}: expense_feed's map, shared.

    One identity source for both SEC feeds (EDGAR's
    company_tickers_mf.json plus expense_feed's documented manual
    additions).  Any failure yields {} — every ticker unmapped, the
    feed degrades to the caller's fallbacks.
    """
    try:
        import expense_feed
        mapping = expense_feed._fund_map()
        return mapping if isinstance(mapping, dict) else {}
    except Exception:
        return {}


# ------------------------------------------------------------ submissions
def _submissions(cik):
    """data.sec.gov submissions document for a CIK, disk-cached."""
    path = os.path.join(_SUBMISSIONS_DIR, f"{cik:010d}.json")
    cached = _read_json(path)
    fetched = _parse_date(cached.get("_fetched_date"))
    if (cached.get("filings") and fetched is not None
            and abs((_today() - fetched).days) <= _SUBMISSIONS_TTL_DAYS):
        return cached
    raw = _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    if raw is None:
        return cached if cached.get("filings") else {}
    try:
        data = json.loads(raw)
    except Exception:
        return cached if cached.get("filings") else {}
    if not isinstance(data, dict):
        return cached if cached.get("filings") else {}
    data["_fetched_date"] = _today().isoformat()
    _write_json(path, data)
    return data


def _submissions_index_file(cik, name):
    """One filings.files index document, disk-cached alongside."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name))
    path = os.path.join(_SUBMISSIONS_DIR, f"{cik:010d}_{safe}")
    cached = _read_json(path)
    fetched = _parse_date(cached.get("_fetched_date"))
    if (cached.get("form") and fetched is not None
            and abs((_today() - fetched).days) <= _SUBMISSIONS_TTL_DAYS):
        return cached
    raw = _get(f"https://data.sec.gov/submissions/{name}")
    if raw is None:
        return cached if cached.get("form") else {}
    try:
        data = json.loads(raw)
    except Exception:
        return cached if cached.get("form") else {}
    if not isinstance(data, dict):
        return cached if cached.get("form") else {}
    data["_fetched_date"] = _today().isoformat()
    _write_json(path, data)
    return data


def _entries_from_columns(columns):
    """NPORT-P / NPORT-P/A entries from a submissions column block."""
    forms = columns.get("form") or []
    out = []
    for i, form in enumerate(forms):
        if form not in ("NPORT-P", "NPORT-P/A"):
            continue
        try:
            out.append({
                "form": form,
                "filing_date": (columns.get("filingDate") or [])[i],
                "report_date": (columns.get("reportDate") or [])[i],
                "accession": (columns.get("accessionNumber") or [])[i],
                "doc": (columns.get("primaryDocument") or [])[i],
            })
        except Exception:
            continue
    return out


# ------------------------------------------------------------ XML parsing
_SERIES_RE = re.compile(
    r"<(?:\w+:)?seriesId>\s*(S\d{9})\s*</(?:\w+:)?seriesId>")
_FUNDINFO_RE = re.compile(
    r"<(?:\w+:)?fundInfo>(.*?)</(?:\w+:)?fundInfo>", re.S)
_NETASSETS_RE = re.compile(
    r"<(?:\w+:)?netAssets>\s*([0-9]+(?:\.[0-9]+)?)\s*"
    r"</(?:\w+:)?netAssets>")


def _parse_nport_prefix(raw):
    """(series_id, series net assets) from a filing XML head.

    Identity (seriesId) sits in the header / genInfo at the top of
    the document; the series' total net assets sit in fundInfo,
    immediately after genInfo — both long before the holdings.
    """
    if not raw:
        return None, None
    txt = raw.decode("utf-8", "ignore")
    series = None
    match = _SERIES_RE.search(txt[:200_000])
    if match:
        series = match.group(1)
    net_assets = None
    block = _FUNDINFO_RE.search(txt)
    if block:
        value = _NETASSETS_RE.search(block.group(1))
        if value:
            net_assets = float(value.group(1))
    return series, net_assets


def _fetch_filing(cik, entry, wanted):
    """(header_series_id, fundInfo net assets) for one filing.

    Reads a small prefix first (header + genInfo sit at the top of
    the document, before the holdings).  Only when the header names
    a wanted series but fundInfo was cut off does it refetch deeper.
    """
    accn = entry["accession"].replace("-", "")
    # submissions' primaryDocument for NPORT-P carries the XSL
    # rendering prefix (xslFormNPORT-P_X01/primary_doc.xml); the raw
    # XML sits at the accession root under the bare document name.
    doc = os.path.basename(entry["doc"])
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{doc}"
    raw = _get(url, max_bytes=_XML_PREFIX_BYTES)
    if raw is None:
        return None, None
    series, net_assets = _parse_nport_prefix(raw)
    if series is not None and series in wanted and net_assets is None:
        raw = _get(url, max_bytes=_XML_REFETCH_BYTES)
        if raw is not None:
            series, net_assets = _parse_nport_prefix(raw)
    return series, net_assets


# ------------------------------------------------------------ per-CIK walk
def _walk_entries(cik, entries, remaining, results, walked_accessions):
    """Walk entries newest-first, resolving series in ``remaining``.

    Mutates ``remaining`` / ``results`` / ``walked_accessions``;
    returns the number of filings fetched in this pass.
    """
    walked = 0
    for entry in entries:
        if not remaining or walked >= _MAX_WALK_PER_CIK:
            break
        if entry["accession"] in walked_accessions:
            continue
        walked_accessions.add(entry["accession"])
        walked += 1
        series, net_assets = _fetch_filing(
            cik, entry, wanted=set(remaining))
        if series is None or series not in remaining:
            continue
        ticker = remaining.pop(series)
        if net_assets is None:
            results[ticker] = {
                "aum": None, "period_end": entry["report_date"],
                "filing_date": entry["filing_date"],
                "accession": entry["accession"], "form": entry["form"],
                "source": "nport",
                "fail": "fundinfo_netassets_not_parsed"}
        else:
            results[ticker] = {
                "aum": float(net_assets),
                "period_end": entry["report_date"],
                "filing_date": entry["filing_date"],
                "accession": entry["accession"], "form": entry["form"],
                "source": "nport", "fail": None}
    return walked


def _walk_cik(cik, wanted, as_of):
    """wanted: {seriesId: ticker}. Returns {ticker: result-dict}."""
    results = {}
    remaining = dict(wanted)
    subs = _submissions(cik)
    filings = subs.get("filings") or {}
    recent = filings.get("recent") or {}

    def _pit(entries):
        out = []
        for entry in entries:
            filed = _parse_date(entry.get("filing_date"))
            if filed is not None and filed <= as_of:
                out.append(entry)
        out.sort(key=lambda e: (e["filing_date"], e["accession"]),
                 reverse=True)
        return out

    walked_accessions = set()
    _walk_entries(cik, _pit(_entries_from_columns(recent)),
                  remaining, results, walked_accessions)
    if remaining:
        # filings.files index fallback: the recent window holds the
        # newest 1,000 filings; older NPORT-P filings for this
        # registrant live in these index documents.
        index_files = filings.get("files") or []
        index_files = sorted(
            index_files,
            key=lambda f: str(f.get("filingTo") or ""), reverse=True)
        for index in index_files[:_MAX_INDEX_FILES]:
            if not remaining:
                break
            name = index.get("name")
            if not name:
                continue
            columns = _submissions_index_file(cik, name)
            if not columns:
                continue
            _walk_entries(cik, _pit(_entries_from_columns(columns)),
                          remaining, results, walked_accessions)
    for ticker in remaining.values():
        results[ticker] = _undetermined(fail="no_matching_nport_in_walk")
    return results


# ------------------------------------------------------------ per-ticker PIT
def _undetermined(fail=None):
    return {"aum": None, "period_end": None, "filing_date": None,
            "accession": None, "form": None, "source": None,
            "fail": fail}


def _cache_lookup(cache, key, as_of_date):
    cached = cache.get(key) if isinstance(cache, dict) else None
    if not isinstance(cached, dict):
        return None
    cached_as_of = _parse_date(cached.get("as_of"))
    result = cached.get("result")
    if (cached_as_of is not None and isinstance(result, dict)
            and abs((as_of_date - cached_as_of).days) <= _TTL_DAYS
            and result.get("aum") is not None):
        return {
            "aum": float(result["aum"]),
            "period_end": result.get("period_end"),
            "filing_date": result.get("filing_date"),
            "accession": result.get("accession"),
            "form": result.get("form"),
            "source": result.get("source") or "nport",
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


def net_assets(ticker, as_of=None, name=None):
    """Return the filings-based net assets for ``ticker``.

    The detail dict carries ``aum`` (series-level total net assets
    in dollars from the newest NPORT-P/NPORT-P/A filing whose
    header seriesId is the ticker's and whose filingDate is on or
    before ``as_of``, default today) plus the value's
    ``period_end``, ``filing_date``, ``accession`` and ``form``
    stamps — or ``aum=None`` with a ``fail`` reason whenever it
    could not be determined.  This function never raises into the
    screener pipeline.
    """
    batch = net_assets_batch([ticker], as_of=as_of, names=None)
    return batch.get(_ticker_key(ticker), _undetermined())


def net_assets_batch(tickers, as_of=None, names=None):
    """Batch form for the Phase B wiring: {TICKER: detail-dict}.

    One walk per registrant CIK (cache-served when fresh), computed
    on a small thread pool over the paced getter.  Never raises;
    once a 403/429 latches the hard stop, the remaining tickers
    return undetermined without further SEC traffic.
    """
    try:
        as_of_date = _parse_date(as_of) if as_of is not None else _today()
        if as_of_date is None:
            return {}
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
                out[key] = hit
            else:
                todo.append(key)
        if todo and not _hard_stop.is_set():
            fmap = _fund_map()
            by_cik = {}
            for key in todo:
                fm = fmap.get(key)
                if fm is None:
                    out[key] = _undetermined(fail="ticker_not_mapped")
                elif not fm.get("seriesId"):
                    out[key] = _undetermined(
                        fail="no_series_id_uit_or_cik_level")
                else:
                    by_cik.setdefault(int(fm["cik"]), {})[
                        fm["seriesId"]] = key

            def _one(cik_wanted):
                cik, wanted = cik_wanted
                try:
                    if _hard_stop.is_set():
                        return {t: _undetermined(fail="sec_hard_stop")
                                for t in wanted.values()}
                    return _walk_cik(cik, wanted, as_of_date)
                except _HardStop:
                    return {t: _undetermined(fail="sec_hard_stop")
                            for t in wanted.values()}
                except Exception:
                    return {t: _undetermined(fail="walk_error")
                            for t in wanted.values()}

            if by_cik:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    for res in pool.map(_one, sorted(by_cik.items())):
                        for key, result in res.items():
                            out[key] = result
                            if result.get("aum") is not None:
                                _cache_store(key, as_of_date, result)
        else:
            for key in todo:
                out.setdefault(key, _undetermined(fail="sec_hard_stop"))
        return {key: out.get(key, _undetermined()) for key in keys}
    except _HardStop:
        return {key: _undetermined(fail="sec_hard_stop")
                for key in (_ticker_key(t) for t in tickers or []) if key}
    except Exception:
        return {}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="SEC N-PORT fund-size (net assets) feed")
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--as-of")
    args = parser.parse_args()
    print(json.dumps(
        {t: net_assets(t, args.as_of) for t in args.tickers},
        indent=1))
