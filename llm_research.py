#!/usr/bin/env python3
"""
Three LLM research layers for the stock screener.

  Layer 1 (world)  : web news -> current market drivers + risk events (JSON)
  Layer 2 (stocks) : per-ticker headlines + world context -> brief (JSON)
  Layer 3 (rerank) : quant base scores + layers 1&2 -> per-stock
                     event_dependence (0..1) and continuation (0..1),
                     then final rerank:
                         final = base - 1.5*event_dependence + 1.0*continuation
                     Stocks riding risky event drivers go DOWN;
                     stocks likely to keep doing well go UP.

Backends (chosen with --llm-backend):
  agent : file handoff, NO API KEYS NEEDED.
          build_research_bundle() writes research_bundle.json;
          an agent (human, or an AI like Muse) researches the web and
          writes llm_outputs.json; apply_llm_outputs() reranks.
  auto  : OpenAI-compatible API failover chain (stdlib only, no new deps):
            GROQ_API_KEY      -> llama-3.3-70b-versatile   (free tier)
         -> CEREBRAS_API_KEY  -> llama-3.3-70b             (free tier)
         -> OPENROUTER_API_KEY-> meta-llama/llama-3.3-70b-instruct:free
         -> Ollama localhost  -> $OLLAMA_MODEL or qwen2.5:7b (local, free)
          One provider dying = 2s failover, not a broken pipeline.
          If nothing is configured/reachable, raises NoLLMBackend and the
          caller falls back to the rules-based news layer.

Caching: research_cache/<sha1>.json (world: 24h TTL). Re-runs cost zero.
"""

import hashlib
import json
import os
import re
import time

# Match the pipeline's timezone pin (see screener.py): all pipeline dates are
# user-local (America/Los_Angeles), never ambient-TZ dependent.
if not os.environ.get("TZ"):
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
import urllib.request

CACHE_DIR = "research_cache"
WORLD_TTL = 24 * 3600


class NoLLMBackend(Exception):
    pass


# ---------------------------------------------------------------- utils

def _json_default(o):
    try:
        import numpy as np
        if isinstance(o, (np.floating,)):
            return float(o)
        if isinstance(o, (np.integer,)):
            return int(o)
    except Exception:
        pass
    return str(o)


def _sha1(obj):
    return hashlib.sha1(
        json.dumps(obj, sort_keys=True, default=_json_default).encode()
    ).hexdigest()


def _cache_get(key, ttl=None):
    p = os.path.join(CACHE_DIR, key + ".json")
    if not os.path.exists(p):
        return None
    if ttl and time.time() - os.path.getmtime(p) > ttl:
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def _cache_put(key, val):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, key + ".json"), "w") as f:
            json.dump(val, f, default=_json_default)
    except Exception:
        pass


def _extract_json(text):
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        return json.loads(m.group(0))
    raise ValueError("no JSON object found in LLM output")


# ---------------------------------------------------------------- LLM router

def _providers():
    provs = []
    if os.environ.get("GROQ_API_KEY"):
        provs.append(("groq", "https://api.groq.com/openai/v1/chat/completions",
                      os.environ["GROQ_API_KEY"], "llama-3.3-70b-versatile"))
    if os.environ.get("CEREBRAS_API_KEY"):
        provs.append(("cerebras", "https://api.cerebras.ai/v1/chat/completions",
                      os.environ["CEREBRAS_API_KEY"], "llama-3.3-70b"))
    if os.environ.get("OPENROUTER_API_KEY"):
        provs.append(("openrouter", "https://openrouter.ai/api/v1/chat/completions",
                      os.environ["OPENROUTER_API_KEY"],
                      "meta-llama/llama-3.3-70b-instruct:free"))
    provs.append(("ollama", "http://localhost:11434/v1/chat/completions",
                  None, os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")))
    return provs


def _post_chat(url, key, model, messages, max_tokens, temperature, log):
    body = {"model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature}
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key

    def _send(payload):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.load(r)

    try:
        # try JSON mode first
        payload = dict(body, response_format={"type": "json_object"})
        resp = _send(payload)
    except Exception:
        resp = _send(body)  # retry without response_format (Ollama etc.)
    return resp["choices"][0]["message"]["content"]


def call_llm(messages, max_tokens=1500, temperature=0.2, log=None):
    """Call free LLM providers in failover order. Returns (text, provider)."""
    log = log or (lambda m: None)
    last_err = None
    for name, url, key, model in _providers():
        try:
            text = _post_chat(url, key, model, messages, max_tokens,
                              temperature, log)
            log(f"LLM served by {name}/{model}")
            return text, f"{name}/{model}"
        except Exception as e:
            last_err = e
            log(f"LLM provider {name} failed ({str(e)[:100]}) — trying next")
    raise NoLLMBackend(
        "All LLM providers failed (last: %s). Set GROQ_API_KEY, "
        "CEREBRAS_API_KEY or OPENROUTER_API_KEY, or run Ollama locally."
        % (str(last_err)[:120],))


# ---------------------------------------------------------------- Layer 1: world

WORLD_SYSTEM = (
    "You are a markets analyst. Given recent financial headlines, identify "
    "the CURRENT market drivers and risk events. Be specific and dated to "
    "this week. Distinguish structural drivers (rates, earnings, policy) "
    "from event-driven spikes (conflicts, disasters, squeezes) that tend to "
    "mean-revert. Output STRICT JSON only, no prose."
)

WORLD_USER = """Headlines from the last few days:
{headlines}

Return JSON:
{{
  "drivers": [{{"title": "...", "summary": "2 sentences max",
                "affected_sectors": ["..."], "direction": "bullish|bearish|mixed"}}],
  "risk_events": [{{"title": "...", "summary": "2 sentences max",
                    "affected_sectors": ["..."], "severity": 1-5}}]
}}
List at most 6 drivers and 6 risk events. Severity 5 = could whipsaw exposed stocks."""


def world_research(market_headlines, log=None):
    log = log or (lambda m: None)
    key = "world_" + _sha1(market_headlines[:80])
    hit = _cache_get(key, ttl=WORLD_TTL)
    if hit:
        log("world research: cache hit")
        return hit
    msgs = [{"role": "system", "content": WORLD_SYSTEM},
            {"role": "user", "content": WORLD_USER.format(
                headlines="\n".join("- " + h for h in market_headlines[:80]))}]
    text, prov = call_llm(msgs, max_tokens=1800, log=log)
    out = _extract_json(text)
    out["_provider"] = prov
    _cache_put(key, out)
    return out


# ---------------------------------------------------------------- Layer 2+3: stock brief + scores (one call per stock)

STOCK_SYSTEM = (
    "You are an equity risk analyst. You do NOT predict prices. You assess "
    "how much a stock's recent run-up depends on temporary event/risk drivers "
    "versus durable business quality, and how likely it is to keep performing "
    "without a sharp drawdown. Be conservative: when in doubt, flag risk. "
    "Output STRICT JSON only, no prose."
)

STOCK_USER = """Stock: {ticker} ({name}) — {sector}
Quant stats: 1y return {ret_1y:+.0%}, 60d vol {vol:.0%}, 1y max drawdown {dd:.0%},
beta {beta}, ROE {roe}, profit margin {margin}, forward P/E {fpe}, quant base score {base:+.2f}
Insider signal: 90d net insider selling / market cap = {insider} (positive = net selling, a caution flag)
Earnings: {earnings}

Current world drivers: {drivers}
Current risk events: {risks}

Recent headlines for this stock:
{headlines}

Return JSON:
{{
  "event_dependence": 0.0-1.0,
  "continuation": 0.0-1.0,
  "confidence": 0.0-1.0,
  "risks": ["short risk phrases"],
  "rationale": "2 sentences max"
}}
event_dependence = how much the run-up depends on the risky event drivers above
  (0 = pure business quality, 1 = entirely riding a spiking event).
continuation = probability-flavoured 0..1 that it keeps doing well without a
  sharp post-buy drawdown, given quality + trend + news.
confidence = how confident you are in THIS assessment (0..1): high when
  headlines are specific and corroborated, low when coverage is thin,
  contradictory, or polluted with unrelated news.
Penalize continuation when: earnings are within 14 days, insiders are selling
heavily, or the move is a vertical spike on thin operating news."""


ETF_USER = """ETF: {ticker} ({name}) — category: {sector}
Quant stats: 1y return {ret_1y:+.0%}, 60d vol {vol:.0%}, 1y max drawdown {dd:.0%},
expense ratio {expense}, AUM {aum}, quant base score {base:+.2f}
What it tracks: infer from the name/headlines (e.g. sector, commodity, theme).

Current world drivers: {drivers}
Current risk events: {risks}

Recent headlines for this ETF (or its holdings/theme):
{headlines}

Return JSON:
{{
  "event_dependence": 0.0-1.0,
  "continuation": 0.0-1.0,
  "confidence": 0.0-1.0,
  "risks": ["short risk phrases"],
  "rationale": "2 sentences max"
}}
event_dependence = how much the run-up depends on the risky event drivers above
  (0 = structural theme strength, 1 = entirely riding a spiking event).
continuation = probability-flavoured 0..1 that it keeps doing well without a
  sharp post-buy drawdown, given trend + structure + news.
confidence = how confident you are in THIS assessment (0..1): high when
  headlines are specific and corroborated, low when coverage is thin,
  contradictory, or polluted with unrelated news.
ETFs are diversified: judge the THEME's durability and the fund's structure
(high fees, tiny AUM/closure risk, concentration). Penalize continuation when
the theme is a vertical spike on an unwindable event."""


def stock_research(ticker, name, sector, stats, headlines, world, log=None):
    log = log or (lambda m: None)
    key = "stock_" + _sha1([ticker, headlines[:10],
                            json.dumps(world.get("risk_events", []))[:500]])
    hit = _cache_get(key, ttl=WORLD_TTL)
    if hit:
        return hit

    def _f(x, d="n/a"):
        try:
            return f"{float(x):.2f}" if x is not None else d
        except Exception:
            return d

    def _fmt_insider(x):
        try:
            v = float(x)
            return f"{v:.4%} of market cap"
        except Exception:
            return "n/a"

    def _fmt_earn(x):
        try:
            d = int(float(x))
            return f"in {d} days — WITHIN 14d window, caution" if 0 <= d <= 14 \
                else f"in {d} days"
        except Exception:
            return "date unknown"

    def _fmt_aum(x):
        try:
            v = float(x)
            return f"${v/1e9:.2f}B" if v >= 1e9 else f"${v/1e6:.0f}M"
        except Exception:
            return "n/a"

    is_etf = stats.get("kind") == "etf"
    template = ETF_USER if is_etf else STOCK_USER
    fmt = {"ticker": ticker, "name": name, "sector": sector,
           "ret_1y": stats.get("ret_1y", 0) or 0,
           "vol": stats.get("vol60", 0) or 0,
           "dd": stats.get("maxdd", 0) or 0,
           "base": stats.get("base_score", 0) or 0,
           "drivers": "; ".join(d.get("title", "") for d in world.get("drivers", [])[:6]),
           "risks": "; ".join(r.get("title", "") for r in world.get("risk_events", [])[:6]),
           "headlines": "\n".join("- " + h for h in headlines[:12]) or "(no headlines found)"}
    if is_etf:
        try:
            exp = float(stats.get("expense_ratio"))
            fmt["expense"] = f"{exp:.2%}"
        except Exception:
            fmt["expense"] = "n/a"
        fmt["aum"] = _fmt_aum(stats.get("aum"))
    else:
        fmt.update({
            "beta": _f(stats.get("beta")), "roe": _f(stats.get("roe")),
            "margin": _f(stats.get("margin")), "fpe": _f(stats.get("fpe")),
            "insider": _fmt_insider(stats.get("insider_ratio")),
            "earnings": _fmt_earn(stats.get("earnings_in_days")),
        })
    msgs = [{"role": "system", "content": STOCK_SYSTEM},
            {"role": "user", "content": template.format(**fmt)}]
    text, prov = call_llm(msgs, max_tokens=900, log=log)
    out = _extract_json(text)
    out["_provider"] = prov
    # clamp
    out["event_dependence"] = max(0.0, min(1.0, float(out.get("event_dependence", 0.3))))
    out["continuation"] = max(0.0, min(1.0, float(out.get("continuation", 0.5))))
    try:
        out["confidence"] = max(0.0, min(1.0, float(out.get("confidence", 0.7))))
    except Exception:
        out["confidence"] = 0.7
    _cache_put(key, out)
    return out


def run_auto_layers(candidates, market_headlines, log=None):
    """Run all three layers via the API failover chain. Returns outputs dict
    in the same shape as the agent-backend llm_outputs.json."""
    log = log or (lambda m: None)
    world = world_research(market_headlines, log=log)
    log(f"world: {len(world.get('drivers', []))} drivers, "
        f"{len(world.get('risk_events', []))} risk events")
    stocks = {}
    for i, c in enumerate(candidates):
        log(f"stock research {i + 1}/{len(candidates)}: {c['ticker']}")
        try:
            stocks[c["ticker"]] = stock_research(
                c["ticker"], c.get("name", ""), c.get("sector", ""),
                c, c.get("headlines", []), world, log=log)
        except Exception as e:
            log(f"  stock research failed for {c['ticker']}: {e}")
            stocks[c["ticker"]] = {"event_dependence": 0.3, "continuation": 0.5,
                                   "risks": [], "rationale": "research failed; neutral"}
    return {"world": world, "stocks": stocks}


# ---------------------------------------------------------------- agent backend: bundle / apply

def build_research_bundle(candidates, market_headlines, meta, market_internals=None):
    """Build the JSON bundle an agent (human/AI) researches from."""
    return {
        "generated_at": __import__("datetime").datetime.now().isoformat(),
        "meta": meta,
        "world_headlines": market_headlines[:120],
        "market_internals": market_internals or {},
        "instructions": (
            "You are the LLM research layer. 1) Read world_headlines and list "
            "current market drivers + risk events. 2) For EACH candidate, read "
            "its headlines and stats, then score event_dependence (0..1: how "
            "much the run-up rides risky event drivers), continuation "
            "(0..1: likely to keep doing well without a sharp drawdown), and "
            "confidence (0..1: how confident you are in the assessment given "
            "headline quality). "
            "market_internals carries the latest VIX (market fear gauge) and "
            "10Y Treasury yield (borrowing-cost gauge) — use them as numeric "
            "context for the world layer, independent of headline emphasis. "
            "Candidates have kind=stock or kind=etf. For ETFs, judge the "
            "THEME's durability and the fund's structure (fees, AUM/closure "
            "risk, concentration) — use the ETF stats provided. "
            "Downgrade event-riders; upgrade durable quality. Penalize "
            "continuation when earnings are within 14 days (earn_soon), "
            "insiders are net selling heavily, or the move is a vertical "
            "spike on thin operating news. Be conservative. "
            "Write llm_outputs.json in the schema below. "
            "Stamp EVERY assessment with assessed_date (today's date, "
            "YYYY-MM-DD) — later runs must be able to tell fresh research "
            "from carried-over assessments."
            " Every risk_event must state its market mechanism in plain "
            "words — not just what is happening, but HOW it reaches stock "
            "prices (e.g. Hormuz disruption -> oil supply risk -> inflation "
            "-> rate pressure -> lower valuations)."
        ),
        "output_schema": {
            "world": {"drivers": [{"title": "", "summary": "",
                                   "affected_sectors": [], "direction": ""}],
                      "risk_events": [{"title": "", "summary": "",
                                       "affected_sectors": [], "severity": 0}]},
            "stocks": {"TICKER": {"event_dependence": 0.0, "continuation": 0.0,
                                  "confidence": 0.0, "risks": [],
                                  "rationale": "",
                                  "assessed_date": "YYYY-MM-DD"}}
        },
        "candidates": candidates,
    }


def apply_llm_outputs(df, outputs, w_down=1.5, w_up=1.0, w_outlier=0.5):
    """Rerank: event-dependent stocks go DOWN, likely-continuers go UP.

    final = base_w - w_down*dep + w_up*cont - w_outlier*excess*dep
    base_w = base_score winsorized at p95 (see winsorize_base); excess is the
    amount above the cap. The interaction term means a statistical outlier
    gets extra penalty exactly when it is ALSO event-driven (the SBLK case);
    a low-dep outlier keeps its score.
    """
    import pandas as pd
    import numpy as np
    stocks = (outputs or {}).get("stocks", {})
    df = df.copy()
    base = df["base_w"] if "base_w" in df.columns else df["base_score"]
    if "base_cap" in df.columns:
        cap = pd.to_numeric(df["base_cap"], errors="coerce")
    else:
        cap = pd.Series(df.attrs.get("base_cap",
                                     float(df["base_score"].quantile(0.95))),
                        index=df.index)
    excess = (df["base_score"] - cap).clip(lower=0).fillna(0)
    downs, ups, rats, risks, adates = [], [], [], [], []
    for _, r in df.iterrows():
        s = stocks.get(r["ticker"], {})
        try:
            down = max(0.0, min(1.0, float(s.get("event_dependence", 0.3))))
        except Exception:
            down = 0.3
        try:
            up = max(0.0, min(1.0, float(s.get("continuation", 0.5))))
        except Exception:
            up = 0.5
        downs.append(down)
        ups.append(up)
        rats.append(s.get("rationale", ""))
        _rk = s.get("risks", [])
        # risks=null would crash "; ".join — coerce defensively
        if isinstance(_rk, str):
            risks.append(_rk)
        elif isinstance(_rk, (list, tuple)):
            risks.append("; ".join(str(x) for x in _rk))
        else:
            risks.append("")
        # provenance: when was this ticker actually researched? Lets later
        # runs (and auditors) tell fresh assessments from carried-over ones.
        adates.append(str(s.get("assessed_date", "") or ""))
    df["llm_event_dependence"] = downs
    df["llm_continuation"] = ups
    df["llm_rationale"] = rats
    df["llm_risks"] = risks
    df["llm_assessed_date"] = adates
    # research-driven hard exclusion (e.g. rule violations the quant gates
    # can't see, like a global mandate slipping the keyword filter)
    exc, exc_r = [], []
    for _, r in df.iterrows():
        s = stocks.get(r["ticker"], {})
        # Strict True only: bool("false") is True in Python, so a worker
        # writing "exclude": "false" (string) would silently drop the ticker.
        exc.append(s.get("exclude") is True)
        exc_r.append(str(s.get("exclude_reason", "")))
    df["llm_excluded"] = exc
    df["llm_exclude_reason"] = exc_r
    confs = []
    for _, r in df.iterrows():
        s = stocks.get(r["ticker"], {})
        try:
            c = max(0.0, min(1.0, float(s.get("confidence", 0.7))))
        except Exception:
            c = 0.7
        confs.append(c)
    df["llm_confidence"] = confs
    df["final_score"] = base - w_down * df["llm_event_dependence"] \
        + w_up * df["llm_continuation"] \
        - w_outlier * excess * df["llm_event_dependence"]
    return df.sort_values("final_score", ascending=False)
