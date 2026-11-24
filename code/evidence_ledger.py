#!/usr/bin/env python3
"""Evidence ledger builder for the research stage (added 2026-11-24,
Dan approved — first-read completeness ship).

Builds, for each requested ticker, a mechanically pre-filled EVIDENCE
LEDGER: the checkable filing facts a researcher needs, each classified
Confirmed positive / Confirmed negative / Unknown-not disclosed /
Not applicable, each with its source and the date of the evidence.
The ledger is EVIDENCE, not a verdict: researchers code the facts
consistent with it unless they find contrary primary evidence, which
they must cite (the treatment conditions of the 2026-11-24 first-read
experiment, program_2026-11-24/first_read/, where the ledger raised
fact recall from 2.0% to 84.0% at an unchanged false-positive rate).

ADDITIVE AND RECORDING ONLY: this module reads the research store in
place and writes per-name ledger files under ledgers/<as-of>/. Nothing
in the screener imports it; nothing scores, gates, ranks, or rates
from its output.

Fields (point-in-time at the as-of date — only filings accepted on or
before that date are used):

  Insider purchases   Form 4 code P (open-market purchases), trailing
                      6 months by FILING date (form4_transactions
                      store table, itself stamped by filing date).
  Buybacks            Executed repurchase payments, FULL trailing
                      window scan: every balance-sheet PIT version
                      (annual + quarterly) filed by the as-of date
                      whose reported period ends inside the trailing
                      ~13 months. (The experiment's ledger checked
                      only the latest filing and capped buyback
                      recall at 74.2%; the full-window scan is the
                      ship's construction fix (a).) Authorization
                      amounts are not in the store and are never
                      claimed.
  Dividend increases  mechanical_facts_pit latest version filed by
                      the as-of date: declared dividends per share,
                      trailing 12m vs the prior 12m (prior vs new
                      figures printed). Same snapshot and lag caveat
                      as mech_prefill.py: a raise announced after the
                      snapshot's newest filing reads as no-raise until
                      the snapshot is rebuilt.
  Acquisitions        Construction fix (b): a deal is Confirmed only
                      on an 8-K Item 2.01 (completion of acquisition
                      or disposition) inside the trailing 12 months,
                      or the mechanical cash-spend flag (trailing-12m
                      cash acquisition spend >= 5% of beginning
                      assets). Item 1.01 filings (entry into a
                      material definitive agreement) are reported
                      separately as agreements that MAY include deals
                      and never confirm one on their own: a name with
                      only 1.01 filings reads Unknown-not disclosed
                      on this line.
  Customer concentration  No mechanical source exists in the store.
                      Always Unknown-not disclosed, labeled as such —
                      never silently absent (experiment fix (3)).
  Regulatory risks    8-K items 1.05 / 4.02 / 3.01 / 5.02 in the
                      trailing 12 months (cybersecurity incident,
                      non-reliance on prior financials, delisting
                      notice, officer/director departure class). The
                      events table carries item codes only, so the
                      finding names the item classes, not a narrative.
  Earnings catalysts  Latest earnings announcement on or before the
                      as-of date (8-K Item 2.02 events table; coverage
                      starts 2021), with its accession.
  Debt changes        Total debt from the balance-sheet PIT, using
                      the production wide construction
                      (fundamentals_balance_sheet_debt_wide_pit:
                      borrowings + commercial paper + lease
                      liabilities, the construction the shipped
                      leverage leg measures; narrow total_debt as
                      fallback within the same version). Current =
                      the version with the latest period end among
                      filings accepted by the as-of date; comparison =
                      the latest period end at least ~9 months
                      earlier (same filing panel preferred), as filed
                      by the as-of date. A move
                      beyond +/-10% is reported with both levels.
                      Maturity schedules are not in the store; this
                      line reports the level change only and says so.

Classification labels replicate the experiment's tested treatment
verbatim (see the first_read RESULTS.md): "Confirmed positive" = a
favorable fact is confirmed present. "Confirmed negative" = the check
was run and the named condition is absent, or an adverse fact
(acquisition activity, a regulatory item, a material debt increase)
is confirmed present. The finding text always states the underlying
fact in plain words, so read the finding, not just the label.
"Unknown-not disclosed" = the store cannot determine the field.
"Not applicable" = the field does not apply (funds: issuer-level
filing facts do not describe an ETF).

Kind resolution for the Not-applicable rule: an explicit --etf list
wins; otherwise the newest daily and watchlist frames in frames/ are
consulted; otherwise the name is treated as a stock.

Usage:
  python3 evidence_ledger.py AAPL MSFT [--as-of YYYY-MM-DD]
                             [--out-dir DIR] [--etf VTV,VOO]
                             [--tickers-file PATH]

Writes <out-dir>/<TICKER>.json and <TICKER>.md per name plus an
index.json (as-of, data vintages, per-name classifications, elapsed
seconds). Never raises on an unknown ticker: it writes a ledger whose
fields are Unknown with the reason recorded.
"""

import argparse
import json
import os
import sys
import time
from datetime import date

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "research_data")

F4_CSV = os.path.join(STORE, "filings", "form4_transactions_2017-2026.csv")
MECH_CSV = os.path.join(STORE, "fundamentals", "mechanical_facts_pit.csv")
BSA_CSV = os.path.join(STORE, "fundamentals",
                       "fundamentals_balance_sheet_pit.csv")
BSQ_CSV = os.path.join(STORE, "fundamentals",
                       "fundamentals_balance_sheet_quarterly_pit.csv")
WIDE_CSV = os.path.join(STORE, "fundamentals",
                        "fundamentals_balance_sheet_debt_wide_pit.csv")
EV8_CSV = os.path.join(STORE, "events", "form8k_events_2017-2026.csv")
EARN_CSV = os.path.join(STORE, "filings",
                        "earnings_announcement_events_2021-2026.csv")

POS = "Confirmed positive"
NEG = "Confirmed negative"
UNK = "Unknown-not disclosed"
NA = "Not applicable"

REG_ITEMS = {"1.05", "4.02", "3.01", "5.02"}
REG_ITEM_NAMES = {
    "1.05": "cybersecurity incident (1.05)",
    "4.02": "non-reliance on prior financial statements (4.02)",
    "3.01": "delisting / listing-standard notice (3.01)",
    "5.02": "officer or director departure / appointment (5.02)",
}


def _z10(cik):
    return str(int(cik)).zfill(10)


def _ticker_to_cik(mech):
    """Ticker -> CIK (zero-padded str), mirroring mech_prefill.py: the
    SEC company tickers map first, then the mechanical facts file's own
    labels, then the Form 4 / 8-K / earnings tables' ticker labels."""
    out = {}
    sec_json = os.path.join(STORE, "fundamentals", "company_tickers.json")
    if os.path.exists(sec_json):
        with open(sec_json) as fh:
            for row in json.load(fh).values():
                out[str(row["ticker"]).upper()] = str(row["cik_str"]).zfill(10)
    lab = mech[["tickers", "cik"]].dropna(subset=["tickers"])
    for ticks, cik in zip(lab["tickers"], lab["cik"]):
        for tick in str(ticks).split(";"):
            out.setdefault(tick.strip().upper(), cik)
    return out


def _frame_kinds():
    """Ticker -> kind from the newest daily and watchlist frames."""
    kinds = {}
    frames_dir = os.path.join(HERE, "frames")
    if not os.path.isdir(frames_dir):
        return kinds
    import glob
    for pat in ("frame_*_daily.json", "frame_*_watchlist.json"):
        files = sorted(glob.glob(os.path.join(frames_dir, pat)))
        if not files:
            continue
        try:
            with open(files[-1]) as fh:
                frame = json.load(fh)
            for r in frame.get("rows", []):
                kinds.setdefault(str(r["ticker"]).upper(), r.get("kind"))
        except Exception:
            continue
    return kinds


class Store:
    """Loads the store tables once, filtered to the requested CIKs."""

    def __init__(self, ciks_int):
        self.vintages = {}
        want = set(ciks_int)

        f4 = pd.read_csv(F4_CSV, usecols=["cik", "ticker", "filing_date",
                                          "transaction_date", "code",
                                          "shares", "price", "accession",
                                          "is_officer", "is_director",
                                          "is_tenpct"])
        self.vintages["form4_max_filing_date"] = str(f4["filing_date"].max())
        self.f4 = f4[f4.cik.isin(want)].copy()
        self.f4["filing_date"] = pd.to_datetime(self.f4["filing_date"])

        mech = pd.read_csv(MECH_CSV, dtype={"cik": str})
        self.vintages["mech_max_filed_date"] = str(mech["filed_date"].max())
        self.mech_all = mech  # kept whole for the ticker->CIK map labels
        self.mech = mech[mech.cik.isin({_z10(c) for c in want})].copy()
        self.mech["filed_date"] = pd.to_datetime(self.mech["filed_date"])
        self.mech = self.mech.sort_values(["cik", "filed_date", "period_end"],
                                          kind="mergesort")

        bs_frames = []
        for path, tag in ((BSA_CSV, "annual"), (BSQ_CSV, "quarterly")):
            bs = pd.read_csv(path, usecols=["cik", "filed_date", "period_end",
                                            "total_debt", "buyback_payments"],
                             dtype={"cik": str})
            self.vintages[f"balance_sheet_{tag}_max_filed_date"] = \
                str(bs["filed_date"].max())
            bs = bs[bs.cik.isin({_z10(c) for c in want})].copy()
            bs["filed_date"] = pd.to_datetime(bs["filed_date"])
            bs["period_end"] = pd.to_datetime(bs["period_end"])
            bs["src"] = tag
            bs_frames.append(bs)
        self.bs = pd.concat(bs_frames).sort_values(
            ["cik", "filed_date", "period_end"], kind="mergesort")

        wide = pd.read_csv(WIDE_CSV, usecols=["panel", "cik", "filed_date",
                                              "period_end", "total_debt",
                                              "total_debt_wide"],
                           dtype={"cik": str})
        self.vintages["debt_wide_max_filed_date"] = str(wide["filed_date"].max())
        wide = wide[wide.cik.isin({_z10(c) for c in want})].copy()
        wide["filed_date"] = pd.to_datetime(wide["filed_date"])
        wide["period_end"] = pd.to_datetime(wide["period_end"])
        wide["debt_val"] = wide["total_debt_wide"].fillna(wide["total_debt"])
        wide["debt_construction"] = wide["total_debt_wide"].notna().map(
            {True: "wide", False: "narrow"})
        self.wide = wide

        ev8 = pd.read_csv(EV8_CSV, usecols=["cik", "ticker", "filing_datetime",
                                            "form", "items", "accession"])
        self.vintages["form8k_max_filing_datetime"] = \
            str(ev8["filing_datetime"].max())
        self.ev8 = ev8[ev8.cik.isin(want)].copy()
        self.ev8["filing_dt"] = pd.to_datetime(self.ev8["filing_datetime"],
                                               utc=True).dt.tz_localize(None)

        earn = pd.read_csv(EARN_CSV, usecols=["ticker", "cik", "event_date",
                                              "accession"])
        self.vintages["earnings_events_max_date"] = str(earn["event_date"].max())
        self.earn = earn[earn.cik.isin(want)].copy()
        self.earn["event_date"] = pd.to_datetime(self.earn["event_date"])


def _items_of(s):
    if pd.isna(s):
        return set()
    return {x.strip() for x in str(s).split(",")}


def _f(field, finding, classification, source, evidence_date=None, **details):
    return {"field": field, "finding": finding,
            "classification": classification, "source": source,
            "evidence_date": evidence_date, "details": details}


def _na_fields(reason):
    return [
        _f("Insider purchases", reason, NA, "Not applicable"),
        _f("Buybacks", reason, NA, "Not applicable"),
        _f("Dividend increases", reason, NA, "Not applicable"),
        _f("Acquisitions", reason, NA, "Not applicable"),
        _f("Customer concentration",
           "Customer concentration is not mechanically extractable from "
           "the available data; no disclosure was extracted for this "
           "field.", UNK, "Not available"),
        _f("Regulatory risks", reason, NA, "Not applicable"),
        _f("Earnings catalysts", reason, NA, "Not applicable"),
        _f("Debt changes", reason, NA, "Not applicable"),
    ]


def build_ledger(store, ticker, cik_int, kind, as_of):
    asof = pd.Timestamp(as_of)
    fields = []
    if kind == "etf":
        return _na_fields(
            "Fund: issuer-level filing facts (insider transactions, "
            "repurchases, dividends, deals, borrowings of an operating "
            "company) do not apply to an ETF; fund facts are coded "
            "from flow and event coverage in the research stage.")

    cik_z = _z10(cik_int) if cik_int is not None else None
    f4 = store.f4[store.f4.cik == cik_int] if cik_int is not None \
        else store.f4.iloc[0:0]
    mech = store.mech[store.mech.cik == cik_z] if cik_z \
        else store.mech.iloc[0:0]
    bs = store.bs[store.bs.cik == cik_z] if cik_z else store.bs.iloc[0:0]
    wd = store.wide[store.wide.cik == cik_z] if cik_z \
        else store.wide.iloc[0:0]
    ev8 = store.ev8[store.ev8.cik == cik_int] if cik_int is not None \
        else store.ev8.iloc[0:0]
    earn = store.earn[store.earn.cik == cik_int] if cik_int is not None \
        else store.earn.iloc[0:0]

    # ---- 1. Insider purchases (Form 4 code P, trailing 6m by filing) ----
    w = f4[(f4.code == "P") & (f4.filing_date > asof - pd.Timedelta(days=183))
           & (f4.filing_date <= asof)]
    if len(w):
        roles = []
        if w["is_officer"].fillna(0).astype(int).any():
            roles.append("officer")
        if w["is_director"].fillna(0).astype(int).any():
            roles.append("director")
        if w["is_tenpct"].fillna(0).astype(int).any():
            roles.append("10% holder")
        fields.append(_f(
            "Insider purchases",
            f"{len(w)} open-market purchase transaction(s) (Form 4 code P) "
            f"filed in the trailing 6 months, totaling "
            f"{int(w.shares.sum()):,} shares"
            + (f"; buyers include: {', '.join(roles)}." if roles else "."),
            POS, "SEC Form 4 filings (store: form4_transactions)",
            str(w.filing_date.max().date()),
            transactions=int(len(w)), shares=int(w.shares.sum()),
            first_filing=str(w.filing_date.min().date()),
            latest_accession=str(w.sort_values("filing_date")
                                   .iloc[-1]["accession"])))
    elif len(f4):
        fields.append(_f(
            "Insider purchases",
            "Form 4 history was searched for the trailing 6 months: no "
            "open-market purchases (code P) found.",
            NEG, "SEC Form 4 filings (store: form4_transactions)",
            str(asof.date()), transactions=0))
    else:
        fields.append(_f(
            "Insider purchases",
            "No Form 4 records for this company in the available data; "
            "insider purchases cannot be confirmed or excluded.",
            UNK, "SEC Form 4 filings (store: form4_transactions)", None))

    # ---- 2. Buybacks (FULL trailing-window scan, fix (a)) ----
    win = bs[(bs.filed_date <= asof)
             & (bs.period_end > asof - pd.Timedelta(days=400))
             & (bs.period_end <= asof) & bs.buyback_payments.notna()]
    if len(win):
        pos_rows = win[win.buyback_payments > 0]
        if len(pos_rows):
            latest = pos_rows.sort_values("filed_date").iloc[-1]
            n_filings = int(pos_rows.filed_date.nunique())
            fields.append(_f(
                "Buybacks",
                f"Executed share repurchases are reported in "
                f"{n_filings} filing(s) whose periods end inside the "
                f"trailing 12 months; the largest single reported figure "
                f"is ${pos_rows.buyback_payments.max()/1e6:,.0f}M (latest "
                f"such filing {str(latest.filed_date.date())}, period "
                f"ended {str(latest.period_end.date())}, "
                f"${latest.buyback_payments/1e6:,.0f}M). Full-window "
                f"scan: every filing in the window was checked, not "
                f"only the latest. Authorization amounts are not in "
                f"the available data.",
                POS, "SEC 10-K/10-Q filings (XBRL, balance-sheet PIT)",
                str(pos_rows.filed_date.max().date()),
                filings_with_repurchases=n_filings,
                max_reported_usd=float(pos_rows.buyback_payments.max())))
        else:
            fields.append(_f(
                "Buybacks",
                "Filings whose periods end inside the trailing 12 "
                "months report no share repurchase payments (full-window "
                "scan). Authorization amounts are not in the available "
                "data.",
                NEG, "SEC 10-K/10-Q filings (XBRL, balance-sheet PIT)",
                str(win.filed_date.max().date()), filings_with_repurchases=0))
    else:
        fields.append(_f(
            "Buybacks",
            "No repurchase data available for this company in the "
            "available filings data.",
            UNK, "SEC 10-K/10-Q filings (XBRL, balance-sheet PIT)", None))

    # ---- 3. Dividend increases (mechanical facts, latest <= as-of) ----
    m = mech[mech.filed_date <= asof]
    mrow = m.iloc[-1] if len(m) else None
    if mrow is not None and pd.notna(mrow["dividend_up_mech"]):
        if int(mrow["dividend_up_mech"]) == 1:
            fields.append(_f(
                "Dividend increases",
                f"Dividends declared per share rose over the trailing "
                f"12 months: ${mrow['div_ttm_prior']:.3f} to "
                f"${mrow['div_ttm']:.3f} per share.",
                POS, "SEC filings (XBRL dividends declared, "
                     "mechanical_facts_pit)",
                str(pd.Timestamp(mrow["filed_date"]).date()),
                div_ttm=float(mrow["div_ttm"]),
                div_ttm_prior=float(mrow["div_ttm_prior"]),
                div_source=str(mrow["div_source"])))
        else:
            fields.append(_f(
                "Dividend increases",
                f"No dividend increase over the trailing 12 months "
                f"(declared per share ${mrow['div_ttm']:.3f} vs "
                f"${mrow['div_ttm_prior']:.3f} a year earlier).",
                NEG, "SEC filings (XBRL dividends declared, "
                     "mechanical_facts_pit)",
                str(pd.Timestamp(mrow["filed_date"]).date()),
                div_ttm=float(mrow["div_ttm"]),
                div_ttm_prior=float(mrow["div_ttm_prior"]),
                div_source=str(mrow["div_source"])))
    else:
        fields.append(_f(
            "Dividend increases",
            "Dividend history could not be determined from the "
            "available filings data.",
            UNK, "SEC filings (XBRL dividends declared, "
                 "mechanical_facts_pit)", None))

    # ---- 4. Acquisitions (fix (b): 2.01 / cash spend confirm; 1.01 does not)
    e = ev8[(ev8.filing_dt <= asof)
            & (ev8.filing_dt > asof - pd.Timedelta(days=365))]
    e201 = e[e["items"].map(lambda x: "2.01" in _items_of(x))]
    e101 = e[e["items"].map(lambda x: "1.01" in _items_of(x))]
    mech_acq = bool(mrow is not None and pd.notna(mrow["big_acquisition_mech"])
                    and int(mrow["big_acquisition_mech"]) == 1)
    if len(e201) or mech_acq:
        bits = []
        if len(e201):
            bits.append(f"{len(e201)} 8-K filing(s) with Item 2.01 "
                        f"(completion of acquisition or disposition) in "
                        f"the trailing 12 months")
        if mech_acq:
            bits.append("cash acquisition spend over the trailing 12 "
                        "months reached at least 5% of total assets "
                        "(mechanical facts)")
        fields.append(_f(
            "Acquisitions",
            "Large acquisition activity confirmed: " + "; ".join(bits) + ".",
            NEG, "SEC 8-K filings; SEC filings (XBRL)",
            str(e201.filing_dt.max().date()) if len(e201)
            else str(pd.Timestamp(mrow["filed_date"]).date()),
            item_201_filings=int(len(e201)),
            item_101_filings=int(len(e101)), mech_cash_spend_flag=mech_acq))
    elif len(e101):
        fields.append(_f(
            "Acquisitions",
            f"{len(e101)} 8-K filing(s) with Item 1.01 (entry into a "
            f"material definitive agreement) in the trailing 12 months, "
            f"but no Item 2.01 completion filing and no large cash "
            f"acquisition spend: an agreement is not a confirmed deal, "
            f"so acquisition activity cannot be mechanically confirmed "
            f"or excluded for this name.",
            UNK, "SEC 8-K filings; SEC filings (XBRL)",
            str(e101.filing_dt.max().date()),
            item_201_filings=0, item_101_filings=int(len(e101)),
            mech_cash_spend_flag=False))
    elif len(ev8) or len(m):
        fields.append(_f(
            "Acquisitions",
            "No Item 2.01 completion filings, no Item 1.01 agreement "
            "filings, and no large cash acquisition spend found in the "
            "trailing 12 months.",
            NEG, "SEC 8-K filings; SEC filings (XBRL)", str(asof.date()),
            item_201_filings=0, item_101_filings=0,
            mech_cash_spend_flag=False))
    else:
        fields.append(_f(
            "Acquisitions",
            "No 8-K or mechanical-facts coverage for this company in "
            "the available data; acquisition activity cannot be "
            "confirmed or excluded.",
            UNK, "SEC 8-K filings; SEC filings (XBRL)", None))

    # ---- 5. Customer concentration (no source exists) ----
    fields.append(_f(
        "Customer concentration",
        "Customer concentration is not mechanically extractable from "
        "the available data; no disclosure was extracted for this "
        "field.", UNK, "Not available", None))

    # ---- 6. Regulatory risks (8-K items, trailing 12m) ----
    reg = e[e["items"].map(lambda x: bool(_items_of(x) & REG_ITEMS))]
    if len(reg):
        its = sorted({i for x in reg["items"]
                      for i in _items_of(x) & REG_ITEMS})
        fields.append(_f(
            "Regulatory risks",
            f"{len(reg)} filing(s) in the trailing 12 months carried "
            f"material event items: "
            + ", ".join(REG_ITEM_NAMES[i] for i in its) + ".",
            NEG, "SEC 8-K filings (store: form8k_events)",
            str(reg.filing_dt.max().date()),
            filings=int(len(reg)), items=its))
    elif len(ev8):
        fields.append(_f(
            "Regulatory risks",
            "No 8-K filings with items 1.05, 4.02, 3.01 or 5.02 in the "
            "trailing 12 months.",
            NEG, "SEC 8-K filings (store: form8k_events)",
            str(asof.date()), filings=0))
    else:
        fields.append(_f(
            "Regulatory risks",
            "No 8-K records for this company in the available data; "
            "the regulatory-items check cannot be run.",
            UNK, "SEC 8-K filings (store: form8k_events)", None))

    # ---- 7. Earnings catalysts (latest 2.02 event <= as-of) ----
    ee = earn[earn.event_date <= asof].sort_values("event_date")
    if len(ee):
        last = ee.iloc[-1]
        d = pd.Timestamp(last["event_date"])
        fields.append(_f(
            "Earnings catalysts",
            f"Most recent earnings announcement was "
            f"{(asof - d).days} days before the as-of date "
            f"({str(d.date())}, accession {last['accession']}).",
            POS, "SEC 8-K item 2.02 filings (store: "
                 "earnings_announcement_events)",
            str(d.date()), days_before_asof=int((asof - d).days),
            accession=str(last["accession"])))
    else:
        fields.append(_f(
            "Earnings catalysts",
            "No earnings announcement found on or before the as-of "
            "date in the available data (the store's earnings-event "
            "table starts 2021).",
            UNK, "SEC 8-K item 2.02 filings (store: "
                 "earnings_announcement_events)", None))

    # ---- 8. Debt changes (wide construction; latest period end governs)
    dq = wd[(wd.filed_date <= asof) & wd.debt_val.notna()].copy()
    if len(dq):
        cur = dq.sort_values(["period_end", "filed_date"]).iloc[-1]
        prior = dq[dq.period_end
                   <= cur.period_end - pd.Timedelta(days=270)]
        same_panel = prior[prior.panel == cur.panel]
        if len(same_panel):
            prior = same_panel
        if len(prior) and prior.sort_values(
                ["period_end", "filed_date"]).iloc[-1].debt_val > 0:
            prev = prior.sort_values(["period_end", "filed_date"]).iloc[-1]
            chg = cur.debt_val / prev.debt_val - 1
            if abs(chg) > 0.10:
                fields.append(_f(
                    "Debt changes",
                    f"Total debt changed {chg:+.0%} over roughly the "
                    f"last year, from ${prev.debt_val/1e6:,.0f}M "
                    f"(period ended {str(prev.period_end.date())}, as "
                    f"filed {str(prev.filed_date.date())}) to "
                    f"${cur.debt_val/1e6:,.0f}M (period ended "
                    f"{str(cur.period_end.date())}, filed "
                    f"{str(cur.filed_date.date())}; wide construction: "
                    f"borrowings + leases). Maturity schedules are "
                    f"not in the available data.",
                    NEG if chg > 0 else POS,
                    "SEC 10-Q/10-K filings (XBRL, balance-sheet PIT, "
                    "wide debt construction)",
                    str(cur.filed_date.date()),
                    change_pct=float(chg),
                    debt_now_usd=float(cur.debt_val),
                    debt_prior_usd=float(prev.debt_val),
                    construction=str(cur.debt_construction)))
            else:
                fields.append(_f(
                    "Debt changes",
                    f"Total debt was broadly stable over the last "
                    f"year (${cur.debt_val/1e6:,.0f}M at period end "
                    f"{str(cur.period_end.date())}, {chg:+.0%} vs a "
                    f"year earlier; wide construction: borrowings + "
                    f"leases). Maturity schedules are not in the "
                    f"available data.",
                    NEG, "SEC 10-Q/10-K filings (XBRL, balance-sheet "
                         "PIT, wide debt construction)",
                    str(cur.filed_date.date()),
                    change_pct=float(chg),
                    debt_now_usd=float(cur.debt_val),
                    construction=str(cur.debt_construction)))
        else:
            fields.append(_f(
                "Debt changes",
                f"Latest balance sheet reports total debt of "
                f"${cur.debt_val/1e6:,.0f}M (period ended "
                f"{str(cur.period_end.date())}, filed "
                f"{str(cur.filed_date.date())}); no year-earlier "
                f"comparison available. Maturity schedules are not "
                f"in the available data.",
                UNK, "SEC 10-Q/10-K filings (XBRL, balance-sheet PIT, "
                     "wide debt construction)",
                str(cur.filed_date.date()),
                debt_now_usd=float(cur.debt_val),
                construction=str(cur.debt_construction)))
    else:
        fields.append(_f(
            "Debt changes",
            "No debt data available for this company in the available "
            "filings data.",
            UNK, "SEC 10-Q/10-K filings (XBRL, balance-sheet PIT, "
                 "wide debt construction)", None))

    return fields


def render_markdown(led):
    lines = [
        f"EVIDENCE LEDGER — {led['ticker']} (as of {led['as_of']}; "
        f"mechanically pre-filled from point-in-time filings data; "
        f"every entry is dated on or before the as-of date):",
    ]
    for f in led["fields"]:
        line = (f"- {f['field']}: {f['finding']} "
                f"[Classification: {f['classification']}; "
                f"Source: {f['source']}")
        if f["evidence_date"]:
            line += f"; Evidence date: {f['evidence_date']}"
        line += "]"
        lines.append(line)
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--tickers-file", default=None,
                    help="file with one ticker per line (or comma list)")
    ap.add_argument("--as-of", default=None,
                    help="YYYY-MM-DD (default: today, local)")
    ap.add_argument("--out-dir", default=None,
                    help="default: ledgers/<as-of>/ under the project dir")
    ap.add_argument("--etf", default=None,
                    help="comma list of tickers to treat as funds "
                         "(overrides frame lookup)")
    args = ap.parse_args(argv)

    as_of = args.as_of or date.today().isoformat()
    tickers = [t.upper() for t in args.tickers]
    if args.tickers_file:
        with open(args.tickers_file) as fh:
            tickers += [t.strip().upper() for chunk in fh.read().split(",")
                        for t in chunk.split()]
    tickers = sorted({t for t in tickers if t})
    if not tickers:
        print("no tickers requested", file=sys.stderr)
        return 2
    out_dir = args.out_dir or os.path.join(HERE, "ledgers", as_of)
    os.makedirs(out_dir, exist_ok=True)

    t0 = time.time()
    mech_all = pd.read_csv(MECH_CSV, dtype={"cik": str},
                           usecols=["cik", "tickers", "filed_date"])
    t2c = _ticker_to_cik(mech_all)
    kinds = _frame_kinds()
    if args.etf:
        for t in args.etf.split(","):
            kinds[t.strip().upper()] = "etf"
    cik_of = {}
    for t in tickers:
        z = t2c.get(t)
        cik_of[t] = int(z) if z else None
    store = Store([c for c in cik_of.values() if c is not None])

    index = {"as_of": as_of, "built_at": pd.Timestamp.now().isoformat(),
             "data_vintages": store.vintages, "ledgers": {}}
    for t in tickers:
        led = {"ticker": t, "cik": cik_of[t],
               "kind": kinds.get(t, "stock"), "as_of": as_of,
               "fields": []}
        try:
            led["fields"] = build_ledger(store, t, cik_of[t], led["kind"],
                                         as_of)
        except Exception as exc:  # never raise on a name
            led["fields"] = _na_fields(
                f"Ledger build failed for this name ({exc}); all "
                f"fields unknown.") if led["kind"] == "etf" else [
                _f(fld, f"Ledger build failed for this name ({exc}).",
                   UNK, "Not available")
                for fld in ("Insider purchases", "Buybacks",
                            "Dividend increases", "Acquisitions",
                            "Customer concentration", "Regulatory risks",
                            "Earnings catalysts", "Debt changes")]
            led["build_error"] = str(exc)
        with open(os.path.join(out_dir, f"{t}.json"), "w") as fh:
            json.dump(led, fh, indent=1, default=str)
        with open(os.path.join(out_dir, f"{t}.md"), "w") as fh:
            fh.write(render_markdown(led))
        index["ledgers"][t] = {
            f["field"]: f["classification"] for f in led["fields"]}
    index["elapsed_seconds"] = round(time.time() - t0, 2)
    index["n_tickers"] = len(tickers)
    with open(os.path.join(out_dir, "index.json"), "w") as fh:
        json.dump(index, fh, indent=1, default=str)
    print(f"evidence ledgers as of {as_of}: {len(tickers)} names -> "
          f"{out_dir} in {index['elapsed_seconds']}s "
          f"(mech snapshot newest filing: "
          f"{store.vintages['mech_max_filed_date']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
