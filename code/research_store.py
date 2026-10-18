"""Unified ticker-keyed research store shared by the Mint (Chip) main and
watchlist pipelines.

Problem: the main flow researches into llm_outputs.json and the watchlist
flow into watchlist_outputs.json. The same ticker gets assessed twice, an
hour apart, often with different scores -> divergent EVs/blurbs across the
two pages, and duplicated web-research cost (research is the long pole).

This module is PURE PLUMBING. It stores and serves researcher assessments;
it does not score, rank, gate, or select anything. The EV formula, gates,
floors, weights, benchmarks, compute_rating, and z-score normalization are
untouched — methodology changes remain the monthly audit's job.

Schema v1:
    {"meta": {"version": 1, "updated": "YYYY-MM-DD"},
     "tickers": {TICKER: {
        "assessment": {"event_dependence": 0..1, "continuation": 0..1,
                       "confidence": 0..1, "risks": [...], "rationale": "...",
                       "assessed_date": "YYYY-MM-DD",
                       "assessed_by": "mint-chip-daily" | "mint-chip-watchlist-daily",
                       ...optional "exclude"/"exclude_reason"},
        "blurb": {"what": "...", "why": "...",
                  "written_date": "YYYY-MM-DD",
                  "based_on_assessment": "YYYY-MM-DD"}}},
     "worlds": {FLOW: {"world": {"drivers": [...], "risk_events": [...], ...},
                       "written_date": "YYYY-MM-DD"}}}

FLOW is "mint-chip-daily" or "mint-chip-watchlist-daily". Each flow keeps
its own world/macro layer (the drivers + risk events its researcher wrote);
the adapter serves the calling flow's world so log_world_layer() keeps
working exactly as it did with the legacy files.
"""

import argparse
import contextlib
import json
import os
import sys
import tempfile
from datetime import datetime, date

STORE_VERSION = 1
STORE_FILENAME = "research_store.json"

BY_DAILY = "mint-chip-daily"
BY_WATCHLIST = "mint-chip-watchlist-daily"

# Assessment fields carried over from the legacy files. Unknown extra keys
# are dropped on write so the store keeps a stable, auditable shape.
_ASSESSMENT_FIELDS = ("event_dependence", "continuation", "confidence",
                      "risks", "rationale", "assessed_date",
                      "news_checked_through", "earnings_note",
                      "exclude", "exclude_reason")
_COMPARE_FIELDS = ("event_dependence", "continuation", "confidence",
                   "risks", "rationale", "assessed_date",
                   "news_checked_through", "earnings_note",
                   "exclude", "exclude_reason")


def _today_str():
    return date.today().strftime("%Y-%m-%d")


def blank_store():
    return {"meta": {"version": STORE_VERSION, "updated": _today_str()},
            "tickers": {},
            "worlds": {}}


def is_store(obj):
    """True if obj looks like a research-store document (vs a legacy file).

    Lenient on purpose: stores built before the "worlds" key existed are
    still stores (world accessors default it in).
    """
    return (isinstance(obj, dict)
            and isinstance(obj.get("meta"), dict)
            and isinstance(obj.get("tickers"), dict))


def load_store(path=STORE_FILENAME):
    """Load the store; return a blank store if the file does not exist.

    A corrupt/unreadable file raises — silently clobbering researcher work
    would be worse than failing loudly.
    """
    if not os.path.exists(path):
        return blank_store()
    with open(path) as f:
        obj = json.load(f)
    if not is_store(obj):
        raise ValueError(f"{path} exists but is not a research-store document")
    return obj


def save_store(store, path=STORE_FILENAME):
    """Atomic save: write temp file + os.replace, so a crash can never leave
    a torn JSON document behind."""
    path = str(path)
    store.setdefault("meta", {})["version"] = STORE_VERSION
    store["meta"]["updated"] = _today_str()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".research_store.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(store, f, indent=1)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def locked_store(path=STORE_FILENAME):
    """Serialize read-modify-write cycles across concurrent CLI invocations.

    The atomic rename in save_store already prevents torn writes; the lock
    additionally prevents two processes from interleaving load/save and
    losing each other's puts. Best-effort: if flock is unavailable we still
    proceed (rename atomicity holds).
    """
    try:
        import fcntl
    except ImportError:
        fcntl = None
    lock_path = str(path) + ".lock"
    if fcntl is None:
        yield
        return
    with open(lock_path, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX)
        except OSError:
            pass
        try:
            yield
        finally:
            try:
                fcntl.flock(lf, fcntl.LOCK_UN)
            except OSError:
                pass


def _clean_assessment(assessment, by):
    a = dict(assessment or {})
    return {k: a.get(k) for k in _ASSESSMENT_FIELDS
            } | {"assessed_by": by}


def _assessments_equal(a, b):
    return all((a or {}).get(k) == (b or {}).get(k)
               for k in _COMPARE_FIELDS)


def get_assessment(store, ticker):
    """Return the stored assessment dict for TICKER, or None."""
    if not ticker:
        return None
    entry = (store.get("tickers") or {}).get(str(ticker).upper()) or {}
    return entry.get("assessment")


def put_assessment(store, ticker, assessment, by):
    """Store an assessment. Conflict rule: newer assessed_date wins; on a
    tie the existing entry is kept (first writer wins) and the collision is
    reported. Returns one of: 'added', 'replaced', 'kept-newer',
    'kept-tie', 'noop-identical'.
    """
    t = str(ticker).upper()
    tickers = store.setdefault("tickers", {})
    entry = tickers.setdefault(t, {})
    new = _clean_assessment(assessment, by)
    old = entry.get("assessment")
    new_date = str(new.get("assessed_date") or "")
    if not old:
        entry["assessment"] = new
        return "added"
    if _assessments_equal(old, new):
        return "noop-identical"
    old_date = str(old.get("assessed_date") or "")
    if new_date > old_date:
        entry["assessment"] = new
        return "replaced"
    if new_date == old_date:
        return "kept-tie"
    return "kept-newer"


def is_fresh(assessment, today_str):
    """Fresh iff assessed_date is within the last 7 days (inclusive).

    Mirrors the standing 7-day research-freshness rule both flows use.
    Malformed/missing dates are not fresh.
    """
    try:
        d0 = datetime.strptime(str((assessment or {}).get("assessed_date")
                                   or ""), "%Y-%m-%d").date()
        d1 = datetime.strptime(str(today_str), "%Y-%m-%d").date()
        return 0 <= (d1 - d0).days <= 7
    except (ValueError, TypeError):
        return False


def get_blurb(store, ticker):
    if not ticker:
        return None
    entry = (store.get("tickers") or {}).get(str(ticker).upper()) or {}
    return entry.get("blurb")


def put_blurb(store, ticker, blurb):
    """Store a page blurb. blurb = {what, why, written_date,
    based_on_assessment}. Overwrites unconditionally — blurbs are cheap to
    regenerate and the writer always has the latest assessment in hand."""
    t = str(ticker).upper()
    b = dict(blurb or {})
    tickers = store.setdefault("tickers", {})
    tickers.setdefault(t, {})["blurb"] = {
        "what": b.get("what", ""),
        "why": b.get("why", ""),
        "written_date": b.get("written_date", _today_str()),
        "based_on_assessment": b.get("based_on_assessment", ""),
    }
    return True


def _check_flow(flow):
    if flow not in (BY_DAILY, BY_WATCHLIST):
        raise ValueError(f"unknown flow: {flow!r} "
                         f"(expected {BY_DAILY!r} or {BY_WATCHLIST!r})")
    return flow


def put_world(store, flow, world_dict):
    """Store a flow's world/macro layer. Overwrites unconditionally — the
    writer always has the latest world in hand. Each flow keeps its own
    world (drivers + risk events + internals as the researcher wrote them);
    the adapter serves the calling flow's copy. Returns True."""
    _check_flow(flow)
    store.setdefault("worlds", {})[flow] = {
        "world": dict(world_dict or {}),
        "written_date": _today_str(),
    }
    return True


def get_world(store, flow):
    """Return the stored world dict for FLOW, or None if absent."""
    if not flow:
        return None
    entry = (store.get("worlds") or {}).get(flow) or {}
    return entry.get("world")


def _world_written_date(data):
    """Provenance date for a migrated world layer: the max assessed_date
    among the file's stock assessments; if none are stamped, fall back to
    the file's own top-level assessed_date (the watchlist file carries one),
    then to today."""
    dates = [str(a.get("assessed_date"))
             for a in ((data or {}).get("stocks") or {}).values()
             if (a or {}).get("assessed_date")]
    if dates:
        return max(dates)
    return str((data or {}).get("assessed_date") or _today_str())


def migrate_from_legacy(llm_path, watchlist_path):
    """Merge both legacy research files into a fresh store.

    Migration order is main flow first, then watchlist; per put_assessment's
    conflict rule, newer assessed_date wins and ties keep the first writer
    (the main flow's assessment). Each file's "world" layer is migrated into
    worlds[<that file's flow>] with its provenance date. Returns (store,
    report) where report maps outcome -> count and lists tie collisions.
    """
    store = blank_store()
    report = {"added": 0, "replaced": 0, "kept-newer": 0, "kept-tie": 0,
              "noop-identical": 0, "tie_collisions": [],
              "worlds_migrated": [],
              "per_source": {BY_DAILY: 0, BY_WATCHLIST: 0}}
    for path, by in ((llm_path, BY_DAILY), (watchlist_path, BY_WATCHLIST)):
        if not path or not os.path.exists(path):
            continue
        with open(path) as f:
            data = json.load(f)
        for t, a in ((data or {}).get("stocks") or {}).items():
            outcome = put_assessment(store, t, a, by)
            report[outcome] = report.get(outcome, 0) + 1
            if outcome == "added":
                report["per_source"][by] += 1
            if outcome == "kept-tie":
                old = get_assessment(store, t) or {}
                report["tie_collisions"].append({
                    "ticker": str(t).upper(),
                    "kept_assessed_by": old.get("assessed_by"),
                    "dropped_assessed_by": by,
                    "assessed_date": old.get("assessed_date"),
                })
        world = (data or {}).get("world")
        if isinstance(world, dict) and world:
            store.setdefault("worlds", {})[by] = {
                "world": world,
                "written_date": _world_written_date(data),
            }
            report["worlds_migrated"].append(by)
    return store, report


def to_outputs_format(store, flow=None):
    """Adapt the store to the legacy research-outputs shape that
    apply_llm_outputs() / validate_outputs() / log_world_layer() consume:
        {"stocks": {TICKER: {event_dependence, ..., assessed_date}},
         "world": <this flow's world dict, or {} if none stored>}

    flow is "mint-chip-daily" or "mint-chip-watchlist-daily". Each flow
    keeps its own world layer (migrated from its own legacy file), so the
    Phase B log shows the same macro context the researcher wrote — never
    the other flow's, never a spurious "NOT PROVIDED".
    """
    import copy
    stocks = {}
    for t, entry in (store.get("tickers") or {}).items():
        a = (entry or {}).get("assessment") or {}
        stocks[str(t).upper()] = {k: a.get(k) for k in _ASSESSMENT_FIELDS
                                  if k in a}
    world = {}
    if flow:
        w = get_world(store, flow)
        if isinstance(w, dict):
            world = copy.deepcopy(w)
    return {"stocks": stocks, "world": world}


def adapt_if_store(obj, flow=None):
    """If obj is a research-store document, adapt it to research-outputs
    shape (serving the given flow's world layer); otherwise return it
    unchanged (legacy passthrough)."""
    if is_store(obj):
        return to_outputs_format(obj, flow)
    return obj


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="Unified Mint research store (ticker-keyed assessments).")
    p.add_argument("--store", default=STORE_FILENAME,
                   help="store file path (default: ./research_store.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get-assessment", help="print an assessment as JSON")
    g.add_argument("ticker")

    g = sub.add_parser("put-assessment", help="store an assessment")
    g.add_argument("ticker")
    g.add_argument("--json", required=True, help="assessment JSON object")
    g.add_argument("--by", required=True,
                   choices=[BY_DAILY, BY_WATCHLIST],
                   help="which flow produced it")

    g = sub.add_parser("get-blurb", help="print a blurb as JSON")
    g.add_argument("ticker")

    g = sub.add_parser("put-blurb", help="store a blurb")
    g.add_argument("ticker")
    g.add_argument("--json", required=True, help="blurb JSON object")

    g = sub.add_parser("put-world", help="store a flow's world/macro layer")
    g.add_argument("--flow", required=True,
                   choices=[BY_DAILY, BY_WATCHLIST],
                   help="which flow's world layer this is")
    g.add_argument("--json", required=True, help="world JSON object")

    g = sub.add_parser("get-world", help="print a flow's world as JSON")
    g.add_argument("--flow", required=True,
                   choices=[BY_DAILY, BY_WATCHLIST])

    g = sub.add_parser("migrate", help="merge legacy files into a store")
    g.add_argument("--llm", default="llm_outputs.json")
    g.add_argument("--watchlist", default="watchlist_outputs.json")
    g.add_argument("--out", default=STORE_FILENAME)

    g = sub.add_parser("to-outputs",
                       help="print the store adapted to research-outputs "
                            "shape (what Phase B consumes)")
    g.add_argument("--flow", default=None,
                   choices=[BY_DAILY, BY_WATCHLIST],
                   help="serve this flow's world layer (default: none)")
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv or sys.argv[1:])
    if args.cmd == "migrate":
        store, report = migrate_from_legacy(args.llm, args.watchlist)
        with locked_store(args.out):
            save_store(store, args.out)
        print(json.dumps({"out": args.out,
                          "tickers": len(store["tickers"]),
                          "report": report}, indent=1))
        return 0

    if args.cmd == "to-outputs":
        store = load_store(args.store)
        print(json.dumps(to_outputs_format(store, args.flow), indent=1))
        return 0

    if args.cmd == "get-world":
        store = load_store(args.store)
        w = get_world(store, args.flow)
        print(json.dumps(w, indent=1) if w is not None else "null")
        return 0 if w is not None else 1

    if args.cmd == "put-world":
        try:
            payload = json.loads(args.json)
        except json.JSONDecodeError as e:
            print(f"bad --json: {e}", file=sys.stderr)
            return 2
        if not isinstance(payload, dict):
            print("--json must be a JSON object", file=sys.stderr)
            return 2
        with locked_store(args.store):
            store = load_store(args.store)
            put_world(store, args.flow, payload)
            save_store(store, args.store)
        print(json.dumps({"flow": args.flow, "stored": True}))
        return 0

    if args.cmd == "get-assessment":
        store = load_store(args.store)
        a = get_assessment(store, args.ticker)
        print(json.dumps(a, indent=1) if a is not None else "null")
        return 0 if a is not None else 1

    if args.cmd == "get-blurb":
        store = load_store(args.store)
        b = get_blurb(store, args.ticker)
        print(json.dumps(b, indent=1) if b is not None else "null")
        return 0 if b is not None else 1

    if args.cmd == "put-assessment":
        try:
            payload = json.loads(args.json)
        except json.JSONDecodeError as e:
            print(f"bad --json: {e}", file=sys.stderr)
            return 2
        if not isinstance(payload, dict):
            print("--json must be a JSON object", file=sys.stderr)
            return 2
        with locked_store(args.store):
            store = load_store(args.store)
            outcome = put_assessment(store, args.ticker, payload, args.by)
            save_store(store, args.store)
        print(json.dumps({"ticker": args.ticker.upper(),
                          "outcome": outcome}))
        return 0

    if args.cmd == "put-blurb":
        try:
            payload = json.loads(args.json)
        except json.JSONDecodeError as e:
            print(f"bad --json: {e}", file=sys.stderr)
            return 2
        if not isinstance(payload, dict):
            print("--json must be a JSON object", file=sys.stderr)
            return 2
        with locked_store(args.store):
            store = load_store(args.store)
            put_blurb(store, args.ticker, payload)
            save_store(store, args.store)
        print(json.dumps({"ticker": args.ticker.upper(), "stored": True}))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
