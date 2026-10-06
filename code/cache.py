"""Date-partitioned on-disk cache for expensive pipeline steps.

Every expensive call (screener universe, price downloads, fundamentals,
headlines, insider/earnings enrichment) is cached under
<cache_dir>/<YYYYMMDD>/<step>.pkl. A new day automatically refreshes.
Re-running the same day reuses everything: a full re-run with warm cache
finishes in ~1-2 minutes instead of ~15.

Usage:
    cache = StepCache("cache", enabled=True, log=log)
    universe = cache.get("universe")
    if universe is None:
        universe = expensive_call()
        cache.put("universe", universe)

For ticker-set-dependent steps, key by content hash:
    key = "prices_" + StepCache.tickers_key(tickers)
"""

import hashlib
import os
import pickle
import time

# Match the pipeline's timezone pin (see screener.py): cache partitions are
# user-local dates, so evening runs don't land in a UTC-tomorrow partition.
if not os.environ.get("TZ"):
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()


class StepCache:
    def __init__(self, root, date_str=None, enabled=True, log=None,
                 keep_days=7):
        from datetime import datetime
        self.root = root
        self.date = date_str or datetime.now().strftime("%Y%m%d")
        self.enabled = enabled
        self.log = log or (lambda m: None)
        self.hits = 0
        self.misses = 0
        if self.enabled:
            self._prune_old(keep_days)

    def _prune_old(self, keep_days):
        """Drop cache dirs older than keep_days to bound disk use."""
        try:
            if not os.path.isdir(self.root):
                return
            cutoff = time.time() - keep_days * 86400
            for d in os.listdir(self.root):
                p = os.path.join(self.root, d)
                if os.path.isdir(p) and len(d) == 8 and d.isdigit():
                    try:
                        if os.path.getmtime(p) < cutoff:
                            import shutil
                            shutil.rmtree(p, ignore_errors=True)
                            self.log(f"cache: pruned old dir {d}")
                    except Exception:
                        pass
        except Exception:
            pass

    def _path(self, name):
        safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)
        return os.path.join(self.root, self.date, safe + ".pkl")

    @staticmethod
    def tickers_key(tickers):
        h = hashlib.sha1(",".join(sorted(tickers)).encode()).hexdigest()[:12]
        return h

    def get(self, name):
        if not self.enabled:
            return None
        p = self._path(name)
        if not os.path.exists(p):
            self.misses += 1
            self.log(f"cache MISS: {name}")
            return None
        try:
            with open(p, "rb") as f:
                obj = pickle.load(f)
            self.hits += 1
            self.log(f"cache HIT: {name}")
            return obj
        except Exception as e:
            self.misses += 1
            self.log(f"cache MISS (unreadable): {name}: {e}")
            return None

    def put(self, name, obj):
        if not self.enabled:
            return
        p = self._path(name)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "wb") as f:
                pickle.dump(obj, f, protocol=4)
            os.replace(tmp, p)
            self.log(f"cache PUT: {name}")
        except Exception as e:
            self.log(f"cache PUT failed for {name}: {e}")

    def report(self):
        return {"hits": self.hits, "misses": self.misses,
                "dir": os.path.join(self.root, self.date)}
