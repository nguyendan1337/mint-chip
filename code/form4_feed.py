#!/usr/bin/env python3
"""Mechanical SEC Form 4 insider-buying feed for the Mint screener.

The filing-based ``f4_buy_any`` fact graduated in
``form4_study_2026-11-11``: at least one open-market insider purchase
(transaction code P) filed in the trailing six months was associated
with a +7.0pp pooled next-year return gap on the 2022-2025 research
panel.  Unlike the fourteen research facts, this signal cannot be coded
reliably from headlines (the headline version fired on 0 of 240 panel
rows), so it is stamped mechanically from EDGAR, mirroring a price
feed rather than a researcher judgment.

Public entry point: :func:`insider_buying`.  It never raises: any SEC,
network, parsing, or cache failure returns ``buy=None`` so the caller
can treat the fact as unavailable and leave the EV term at zero.
"""

import json
import os
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import requests

USER_AGENT = "MintResearch test@example.com"
# The two environment overrides exist so sandbox rehearsals can prove
# the pipeline's failure behaviour (bad UA / isolated cache) without
# editing production code.  In production neither is set and the UA is
# exactly the declared string above.
_USER_AGENT = os.environ.get("FORM4_USER_AGENT", USER_AGENT)
_HERE = os.path.dirname(os.path.abspath(__file__))
_CACHE_PATH = os.environ.get(
    "FORM4_CACHE_PATH", os.path.join(_HERE, "form4_cache.json"))
_CIK_CACHE_PATH = os.environ.get(
    "FORM4_CIK_CACHE_PATH", os.path.join(_HERE, "form4_cik_cache.json"))
_MIN_REQUEST_INTERVAL = 0.21  # a shade over 5 requests/second
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


def _months_ago(day, months):
    """Calendar-month subtraction, clamped to the target month's end."""
    month_index = (day.year * 12 + day.month - 1) - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return date(year, month, min(day.day, last_day))


def _get(url):
    """One paced SEC GET with the declared user agent.

    Request *starts* are reserved under a lock at <=5/second, but the
    responses are awaited outside the lock, so the per-filing document
    fetches in _compute can overlap their network latency without ever
    exceeding the SEC pace cap.
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
                 "Accept": "application/json, text/xml, */*"},
        timeout=30,
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


def _filing_records(payload):
    """Yield filing dicts from a submissions columnar payload."""
    if not isinstance(payload, dict):
        return
    columns = ("accessionNumber", "filingDate", "form", "primaryDocument")
    arrays = [payload.get(name) for name in columns]
    if not all(isinstance(values, list) for values in arrays):
        return
    for accession, filed, form, document in zip(*arrays):
        yield {
            "accession": str(accession or ""),
            "filing_date": _parse_date(filed),
            "form": str(form or ""),
            "primary_document": str(document or ""),
        }


def _all_filings(cik, start, as_of):
    """Form 4 / 4-A filings in [start, as_of], including archive files."""
    base = _get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
    filings_block = base.get("filings") or {}
    recent = filings_block.get("recent") or {}
    records = {}
    for record in _filing_records(recent):
        records[record["accession"]] = record

    recent_dates = [r["filing_date"] for r in records.values()
                    if r["filing_date"] is not None]
    # The recent block is a bounded window.  Pull an older archive file
    # whenever it overlaps the requested window and the recent block
    # does not already reach back to the window's start.
    if not recent_dates or min(recent_dates) > start:
        for file_info in filings_block.get("files") or []:
            if not isinstance(file_info, dict):
                continue
            file_from = _parse_date(file_info.get("filingFrom"))
            file_to = _parse_date(file_info.get("filingTo"))
            name = file_info.get("name")
            if not name or file_from is None or file_to is None:
                continue
            if file_to < start or file_from > as_of:
                continue
            payload = _get(f"https://data.sec.gov/submissions/{name}").json()
            for record in _filing_records(payload):
                records[record["accession"]] = record

    selected = []
    for record in records.values():
        filed = record["filing_date"]
        if record["form"] not in ("4", "4/A"):
            continue
        if filed is None or not (start <= filed <= as_of):
            continue
        if not record["accession"] or not record["primary_document"]:
            continue
        selected.append(record)
    selected.sort(key=lambda r: (r["filing_date"], r["accession"]))
    return selected


def _text(node, path):
    found = node.find(path) if node is not None else None
    if found is None or found.text is None:
        return ""
    return str(found.text).strip()


def _float_text(node, path):
    text = _text(node, path)
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _parse_purchases(xml_bytes, accession):
    """Code-P non-derivative purchases from one ownership document."""
    root = ET.fromstring(xml_bytes)
    owner = (_text(root, "./reportingOwner/reportingOwnerId/rptOwnerName")
             or "unknown owner")
    purchases = []
    for transaction in root.iter("nonDerivativeTransaction"):
        code = _text(transaction, "./transactionCoding/transactionCode")
        if code != "P":
            continue
        trans_date = _parse_date(
            _text(transaction, "./transactionDate/value"))
        shares = _float_text(
            transaction, "./transactionAmounts/transactionShares/value")
        price = _float_text(
            transaction,
            "./transactionAmounts/transactionPricePerShare/value")
        title = _text(transaction, "./securityTitle/value") or "security"
        purchases.append({
            "owner": owner,
            "transaction_date": trans_date,
            "shares": shares,
            "price": price,
            "value": shares * price,
            "title": title,
            "accession": accession,
        })
    return purchases


def _fetch_purchases(cik, filing):
    accession = filing["accession"]
    compact_accession = accession.replace("-", "")
    cik_number = str(int(cik))
    document = filing["primary_document"].lstrip("/")
    # Submissions lists the *rendered* document for most Form 4s
    # (xslF345X03/<file>.xml): that path returns the XSL stylesheet,
    # not the filing.  The raw ownership XML sits in the same accession
    # folder under its bare filename.
    _parts = document.split("/")
    if len(_parts) > 1 and _parts[0].lower().startswith("xsl"):
        document = _parts[-1]
    url = (f"https://www.sec.gov/Archives/edgar/data/{cik_number}/"
           f"{compact_accession}/{document}")
    response = _get(url)
    content = response.content
    # A small number of filings use a text submission as the primary
    # document; the ownership XML is embedded in that text.
    if b"<ownershipDocument" not in content[:200000]:
        start = content.find(b"<ownershipDocument")
        if start < 0:
            return []
        content = content[start:]
    return _parse_purchases(content, accession)


def _compute(ticker, as_of):
    cik = _cik_map().get(_ticker_key(ticker))
    if not cik:
        return {"buy": None, "n_buys": 0, "total_value": 0.0,
                "evidence": []}
    filing_start = _months_ago(as_of, 7)  # one buffer month
    transaction_start = _months_ago(as_of, 6)
    purchases = []
    seen = set()
    # CIK LINEAGE (2026-11-12, cik_diagnostic_2026-11-12): when the
    # resolved CIK is a known successor, insider filings from before
    # the succession sit under the legacy CIK. Scan both and union by
    # accession; post-succession filings land on the successor, so
    # the current map stays primary and is never overridden.
    tagged = []
    seen_accessions = set()
    for filing_cik in _lineage_ciks(cik):
        for filing in _all_filings(filing_cik, filing_start, as_of):
            if filing["accession"] in seen_accessions:
                continue
            seen_accessions.add(filing["accession"])
            tagged.append((filing_cik, filing))
    if tagged:
        with ThreadPoolExecutor(max_workers=8) as pool:
            per_filing = list(pool.map(
                lambda pair: _fetch_purchases(pair[0], pair[1]), tagged))
    else:
        per_filing = []
    for filing_purchases in per_filing:
        for purchase in filing_purchases:
            trans_date = purchase["transaction_date"]
            if trans_date is None:
                continue
            if not (transaction_start <= trans_date <= as_of):
                continue
            # Amendments and co-filed reports can restate one economic
            # purchase.  The study's feature was owner-level, but the
            # live binary/value should not double-count the identical
            # owner/date/share/price/title record.
            key = (purchase["owner"], trans_date.isoformat(),
                   round(purchase["shares"], 6),
                   round(purchase["price"], 6), purchase["title"])
            if key in seen:
                continue
            seen.add(key)
            purchases.append(purchase)
    purchases.sort(key=lambda p: (p["transaction_date"], p["owner"]))
    evidence = []
    for purchase in purchases:
        evidence.append(
            f"{purchase['owner']} bought ${purchase['value']:,.0f} on "
            f"{purchase['transaction_date'].isoformat()} "
            f"({purchase['accession']})")
    return {
        "buy": 1 if purchases else 0,
        "n_buys": len(purchases),
        "total_value": float(sum(p["value"] for p in purchases)),
        "evidence": evidence,
    }


def insider_buying(ticker, as_of=None):
    """Return the filing-based insider-buying fact for ``ticker``.

    ``buy`` is 1 when at least one open-market purchase (Form 4 code P)
    was both transacted in the trailing six months and filed by
    ``as_of``; 0 when EDGAR was reached and no such purchase was found;
    None whenever the fact could not be determined.  This function
    never raises into the screener pipeline.
    """
    try:
        as_of_date = _parse_date(as_of) if as_of is not None else _today()
        if as_of_date is None:
            return {"buy": None, "n_buys": 0, "total_value": 0.0,
                    "evidence": []}
        key = _ticker_key(ticker)
        if not key:
            return {"buy": None, "n_buys": 0, "total_value": 0.0,
                    "evidence": []}

        cache = _read_json(_CACHE_PATH)
        cached = cache.get(key) if isinstance(cache, dict) else None
        if isinstance(cached, dict):
            cached_as_of = _parse_date(cached.get("as_of"))
            result = cached.get("result")
            if (cached_as_of is not None and isinstance(result, dict)
                    and abs((as_of_date - cached_as_of).days) <= 7
                    and result.get("buy") in (0, 1)):
                return {
                    "buy": int(result["buy"]),
                    "n_buys": int(result.get("n_buys") or 0),
                    "total_value": float(result.get("total_value") or 0.0),
                    "evidence": list(result.get("evidence") or []),
                }

        result = _compute(key, as_of_date)
        if result.get("buy") in (0, 1):
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
        return {"buy": None, "n_buys": 0, "total_value": 0.0,
                "evidence": []}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SEC Form 4 buying feed")
    parser.add_argument("ticker")
    parser.add_argument("--as-of")
    args = parser.parse_args()
    print(json.dumps(insider_buying(args.ticker, args.as_of), indent=1))
